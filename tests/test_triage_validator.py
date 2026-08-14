"""The honesty guarantees, made executable.

Prompt instructions are advisory. Everything asserted here is enforced by code,
which is the only reason any of it can be relied on during an incident.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelens.triage.context import build_bundle
from tracelens.triage.engine import triage
from tracelens.triage.tools import ToolBox
from tracelens.triage.validator import validate

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def bundle(request):
    dataset = request.getfixturevalue("dataset")
    return build_bundle(dataset, "test complaint")[0]


# -- the hard gate ---------------------------------------------------------- #


def test_fabricated_citation_is_dropped_entirely(bundle):
    result = validate(
        {
            "verdict": "hypotheses",
            "hypotheses": [
                {
                    "finding_id": "D1.channel_drop.push",
                    "summary": "invented",
                    "evidence_refs": ["corr-9999"],
                }
            ],
        },
        bundle,
    )
    assert result.hypotheses == []
    assert result.verdict == "insufficient_evidence"
    assert result.rejections[0].bad_refs == ["corr-9999"]


def test_hypothesis_citing_no_evidence_is_dropped(bundle):
    result = validate(
        {"hypotheses": [{"finding_id": "D1.channel_drop.push", "evidence_refs": []}]},
        bundle,
    )
    assert result.hypotheses == []
    assert "cites no evidence" in result.rejections[0].reason


def test_hypothesis_citing_an_invented_finding_is_dropped(bundle):
    result = validate(
        {"hypotheses": [{"finding_id": "D9.does_not_exist", "evidence_refs": ["corr-0005"]}]},
        bundle,
    )
    assert result.hypotheses == []
    assert "does not exist" in result.rejections[0].reason


def test_single_hypothesis_without_alternatives_is_flagged(bundle):
    result = validate(
        {
            "hypotheses": [
                {
                    "finding_id": "D5.log_noise",
                    "summary": "logs are noisy",
                    "evidence_refs": ["unjoinable_share"],
                }
            ]
        },
        bundle,
    )
    assert len(result.hypotheses) == 1
    assert any("schema requires" in r.reason for r in result.rejections)


def test_confidence_is_inherited_not_asserted(bundle):
    """The model claiming certainty must not change what the user is shown."""
    result = validate(
        {
            "hypotheses": [
                {
                    "finding_id": "D3.provider_degradation.email",
                    "summary": "definitely the SDK bump, 100% certain",
                    "confidence": "observed",
                    "severity": "critical",
                    "evidence_refs": ["incident_window"],
                }
            ]
        },
        bundle,
    )
    hypothesis = result.hypotheses[0]
    assert hypothesis.confidence == "ambiguous"
    assert hypothesis.severity == "medium"


def test_validator_preserves_model_order(bundle):
    """Re-sorting by severity would answer every complaint with the push outage.
    Relevance to the question asked is the model's judgement to make."""
    result = validate(
        {
            "hypotheses": [
                {
                    "finding_id": "D3.provider_degradation.email",
                    "evidence_refs": ["incident_window"],
                },
                {"finding_id": "D1.channel_drop.push", "evidence_refs": ["push.stopped"]},
            ]
        },
        bundle,
    )
    assert [h.finding_id for h in result.hypotheses] == [
        "D3.provider_degradation.email",
        "D1.channel_drop.push",
    ]


def test_alternatives_are_reattached_from_the_finding(bundle):
    """A model that drops a competing explanation does not get to lose it."""
    result = validate(
        {
            "hypotheses": [
                {
                    "finding_id": "D3.provider_degradation.email",
                    "evidence_refs": ["incident_window"],
                }
            ]
        },
        bundle,
    )
    assert {a["id"] for a in result.hypotheses[0].alternatives} == {
        "H1.provider_side",
        "H2.client_side",
    }


# -- golden set ------------------------------------------------------------- #


GOLDEN = [
    (1, "D1.channel_drop.push"),
    (2, "D2.duplicate_delivery"),
    (3, "D3.provider_degradation.email"),
    (4, "D4.trace_context_break.sms"),
    (5, "D5.log_noise"),
]


@pytest.mark.parametrize("index,expected", GOLDEN)
def test_each_symptom_routes_to_its_finding(dataset, index, expected):
    complaint = dataset.symptoms[index - 1].text
    result = triage(dataset, complaint, use_stub=True).result
    assert result.hypotheses, f"symptom {index} produced no hypothesis"
    assert result.hypotheses[0].finding_id == expected


def test_the_trap_case(dataset):
    """Symptom 3 must rank throttling first, cite the gap, AND keep the
    ambiguity. Confidently blaming c52a0f9 fails -- so does confidently
    crediting e18d773."""
    result = triage(dataset, dataset.symptoms[2].text, use_stub=True).result
    top = result.hypotheses[0]
    assert top.finding_id == "D3.provider_degradation.email"
    assert top.confidence == "ambiguous"
    assert {a["id"] for a in top.alternatives} == {"H1.provider_side", "H2.client_side"}
    # Assert substance, not phrasing. An earlier version of this test required the
    # literal words "ruled out"; the live model said "postdates the onset ... by 5
    # hours" instead, which is the same claim made better. Pin the sha and the
    # timing argument, and let the model choose its own words.
    ruled = " ".join(f"{r.get('claim','')} {r.get('why_not','')}" for r in result.ruled_out)
    assert "c52a0f9" in ruled, "the blamed deploy must be named"
    assert any(k in ruled.lower() for k in ("postdate", "after", "5 hour")), (
        "the rule-out must rest on the timing gap, however it is worded"
    )


@pytest.mark.parametrize(
    "complaint",
    ["the CSV export job is failing", "our Salesforce sync stopped last night"],
)
def test_out_of_scope_complaint_returns_insufficient_evidence(dataset, complaint):
    """The test that catches pattern-matching to the nearest available finding.

    Both subjects are unrelated to a message-delivery pipeline. Verified live:
    with PLATFORM.md in the prompt the model returns insufficient evidence for
    both and cites the architecture when explaining why.

    Note what this test deliberately does NOT use: "our webhooks stopped firing".
    See test_semantically_adjacent_term_is_a_known_limitation below.
    """
    result = triage(dataset, complaint, use_stub=True).result
    assert result.verdict == "insufficient_evidence"
    assert result.hypotheses == []
    assert result.checked, "must report what was checked"
    assert result.would_resolve, "must report what would be needed"


def test_semantically_adjacent_term_is_a_known_limitation(dataset):
    """A documented gap, pinned so it cannot be quietly forgotten.

    "Webhooks" are not part of this platform, but they are semantically adjacent
    to push notifications -- both are outbound fire-and-forget calls to a remote
    endpoint -- and push happens to be 100% dead in this data.

    Live, with architecture context, the model still answers with the push
    finding. It does hedge ("if 'webhooks' refers to push notifications") and it
    does surface the terminology mismatch as the first thing that would resolve
    the question, but the verdict is `hypotheses` and the label is CRITICAL. A
    reader skimming sees a confident answer to a question about something that
    does not exist here.

    Arguably that is the more useful reply than a flat refusal -- an on-call
    engineer would likely say the same thing. What is wrong is the presentation:
    the term-mapping assumption belongs in the verdict, not in prose. Left as a
    limitation rather than patched, because the fix is unverified.
    """
    result = triage(dataset, "our webhooks stopped firing", use_stub=True).result
    # The stub declines; the live model does not. Asserting the stub's behaviour
    # here would restate the exact false confidence that hid this for weeks.
    assert result.verdict == "insufficient_evidence", (
        "stub behaviour only -- see the docstring; the live model answers instead"
    )


def test_triage_is_deterministic(dataset):
    """An analyzer that answers differently each run is worse than none."""
    complaint = dataset.symptoms[2].text
    runs = [
        [h.finding_id for h in triage(dataset, complaint, use_stub=True).result.hypotheses]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


# -- context and tools ------------------------------------------------------ #


def test_context_size_is_bounded_by_findings_not_telemetry(bundle):
    payload = json.dumps(bundle.as_prompt_payload())
    assert len(payload) < 40_000, "prompt payload must not scale with telemetry volume"
    assert "spans" not in bundle.as_prompt_payload()


def test_tools_truncate_and_say_so(context, bundle):
    box = ToolBox(bundle, context)
    result = box.run("query_messages", {"limit": 3})
    assert result["total"] == 41
    assert result["returned"] == 3
    assert result["truncated"] is True
    assert "38 more not shown" in result["note_truncated"]


def test_unknown_tool_returns_an_error_rather_than_raising(context, bundle):
    box = ToolBox(bundle, context)
    assert "error" in box.run("run_query", {"sql": "SELECT 1"})
    assert "error" in box.run("get_trace", {"correlation_id": "corr-9999"})


def test_recorded_examples_are_committed_and_labelled():
    files = sorted(EXAMPLES.glob("*.json"))
    assert files, "no recorded transcripts committed"
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "complaint" in payload and "response" in payload
        note, source = payload["note"], payload.get("source", "")
        # Every transcript must say which it is. A stub run passed off as a model
        # run would be the single most dishonest thing in the repo.
        assert ("stub" in note.lower()) or ("live model run" in note.lower()), (
            f"{path.name} does not disclose whether it came from the model or the stub"
        )
        if source.startswith("live"):
            assert "stub" not in note.lower()
        else:
            assert "stub" in note.lower()


def test_duplicate_finding_ids_are_merged_not_listed_twice(bundle):
    """Regression from the first live run: asked for >=2 hypotheses when only one
    finding matched, the model split that finding into two entries. Two entries
    with one finding_id read as two independent explanations."""
    result = validate(
        {
            "hypotheses": [
                {
                    "finding_id": "D3.provider_degradation.email",
                    "summary": "provider throttling",
                    "evidence_refs": ["incident_window", "c52a0f9"],
                },
                {
                    "finding_id": "D3.provider_degradation.email",
                    "summary": "and the recovery is ambiguous",
                    "evidence_refs": ["e18d773", "blast_radius"],
                },
            ]
        },
        bundle,
    )
    assert len(result.hypotheses) == 1
    assert result.hypotheses[0].evidence_refs == [
        "incident_window",
        "c52a0f9",
        "e18d773",
        "blast_radius",
    ], "citations from the merged duplicate must be preserved, not discarded"
    assert any("duplicate hypothesis" in r.reason for r in result.rejections)
    assert result.verdict == "hypotheses", "a merged single hypothesis with alternatives is valid"


def test_prompt_rule_matches_what_the_validator_enforces(bundle):
    """The prompt said 'at least two hypotheses' while the validator accepted one
    with alternatives. That gap is what pushed the model into padding."""
    from tracelens.triage import prompts

    assert "DIFFERENT finding_id" in prompts.SYSTEM
    assert "Do NOT list the same finding_id twice" in prompts.SYSTEM
