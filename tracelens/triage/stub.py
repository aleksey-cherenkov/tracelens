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


def recorded_for(complaint: str) -> tuple[dict, str] | None:
    """Find a committed transcript for this complaint.

    Returns (response, label). The label matters: a reviewer without an API key
    is seeing a real model answer replayed from disk, and "recorded" alone does
    not say that. It names the model and the file so the claim is checkable.
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
            # "live (claude-sonnet-5, effort=medium, key from .env)" -> the useful part
            detail = origin[origin.find("(") + 1 : origin.rfind(")")]
            detail = detail.split(", key from")[0]
            label = f"replayed live {detail} run from examples/{path.name}"
        else:
            label = f"replayed stub run from examples/{path.name}"
        return payload.get("response"), label
    return None


def respond(complaint: str, bundle: EvidenceBundle) -> tuple[dict, str]:
    """Return (raw response dict, source label)."""
    recorded = recorded_for(complaint)
    if recorded is not None:
        return recorded

    lowered = complaint.lower()
    matched_prefixes = [
        prefix for pattern, prefix in ROUTES if re.search(pattern, lowered)
    ]

    matches = [
        finding
        for finding in bundle.ranked()
        if any(finding.id.startswith(prefix) for prefix in matched_prefixes)
    ]

    # The keyword table above is tuned to one pipeline's vocabulary. On an
    # unfamiliar export it matches nothing, so fall back to scoring the complaint
    # against each finding's own words. Crude, but it degrades instead of going
    # silent -- and going silent would look identical to "no problem here".
    if not matches:
        matches = _score_against_findings(lowered, bundle)

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


STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "it",
    "is", "are", "was", "were", "we", "our", "us", "they", "their", "i", "my",
    "that", "this", "these", "those", "some", "any", "all", "not", "no", "never",
    "ever", "got", "get", "see", "saw", "seems", "looks", "like", "only", "just",
    "back", "from", "with", "at", "as", "by", "up", "out", "about", "said",
    "someone", "really", "probably", "confirmed", "think", "one", "two",
}

MIN_OVERLAP = 2
"""Below this, a match is coincidence. Returning nothing here is what produces
an honest insufficient_evidence rather than the nearest-looking finding."""


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]{3,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def _related(left: str, right: str) -> bool:
    """Prefix match rather than stemming.

    A hand-rolled stemmer got this wrong in a way worth remembering: 'settlement'
    reduced to 'settl' while 'settle' stayed whole, so the two never matched and a
    complaint about payments not settling missed the settlement invariant
    entirely. Comparing on a shared prefix sidesteps the whole class of bug.
    """
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 4 and longer.startswith(shorter)


def _overlap(wanted: set[str], text: str) -> int:
    available = _tokens(text)
    return sum(1 for word in wanted if any(_related(word, other) for other in available))


def _score_against_findings(complaint: str, bundle: EvidenceBundle) -> list:
    """Rank findings by word overlap with the complaint.

    This is the part a real model does far better, and its crudeness here is the
    argument for using one: "supporters got the same confirmation email twice"
    and "duplicate delivery" share no tokens at all.
    """
    wanted = _tokens(complaint)
    if not wanted:
        return []

    scored = []
    for finding in bundle.ranked():
        # A hit on the finding's own identifier counts double: ids are canonical
        # names for the phenomenon (settlement, conservation, context_break), so
        # matching one is a far stronger signal than matching prose.
        identifier = finding.id.replace(".", " ").replace("_", " ")
        score = _overlap(wanted, f"{finding.title} {finding.summary}") + 2 * _overlap(
            wanted, identifier
        )
        if score >= MIN_OVERLAP:
            scored.append((score, finding))
    scored.sort(key=lambda pair: (pair[0], pair[1].rank_score), reverse=True)
    return [finding for _, finding in scored[:2]]
