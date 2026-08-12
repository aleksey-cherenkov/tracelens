"""Terminal UI.

The trace view is the one that earns its keep: a waterfall with the join method
on every hop, a marker where the trace ID changes, and a terminal marker where the
path stops. Two commands against two correlation IDs make the two worst findings
obvious without reading a word of prose.
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markup import escape
from rich.text import Text

from .accounting import Outcome
from .config import DEFAULT, Config
from .detectors import build_context, run_all
from .detectors.blindspot import classify_log
from .loader import load_dataset
from .model import Dataset, fmt_ts

SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "dim",
}
CONFIDENCE_STYLE = {
    "observed": "green",
    "inferred": "yellow",
    "ambiguous": "magenta",
}


def _console(plain: bool) -> Console:
    return Console(width=100, no_color=plain, highlight=not plain)


# --------------------------------------------------------------------------- #
# trace
# --------------------------------------------------------------------------- #


def cmd_trace(args, dataset: Dataset, console: Console) -> int:
    context = build_context(dataset, args.config)
    trace = context.traces.get(args.correlation_id)
    if trace is None:
        console.print(f"[red]no message '{args.correlation_id}'[/]")
        return 1

    header = (
        f"[bold]{trace.correlation_id}[/]  {trace.channel}  {trace.tenant_id}  "
        f"accepted {fmt_ts(trace.accepted.accepted_at) if trace.accepted else '?'}"
    )
    console.print(Panel(header, expand=False))

    table = Table(box=None, pad_edge=False, padding=(0, 1, 0, 0))
    table.add_column("", width=1)
    table.add_column("stage", width=21, no_wrap=True)
    table.add_column("service", width=12, no_wrap=True)
    table.add_column("start", width=12, no_wrap=True)
    table.add_column("dur", width=7, justify="right", no_wrap=True)
    table.add_column("", width=6, no_wrap=True)
    table.add_column("trace", width=8, no_wrap=True)
    table.add_column("hop to next", width=26, no_wrap=True)

    stages = sorted(trace.stage_spans.items(), key=lambda kv: kv[0].index)
    longest = max((s.duration_ms for _, s in stages), default=1) or 1
    previous_trace = None

    for stage, span in stages:
        marker = ""
        if previous_trace is not None and span.trace_id != previous_trace:
            marker = Text("!", style="bold magenta")  # trace context changed here
        previous_trace = span.trace_id

        record = trace.join_for(stage)
        if record is None:
            hop = ""
        elif record.method.value == "absent":
            hop = Text("STOPPED — nothing next", style="bold red")
        elif record.method.value == "correlation_fallback":
            hop = Text(f"{record.gap_ms:>5.0f}ms  corr fallback", style="magenta")
        elif record.nested:
            hop = Text(f"{record.gap_ms:>5.0f}ms  nested in parent", style="dim")
        else:
            hop = f"{record.gap_ms:>5.0f}ms  parent/child"

        table.add_row(
            marker,
            stage.label,
            span.service.replace("comms-", ""),
            fmt_ts(span.start_time)[11:23],
            f"{span.duration_ms}ms",
            "█" * max(1, round(span.duration_ms / longest * 6)),
            span.short_trace,
            hop,
        )

    console.print(table)

    if trace.trace_context_break:
        console.print(
            f"[magenta]! trace context breaks: {len({s.trace_id for s in trace.spans})} "
            f"distinct trace IDs. Joined on correlation_id instead.[/]"
        )
    if not trace.reached_provider:
        console.print(
            f"[bold red]X never reached a provider — stopped after "
            f"{trace.terminal_stage.label} in {trace.terminal_stage.service}[/]"
        )
    else:
        console.print(
            f"[green]reached provider[/] — end-to-end {trace.end_to_end_ms:.0f}ms "
            f"(first attempt)"
        )
    if len(trace.attempts) > 1:
        console.print(f"[yellow]{len(trace.attempts)} delivery attempts:[/]")
        for attempt in trace.attempts:
            console.print(
                f"   #{attempt.index + 1} {fmt_ts(attempt.start_time)} "
                f"{attempt.send.duration_ms}ms status={attempt.provider_status} "
                f"sqs.receive_count={attempt.receive_count}"
            )
    for anomaly in trace.anomalies:
        console.print(f"[dim]note: {anomaly}[/]")
    return 0


# --------------------------------------------------------------------------- #
# account
# --------------------------------------------------------------------------- #


def cmd_account(args, dataset: Dataset, console: Console) -> int:
    context = build_context(dataset, args.config)
    accounting = context.accounting

    console.print(
        Panel(
            f"[bold]{accounting.total}[/] messages accepted with 202 — "
            "the platform's promise ledger",
            expand=False,
        )
    )

    funnel = Table(title="delivery outcome", box=None)
    funnel.add_column("outcome", width=38)
    funnel.add_column("count", justify="right", width=8)
    funnel.add_column("share", justify="right", width=8)
    for outcome, count in [
        (Outcome.DELIVERED_ONCE, accounting.delivered_once),
        (Outcome.DELIVERED_DUPLICATE, accounting.delivered_duplicate),
        (Outcome.STOPPED, accounting.stopped),
    ]:
        style = "red bold" if outcome is Outcome.STOPPED and count else ""
        funnel.add_row(
            Text(outcome.label, style=style),
            Text(str(count), style=style),
            Text(f"{accounting.share(count):.1%}", style=style),
        )
    funnel.add_row("provider calls issued", str(accounting.provider_calls), "")
    console.print(funnel)

    if args.by == "channel":
        table = Table(title="per channel", box=None)
        for column in ("channel", "accepted", "delivered", "lost", "duplicated", "trace intact"):
            table.add_column(column, justify="right" if column != "channel" else "left")
        for name, bucket in accounting.by_channel.items():
            lost = Text(str(bucket.lost), style="bold red" if bucket.lost else "")
            intact = f"{bucket.trace_intact}/{bucket.accepted}"
            table.add_row(
                name,
                str(bucket.accepted),
                str(bucket.delivered),
                lost,
                str(bucket.duplicated),
                Text(intact, style="magenta" if bucket.trace_intact < bucket.accepted else ""),
            )
        console.print(table)
    else:
        key = args.by
        table = Table(title=f"per {key}", box=None)
        table.add_column(key)
        table.add_column("accepted", justify="right")
        table.add_column("lost", justify="right")
        groups: dict[str, list] = {}
        for outcome in accounting.outcomes:
            trace = context.traces[outcome.correlation_id]
            if key == "tenant":
                label = outcome.tenant_id or "unknown"
            else:
                label = (
                    trace.accepted.accepted_at.date().isoformat() if trace.accepted else "unknown"
                )
            groups.setdefault(label, []).append(outcome)
        for label in sorted(groups):
            items = groups[label]
            lost = sum(1 for o in items if o.outcome is Outcome.STOPPED)
            table.add_row(
                label, str(len(items)), Text(str(lost), style="bold red" if lost else "")
            )
        console.print(table)

    if accounting.stopped:
        stages = ", ".join(
            f"{count} after {stage.label}"
            for stage, count in accounting.stopped_by_stage.items()
        )
        console.print(f"\n[bold red]{accounting.stopped} never delivered:[/] {stages}")
        console.print(f"[dim]{', '.join(accounting.stopped_ids())}[/]")
    return 0


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


def cmd_health(args, dataset: Dataset, console: Console) -> int:
    context = build_context(dataset, args.config)
    health = context.health

    errors = health.errors
    panel = Table(box=None, show_header=False)
    panel.add_column(width=34)
    panel.add_column(justify="right", width=14)
    panel.add_row("spans with status != OK", f"{errors.span_status_errors}/{errors.total_spans}")
    panel.add_row("provider calls with non-2xx", f"{errors.provider_errors}/{errors.provider_calls}")
    panel.add_row(
        Text("accepted but never delivered", style="bold red"),
        Text(f"{errors.delivery_failures}/{errors.accepted}", style="bold red"),
    )
    console.print(Panel(panel, title="error rates — three numbers, never collapsed", expand=False))
    if errors.diverges:
        console.print(
            "[bold red]divergence:[/] telemetry reports zero errors while "
            f"{errors.delivery_failures} promised messages were never delivered. "
            "Any alert built on span status or log level sees a healthy pipeline."
        )

    services = Table(title="per service", box=None)
    for column in ("service", "spans", "msgs", "status errs", "retries", "logs", "scoped"):
        services.add_column(column, justify="right" if column != "service" else "left")
    for name, item in health.services.items():
        if args.service and name != args.service:
            continue
        services.add_row(
            name,
            str(item.spans),
            str(item.messages),
            str(item.span_status_errors),
            str(item.retries),
            str(item.log_records),
            str(item.scoped_logs),
        )
    console.print(services)

    hops = Table(title="per hop", box=None)
    for column in ("hop", "async", "seen", "lost", "trace breaks", "n", "median", "spread"):
        hops.add_column(column, justify="right" if column != "hop" else "left")
    for name, hop in health.hops.items():
        if args.hop and name != args.hop:
            continue
        latency = hop.latency
        if hop.nested:
            median = "nested"
            spread = "offset, not latency"
        elif latency.n == 0:
            median = "-"
            spread = "-"
        elif not latency.has_variance:
            median = f"{latency.median:.0f}ms"
            spread = "variance: none"
        else:
            median = f"{latency.median:.0f}ms"
            spread = f"{latency.minimum:.0f}-{latency.maximum:.0f}ms p95 {latency.p95:.0f}"
        hops.add_row(
            name,
            "yes" if hop.asynchronous else "",
            str(hop.observed),
            Text(str(hop.absent), style="bold red" if hop.absent else ""),
            Text(str(hop.fallback_joins), style="magenta" if hop.fallback_joins else ""),
            str(latency.n),
            median,
            spread,
        )
    console.print(hops)

    e2e = Table(title="end-to-end latency, first attempt only", box=None)
    for column in ("channel", "n", "min", "median", "max", "spread"):
        e2e.add_column(column, justify="right" if column != "channel" else "left")
    for channel, summary in health.end_to_end.items():
        e2e.add_row(
            channel,
            str(summary.n),
            f"{summary.minimum:.0f}ms",
            f"{summary.median:.0f}ms",
            f"{summary.maximum:.0f}ms",
            "variance: none" if not summary.has_variance else f"p95 {summary.p95:.0f}ms",
        )
    console.print(e2e)

    console.print(
        f"\nretries against provider: {health.retries_provider}   "
        f"queue redeliveries: {health.redeliveries}   "
        "[dim](different phenomena, never summed)[/]"
    )
    worst = health.worst_hop()
    if worst:
        console.print(f"\n[bold]needs attention:[/] {worst.name} — {worst.needs_attention}")
    return 0


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #


def cmd_findings(args, dataset: Dataset, console: Console) -> int:
    context = build_context(dataset, args.config)
    findings = run_all(context)
    if args.severity:
        findings = [f for f in findings if f.severity == args.severity]
    if not findings:
        console.print("[green]no findings[/]")
        return 0

    for finding in findings:
        severity = Text(finding.severity.upper(), style=SEVERITY_STYLE.get(finding.severity, ""))
        confidence = Text(finding.confidence, style=CONFIDENCE_STYLE.get(finding.confidence, ""))
        console.print()
        console.print(
            Panel(
                f"{finding.summary}",
                title=f"{escape(finding.id)} — {finding.title}",
                subtitle=f"{severity} / {confidence} / {finding.affected_count} affected",
                expand=True,
            )
        )
        if not args.quiet:
            for item in finding.evidence:
                console.print(f"  [dim]{item.kind:>14}[/] {escape(item.ref)}: {escape(item.detail)}")
        if finding.affected:
            console.print(f"  [dim]affected:[/] {', '.join(finding.affected)}")
        if finding.alternatives:
            console.print("  [magenta]competing explanations — data cannot separate these:[/]")
            for alternative in finding.alternatives:
                console.print(f"    - {escape(alternative.id)}: {alternative.summary}")
        if finding.would_resolve:
            console.print("  [dim]would resolve:[/]")
            for item in finding.would_resolve:
                console.print(f"    - {item}")
        if finding.low_confidence_rate:
            console.print(
                "  [yellow]rate confidence low — sample below min_samples, read as a count[/]"
            )
        if finding.params:
            console.print(f"  [dim]params: {finding.params}[/]")
    return 0


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #


def cmd_logs(args, dataset: Dataset, console: Console) -> int:
    records = dataset.logs
    if args.corr:
        records = [r for r in records if r.correlation_id == args.corr]
    if args.service:
        records = [r for r in records if r.service == args.service]
    if args.grep:
        needle = args.grep.lower()
        records = [r for r in records if needle in r.message.lower()]

    suppressed: dict[str, int] = {}
    shown = []
    for record in records:
        category = classify_log(record.message)
        # Denylist, not allowlist: an unrecognised line stays visible, because a
        # log line nobody has classified yet is more likely to matter, not less.
        if category and not (args.show_suppressed or args.no_filter):
            suppressed[category] = suppressed.get(category, 0) + 1
            continue
        shown.append(record)

    for record in shown[: args.limit]:
        style = {"WARN": "yellow", "ERROR": "red", "DEBUG": "dim"}.get(record.level, "")
        corr = record.correlation_id or "-"
        console.print(
            Text(
                f"{fmt_ts(record.timestamp)} {record.level:<5} {record.service:<19} "
                f"{corr:<12} {record.message}",
                style=style,
            )
        )
    if len(shown) > args.limit:
        console.print(f"[dim]... {len(shown) - args.limit} more shown-eligible lines[/]")

    total_suppressed = sum(suppressed.values())
    if total_suppressed:
        detail = ", ".join(f"{count} {name}" for name, count in sorted(suppressed.items()))
        console.print(
            f"\n[dim]{total_suppressed} lines suppressed — {detail}. "
            "Use --show-suppressed or --no-filter to restore.[/]"
        )
    unjoinable = sum(1 for r in dataset.logs if not r.is_message_scoped)
    console.print(
        f"[dim]unjoinable share: {unjoinable}/{len(dataset.logs)} "
        f"({unjoinable / len(dataset.logs):.1%}) — a rise here means someone shipped "
        "code that logs outside a trace context.[/]"
    )
    return 0


# --------------------------------------------------------------------------- #
# triage
# --------------------------------------------------------------------------- #


def cmd_triage(args, dataset: Dataset, console: Console) -> int:
    from .triage.engine import triage

    if args.symptom:
        index = args.symptom - 1
        if not 0 <= index < len(dataset.symptoms):
            console.print(f"[red]symptom must be 1..{len(dataset.symptoms)}[/]")
            return 1
        symptom = dataset.symptoms[index]
        complaint = symptom.text
        console.print(Panel(f"[bold]{symptom.source}[/]\n{complaint}", title="complaint"))
    else:
        complaint = args.complaint
        console.print(Panel(complaint, title="complaint"))

    # None means "decide for me" — force the stub only when asked explicitly.
    run = triage(dataset, complaint, args.config, use_stub=True if args.stub else None)
    result = run.result

    console.print(
        f"[dim]source: {result.source}"
        + (f" | tool calls: {result.tool_calls}" if result.tool_calls else "")
        + f" | evidence index: {len(run.bundle.index)} refs[/]"
    )

    if result.insufficient:
        console.print(
            Panel(
                "No finding's evidence matches this complaint. Rather than "
                "pattern-matching to the nearest available answer, here is what was "
                "checked and what would be needed.",
                title="[yellow]insufficient evidence[/]",
            )
        )
        console.print("[dim]checked:[/]")
        for item in result.checked:
            console.print(f"  - {escape(str(item))}")
        console.print("[dim]would resolve:[/]")
        for item in result.would_resolve:
            console.print(f"  - {escape(str(item))}")
        return 0

    for position, hypothesis in enumerate(result.hypotheses, 1):
        severity = Text(hypothesis.severity.upper(), style=SEVERITY_STYLE.get(hypothesis.severity, ""))
        confidence = Text(hypothesis.confidence, style=CONFIDENCE_STYLE.get(hypothesis.confidence, ""))
        console.print()
        console.print(
            Panel(
                hypothesis.summary,
                title=f"#{position}  {escape(hypothesis.finding_id)}",
                subtitle=f"{severity} / {confidence}",
            )
        )
        console.print(f"  [dim]cites:[/] {escape(', '.join(hypothesis.evidence_refs))}")
        if hypothesis.why_this_rank:
            console.print(f"  [dim]why this rank:[/] {hypothesis.why_this_rank}")
        if hypothesis.alternatives:
            console.print("  [magenta]unresolved — both survive the evidence:[/]")
            for alternative in hypothesis.alternatives:
                console.print(f"    - {escape(alternative['id'])}: {alternative['summary']}")

    if result.ruled_out:
        console.print("\n[bold]ruled out:[/]")
        for item in result.ruled_out:
            console.print(f"  - {escape(str(item.get('claim')))}")
            console.print(f"    [dim]{escape(str(item.get('why_not')))}[/]")

    if result.would_resolve:
        console.print("\n[bold]would resolve the remaining ambiguity:[/]")
        for item in result.would_resolve:
            console.print(f"  - {escape(str(item))}")

    if result.rejections:
        console.print(
            f"\n[red]validator dropped {len(result.rejections)} hypothesis/es:[/]"
        )
        for rejection in result.rejections:
            refs = f" ({', '.join(rejection.bad_refs)})" if rejection.bad_refs else ""
            console.print(f"  - {rejection.finding_id}: {rejection.reason}{refs}")
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def cmd_report(args, dataset: Dataset, console: Console) -> int:
    from .report import write_report

    path = write_report(dataset, args.out, args.config)
    console.print(f"[green]wrote[/] {path}")
    return 0


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracelens", description="Message pipeline trace analyzer"
    )
    parser.add_argument("--data", help="path to the telemetry export")
    parser.add_argument("--plain", action="store_true", help="no colour, for piping")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("trace", help="show one message end to end")
    p.add_argument("correlation_id")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("account", help="delivery accounting against the promise ledger")
    p.add_argument("--by", choices=["channel", "tenant", "day"], default="channel")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("health", help="per-service and per-hop health")
    p.add_argument("--service")
    p.add_argument("--hop")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("findings", help="everything the detectors found")
    p.add_argument("--severity", choices=["critical", "high", "medium", "low"])
    p.add_argument("--quiet", action="store_true", help="titles and summaries only")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("logs", help="log viewer with noise suppression")
    p.add_argument("--corr")
    p.add_argument("--service")
    p.add_argument("--grep")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--show-suppressed", action="store_true")
    p.add_argument("--no-filter", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("triage", help="AI-assisted triage of a plain-language complaint")
    p.add_argument("complaint", nargs="?")
    p.add_argument("--symptom", type=int, help="replay symptom N from symptoms.json")
    p.add_argument("--stub", action="store_true", help="force the offline stand-in")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("report", help="single-file HTML report")
    p.add_argument("--out", default="report.html")
    p.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = DEFAULT

    if args.command == "triage" and not args.complaint and not args.symptom:
        parser.error("triage needs a complaint or --symptom N")

    console = _console(args.plain)
    try:
        dataset = load_dataset(args.data)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        return 2

    return args.func(args, dataset, console)


if __name__ == "__main__":
    sys.exit(main())
