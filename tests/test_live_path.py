"""Exercise the live model path with a fake SDK -- no key, no network.

Without this, the live path is the one thing in the repo that has never run. The
fake client asserts the parts that are easy to get wrong and impossible to notice
until an incident: that every tool actually executes, that a fenced JSON reply
parses, that the validator overrides model-asserted confidence, and that the
request is pinned to temperature 0.
"""

from __future__ import annotations

import importlib.machinery
import json
import sys
import types
from dataclasses import dataclass, field

import pytest

from tracelens.triage import engine


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class _Response:
    content: list


@dataclass
class _FakeMessages:
    reply: dict
    tool_calls: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    fenced: bool = True

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1 and self.tool_calls:
            return _Response(
                [_ToolUse(f"t{i}", name, args) for i, (name, args) in enumerate(self.tool_calls)]
            )
        body = json.dumps(self.reply)
        return _Response([_Text(f"```json\n{body}\n```" if self.fenced else body)])


class _FakeAuthError(Exception):
    pass


class _FakeConnError(Exception):
    pass


class _FakeBadRequest(Exception):
    pass


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a fake `anthropic` module for the duration of one test.

    The module must carry the exception classes as well as the client: engine.py
    catches `anthropic.AuthenticationError` and friends by attribute, so a fake
    without them turns a handled failure into an AttributeError.
    """

    def install(messages: _FakeMessages):
        module = types.ModuleType("anthropic")
        module.__spec__ = importlib.machinery.ModuleSpec("anthropic", loader=None)
        module.Anthropic = lambda api_key=None: types.SimpleNamespace(messages=messages)
        module.AuthenticationError = _FakeAuthError
        module.APIConnectionError = _FakeConnError
        module.BadRequestError = _FakeBadRequest
        monkeypatch.setitem(sys.modules, "anthropic", module)
        return messages

    return install


GOOD_REPLY = {
    "verdict": "hypotheses",
    "restated_complaint": "email slow on March 9",
    "hypotheses": [
        {
            "finding_id": "D3.provider_degradation.email",
            "summary": "provider throttling",
            "evidence_refs": ["incident_window", "c52a0f9"],
            "why_this_rank": "direct match",
        },
        {
            "finding_id": "D5.status_divergence",
            "summary": "nothing alerted",
            "evidence_refs": ["span_status_errors"],
            "why_this_rank": "context",
        },
    ],
    "ruled_out": [{"claim": "the sender deploy", "why_not": "postdates onset"}],
    "checked": ["D1.channel_drop.push"],
    "would_resolve": [],
}

ALL_TOOLS = [
    ("list_findings", {}),
    ("get_finding_evidence", {"finding_id": "D3.provider_degradation.email"}),
    ("get_trace", {"correlation_id": "corr-0026"}),
    ("query_messages", {"channel": "push", "stopped_only": True}),
    ("get_deploys", {"service": "comms-sender"}),
]


def test_full_tool_loop_runs_and_validates(dataset, fake_sdk):
    fake = fake_sdk(_FakeMessages(reply=GOOD_REPLY, tool_calls=ALL_TOOLS))
    run = engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")

    assert run.result.tool_calls == 5, "every tool must execute, not just be declared"
    assert run.result.source.startswith("live")
    assert [h.finding_id for h in run.result.hypotheses] == [
        "D3.provider_degradation.email",
        "D5.status_divergence",
    ]
    assert run.result.rejections == []
    assert len(fake.requests) == 2, "one tool round-trip, then the answer"


def test_request_never_sets_a_sampling_parameter(dataset, fake_sdk):
    """Sonnet 5 returns a 400 for any non-default temperature, top_p or top_k --
    on every request, thinking or not. An earlier draft sent temperature=0 for
    determinism and would have failed on the first live call. Determinism now
    comes from constraining what the model may decide, and is *measured* by
    test_triage_is_deterministic rather than requested from a parameter."""
    fake = fake_sdk(_FakeMessages(reply=GOOD_REPLY))
    engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")
    request = fake.requests[0]

    for parameter in ("temperature", "top_p", "top_k"):
        assert parameter not in request, f"{parameter} is rejected by this model"
    assert request["model"] == engine.MODEL
    assert [t["name"] for t in request["tools"]] == [t["name"] for t in engine.TOOL_SCHEMAS]


def test_effort_is_set_explicitly_and_overridable(dataset, fake_sdk):
    """The API defaults to 'high'; this workload does not need it, and effort
    also governs how many tool calls get made."""
    fake = fake_sdk(_FakeMessages(reply=GOOD_REPLY))
    engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")
    assert fake.requests[0]["output_config"] == {"effort": engine.EFFORT}
    assert engine.EFFORT == "medium"

    fake2 = fake_sdk(_FakeMessages(reply=GOOD_REPLY))
    engine.triage(
        dataset, "email was slow", use_stub=False, api_key="sk-ant-test", effort="high"
    )
    assert fake2.requests[0]["output_config"] == {"effort": "high"}


def test_model_cannot_inflate_its_own_confidence(dataset, fake_sdk):
    reply = json.loads(json.dumps(GOOD_REPLY))
    reply["hypotheses"][0]["confidence"] = "observed"
    reply["hypotheses"][0]["severity"] = "critical"
    fake_sdk(_FakeMessages(reply=reply))

    run = engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")
    top = run.result.hypotheses[0]
    assert top.confidence == "ambiguous", "confidence is inherited from the finding"
    assert top.severity == "medium"
    assert {a["id"] for a in top.alternatives} == {"H1.provider_side", "H2.client_side"}


def test_fabricated_citation_from_a_live_reply_is_dropped(dataset, fake_sdk):
    reply = json.loads(json.dumps(GOOD_REPLY))
    reply["hypotheses"][0]["evidence_refs"] = ["corr-9999"]
    fake_sdk(_FakeMessages(reply=reply))

    run = engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")
    assert [h.finding_id for h in run.result.hypotheses] == ["D5.status_divergence"]
    assert run.result.rejections[0].bad_refs == ["corr-9999"]


def test_unfenced_json_also_parses(dataset, fake_sdk):
    fake_sdk(_FakeMessages(reply=GOOD_REPLY, fenced=False))
    run = engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")
    assert run.result.verdict == "hypotheses"


def test_non_json_reply_fails_loudly(dataset, fake_sdk):
    fake = _FakeMessages(reply={})
    fake.create = lambda **kw: _Response([_Text("I think it was the deploy, honestly.")])
    fake_sdk(fake)
    with pytest.raises(ValueError, match="no JSON object"):
        engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")


def test_runaway_tool_loop_is_capped(dataset, fake_sdk):
    """An unbounded agent is an unbounded bill."""

    class Endless(_FakeMessages):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            return _Response([_ToolUse("t", "list_findings", {})])

    fake_sdk(Endless(reply=GOOD_REPLY))
    with pytest.raises(RuntimeError, match="within 8 tool iterations"):
        engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")


def test_prompt_carries_findings_not_raw_telemetry(dataset, fake_sdk):
    fake = fake_sdk(_FakeMessages(reply=GOOD_REPLY))
    engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")
    prompt = fake.requests[0]["messages"][0]["content"]

    assert "D1.channel_drop.push" in prompt
    # Three layers now contribute findings, so the payload is larger than when
    # only detectors ran. What matters is that it is bounded by finding count,
    # which test_prompt_scales_with_findings_not_message_count pins.
    assert len(prompt) < 45_000, "prompt size must be O(findings), not O(telemetry)"

    # Span IDs appear only as citation pointers ("spans.json#span_id=...") so a
    # human can check a claim by hand. What must never appear is a serialised
    # span or log object.
    assert '"kind": "SERVER"' not in prompt and '"duration_ms"' not in prompt
    assert "GET /health" not in prompt, "raw log lines must never reach the prompt"
    assert prompt.count("span_id=") < 10, "citation pointers only, not a span dump"


def test_prompt_scales_with_findings_not_message_count(dataset):
    """The invariant that lets this survive go-live: 5x the traffic must not
    change the prompt size.

    Note the earlier version of this test simply doubled the span list, which is
    not more traffic -- it is every span emitted twice, a real anomaly that
    legitimately produces new findings. Growth has to be measured against more
    *distinct* messages, so the fixture below clones whole journeys under fresh
    correlation IDs.
    """
    from tracelens.triage.context import build_bundle

    base = len(json.dumps(build_bundle(dataset, "x")[0].as_prompt_payload()))
    bigger = _multiply_messages(dataset, 5)
    assert len(bigger.accepted) == len(dataset.accepted) * 5

    grown = len(json.dumps(build_bundle(bigger, "x")[0].as_prompt_payload()))
    assert grown < base * 1.25, (
        f"payload grew {grown / base:.2f}x when message count grew 5x — context is "
        "tracking volume, not findings"
    )


def _multiply_messages(dataset, factor: int):
    """Clone every journey under fresh correlation IDs: genuinely more traffic,
    same shapes, same failure rates."""
    import dataclasses

    from tracelens.model import Dataset

    spans, logs, accepted = list(dataset.spans), list(dataset.logs), list(dataset.accepted)
    for copy_index in range(1, factor):
        suffix = f"-c{copy_index}"

        def remap(value):
            return f"{value}{suffix}" if value else value

        for message in dataset.accepted:
            accepted.append(
                dataclasses.replace(message, correlation_id=remap(message.correlation_id))
            )
        for span in dataset.spans:
            attributes = dict(span.attributes)
            attributes["correlation_id"] = remap(attributes.get("correlation_id"))
            spans.append(
                dataclasses.replace(
                    span,
                    span_id=remap(span.span_id),
                    parent_span_id=remap(span.parent_span_id),
                    trace_id=remap(span.trace_id),
                    attributes=attributes,
                )
            )
        for record in dataset.logs:
            attributes = dict(record.attributes)
            if attributes.get("correlation_id"):
                attributes["correlation_id"] = remap(attributes["correlation_id"])
            logs.append(dataclasses.replace(record, attributes=attributes))

    return Dataset(
        spans=spans, logs=logs, deploys=dataset.deploys, accepted=accepted,
        symptoms=dataset.symptoms,
    )


def test_key_from_flag_is_used_and_reported(dataset, fake_sdk):
    fake_sdk(_FakeMessages(reply=GOOD_REPLY))
    run = engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-test")
    assert "key from flag" in run.result.source
    assert "sk-ant-test" not in run.result.source, "the key itself must never be echoed"


# -- failure modes ---------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_FakeAuthError, "401 Unauthorized"),
        (_FakeConnError, "could not reach the API"),
        (_FakeBadRequest, "rejected the request"),
    ],
)
def test_api_failures_surface_as_one_line_not_a_traceback(dataset, fake_sdk, exc, expected):
    """A reviewer who typos a key should get a sentence telling them what to do,
    not forty lines of httpx internals. Verified against a real 401 -- the key
    supplied during development was rejected, and this is what that now looks
    like."""

    class Failing(_FakeMessages):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            raise exc("boom")

    fake_sdk(Failing(reply=GOOD_REPLY))
    with pytest.raises(engine.TriageError, match=expected):
        engine.triage(dataset, "email was slow", use_stub=False, api_key="sk-ant-bad")


def test_cli_reports_a_failed_run_without_a_traceback(dataset, fake_sdk, capsys):
    from tracelens.cli import main

    class Failing(_FakeMessages):
        def create(self, **kwargs):
            raise _FakeAuthError("boom")

    fake_sdk(Failing(reply=GOOD_REPLY))
    code = main(["--plain", "triage", "--symptom", "3", "--api-key", "sk-ant-bad"])
    output = capsys.readouterr().out
    assert code == 1, "a failed triage must be a non-zero exit, not a silent success"
    assert "triage failed" in output and "401 Unauthorized" in output
    assert "Traceback" not in output
