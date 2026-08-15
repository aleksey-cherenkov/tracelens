"""Orchestration: assemble the overview, call the model, validate.

The model drives which slice it reads, through three bounded tools. That is a
deliberate reversal of the previous design, where code chose the evidence up
front and the model selected from it. Choosing what to look at *is* the
troubleshooting, and it is the part a model does better than a rule.

What code keeps is everything measurable: the route table, the counts, the
distributions, and the stated limits. The model never has a reason to derive a
number, and the validator drops any identifier it was not shown.

The loop is hard-capped. An unbounded agent is an unbounded bill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..config import DEFAULT, Config
from ..keys import resolve as resolve_key
from ..keys import sdk_available
from ..analysis import Analysis, of_export
from ..evidence import SliceIndex
from ..loader import Export
from . import prompts, stub
from .tools import TOOL_SCHEMAS, ToolBox
from .validator import TriageResult, validate

MODEL = "claude-sonnet-5"
"""Sonnet 5 rather than Opus 5 or Haiku 4.5. See DESIGN section 6.4 -- the short
version is that the reasoning has already been done in code, so what is left is
semantic matching and explanation over a fixed evidence set, and that is squarely
Sonnet's job."""

EFFORT = "medium"
"""The API defaults to 'high'. Stepped down because the model is selecting and
explaining, not deriving: every number, ID, and rule-out is precomputed. Lower
effort also means fewer tool calls, which is the behaviour we want on an evidence
set that is already fully assembled. The golden set is how you'd justify moving
this either way."""

MAX_TOOL_ITERATIONS = 8
MAX_TOKENS = 4096


class TriageError(RuntimeError):
    """A triage failure worth showing the user as one line, not a traceback."""


class TriageAuthError(TriageError):
    pass


class TriageConnectionError(TriageError):
    pass


class TriageRequestError(TriageError):
    pass


@dataclass
class TriageRun:
    result: TriageResult
    analysis: Analysis
    index: SliceIndex
    raw: dict


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
    export: Export,
    complaint: str,
    config: Config = DEFAULT,
    use_stub: bool | None = None,
    api_key: str | None = None,
    effort: str = EFFORT,
    include_platform: bool = True,
    key: str | None = None,
) -> TriageRun:
    analysis = of_export(export, key)
    index = SliceIndex()
    index.add_defects(analysis.quality.defects)
    toolbox = ToolBox(analysis, index)

    status = resolve_key(api_key)
    if use_stub is None:
        # A key with no SDK installed is a setup gap, not a reason to fail: fall
        # back and say so, so a reviewer without the extra still sees output.
        use_stub = not status.present or not sdk_available()

    if use_stub:
        raw, source = stub.respond(complaint, analysis, toolbox)
        result = validate(raw, index, analysis.limits)
        result.tool_calls = len(toolbox.calls)
        result.source = source
        if status.present and not sdk_available():
            result.source = (
                f"stub — key found ({status.source}) but the anthropic SDK is not "
                'installed. Run: pip install -e ".[ai]"' 
            )
        return TriageRun(result=result, analysis=analysis, index=index, raw=raw)

    raw = _call_model(complaint, analysis, toolbox, status.key, effort, include_platform)
    result = validate(raw, index, analysis.limits)
    result.tool_calls = len(toolbox.calls)
    context_note = "" if include_platform else ", no platform context"
    result.source = (
        f"live ({MODEL}, effort={effort}{context_note}, key from {status.source})"
    )
    return TriageRun(result=result, analysis=analysis, index=index, raw=raw)


def _call_model(
    complaint: str,
    analysis: Analysis,
    toolbox: ToolBox,
    api_key: str,
    effort: str = EFFORT,
    include_platform: bool = True,
) -> dict:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            'anthropic package not installed. Install with: pip install -e ".[ai]" '
            "or run with --stub"
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)
    messages: list[dict] = [
        {
            "role": "user",
            "content": prompts.user_prompt(
                complaint, analysis.overview(), include_platform=include_platform
            ),
        }
    ]

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                output_config={"effort": effort},
                system=prompts.SYSTEM,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.AuthenticationError as exc:
            raise TriageAuthError(
                "401 Unauthorized. Either the key is not current — check "
                "https://platform.claude.com/settings/keys and re-run `tracelens keys` "
                "to see which source it resolved from — or something between you and "
                "the API is stripping the credential. A genuine API 401 returns a JSON "
                "error body with a Request-Id header; a plain-text 'Unauthorized' with "
                "no Request-Id is a proxy, not Anthropic, and the key may be fine. "
                "`--stub` runs everything except the model call."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise TriageConnectionError(
                f"could not reach the API ({exc}). Behind a proxy or TLS-inspecting "
                "network, point the SDK at your system trust store with "
                "SSL_CERT_FILE=/path/to/ca-bundle.crt."
            ) from exc
        except anthropic.BadRequestError as exc:
            raise TriageRequestError(
                f"the API rejected the request (400): {exc}"
            ) from exc

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

    raise TriageError(
        f"model did not return a final answer within {MAX_TOOL_ITERATIONS} tool "
        "iterations. The cap exists so an unbounded agent cannot become an "
        "unbounded bill; raise MAX_TOOL_ITERATIONS if this is legitimate."
    )
