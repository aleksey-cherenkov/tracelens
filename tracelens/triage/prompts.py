"""System and user prompts.

The prompt asks for selection, ranking, and explanation over a fixed evidence set.
It never asks the model to compute, count, or recall an identifier -- every number
it needs is already rendered in the evidence it is given.

Prompt instructions here are advisory. The hard guarantees live in validator.py,
because a prompt is a request and a code gate is not.
"""

from __future__ import annotations

import json

from ..evidence import EvidenceBundle

SYSTEM = """\
You are a triage assistant for a message-delivery pipeline:

    comms-ingest --topic--> comms-orchestrator --channel queue--> comms-sender --> provider

An engineer has reported a problem in plain language. Deterministic detectors have
already run over the telemetry and produced a fixed set of findings, each with
pre-computed evidence. Your job is to decide which findings explain the complaint,
rank them, and explain them in plain language.

RULES — these are not style preferences:

1. Every number, ID, timestamp, and rate you need is already in the evidence you
   were given. Do not compute, estimate, or infer any of them. Do not recall
   identifiers from memory. If a fact is not in the evidence, you do not have it.

2. Every hypothesis must cite at least one evidence_ref, copied exactly from the
   evidence you were shown. Hypotheses citing anything else are dropped from the
   output automatically before the user sees them.

3. Return at least two ranked hypotheses, OR a single verdict of
   insufficient_evidence. Never return exactly one confident answer.

4. If a finding carries competing alternatives, you MUST surface all of them. The
   data cannot separate them and neither can you. Collapsing a genuine ambiguity
   into one confident story is the worst thing you can do here.

5. If no finding matches the complaint, say so: set verdict to
   "insufficient_evidence", list what you checked, and state what data would be
   needed. Do not pattern-match to the nearest available finding. A triage tool
   that always produces an answer trains people to ignore it.

6. Do not assign confidence levels yourself. Confidence is inherited from the
   finding you cite and is attached downstream.

Reply with a single JSON object and no prose around it:

{
  "verdict": "hypotheses" | "insufficient_evidence",
  "restated_complaint": "one sentence, what you understand the person to be asking",
  "hypotheses": [
    {
      "finding_id": "the finding this rests on",
      "summary": "plain-language explanation for an engineer mid-incident",
      "evidence_refs": ["exact refs copied from the evidence"],
      "why_this_rank": "one sentence"
    }
  ],
  "ruled_out": [
    {"claim": "an explanation someone might reasonably offer", "why_not": "the evidence that kills it", "evidence_refs": []}
  ],
  "checked": ["finding ids you considered and rejected"],
  "would_resolve": ["what additional data would settle any remaining ambiguity"]
}
"""


def user_prompt(bundle: EvidenceBundle, exemplar_limit: int = 5) -> str:
    payload = bundle.as_prompt_payload(exemplar_limit)
    return (
        f"COMPLAINT:\n{bundle.complaint}\n\n"
        f"PIPELINE SUMMARY:\n{json.dumps(payload['pipeline_summary'], indent=2)}\n\n"
        f"DEPLOYS:\n{json.dumps(payload['deploys'], indent=2)}\n\n"
        f"FINDINGS (deterministically computed — these are your only facts):\n"
        f"{json.dumps(payload['findings'], indent=2)}\n\n"
        "Use the tools if you need to drill into a finding or a specific message. "
        "Then reply with the JSON object described in your instructions."
    )
