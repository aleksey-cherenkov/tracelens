"""Offline stand-in for the model.

Two modes, in order:

1. A recorded transcript from `examples/` if one exists for this complaint. Those
   are real responses from real runs, committed so a reviewer can see what the
   model actually said without an API key.

2. Otherwise, a deterministic reading of the route table, so the tool and the
   tests run offline for arbitrary input.

Mode 2 is explicitly NOT a model and does not pretend to be. It finds the routes
that look anomalous *structurally* — shorter than the common one, or revisiting a
node — and picks whichever shares the most words with the complaint. That is
enough to route "push notifications never went out" and "the same email twice" to
different routes, and to decline a question about a system that isn't here.

It exists so the plumbing (payload assembly, tool surface, validator, rendering)
is exercised end to end without a key. What it cannot do is the whole argument
for using a model: it cannot say *why* a route is short, cannot read a timeline,
cannot notice that a deploy postdates the thing it is blamed for, and matches on
shared spelling rather than shared meaning — "supporters got the same
confirmation twice" with the word "email" removed defeats it entirely.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..analysis import Analysis
from ..slices import Filter, select

EXAMPLES = Path(__file__).resolve().parent.parent.parent / "examples"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def recorded_for(complaint: str) -> tuple[dict, str] | None:
    """Find a committed transcript for this complaint.

    Returns (response, label). The label matters: a reviewer without an API key is
    seeing a real model answer replayed from disk, and "recorded" alone does not
    say that. It names the model and the file so the claim is checkable.
    """
    if not EXAMPLES.is_dir():
        return None
    target = _slug(complaint)
    for path in sorted(EXAMPLES.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if _slug(payload.get("complaint", "")) != target:
            continue

        origin = str(payload.get("source", ""))
        if origin.startswith("live"):
            detail = origin[origin.find("(") + 1 : origin.rfind(")")].split(", key from")[0]
            label = f"replayed live {detail} run from examples/{path.name}"
        else:
            label = f"replayed stub run from examples/{path.name}"
        return payload.get("response"), label
    return None


def respond(complaint: str, analysis: Analysis, toolbox=None) -> tuple[dict, str]:
    """Return (raw response dict, source label)."""
    recorded = recorded_for(complaint)
    if recorded is not None:
        return recorded

    table = analysis.routes
    dominant = table.dominant
    if dominant is None or len(table.routes) < 2:
        return _nothing(complaint, analysis), "stub"

    picked = _pick_route(complaint, analysis)
    if picked is None:
        return _nothing(complaint, analysis), "stub"

    if toolbox is not None:
        toolbox.run("list_routes", {})
        toolbox.run("get_slice", {"route": picked.index})
    else:
        select(analysis.log, analysis.grouping, analysis.routes, Filter(route=picked.index))

    ends = picked.ends_at.split(":", 1)[-1]
    if picked.repeats:
        repeated = ", ".join(n.split(":", 1)[-1] for n in picked.repeats)
        summary = (
            f"{picked.count} of {table.total} journeys pass through {repeated} more "
            f"than once. The other {dominant.count} pass through once."
        )
        reading = f"Route table: route {picked.index} revisits {repeated}."
        alternative = (
            "A repeated record is not proof of repeated side effects — the work may "
            "be idempotent, which this data cannot show either way."
        )
        resolve = f"whether the work at {repeated} is safe to do twice"
    else:
        summary = (
            f"{picked.count} of {table.total} journeys last appear at '{ends}' and "
            f"produce nothing after it, while {dominant.count} continue through "
            f"{len(dominant.nodes) - len(picked.nodes)} further steps."
        )
        reading = (
            f"Route table: route {picked.index} diverges from the common one and "
            f"ends at '{ends}'."
        )
        alternative = (
            "The later steps may have run without reporting — this data cannot "
            "separate 'did not happen' from 'was not recorded'."
        )
        resolve = f"whether anything downstream of '{ends}' emits for these journeys at all"

    return (
        {
            "verdict": "hypotheses",
            "restated_complaint": complaint.strip(),
            "hypotheses": [
                {
                    "summary": summary,
                    "evidence_refs": [f"route-{picked.index}", *picked.journeys[:3]],
                    "reading": reading,
                    "alternative": alternative,
                }
            ],
            "ruled_out": [],
            "limits_that_apply": analysis.limits,
            "would_resolve": [resolve],
        },
        "stub",
    )


STOPWORDS = frozenset(
    "the a an and or but of to in on for it is are was were we our us they their i my "
    "that this these those some any all not no never ever got get see saw seems looks "
    "like only just back from with at as by up out about said someone really probably "
    "confirmed think one two none went".split()
)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in STOPWORDS}


def _pick_route(complaint: str, analysis: Analysis):
    """The anomalous route whose own vocabulary best matches the complaint.

    Two filters, both structural: a route is a candidate if it is shorter than the
    most common one (work stopped early) or revisits a node (work done twice).
    Then it has to share a word with the complaint -- taken from its node names
    and from the attribute values its journeys carry, so the matching vocabulary
    comes from the data rather than from a table I wrote.

    Requiring the overlap is what makes an out-of-scope question decline instead
    of getting the nearest available problem. Requiring only *one* word is what
    makes it crude, and the crudeness is the argument for a model.
    """
    table = analysis.routes
    dominant = table.dominant
    wanted = _tokens(complaint)
    if not wanted or dominant is None:
        return None

    best, best_score = None, 0
    for route in table.routes:
        if route is dominant:
            continue
        if len(route.nodes) >= len(dominant.nodes) and not route.repeats:
            continue

        vocabulary = _tokens(" ".join(route.nodes))
        for value in route.journeys:
            journey = analysis.grouping.journeys.get(value)
            if journey:
                for event in journey.events:
                    vocabulary |= _tokens(
                        " ".join(str(v) for v in event.attributes.values())
                    )

        score = len(wanted & vocabulary)
        if score > best_score:
            best, best_score = route, score
    return best


def _nothing(complaint: str, analysis: Analysis) -> dict:
    return {
        "verdict": "insufficient_evidence",
        "restated_complaint": complaint.strip(),
        "hypotheses": [],
        "ruled_out": [],
        "limits_that_apply": analysis.limits,
        "would_resolve": [
            "telemetry covering the subsystem in the complaint — every observed "
            "route reaches the same endpoint, so nothing here shows work stopping",
            "a journey identifier or time window from the reporter to scope the search",
        ],
    }
