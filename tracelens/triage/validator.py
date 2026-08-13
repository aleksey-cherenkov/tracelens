"""The hard gate.

Prompt instructions are advisory; this is not. Any hypothesis citing a reference
absent from the citation index is dropped from the output entirely and counted as
a rejection. A non-zero rejection rate is early warning that the model is drifting
toward fabrication, so it is reported rather than swallowed.

Confidence and ranking are attached here, from the cited Finding, so the model
cannot promote a guess by asserting certainty about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..evidence import SEVERITY_ORDER, EvidenceBundle


@dataclass
class Hypothesis:
    finding_id: str
    summary: str
    evidence_refs: list[str]
    why_this_rank: str
    severity: str
    confidence: str
    alternatives: list[dict] = field(default_factory=list)
    would_resolve: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple[int, int]:
        return (SEVERITY_ORDER.get(self.severity, 0), len(self.evidence_refs))


@dataclass
class Rejection:
    finding_id: str
    reason: str
    bad_refs: list[str] = field(default_factory=list)


@dataclass
class TriageResult:
    verdict: str
    restated_complaint: str
    hypotheses: list[Hypothesis] = field(default_factory=list)
    ruled_out: list[dict] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    would_resolve: list[str] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    tool_calls: int = 0
    source: str = "live"

    @property
    def insufficient(self) -> bool:
        return self.verdict == "insufficient_evidence" or not self.hypotheses


def validate(raw: dict, bundle: EvidenceBundle) -> TriageResult:
    result = TriageResult(
        verdict=str(raw.get("verdict", "hypotheses")),
        restated_complaint=str(raw.get("restated_complaint", "")),
        ruled_out=list(raw.get("ruled_out") or []),
        checked=[str(c) for c in (raw.get("checked") or [])],
        would_resolve=[str(w) for w in (raw.get("would_resolve") or [])],
    )

    for item in raw.get("hypotheses") or []:
        finding_id = str(item.get("finding_id", ""))
        refs = [str(r) for r in (item.get("evidence_refs") or [])]
        finding = bundle.by_id(finding_id)

        if finding is None:
            result.rejections.append(
                Rejection(finding_id, "cites a finding that does not exist")
            )
            continue

        if not refs:
            result.rejections.append(Rejection(finding_id, "cites no evidence at all"))
            continue

        unknown = [r for r in refs if not bundle.index.knows(r)]
        if unknown:
            result.rejections.append(
                Rejection(finding_id, "cites unresolvable evidence", unknown)
            )
            continue

        result.hypotheses.append(
            Hypothesis(
                finding_id=finding_id,
                summary=str(item.get("summary", "")),
                evidence_refs=refs,
                why_this_rank=str(item.get("why_this_rank", "")),
                # Code-owned: inherited from the finding, never from the model.
                severity=finding.severity,
                confidence=finding.confidence,
                alternatives=[h.as_dict() for h in finding.alternatives],
                would_resolve=list(finding.would_resolve),
            )
        )

    # Collapse repeats of the same finding. Observed on the first live run: asked
    # for ">=2 hypotheses" when only one finding matched the complaint, the model
    # satisfied the count by splitting that finding into two entries -- the second
    # restating the ambiguity that is already attached to the first. Two entries
    # bearing one finding_id is padding, and it reads as two independent
    # explanations when there is one. Citations are unioned so nothing is lost.
    deduped: list[Hypothesis] = []
    seen: dict[str, Hypothesis] = {}
    for hypothesis in result.hypotheses:
        first = seen.get(hypothesis.finding_id)
        if first is None:
            seen[hypothesis.finding_id] = hypothesis
            deduped.append(hypothesis)
            continue
        for ref in hypothesis.evidence_refs:
            if ref not in first.evidence_refs:
                first.evidence_refs.append(ref)
        result.rejections.append(
            Rejection(
                hypothesis.finding_id,
                "duplicate hypothesis for the same finding — merged into the first "
                "(its alternatives are already surfaced there)",
            )
        )
    result.hypotheses = deduped

    # A finding whose alternatives the model dropped gets them reattached rather
    # than silently losing the ambiguity.
    for hypothesis in result.hypotheses:
        for item in hypothesis.would_resolve:
            if item not in result.would_resolve:
                result.would_resolve.append(item)

    # Order is deliberately NOT re-sorted here. Ranking hypotheses by fit to what
    # the person actually asked is the model's job -- it is the fuzzy language
    # work code does badly. Re-sorting by severity would make the most severe
    # finding win every complaint regardless of relevance, so a question about
    # slow email would be answered with the push outage. What code owns is the
    # severity and confidence *labels* attached above, which the model cannot
    # inflate, and the global finding ranking in evidence.py.

    if not result.hypotheses:
        result.verdict = "insufficient_evidence"
    elif len(result.hypotheses) == 1 and not result.hypotheses[0].alternatives:
        # The schema requires >=2 hypotheses or an explicit insufficient verdict.
        # One surviving hypothesis with no competing alternatives is a schema
        # violation, flagged rather than presented as a confident answer.
        result.rejections.append(
            Rejection(
                result.hypotheses[0].finding_id,
                "single hypothesis with no alternatives — schema requires >=2 or "
                "an explicit insufficient_evidence verdict",
            )
        )

    return result
