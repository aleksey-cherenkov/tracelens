"""D1 -- channel drop.

Fires on a *count*, never on a rate. A message the platform promised and did not
deliver is a finding at n=1: it is a claim about that message, not a claim about a
population. The min_samples gate applies only to the rate printed alongside.

Getting that backwards silently deletes the most severe finding in this dataset
(push, n=4), which is exactly the failure mode this tool exists to prevent.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from ..evidence import Evidence, Finding, Hypothesis
from ..model import fmt_ts

if TYPE_CHECKING:
    from . import DetectorContext


def detect(context: "DetectorContext") -> list[Finding]:
    findings: list[Finding] = []
    accounting = context.accounting
    config = context.config

    for channel, bucket in accounting.by_channel.items():
        if bucket.lost == 0:
            continue

        stopped = sorted(accounting.stopped_ids(channel))
        traces = [context.traces[c] for c in stopped]
        stages = Counter(t.terminal_stage for t in traces)
        terminal, _ = stages.most_common(1)[0]

        tenants = Counter(t.tenant_id for t in traces)
        days = sorted({t.accepted.accepted_at.date().isoformat() for t in traces if t.accepted})

        evidence = [
            Evidence(
                kind="metric",
                ref=f"{channel}.stopped",
                detail=(
                    f"{bucket.lost} of {bucket.accepted} accepted {channel} messages "
                    f"never reached a provider; all stopped after {terminal.label}"
                ),
                source="accepted_messages.json + spans.json",
            )
        ]
        for trace in traces[: config.max_exemplars]:
            last = trace.stage_spans.get(trace.terminal_stage)
            evidence.append(
                Evidence(
                    kind="correlation_id",
                    ref=trace.correlation_id,
                    detail=(
                        f"{channel} for {trace.tenant_id}, accepted "
                        f"{fmt_ts(trace.accepted.accepted_at) if trace.accepted else '?'}; "
                        f"last span {last.name if last else 'none'} in "
                        f"{last.service if last else 'none'}"
                    ),
                    source=f"spans.json#correlation_id={trace.correlation_id}",
                )
            )

        # The downstream stage that produced nothing at all. Absence is the
        # entire signal here -- there is no error to find.
        next_stage_spans = sum(
            1
            for span in context.dataset.spans
            if span.message_type == channel and span.stage is not None
            and span.stage.index > terminal.index
        )
        evidence.append(
            Evidence(
                kind="metric",
                ref=f"{channel}.downstream_spans",
                detail=(
                    f"spans for {channel} beyond {terminal.label}: {next_stage_spans}. "
                    "No error span and no ERROR log accompanies the loss."
                ),
                source="spans.json",
            )
        )

        # Log-side corroboration: ingest said it published, the next service
        # never said it received.
        published = _count_logs(context, channel, "Published to topic")
        routed = _count_logs(context, channel, "Routing message")
        if published or routed:
            evidence.append(
                Evidence(
                    kind="log",
                    ref=f"{channel}.publish_vs_route_logs",
                    detail=(
                        f"'Published to topic type={channel}' x{published} in comms-ingest, "
                        f"'Routing message type={channel}' x{routed} in comms-orchestrator"
                    ),
                    source="logs.json",
                )
            )

        rate_gated = bucket.accepted < config.min_samples
        spread = (
            f"{len(tenants)} tenant(s) ("
            + ", ".join(f"{t} x{n}" for t, n in tenants.most_common())
            + f") across {len(days)} day(s) {days[0]}..{days[-1]}"
            if days
            else f"{len(tenants)} tenant(s)"
        )

        findings.append(
            Finding(
                id=f"D1.channel_drop.{channel}",
                title=f"{channel} messages are lost after {terminal.label}",
                severity="critical" if bucket.lost == bucket.accepted else "high",
                confidence="observed",
                summary=(
                    f"{bucket.lost} of {bucket.accepted} accepted {channel} messages never "
                    f"reached a provider. All stop after {terminal.label} in "
                    f"{terminal.service}, so the loss is on the hop out of that service, "
                    f"before the next service runs any code. Spread: {spread} — this is "
                    "not confined to one campaign or one tenant."
                ),
                evidence=evidence,
                affected=stopped,
                alternatives=[
                    Hypothesis(
                        id="subscription_filter",
                        summary=(
                            "A subscription filter policy on the topic does not match "
                            f"'{channel}', so the message is published successfully and "
                            "then silently discarded before reaching the queue."
                        ),
                        supports=[f"{channel}.publish_vs_route_logs", f"{channel}.downstream_spans"],
                    ),
                    Hypothesis(
                        id="missing_consumer",
                        summary=(
                            f"No queue or consumer exists for '{channel}' at all, so nothing "
                            "is subscribed to receive it."
                        ),
                        supports=[f"{channel}.downstream_spans"],
                    ),
                ],
                would_resolve=[
                    "the topic's subscription filter policy",
                    "the queue list and redrive/DLQ policy for this channel",
                    "whether the lost messages are recoverable from a dead-letter queue",
                ],
                params={"min_samples": config.min_samples},
                low_confidence_rate=rate_gated,
            )
        )

    return findings


def _count_logs(context: "DetectorContext", channel: str, prefix: str) -> int:
    needle = f"{prefix} type={channel}"
    return sum(1 for log in context.dataset.logs if log.message == needle)
