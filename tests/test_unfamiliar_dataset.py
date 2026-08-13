"""Point the tool at a pipeline it has never seen.

Every other test runs against the export the detectors were written for, so they
prove the tool remembers. This file proves it can still work when nothing is
familiar: different services, different channel names, a different number of
stages, and a failure that none of D1-D5 encodes.

The synthetic pipeline below is deliberately unlike the real one:

    api-gateway  ->  fraud-check  ->  ledger-writer  ->  settlement  ->  bank
    (5 stages, not 7; 'rail' not 'message_type'; 'wire'/'ach' not email/sms/push)

and the injected fault is one no detector knows: a *middle* stage silently
swallowing one rail, plus a stage that appears for only some messages. If the
invariants layer is doing real work, it finds both without being told.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tracelens.analysis import analyse
from tracelens.loader import load_dataset
from tracelens.model import Dataset
from tracelens.topology import discover, profile

START = datetime(2027, 6, 1, 9, 0, tzinfo=timezone.utc)

# service, span name template, kind
PIPELINE = [
    ("api-gateway", "POST /v2/payments", "SERVER"),
    ("fraud-check", "screen {rail}", "CONSUMER"),
    ("ledger-writer", "post entry", "INTERNAL"),
    ("settlement", "batch {rail}", "PRODUCER"),
    ("bank-adapter", "submit {rail}", "CLIENT"),
]


def _build_export(tmp_path, *, swallow_rail="ach", swallow_after=1, extra_stage_every=7):
    """A five-stage payments pipeline with an injected mid-pipeline drop."""
    spans, logs, accepted = [], [], []
    span_counter = 0

    for index in range(60):
        rail = ("wire", "ach", "card")[index % 3]
        correlation_id = f"pay-{index:04d}"
        started = START + timedelta(minutes=17 * index)
        accepted.append(
            {
                "correlation_id": correlation_id,
                "message_type": rail,
                "tenant_id": f"merchant-{index % 4}",
                "accepted_at": _ts(started),
            }
        )

        trace_id = f"{index:032x}"
        parent = None
        # The fault: one rail never gets past the fraud-check stage.
        depth = swallow_after + 1 if rail == swallow_rail else len(PIPELINE)

        for stage, (service, name, kind) in enumerate(PIPELINE[:depth]):
            span_counter += 1
            span_id = f"{span_counter:016x}"
            spans.append(
                {
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "parent_span_id": parent,
                    "service": service,
                    "name": name.replace("{rail}", rail),
                    "kind": kind,
                    "start_time": _ts(started + timedelta(milliseconds=120 * stage)),
                    "duration_ms": 40 + stage,
                    "status": "OK",
                    "attributes": {
                        "correlation_id": correlation_id,
                        "message_type": rail,
                        "tenant_id": f"merchant-{index % 4}",
                    },
                }
            )
            parent = span_id
            logs.append(
                {
                    "timestamp": _ts(started + timedelta(milliseconds=120 * stage + 5)),
                    "service": service,
                    "level": "INFO",
                    "message": f"handled {name.replace('{rail}', rail)}",
                    "trace_id": trace_id,
                    "attributes": {"correlation_id": correlation_id},
                }
            )

        # A stage that appears for only some messages -- a different route, not a
        # truncation. Nothing in the tool has a rule for this.
        if depth == len(PIPELINE) and index % extra_stage_every == 0:
            span_counter += 1
            spans.append(
                {
                    "trace_id": trace_id,
                    "span_id": f"{span_counter:016x}",
                    "parent_span_id": parent,
                    "service": "bank-adapter",
                    "name": "retry submit",
                    "kind": "CLIENT",
                    "start_time": _ts(started + timedelta(milliseconds=900)),
                    "duration_ms": 700,
                    "status": "OK",
                    "attributes": {"correlation_id": correlation_id, "message_type": rail},
                }
            )

    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("spans.json", spans),
        ("logs.json", logs),
        ("accepted_messages.json", accepted),
        ("deploys.json", []),
        ("symptoms.json", {"symptoms": [{"from": "ops", "text": "some payments never settle"}]}),
    ):
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    return directory


def _ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@pytest.fixture(scope="module")
def foreign(tmp_path_factory):
    return load_dataset(_build_export(tmp_path_factory.mktemp("foreign")))


# --------------------------------------------------------------------------- #


def test_it_loads_a_pipeline_it_has_never_seen(foreign):
    assert len(foreign.accepted) == 60
    assert {s.service for s in foreign.spans} == {
        "api-gateway",
        "fraud-check",
        "ledger-writer",
        "settlement",
        "bank-adapter",
    }


def test_topology_is_learned_not_assumed(foreign):
    topology = discover(foreign)
    # Five stages, not the seven this repo was written around.
    assert len(topology.entry_nodes) == 1
    assert "api-gateway:POST /v2/payments" in topology.entry_nodes
    # Rails collapse onto templated nodes exactly as channels do in the real data.
    assert "fraud-check:screen {message_type}" in topology.nodes
    assert not any("wire" in node or "ach" in node for node in topology.nodes)


def test_the_injected_mid_pipeline_drop_is_found(foreign):
    """No detector encodes 'a middle stage swallows one rail'. The conservation
    invariant finds it anyway, and names the discriminating attribute."""
    analysis = analyse(foreign, include_novelty=False)
    conservation = [f for f in analysis.findings if f.id.startswith("INV.conservation")]
    assert conservation, "conservation invariant did not fire on a silent drop"

    finding = conservation[0]
    assert "fraud-check" in finding.id
    assert "message_type=ach" in finding.summary, "the discriminator must name the lost class"
    assert len(finding.affected) == 20
    assert all(c.startswith("pay-") for c in finding.affected)


def test_settlement_reconciliation_fires_without_knowing_the_domain(foreign):
    analysis = analyse(foreign, include_novelty=False)
    settlement = [f for f in analysis.findings if f.id == "INV.settlement"]
    assert settlement and len(settlement[0].affected) == 20


def test_the_minority_route_is_surfaced_separately_from_the_drop(foreign):
    """A stage that appears for only some messages is a different route, not a
    truncation. The tool must distinguish the two, since the fixes differ."""
    analysis = analyse(foreign, include_novelty=False)
    shapes = [f for f in analysis.findings if f.id.startswith("INV.path_shape")]
    assert len(shapes) >= 2, "expected both a truncated route and a divergent route"

    summaries = " ".join(f.summary for f in shapes)
    assert "truncated" in summaries
    assert "diverges" in summaries


def test_detectors_are_skipped_rather_than_inventing_findings(foreign):
    """The closed-world layer must not fire on a pipeline it does not describe.

    Before this was gated, D1 reported that *all three* rails were being dropped:
    no span mapped to a known stage, so every message looked undelivered. An
    earlier version of this test only asserted affected_count <= total, which is
    trivially true and caught nothing. Confidently wrong is worse than silent.
    """
    from tracelens.analysis import stage_coverage

    assert stage_coverage(foreign) < 0.6, "fixture is too similar to the real pipeline"

    analysis = analyse(foreign, include_novelty=False)
    assert not [f for f in analysis.findings if f.id.startswith("D")], (
        "detectors fired on a pipeline whose stages they cannot recognise"
    )

    skipped = [f for f in analysis.findings if f.id == "ERR.taxonomy_mismatch"]
    assert skipped, "skipping the detector layer must be reported, not silent"
    assert "invariant and novelty layers" in skipped[0].summary


def test_detectors_still_run_on_the_pipeline_they_were_written_for():
    """The gate must not be so eager that it disables the detectors on real data."""
    from tracelens.analysis import stage_coverage

    real = load_dataset()
    assert stage_coverage(real) == 1.0
    analysis = analyse(real, include_novelty=False)
    assert [f for f in analysis.findings if f.id.startswith("D1.")]


def test_no_layer_crashes_and_none_are_silently_empty(foreign):
    analysis = analyse(foreign, include_novelty=False)
    raised = [f for f in analysis.findings if f.id.startswith("ERR.") and f.id != "ERR.taxonomy_mismatch"]
    assert not raised, "a layer raised on unfamiliar data"
    assert analysis.by_layer("invariant"), "invariants found nothing on a broken pipeline"


def test_novelty_reports_the_difference_between_two_pipelines(foreign):
    """The 'what changed?' question, across two genuinely different exports."""
    from tracelens import novelty

    findings = novelty.compare_datasets(load_dataset(), foreign)
    dimensions = {f.params.get("dimension") for f in findings}
    assert {"services", "nodes", "channels"} <= dimensions

    services = next(f for f in findings if f.params.get("dimension") == "services")
    assert services.severity == "critical", "every service vanishing must not be low severity"
    detail = " ".join(e.detail for e in services.evidence)
    assert "api-gateway" in detail and "comms-ingest" in detail


def test_profile_is_shapes_not_volumes(foreign):
    """A fingerprint that moved with traffic would flag every busy Monday."""
    fingerprint = profile(foreign)
    flattened = json.dumps(fingerprint)
    assert "60" not in flattened.replace("v2", ""), "profile leaked a count"
    assert all(isinstance(v, list) for v in fingerprint.values())


def test_analysis_is_deterministic_on_unfamiliar_data(foreign):
    runs = [
        [f.id for f in analyse(foreign, include_novelty=False).findings] for _ in range(3)
    ]
    assert all(run == runs[0] for run in runs), "finding IDs must be stable across runs"


def test_empty_export_produces_no_crash_and_no_false_findings():
    """The degenerate case. Nothing to say is a valid answer."""
    analysis = analyse(Dataset(), include_novelty=False)
    assert not [f for f in analysis.findings if f.id.startswith("ERR.")]
    assert analysis.counts["invariant"] == 0


def test_triage_routes_a_complaint_on_the_unfamiliar_pipeline(foreign):
    """Offline, with no keyword rule for payments vocabulary, the stub must still
    reach the right finding by matching the complaint against the findings' own
    names -- or say insufficient rather than guess."""
    from tracelens.triage.engine import triage

    result = triage(foreign, "some payments never settle", use_stub=True).result
    assert result.hypotheses, "no hypothesis on a complaint the invariants answer"
    assert result.hypotheses[0].finding_id == "INV.settlement"


def test_triage_still_declines_when_nothing_matches(foreign):
    from tracelens.triage.engine import triage

    result = triage(foreign, "our webhooks stopped firing", use_stub=True).result
    assert result.verdict == "insufficient_evidence"
