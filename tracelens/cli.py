"""Terminal UI.

Five commands, in the order you'd actually use them:

    quality   what is wrong with the telemetry, before you trust any of it
    routes    every path work took, and how many took each
    slice     the timeline for a filter — records in order, changes inline
    trace     one journey in full
    ask       put a plain-language question to the model

`quality` is first on purpose. Every other command is downstream of whether the
input can support the claim being made.

`slice` and `ask` are the same view. One you drive, one the model drives — and
that is the argument for the whole design in one sentence.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import routes as routes_module
from . import slices
from .analysis import Analysis, of_export
from .events import parse_time
from .loader import Export, load
from .triage.engine import EFFORT as TRIAGE_EFFORT


def _console(plain: bool) -> Console:
    """ASCII-only output.

    A Windows console defaults to a codepage that cannot render box drawing or
    arrows, and the first live run there produced either mojibake or a
    UnicodeEncodeError depending on which fix I had applied. Forcing UTF-8 onto
    the stream is not enough -- the *terminal* also has to be reading UTF-8, and
    that is not something a CLI should require of its user.

    So the output is ASCII: `safe_box` for rich's frames, plain arrows in ours.
    It costs a little prettiness and it works in every terminal.
    """
    return Console(width=110, no_color=plain, highlight=not plain, safe_box=True)


def _short(node: str) -> str:
    return node.split(":", 1)[-1]


def _filter_from(args) -> slices.Filter:
    where: dict[str, str] = {}
    for pair in args.where or []:
        if "=" not in pair:
            raise SystemExit(f"--where expects name=value, got {pair!r}")
        name, _, value = pair.partition("=")
        where[name.strip()] = value.strip()

    def moment(text: str | None) -> datetime | None:
        if not text:
            return None
        parsed = parse_time(text)
        if parsed is None:
            raise SystemExit(f"could not read {text!r} as a timestamp")
        return parsed

    return slices.Filter(
        where=where,
        route=args.route,
        after=moment(args.after),
        before=moment(args.before),
        values=tuple(args.journey or ()),
    )


# --------------------------------------------------------------------------- #


def cmd_quality(args, analysis: Analysis, export: Export, console: Console) -> int:
    console.print(
        Panel(
            "Telemetry is usually partly broken. Everything below is a defect in "
            "the input and, more importantly, what that defect stops you concluding.",
            title="input quality",
            box=box.ASCII,
        )
    )

    for name, why in export.skipped.items():
        console.print(f"[yellow]skipped {name}:[/] {why}")

    if not analysis.quality.defects:
        console.print("[green]no input defects found by the general checks[/]")

    for defect in analysis.quality.defects:
        console.print()
        console.print(Panel(defect.detail, title=escape(defect.title), box=box.ASCII))
        for limit in defect.limits:
            console.print(f"  [magenta]limit:[/] {escape(limit)}")
        if not args.quiet:
            for line in defect.evidence[:6]:
                console.print(f"  [dim]{escape(line)}[/]")
            for item in defect.would_resolve:
                console.print(f"  [cyan]would resolve:[/] {escape(item)}")

    console.print(
        f"\n[bold]{len(analysis.limits)} limit(s) now travel with every answer this "
        "tool gives.[/]"
    )
    return 0


def cmd_routes(args, analysis: Analysis, export: Export, console: Console) -> int:
    if args.json:
        console.print_json(json.dumps(analysis.routes.as_dict()))
        return 0

    grouping = analysis.grouping
    table = Table(box=box.ASCII, title="how records were grouped into journeys")
    # Widths hold every header without truncation. rich abbreviates with a
    # unicode ellipsis when a header does not fit, which is the one place the
    # output stopped being ASCII.
    table.add_column("identifier", width=18, no_wrap=True)
    table.add_column("coverage", justify="right", width=10)
    table.add_column("groups", justify="right", width=8)
    table.add_column("services/grp", justify="right", width=14)
    table.add_column("median size", justify="right", width=13)
    table.add_column("", width=40)
    for candidate in grouping.candidates:
        chosen = candidate.key == grouping.key
        table.add_row(
            candidate.key,
            f"{candidate.coverage:.0%}",
            str(candidate.groups),
            f"{candidate.sources_per_group:.1f}",
            str(candidate.median_group_size),
            Text("USED", style="bold green")
            if chosen
            else Text(candidate.disqualified, style="dim"),
        )
    console.print(table)
    console.print(
        f"[dim]{len(grouping)} journeys. {grouping.unjoined:,} record(s) carry no "
        f"`{grouping.key}` and join to nothing. Override with --key.[/]\n"
    )

    if analysis.routes.vocabulary:
        console.print(
            "[dim]learned substitutions: "
            + ", ".join(f"{v} <- {k}" for k, v in sorted(analysis.routes.vocabulary.items()))
            + "[/]\n"
        )

    console.print("[bold]routes[/]")
    for line in routes_module.render(analysis.routes):
        console.print(f"  {escape(line)}")

    console.print("\n[bold]where the time goes[/]")
    for dist in analysis.timing.busiest(args.limit):
        console.print(
            f"  {_short(dist.label):<48} {dist.total_ms:>10,.0f}ms total   {dist.describe()}"
        )
    console.print(
        "\n[dim]Descriptive only -- nothing here is called slow. That needs an SLO "
        "this tool has not been given.[/]"
    )
    return 0


def cmd_slice(args, analysis: Analysis, export: Export, console: Console) -> int:
    chosen = slices.select(
        analysis.log,
        analysis.grouping,
        analysis.routes,
        _filter_from(args),
        max_journeys=args.limit,
    )
    for line in slices.render(chosen, analysis.routes):
        console.print(escape(line))
    return 0 if chosen.matched else 1


def cmd_trace(args, analysis: Analysis, export: Export, console: Console) -> int:
    if args.value not in analysis.grouping.journeys:
        console.print(f"[red]no journey '{args.value}'[/]")
        available = list(analysis.grouping.journeys)[:3]
        if available:
            console.print(f"[dim]try one of: {', '.join(available)}[/]")
        return 1

    chosen = slices.select(
        analysis.log,
        analysis.grouping,
        analysis.routes,
        slices.Filter(values=(args.value,)),
        max_journeys=1,
    )
    for line in slices.render(chosen, analysis.routes):
        console.print(escape(line))
    return 0


# --------------------------------------------------------------------------- #


def cmd_ask(args, analysis: Analysis, export: Export, console: Console) -> int:
    from .triage.engine import TriageError, triage

    if args.symptom:
        index = args.symptom - 1
        if not 0 <= index < len(export.symptoms):
            console.print(f"[red]symptom must be 1..{len(export.symptoms)}[/]")
            return 1
        symptom = export.symptoms[index]
        question = symptom.text
        console.print(Panel(f"[bold]{symptom.source}[/]\n{question}", title="complaint", box=box.ASCII))
    else:
        question = args.question
        console.print(Panel(question, title="complaint", box=box.ASCII))

    try:
        run = triage(
            export,
            question,
            use_stub=True if args.stub else None,
            api_key=args.api_key,
            effort=args.effort,
            include_platform=not args.no_platform_context,
            key=args.key,
        )
    except TriageError as exc:
        console.print(f"[red]failed:[/] {escape(str(exc))}")
        return 1

    result = run.result
    console.print(
        f"[dim]source: {result.source} | tool calls: {result.tool_calls} | "
        f"{len(run.index)} identifiers were shown and are citable[/]"
    )
    if result.source.startswith("stub"):
        console.print(
            "[dim]set ANTHROPIC_API_KEY (or put it in a gitignored .env) and "
            + escape('pip install -e ".[ai]"')
            + " for a live answer[/]"
        )

    if result.insufficient:
        console.print(
            Panel(
                "Nothing shown bears on this complaint. Rather than reaching for "
                "the nearest available problem, here is what would be needed.",
                title="[yellow]insufficient evidence[/]",
                box=box.ASCII,
            )
        )
    for position, hypothesis in enumerate(result.hypotheses, 1):
        console.print()
        console.print(Panel(hypothesis.summary, title=f"#{position}", box=box.ASCII))
        if hypothesis.reading:
            console.print(f"  [dim]read from:[/] {escape(hypothesis.reading)}")
        console.print(f"  [dim]cites:[/] {escape(', '.join(hypothesis.evidence_refs))}")
        if hypothesis.alternative:
            console.print(
                f"  [magenta]cannot be separated from:[/] {escape(hypothesis.alternative)}"
            )

    if result.ruled_out:
        console.print("\n[bold]ruled out:[/]")
        for item in result.ruled_out:
            console.print(f"  - {escape(str(item.get('claim')))}")
            console.print(f"    [dim]{escape(str(item.get('why_not')))}[/]")

    if result.limits_that_apply:
        console.print("\n[bold]limits the answer worked under:[/]")
        for limit in result.limits_that_apply:
            console.print(f"  [magenta]-[/] {escape(limit)}")

    if result.limits_unaddressed:
        console.print("\n[bold]limits it did not address:[/]")
        for limit in result.limits_unaddressed:
            console.print(f"  [dim]-[/] {escape(limit)}")

    if result.would_resolve:
        console.print("\n[bold]would resolve the remaining ambiguity:[/]")
        for item in result.would_resolve:
            console.print(f"  - {escape(str(item))}")

    if result.rejections:
        console.print(f"\n[red]validator dropped {len(result.rejections)}:[/]")
        for rejection in result.rejections:
            refs = f" ({', '.join(rejection.bad_refs)})" if rejection.bad_refs else ""
            console.print(f"  - {rejection.reason}{refs}")

    return 0


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracelens", description="Telemetry troubleshooting assistant"
    )
    parser.add_argument("--data", help="path to the telemetry export")
    parser.add_argument("--key", help="identifier to group records by; default is shown by `routes`")
    parser.add_argument("--plain", action="store_true", help="no colour, for piping")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("quality", help="what is wrong with the input, and what that limits")
    p.add_argument("--quiet", action="store_true", help="limits only")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("routes", help="how journeys were grouped, and every path they took")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=8, help="nodes shown in the timing table")
    p.set_defaults(func=cmd_routes)

    p = sub.add_parser("slice", help="the timeline for a filter")
    p.add_argument("--where", action="append", metavar="NAME=VALUE")
    p.add_argument("--route", type=int)
    p.add_argument("--after")
    p.add_argument("--before")
    p.add_argument("--journey", action="append")
    p.add_argument("--limit", type=int, default=slices.MAX_JOURNEYS)
    p.set_defaults(func=cmd_slice)

    p = sub.add_parser("trace", help="one journey in full")
    p.add_argument("value")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("ask", help="put a plain-language question to the model")
    p.add_argument("question", nargs="?")
    p.add_argument("--symptom", type=int, help="replay symptom N from symptoms.json")
    p.add_argument("--stub", action="store_true", help="force the offline stand-in")
    p.add_argument(
        "--api-key",
        help="use this key for one call; overrides the environment and .env, never stored",
    )
    p.add_argument(
        "--no-platform-context",
        action="store_true",
        help="omit PLATFORM.md from the prompt (for A/B testing grounding)",
    )
    p.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=TRIAGE_EFFORT,
        help=f"how many tokens the model may spend (default {TRIAGE_EFFORT})",
    )
    p.set_defaults(func=cmd_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    # The output uses arrows and box drawing. A Windows console defaults to a
    # legacy codepage that cannot encode them, and the first live run there died
    # on a UnicodeEncodeError mid-answer. Forcing UTF-8 costs nothing elsewhere.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # a redirected or closed stream
                pass

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ask" and not args.question and not args.symptom:
        parser.error("ask needs a question or --symptom N")

    console = _console(args.plain)
    try:
        export = load(args.data)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        return 2

    return args.func(args, of_export(export, args.key), export, console)


if __name__ == "__main__":
    sys.exit(main())
