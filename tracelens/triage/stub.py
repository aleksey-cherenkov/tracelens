"""Offline stand-in for the model.

Two modes, in order:

1. A recorded transcript from examples/ if one exists for this complaint. These
   are real responses from real runs, committed so a reviewer can see what the
   model actually said without an API key.

2. A deterministic keyword router otherwise, so the tool and the tests run
   offline for arbitrary input.

Mode 2 is explicitly NOT a model and does not pretend to be. It exists so the
plumbing -- context assembly, tool surface, validator, output rendering -- is
exercised end to end without a key. Anything it gets right, it gets right because
the detectors already did the work; that is the point of the split, but it is not
evidence that the model would agree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..evidence import EvidenceBundle

EXAMPLES = Path(__file__).resolve().parent.parent.parent / "examples"

# Complaint vocabulary -> detector prefix. Deliberately crude: this is the part
# the real model does far better, and its crudeness is the argument for using a
# model here at all.
ROUTES: list[tuple[str, str]] = [
    (r"push|notification|never went out|didn.t go out", "D1.channel_drop"),
    (r"twice|duplicate|same .*(email|message)|again", "D2.duplicate_delivery"),
    (r"slow|latency|degraded|throttl|429|took (a )?long", "D3.provider_degradation"),
    (r"trace|span|only .*(two|first) services|where does the rest", "D4.trace_context_break"),
    (r"log|noise|health check|grep|unusable|scroll", "D5.log_noise"),
    (r"no error|looks healthy|nothing fired|no alert", "D5.status_divergence"),
    (r"queue depth|metric|gauge|backlog", "D5.broken_gauge"),
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def recorded_for(complaint: str) -> dict | None:
    if not EXAMPLES.is_dir():
        return None
    target = _slug(complaint)
    for path in sorted(EXAMPLES.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if _slug(payload.get("complaint", "")) == target:
            return payload.get("response")
    return None


def respond(complaint: str, bundle: EvidenceBundle) -> tuple[dict, str]:
    """Return (raw response dict, source label)."""
    recorded = recorded_for(complaint)
    if recorded is not None:
        return recorded, "recorded"

    lowered = complaint.lower()
    matched_prefixes = [
        prefix for pattern, prefix in ROUTES if re.search(pattern, lowered)
    ]

    matches = [
        finding
        for finding in bundle.ranked()
        if any(finding.id.startswith(prefix) for prefix in matched_prefixes)
    ]

    if not matches:
        return (
            {
                "verdict": "insufficient_evidence",
                "restated_complaint": complaint.strip(),
                "hypotheses": [],
                "ruled_out": [],
                "checked": [f.id for f in bundle.ranked()],
                "would_resolve": [
                    "telemetry covering the subsystem in the complaint — no detector "
                    "produced a finding whose affected messages relate to it",
                    "a correlation ID or time window from the reporter to scope the search",
                ],
            },
            "stub",
        )

    # A complaint usually has one obvious primary match; include the next-best
    # finding so the output always carries an alternative to weigh against.
    others = [f for f in bundle.ranked() if f not in matches]
    selected = matches + others[: max(0, 2 - len(matches))]

    hypotheses = []
    for finding in selected:
        refs = [e.ref for e in finding.evidence[:4]]
        hypotheses.append(
            {
                "finding_id": finding.id,
                "summary": finding.summary,
                "evidence_refs": refs,
                "why_this_rank": (
                    f"{finding.severity} severity, {finding.confidence} confidence, "
                    f"{finding.affected_count} affected message(s)"
                ),
            }
        )

    ruled_out = []
    for finding in matches:
        for item in finding.evidence:
            if "ruled out" in item.detail:
                ruled_out.append(
                    {
                        "claim": f"the deploy {item.ref} caused it",
                        "why_not": item.detail,
                        "evidence_refs": [item.ref],
                    }
                )

    return (
        {
            "verdict": "hypotheses",
            "restated_complaint": complaint.strip(),
            "hypotheses": hypotheses,
            "ruled_out": ruled_out,
            "checked": [f.id for f in bundle.ranked()],
            "would_resolve": [
                w for f in selected for w in f.would_resolve
            ],
        },
        "stub",
    )
