"""The AI layer: the citation gate, the tool surface, and the live path.

Prompt instructions are advisory; everything asserted here is enforced by code,
which is the only reason any of it can be relied on during an incident.

The live path runs against a fake SDK — no key, no network. Without it, the one
part of the repo that touches a model would never have executed.

Note what these tests do NOT hardcode: a finding, a channel, a route number. They
ask the analysis for whatever it found. A test pinned to a specific finding ID
kept passing through two rewrites while testing nothing.
"""

from __future__ import annotations

import importlib.machinery
import json
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tracelens.evidence import SliceIndex
from tracelens.triage import engine
from tracelens.triage.tools import ToolBox
from tracelens.triage.validator import validate

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def index():
    populated = SliceIndex()
    populated.add(["corr-real-1", "corr-real-2", "route-1"])
    return populated


def hypothesis(refs, **extra):
    return {"hypotheses": [{"summary": "something", "evidence_refs": refs, **extra}]}


# -- the citation gate ------------------------------------------------------- #


@pytest.mark.parametrize(
    "refs,expected",
    [
        (["corr-9999-never-shown"], "never shown"),
        (["the push journeys in the timeline above"], "prose"),
        ([], "cites no evidence"),
        (["corr-real-1", "corr-invented"], "never shown"),
    ],
)
def test_a_citation_the_model_was_not_given_is_dropped(index, refs, expected):
    """Four ways to fail, one outcome. The prose case matters most: a model citing
    "the timeline above" has cited nothing, and a naive substring check passes it.
    The mixed case matters second: partial credit would let a fabricated
    identifier ride along beside a real one."""
    result = validate(hypothesis(refs), index)
    assert result.hypotheses == []
    assert result.verdict == "insufficient_evidence"
    assert expected in result.rejections[0].reason


def test_a_real_citation_survives(index):
    result = validate(hypothesis(["corr-real-1"], alternative="or not"), index)
    assert [h.evidence_refs for h in result.hypotheses] == [["corr-real-1"]]
    assert result.rejections == []


def test_a_lone_confident_answer_is_flagged(index):
    """The schema requires a competing explanation or an explicit insufficient
    verdict. One answer with nothing weighed against it is a schema violation —
    'it did not happen' and 'it was not recorded' are indistinguishable here."""
    assert any(
        "no stated alternative" in r.reason
        for r in validate(hypothesis(["corr-real-1"]), index).rejections
    )
    assert not validate(hypothesis(["corr-real-1"], alternative="or not"), index).rejections


def test_duplicate_hypotheses_are_merged_not_listed_twice(index):
    """Observed live on the previous design: asked for two hypotheses when only
    one thing matched, the model split one explanation into two entries, which
    reads as two independent explanations when there is one."""
    result = validate(
        {
            "hypotheses": [
                {"summary": "the journeys stop at the topic and go no further",
                 "evidence_refs": ["corr-real-1"], "alternative": "x"},
                {"summary": "journeys stop at the topic, going no further",
                 "evidence_refs": ["corr-real-2"], "alternative": "x"},
            ]
        },
        index,
    )
    assert len(result.hypotheses) == 1
    assert result.hypotheses[0].evidence_refs == ["corr-real-1", "corr-real-2"]


def test_a_limit_the_model_skipped_is_appended_anyway(index):
    """A limit the answer ignored is exactly the one worth seeing — and one it did
    state must not appear twice."""
    result = validate(
        hypothesis(["corr-real-1"], alternative="x"),
        index,
        limits=["this field cannot indicate health"],
    )
    assert result.limits_that_apply == ["this field cannot indicate health"]

    restated = validate(
        {"hypotheses": [{"summary": "s", "evidence_refs": ["corr-real-1"], "alternative": "x"}],
         "limits_that_apply": ["the status field cannot indicate health here"]},
        index,
        limits=["the status field cannot indicate health"],
    )
    assert len(restated.limits_that_apply) == 1


@pytest.mark.parametrize(
    "sentence",
    [
        "dropped from your answer automatically",
        "Do not recall identifiers from memory",
        "Honour the stated limits",
        "Input quality is part of the answer, not a footnote",
        "Call get_slice at least once",
        "CONTRAST journey",
        "A change near an incident is not a cause",
    ],
)
def test_the_prompt_states_what_the_code_enforces(sentence):
    """Compared against normalised whitespace: the prompt is hard-wrapped, and an
    earlier version of this passed only because the sentences it looked for
    happened not to wrap."""
    from tracelens.triage import prompts

    assert sentence in " ".join(prompts.SYSTEM.split())


# -- the tool surface -------------------------------------------------------- #


@pytest.fixture
def toolbox(analysis):
    return ToolBox(analysis, SliceIndex())


def test_only_what_a_tool_returned_becomes_citable(analysis, toolbox):
    """The gate is only as good as what feeds it."""
    minority = min(analysis.routes.routes, key=lambda r: r.count)
    toolbox.run("list_routes", {})
    toolbox.run("get_slice", {"route": minority.index})

    assert toolbox.index.knows("route-1")
    assert all(toolbox.index.knows(v) for v in minority.journeys)
    assert not toolbox.index.knows("corr-does-not-exist")


def test_the_slice_tool_caps_and_says_so(analysis, toolbox):
    payload = toolbox.run("get_slice", {})
    assert payload["matched_journeys"] == len(analysis.grouping.journeys)
    assert payload["rendered_journeys"] < payload["matched_journeys"]


def test_a_bad_call_is_an_error_rather_than_a_silent_everything(toolbox):
    """Falling back to 'no filter' would answer a question about last Tuesday with
    the whole export and look like it worked."""
    assert "error" in toolbox.run("get_slice", {"after": "last tuesday"})
    assert "error" in toolbox.run("run_query", {"sql": "SELECT 1"})
    assert "error" in toolbox.run("get_journey", {"value": "nope"})


def test_the_surface_has_no_query_and_no_write_path():
    from tracelens.triage.tools import TOOL_SCHEMAS

    assert {t["name"] for t in TOOL_SCHEMAS} == {"list_routes", "get_slice", "get_journey"}


# -- routing ----------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question", ["the CSV export job is failing", "our Salesforce sync stopped last night"]
)
def test_an_out_of_scope_question_declines(export, question):
    """The test that catches reaching for the nearest available problem."""
    result = engine.triage(export, question, use_stub=True).result
    assert result.verdict == "insufficient_evidence"
    assert result.would_resolve


def test_a_semantically_adjacent_term_is_a_known_limitation(export):
    """A documented gap, pinned so it cannot be quietly forgotten.

    "Webhooks" are not part of this platform, but they are adjacent to one of its
    channels — both are outbound fire-and-forget calls — and that channel happens
    to be entirely dead in this data.

    The offline stand-in declines because it matches on spelling. The live model
    answers, hedging and flagging the terminology mismatch, which is arguably the
    more useful reply; what is wrong is that the verdict reads confident.
    Asserting the stand-in's behaviour here would restate the exact false
    confidence that hid this for weeks, so the docstring carries the real finding.
    """
    result = engine.triage(export, "our webhooks stopped firing", use_stub=True).result
    assert result.verdict == "insufficient_evidence", "stand-in behaviour only"


def test_the_answer_is_deterministic_and_carries_the_limits(export):
    """An analyzer that answers differently each run is worse than none."""
    runs = [engine.triage(export, export.symptoms[0].text, use_stub=True).result for _ in range(3)]
    assert all([h.summary for h in r.hypotheses] == [h.summary for h in runs[0].hypotheses] for r in runs)
    assert runs[0].limits_that_apply
    assert runs[0].tool_calls >= 2, "the stand-in must exercise the tools the model uses"


# -- the live path, against a fake SDK --------------------------------------- #


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
                [_ToolUse(f"t{i}", n, a) for i, (n, a) in enumerate(self.tool_calls)]
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
    """Install a fake `anthropic` module for one test.

    It must carry the exception classes as well as the client: engine.py catches
    `anthropic.AuthenticationError` by attribute, so a fake without them turns a
    handled failure into an AttributeError.
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


@pytest.fixture
def tools_then_answer(analysis):
    """What a real turn looks like: read the routes, read a slice, then answer
    citing only what those returned."""
    minority = min(analysis.routes.routes, key=lambda r: r.count)
    calls = [("list_routes", {}), ("get_slice", {"route": minority.index})]
    reply = {
        "verdict": "hypotheses",
        "restated_complaint": "something is wrong",
        "hypotheses": [
            {"summary": "these journeys end early", "evidence_refs": [f"route-{minority.index}"],
             "reading": "the route table shows it", "alternative": "or they were not recorded"},
            {"summary": "a different reading of the same window",
             "evidence_refs": [minority.journeys[0]],
             "reading": "the timeline shows it", "alternative": "or the record is missing"},
        ],
        "ruled_out": [{"claim": "the deploy", "why_not": "postdates onset"}],
        "limits_that_apply": [],
        "would_resolve": [],
    }
    return calls, reply


def test_the_full_turn_runs_and_validates(export, fake_sdk, tools_then_answer):
    calls, reply = tools_then_answer
    fake = fake_sdk(_FakeMessages(reply=reply, tool_calls=calls))
    run = engine.triage(export, "something broke", use_stub=False, api_key="sk-ant-test")

    assert run.result.tool_calls == len(calls), "every tool must execute, not just be declared"
    assert run.result.source.startswith("live")
    assert [h.summary for h in run.result.hypotheses] == [h["summary"] for h in reply["hypotheses"]]
    assert run.result.rejections == []
    assert len(fake.requests) == 2, "one tool round-trip, then the answer"


def test_a_fabricated_citation_from_a_live_reply_is_dropped(export, fake_sdk, tools_then_answer):
    calls, reply = tools_then_answer
    reply = json.loads(json.dumps(reply))
    reply["hypotheses"][0]["evidence_refs"] = ["corr-9999"]
    fake_sdk(_FakeMessages(reply=reply, tool_calls=calls))

    run = engine.triage(export, "something broke", use_stub=False, api_key="sk-ant-test")
    assert len(run.result.hypotheses) == 1
    assert run.result.rejections[0].bad_refs == ["corr-9999"]


def test_the_model_cannot_smuggle_a_severity_label_into_the_answer(export, fake_sdk, tools_then_answer):
    """This tool no longer labels anything CRITICAL — it reports counts. A model
    asserting a severity must not put that word in front of a reader."""
    calls, reply = tools_then_answer
    reply = json.loads(json.dumps(reply))
    reply["hypotheses"][0]["severity"] = "critical"
    fake_sdk(_FakeMessages(reply=reply, tool_calls=calls))

    top = engine.triage(export, "x", use_stub=False, api_key="sk-ant-test").result.hypotheses[0]
    assert not hasattr(top, "severity")


def test_the_request_is_shaped_as_intended(export, fake_sdk, tools_then_answer):
    """Effort is set explicitly. The API defaults to 'high'; this workload does not
    need it, and effort also governs how many tool calls get made."""
    _, reply = tools_then_answer
    fake = fake_sdk(_FakeMessages(reply=reply))
    engine.triage(export, "x", use_stub=False, api_key="sk-ant-test")

    request = fake.requests[0]
    assert request["model"] == engine.MODEL
    assert request["output_config"] == {"effort": engine.EFFORT}
    assert [t["name"] for t in request["tools"]] == [t["name"] for t in engine.TOOL_SCHEMAS]


def test_the_opening_prompt_carries_routes_not_records(export, fake_sdk, tools_then_answer):
    _, reply = tools_then_answer
    fake = fake_sdk(_FakeMessages(reply=reply))
    engine.triage(export, "x", use_stub=False, api_key="sk-ant-test")
    prompt = fake.requests[0]["messages"][0]["content"]

    assert '"routes"' in prompt and "limits" in prompt
    assert len(prompt) < 30_000, "the opening payload is bounded by routes, not traffic"
    assert '"kind": "SERVER"' not in prompt, "records reach the model only through get_slice"


def test_an_unbounded_loop_is_capped(export, fake_sdk, tools_then_answer):
    """An unbounded agent is an unbounded bill."""
    _, reply = tools_then_answer

    class Endless(_FakeMessages):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            return _Response([_ToolUse("t", "list_routes", {})])

    fake_sdk(Endless(reply=reply))
    with pytest.raises(RuntimeError, match="tool iterations"):
        engine.triage(export, "x", use_stub=False, api_key="sk-ant-test")


def test_a_reply_that_is_not_json_fails_loudly(export, fake_sdk):
    fake = _FakeMessages(reply={})
    fake.create = lambda **kw: _Response([_Text("I think it was the deploy, honestly.")])
    fake_sdk(fake)
    with pytest.raises(ValueError, match="no JSON object"):
        engine.triage(export, "x", use_stub=False, api_key="sk-ant-test")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_FakeAuthError, "401 Unauthorized"),
        (_FakeConnError, "could not reach the API"),
        (_FakeBadRequest, "rejected the request"),
    ],
)
def test_api_failures_surface_as_one_line_not_a_traceback(export, fake_sdk, exc, expected):
    """Someone who typos a key should get a sentence telling them what to do, not
    forty lines of httpx internals. Verified against a real 401."""

    class Failing(_FakeMessages):
        def create(self, **kwargs):
            raise exc("boom")

    fake_sdk(Failing(reply={}))
    with pytest.raises(engine.TriageError, match=expected):
        engine.triage(export, "x", use_stub=False, api_key="sk-ant-bad")


def test_the_cli_reports_a_failed_run_without_a_traceback(export, fake_sdk, capsys):
    from tracelens.cli import main

    class Failing(_FakeMessages):
        def create(self, **kwargs):
            raise _FakeAuthError("boom")

    fake_sdk(Failing(reply={}))
    code = main(["--plain", "ask", "--symptom", "1", "--api-key", "sk-ant-bad"])
    output = capsys.readouterr().out
    assert code == 1, "a failed run must be a non-zero exit, not a silent success"
    assert "401 Unauthorized" in output and "Traceback" not in output


def test_the_key_is_used_but_never_echoed(export, fake_sdk, tools_then_answer):
    _, reply = tools_then_answer
    fake_sdk(_FakeMessages(reply=reply))
    run = engine.triage(export, "x", use_stub=False, api_key="sk-ant-test")
    assert "key from flag" in run.result.source
    assert "sk-ant-test" not in run.result.source


# -- the one mistake that cannot be undone ----------------------------------- #

# A real key is the prefix followed by a long run of key characters in one
# literal. Matching the bare prefix would fire on tests that build a synthetic
# key by concatenation, and a check that cries wolf gets deleted.
REAL_KEY = re.compile(r"sk-ant-[\w-]{40,}")

# Everything a key could plausibly be pasted into. `data/` is excluded on
# purpose: it is the supplied export, it is megabytes of JSON, and walking it
# made this test take 47 of the suite's 49 seconds.
SCANNED = ("tracelens", "tests", "scripts", "examples")


def test_no_committed_file_contains_a_key():
    paths = [p for p in ROOT.glob("*") if p.is_file()]
    for folder in SCANNED:
        paths += [p for p in (ROOT / folder).rglob("*") if p.is_file()]

    checked = 0
    for path in paths:
        if "__pycache__" in path.parts or path.suffix in {".pdf", ".pyc"}:
            continue
        checked += 1
        assert not REAL_KEY.search(path.read_text(encoding="utf-8", errors="ignore")), path
    assert checked > 20, "the scan matched almost nothing — it is not doing its job"


def test_that_scan_would_catch_a_real_key():
    """The test for the test, written after the first version fired on a
    deliberately fake key and would have been loosened into uselessness."""
    assert REAL_KEY.search("key: sk-ant-api03-" + "A1b2" * 20)
    assert not REAL_KEY.search('KEY = "sk-ant-api03-" + "x" * 60')
