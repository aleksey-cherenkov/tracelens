"""The hard gate.

Prompt instructions are advisory; this is not. Any hypothesis citing a reference
the model was never shown is dropped from the output entirely and counted as a
rejection. A non-zero rejection rate is early warning that the model is drifting
toward fabrication, so it is reported rather than swallowed.

What changed with the timeline design, and it is worth being honest about: the
model now reads raw records, so the gate can no longer guarantee that a *number*
is right — only that an identifier is real. It cannot invent a journey, a route or
a deploy. It can misread a duration. The mitigation is upstream: the counts and
percentiles it needs are already in the payload, so it has no reason to derive one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..evidence import SliceIndex

# What a citable identifier looks like. Prose is not a citation.
REF_SHAPE = re.compile(r"^[\w][\w./:@-]{2,}$")


@dataclass
class Hypothesis:
    summary: str
    evidence_refs: list[str]
    reading: str = ""
    alternative: str = ""


@dataclass
class Rejection:
    summary: str
    reason: str
    bad_refs: list[str] = field(default_factory=list)


@dataclass
class TriageResult:
    verdict: str
    restated_complaint: str
    hypotheses: list[Hypothesis] = field(default_factory=list)
    ruled_out: list[dict] = field(default_factory=list)
    limits_that_apply: list[str] = field(default_factory=list)
    would_resolve: list[str] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    tool_calls: int = 0
    source: str = "live"

    @property
    def insufficient(self) -> bool:
        return self.verdict == "insufficient_evidence" or not self.hypotheses


def _normalise(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower())}


def _already_said(candidate: str, existing: list[str], overlap: float = 0.7) -> bool:
    """True if `existing` already contains substantially the same sentence.

    Near-duplicates have to collapse, not just exact ones: the model writes its
    own version of what would resolve an ambiguity and it says the same thing in
    slightly different words every time.
    """
    words = _normalise(candidate)
    if not words:
        return True
    for other in existing:
        shared = words & _normalise(other)
        if shared and len(shared) / min(len(words), len(_normalise(other))) >= overlap:
            return True
    return False


def validate(raw: dict, index: SliceIndex, limits: list[str] | None = None) -> TriageResult:
    result = TriageResult(
        verdict=str(raw.get("verdict", "hypotheses")),
        restated_complaint=str(raw.get("restated_complaint", "")),
        ruled_out=list(raw.get("ruled_out") or []),
        limits_that_apply=[str(x) for x in (raw.get("limits_that_apply") or [])],
        would_resolve=[str(w) for w in (raw.get("would_resolve") or [])],
    )

    for item in raw.get("hypotheses") or []:
        summary = str(item.get("summary", ""))
        refs = [str(r).strip() for r in (item.get("evidence_refs") or []) if str(r).strip()]

        if not refs:
            result.rejections.append(Rejection(summary, "cites no evidence at all"))
            continue

        # A ref that isn't shaped like an identifier is prose dressed as a
        # citation. Rejecting it here keeps the index check meaningful.
        malformed = [r for r in refs if not REF_SHAPE.match(r)]
        unknown = [r for r in refs if REF_SHAPE.match(r) and not index.knows(r)]
        if unknown or malformed:
            result.rejections.append(
                Rejection(
                    summary,
                    "cites evidence that was never shown"
                    if unknown
                    else "cites prose rather than an identifier",
                    unknown + malformed,
                )
            )
            continue

        result.hypotheses.append(
            Hypothesis(
                summary=summary,
                evidence_refs=refs,
                reading=str(item.get("reading", "")),
                alternative=str(item.get("alternative") or ""),
            )
        )

    # Collapse repeats. Observed on a live run of the previous design: asked for
    # ">=2 hypotheses" when only one thing matched, the model satisfied the count
    # by splitting one explanation into two entries. Two entries reading as two
    # independent explanations when there is one is worse than a single honest
    # answer. Citations are unioned so nothing is lost.
    deduped: list[Hypothesis] = []
    for hypothesis in result.hypotheses:
        twin = next(
            (h for h in deduped if _already_said(hypothesis.summary, [h.summary])), None
        )
        if twin is None:
            deduped.append(hypothesis)
            continue
        for ref in hypothesis.evidence_refs:
            if ref not in twin.evidence_refs:
                twin.evidence_refs.append(ref)
        result.rejections.append(
            Rejection(hypothesis.summary, "duplicate hypothesis — merged into the first")
        )
    result.hypotheses = deduped

    # Every stated limit the model claimed applies is kept; ones it ignored are
    # appended, because a limit the answer skipped is exactly the one worth seeing.
    for limit in limits or []:
        if not _already_said(limit, result.limits_that_apply):
            result.limits_that_apply.append(limit)

    if not result.hypotheses:
        result.verdict = "insufficient_evidence"
    elif len(result.hypotheses) == 1 and not result.hypotheses[0].alternative:
        # The schema requires >=2 hypotheses, or one plus the competing explanation
        # the data cannot separate it from, or an explicit insufficient verdict.
        # A lone confident answer is a schema violation, flagged rather than shown
        # as though it were the whole truth.
        result.rejections.append(
            Rejection(
                result.hypotheses[0].summary,
                "single hypothesis with no stated alternative — schema requires a "
                "competing explanation or an explicit insufficient_evidence verdict",
            )
        )

    return result
