"""Deploy attribution.

The assignment's trap lives here. The rule-out is arithmetic and belongs in code:
an LLM asked to compare two ISO-8601 timestamps mid-incident is the wrong tool,
and 'the deploy that day' is the explanation everyone reaches for first.
"""

from __future__ import annotations


def _evidence(finding, ref):
    return next((e for e in finding.evidence if e.ref == ref), None)


def test_the_blamed_deploy_is_ruled_out_with_its_reason(findings):
    finding = findings["D3.provider_degradation.email"]
    item = _evidence(finding, "c52a0f9")
    assert item is not None
    assert "ruled out" in item.detail
    assert "5h00m" in item.detail, "the gap must be stated, not just asserted"
    assert "3 affected message(s) had already occurred" in item.detail
    assert "is NOT the cause" in finding.summary


def test_the_recovery_deploy_is_neither_credited_nor_excluded(findings):
    finding = findings["D3.provider_degradation.email"]
    item = _evidence(finding, "e18d773")
    assert item is not None
    assert "INSIDE the recovery window" in item.detail
    assert finding.confidence == "ambiguous"

    ids = {h.id for h in finding.alternatives}
    assert ids == {"H1.provider_side", "H2.client_side"}
    assert any("diff" in w for w in finding.would_resolve)


def test_other_services_deploys_are_surfaced_not_hidden(context, findings):
    """A near-miss that is silently dropped teaches the reader nothing. The
    orchestrator deploy is temporally adjacent and must be shown as ruled out."""
    from datetime import timedelta

    from tracelens.detectors.provider import Window, correlate_deploys

    sends = [
        a.send
        for t in context.traces.values()
        for a in t.attempts
        if t.correlation_id in {"corr-0026", "corr-0032"}
    ]
    window = Window("email", sorted(sends, key=lambda s: s.start_time))
    verdicts, adjacent = correlate_deploys(
        context.dataset.deploys, "comms-sender", window, timedelta(days=7).total_seconds()
    )
    assert {v.deploy.sha for v in verdicts} == {"c52a0f9"}
    assert "7d3b8e1" in {d.sha for d in adjacent}, "orchestrator deploy must be visible"


def test_orchestrator_deploy_is_not_attributed_to_the_duplicates(findings):
    """The orchestrator shipped 2026-03-04 11:00 and the first duplicate is the
    same day at 15:39. A proximity-only correlator blames it; the span topology
    proves the orchestrator published exactly once."""
    finding = findings["D2.duplicate_delivery"]
    assert "7d3b8e1" not in {e.ref for e in finding.evidence}
    assert {h.id for h in finding.alternatives} == {"delete_not_issued", "delete_failed"}
