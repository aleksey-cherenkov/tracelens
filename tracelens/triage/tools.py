"""The model's tool surface: five read-only, bounded drill-downs.

Deliberately analysis-level rather than raw-query. There is no run_query(sql), no
write path, and no tool that returns raw spans or log lines in bulk. That buys
three things: the model cannot author an expensive full scan, every conclusion
traces to a named function that can be re-run by hand, and the surface is small
enough to describe accurately in the prompt.

Flexibility is what's being traded away, knowingly.
"""

from __future__ import annotations

from typing import Any, Callable

from ..detectors import DetectorContext
from ..evidence import EvidenceBundle
from ..model import Stage, fmt_ts

MAX_ITEMS = 20

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_findings",
        "description": (
            "List every finding the deterministic detectors produced, with severity, "
            "confidence, how many messages each affects, and up to 5 exemplar "
            "correlation IDs. Start here."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_finding_evidence",
        "description": (
            "Full evidence list for one finding, including competing alternatives and "
            "what additional data would resolve them. Use before citing a finding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"finding_id": {"type": "string"}},
            "required": ["finding_id"],
        },
    },
    {
        "name": "get_trace",
        "description": (
            "The full path of one message across all three services: stages reached, "
            "per-stage timing, how each hop was joined, delivery attempts, and where it "
            "stopped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"correlation_id": {"type": "string"}},
            "required": ["correlation_id"],
        },
    },
    {
        "name": "query_messages",
        "description": (
            "Filter accepted messages. Returns an exact total plus a capped sample. Use "
            "to check scale, not to enumerate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "email, sms, or push"},
                "tenant_id": {"type": "string"},
                "terminal_stage": {
                    "type": "string",
                    "description": "e.g. publish_topic — where the message stopped",
                },
                "stopped_only": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "get_deploys",
        "description": "Deploy records, optionally filtered by service.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": [],
        },
    },
]


def _truncate(items: list, limit: int) -> tuple[list, int]:
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


class ToolBox:
    """Executes tool calls against precomputed analysis. No I/O, no queries."""

    def __init__(self, bundle: EvidenceBundle, context: DetectorContext) -> None:
        self.bundle = bundle
        self.context = context
        self.calls: list[tuple[str, dict]] = []

    def run(self, name: str, arguments: dict[str, Any]) -> dict:
        self.calls.append((name, dict(arguments)))
        handler: Callable[..., dict] | None = getattr(self, f"_{name}", None)
        if handler is None:
            return {"error": f"unknown tool '{name}'"}
        try:
            return handler(**arguments)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # a bad call costs one iteration, not the run
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- tools -----------------------------------------------------------------

    def _list_findings(self) -> dict:
        return {
            "findings": [
                {
                    "finding_id": f.id,
                    "title": f.title,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "summary": f.summary,
                    "affected_count": f.affected_count,
                    "exemplars": f.exemplars(self.context.config.max_exemplars),
                    "has_competing_alternatives": bool(f.alternatives),
                }
                for f in self.bundle.ranked()
            ]
        }

    def _get_finding_evidence(self, finding_id: str) -> dict:
        finding = self.bundle.by_id(finding_id)
        if finding is None:
            return {
                "error": f"no finding '{finding_id}'",
                "available": [f.id for f in self.bundle.findings],
            }
        evidence, hidden = _truncate(finding.evidence, MAX_ITEMS)
        payload = {
            "finding_id": finding.id,
            "severity": finding.severity,
            "confidence": finding.confidence,
            "evidence": [e.as_dict() for e in evidence],
            "affected_count": finding.affected_count,
            "exemplars": finding.exemplars(self.context.config.max_exemplars),
        }
        if finding.alternatives:
            payload["alternatives"] = [h.as_dict() for h in finding.alternatives]
            payload["note"] = (
                "This finding carries competing explanations the data cannot separate. "
                "Return all of them; do not pick one."
            )
        if finding.would_resolve:
            payload["would_resolve"] = list(finding.would_resolve)
        if finding.params:
            payload["params"] = dict(finding.params)
        if hidden:
            payload["truncated"] = True
            payload["note_truncated"] = f"{hidden} more not shown"
        return payload

    def _get_trace(self, correlation_id: str) -> dict:
        trace = self.context.traces.get(correlation_id)
        if trace is None:
            return {"error": f"no message '{correlation_id}'"}
        stages = []
        for stage, span in sorted(trace.stage_spans.items(), key=lambda kv: kv[0].index):
            record = trace.join_for(stage)
            stages.append(
                {
                    "stage": stage.value,
                    "service": span.service,
                    "span_name": span.name,
                    "start": fmt_ts(span.start_time),
                    "duration_ms": span.duration_ms,
                    "status": span.status,
                    "trace_id": span.trace_id,
                    "join_to_next": (
                        {
                            "method": record.method.value,
                            "kind": record.kind,
                            "gap_ms": record.gap_ms,
                        }
                        if record
                        else None
                    ),
                }
            )
        return {
            "correlation_id": correlation_id,
            "channel": trace.channel,
            "tenant_id": trace.tenant_id,
            "accepted_at": fmt_ts(trace.accepted.accepted_at) if trace.accepted else None,
            "terminal_stage": trace.terminal_stage.value,
            "reached_provider": trace.reached_provider,
            "end_to_end_ms": trace.end_to_end_ms,
            "trace_context_break": trace.trace_context_break,
            "distinct_trace_ids": len({s.trace_id for s in trace.spans}),
            "attempts": [
                {
                    "index": a.index,
                    "start": fmt_ts(a.start_time),
                    "duration_ms": a.send.duration_ms,
                    "sqs_receive_count": a.receive_count,
                    "provider_status": a.provider_status,
                    "provider_final_status": a.provider_final_status,
                    "retry_count": a.send.retry_count,
                }
                for a in trace.attempts
            ],
            "stages": stages,
            "anomalies": trace.anomalies,
        }

    def _query_messages(
        self,
        channel: str | None = None,
        tenant_id: str | None = None,
        terminal_stage: str | None = None,
        stopped_only: bool = False,
        limit: int = MAX_ITEMS,
    ) -> dict:
        matches = []
        for outcome in self.context.accounting.outcomes:
            if channel and outcome.channel != channel:
                continue
            if tenant_id and outcome.tenant_id != tenant_id:
                continue
            if terminal_stage and outcome.terminal_stage.value != terminal_stage:
                continue
            if stopped_only and outcome.stopped_at is None:
                continue
            matches.append(outcome)

        capped = max(1, min(int(limit), MAX_ITEMS))
        shown, hidden = _truncate(matches, capped)
        payload = {
            "total": len(matches),
            "returned": len(shown),
            "messages": [
                {
                    "correlation_id": o.correlation_id,
                    "channel": o.channel,
                    "tenant_id": o.tenant_id,
                    "outcome": o.outcome.value,
                    "terminal_stage": o.terminal_stage.value,
                    "provider_calls": o.provider_calls,
                }
                for o in shown
            ],
        }
        if hidden:
            payload["truncated"] = True
            payload["note_truncated"] = f"{hidden} more not shown"
        return payload

    def _get_deploys(self, service: str | None = None) -> dict:
        deploys = [d for d in self.bundle.deploys if not service or d["service"] == service]
        return {"total": len(deploys), "deploys": deploys}


def stage_names() -> list[str]:
    return [s.value for s in Stage]
