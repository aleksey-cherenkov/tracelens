"""Detector outputs, pinned to exact IDs.

Pinning outputs rather than internals is deliberate: the rules are stated in
general terms and the thresholds are parameters, so a detector may be rewritten
freely as long as it still names the same messages.
"""

from __future__ import annotations

import pytest

from tracelens.config import Config
from tracelens.detectors import build_context, run_all


def test_every_documented_finding_surfaces(findings):
    for finding_id in (
        "D1.channel_drop.push",
        "D2.duplicate_delivery",
        "D3.provider_degradation.email",
        "D4.trace_context_break.sms",
        "D5.status_divergence",
        "D5.log_noise",
        "D5.broken_gauge.queue_depth",
    ):
        assert finding_id in findings, f"{finding_id} did not surface"


def test_d1_names_the_right_messages(findings):
    finding = findings["D1.channel_drop.push"]
    assert set(finding.affected) == {"corr-0005", "corr-0010", "corr-0020", "corr-0036"}
    assert finding.severity == "critical"
    assert finding.confidence == "observed"


def test_d1_fires_below_min_samples(context):
    """The sharpest edge in the spec: push is n=4 against min_samples=20.

    D1 is an existence claim, not a claim about a population, so the gate must
    not touch it. If this test fails the tool's headline finding disappears.
    """
    strict = Config(min_samples=1000)
    findings = {f.id: f for f in run_all(build_context(context.dataset, strict))}
    assert "D1.channel_drop.push" in findings
    assert findings["D1.channel_drop.push"].low_confidence_rate is True


def test_d1_reports_spread_to_counter_the_single_campaign_framing(findings):
    summary = findings["D1.channel_drop.push"].summary
    assert "3 tenant(s)" in summary
    assert "not confined to one campaign" in summary


def test_d2_names_the_right_messages_and_rules_out_double_publish(findings):
    finding = findings["D2.duplicate_delivery"]
    assert set(finding.affected) == {"corr-0014", "corr-0022", "corr-0035"}
    details = " ".join(e.detail for e in finding.evidence)
    assert "queue redelivery" in details
    assert "upstream double-publish" not in details


def test_d2_refuses_to_claim_a_tenant_pattern(findings):
    finding = findings["D2.duplicate_delivery"]
    assert finding.low_confidence_rate is True
    skew = [e for e in finding.evidence if e.ref == "tenant_skew"]
    assert skew and "not claimed as tenant-specific" in skew[0].detail


def test_d3_names_the_right_messages(findings):
    finding = findings["D3.provider_degradation.email"]
    assert set(finding.affected) == {
        "corr-0026", "corr-0027", "corr-0029", "corr-0030", "corr-0031", "corr-0032",
    }


def test_d3_groups_the_overnight_gap_into_one_incident(findings):
    """The window spans a 15h08m overnight gap with no sends at all. A max_gap
    below that splits one incident into two and changes the deploy arithmetic."""
    params = findings["D3.provider_degradation.email"].params
    assert params["largest_gap_in_window_s"] == 54480.0
    assert params["incident_max_gap_s"] > params["largest_gap_in_window_s"]


def test_d3_splits_the_incident_when_max_gap_is_tightened(context):
    tight = Config(incident_max_gap_s=3600)
    findings = [f for f in run_all(build_context(context.dataset, tight))
                if f.id.startswith("D3.")]
    assert len(findings) > 1, "a tighter max_gap must split the window"


def test_d3_baseline_uses_2xx_sends_not_whole_clean_days(findings):
    """Excluding whole days would discard corr-0033 and corr-0035, the clean
    sends that bracket the recovery."""
    assert findings["D3.provider_degradation.email"].params["baseline_ms"] == 235.0


def test_d4_names_all_sms_and_no_email(findings):
    finding = findings["D4.trace_context_break.sms"]
    assert len(finding.affected) == 8
    assert "D4.trace_context_break.email" not in findings


def test_d4_reports_the_orphan_half_as_unreachable(findings):
    reach = [e for e in findings["D4.trace_context_break.sms"].evidence
             if e.ref == "orphan_reachability"]
    assert reach and "no trace-based and no log-based route" in reach[0].detail


def test_d5_gauge_is_flagged_as_constant_and_undimensioned(findings):
    finding = findings["D5.broken_gauge.queue_depth"]
    assert "1200" in finding.title
    assert "no dimension label" in finding.summary


def test_findings_rank_severity_first(findings):
    """A well-evidenced medium must not outrank a thinly evidenced critical."""
    ordered = sorted(findings.values(), key=lambda f: f.rank_score, reverse=True)
    assert ordered[0].id == "D1.channel_drop.push"


@pytest.mark.parametrize("finding_id", ["D1.channel_drop.push", "D3.provider_degradation.email"])
def test_ambiguous_findings_carry_competing_alternatives(findings, finding_id):
    assert len(findings[finding_id].alternatives) >= 2
    assert findings[finding_id].would_resolve
