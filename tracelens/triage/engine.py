"""Orchestration: build the bundle, call the model, validate, rank.

Phase 2 (assembly) is pure code rather than an agent loop. That turns what would
be six serial tool round-trips into one parallel fetch, and it fixes the evidence
set so the answer is auditable. Phases 3 and 4 are the model call and the
validator gate.

The loop is hard-capped. An unbounded agent is an unbounded bill.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..config import DEFAULT, Config
from ..evidence import EvidenceBundle
from ..model import Dataset
from . import prompts, stub
from .context import build_bundle
from .tools import TOOL_SCHEMAS, ToolBox
from .validator import TriageResult, validate

MODEL = "claude-sonnet-5"
MAX_TOOL_ITERATIONS = 8
MAX_TOKENS = 4096


@dataclass
class TriageRun:
    result: TriageResult
    bundle: EvidenceBundle
    raw: dict


def _sdk_available() -> bool:
    from importlib.util import find_spec

    return find_spec("anthropic") is not None


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model response: {text[:200]}")
    return json.loads(text[start : end + 1])


def triage(
    dataset: Dataset,
    complaint: str,
    config: Config = DEFAULT,
    use_stub: bool | None = None,
    api_key: str | None = None,
) -> TriageRun:
    bundle, context = build_bundle(dataset, complaint, config)
    toolbox = ToolBox(bundle, context)

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if use_stub is None:
        # A key with no SDK installed is a setup gap, not a reason to fail: fall
        # back and say so, so a reviewer without the extra still sees output.
        use_stub = not key or not _sdk_available()

    if use_stub:
        raw, source = stub.respond(complaint, bundle)
        result = validate(raw, bundle)
        result.source = source
        if key and not _sdk_available():
            result.source = "stub (ANTHROPIC_API_KEY set but anthropic not installed)"
        return TriageRun(result=result, bundle=bundle, raw=raw)

    raw = _call_model(bundle, toolbox, key)
    result = validate(raw, bundle)
    result.tool_calls = len(toolbox.calls)
    result.source = "live"
    return TriageRun(result=result, bundle=bundle, raw=raw)


def _call_model(bundle: EvidenceBundle, toolbox: ToolBox, api_key: str) -> dict:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "anthropic package not installed. Install with: pip install -e '.[ai]' "
            "or run with --stub"
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)
    messages: list[dict] = [{"role": "user", "content": prompts.user_prompt(bundle)}]

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=0,  # an analyzer that answers differently each run is worse than none
            system=prompts.SYSTEM,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            return _extract_json(text)

        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(toolbox.run(block.name, block.input)),
                    }
                    for block in tool_uses
                ],
            }
        )

    raise RuntimeError(
        f"model did not return a final answer within {MAX_TOOL_ITERATIONS} tool iterations"
    )
