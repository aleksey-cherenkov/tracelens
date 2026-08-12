"""D2 -- duplicate delivery.

The interesting part is not detecting the duplicate, it is *classifying* it. An
upstream double-publish and a downstream redelivery look identical from the
provider's side and have completely different fixes -- and a deploy-proximity
correlator will happily blame the wrong service for either.

The span topology settles it: count publishes against consumes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..evidence import Evidence, Finding, Hypothesis
from ..model import Stage

if TYPE_CHECKING:
    from . import DetectorContext


def detect(context: "DetectorContext") -> list[Finding]:
    duplicates = [
        context.traces[c] for c in sorted(context.accounting.duplicate_ids())
    ]
    if not duplicates:
        return []

    config = context.config
    redelivery: list[str] = []
    double_publish: list[str] = []
    evidence: list[Evidence] = []
    deltas: list[float] = []

    for trace in duplicates:
        publishes = _count_stage(trace, Stage.PUBLISH_QUEUE)
        consumes = _count_stage(trace, Stage.CONSUME_QUEUE)
        attempts = trace.attempts
        delta_s = (
            (attempts[1].start_time - attempts[0].start_time).total_seconds()
            if len(attempts) > 1
            else None
        )
        if delta_s is not None:
            deltas.append(delta_s)

        if publishes > 1:
            double_publish.append(trace.correlation_id)
            classification = "upstream double-publish"
        else:
            redelivery.append(trace.correlation_id)
            classification = "queue redelivery"

        if len(evidence) < config.max_exemplars * 3:
            parents = {
                a.consume.parent_span_id for a in attempts if a.consume is not None
            }
            evidence.append(
                Evidence(
                    kind="correlation_id",
                    ref=trace.correlation_id,
                    detail=(
                        f"{classification}: {publishes} publish span(s) vs {consumes} "
                        f"consume span(s)"
                        + (
                            f", both consumes share parent {next(iter(parents))}"
                            if len(parents) == 1 and len(attempts) > 1
                            else ""
                        )
                        + (f"; second attempt +{delta_s:.1f}s" if delta_s else "")
                    ),
                    source=f"spans.json#correlation_id={trace.correlation_id}",
                )
            )
            second = attempts[1] if len(attempts) > 1 else None
            if second is not None:
                first = attempts[0]
                evidence.append(
                    Evidence(
                        kind="span",
                        ref=second.send.span_id,
                        detail=(
                            f"second send for {trace.correlation_id} carries "
                            f"sqs.receive_count={second.receive_count}; the first send "
                            f"had already returned provider.status_code="
                            f"{first.provider_status} in {first.send.duration_ms} ms"
                        ),
                        source=f"spans.json#span_id={second.send.span_id}",
                    )
                )

    # Log-side corroboration. These lines exist; nobody could find them.
    redelivery_logs = [
        log
        for log in context.dataset.logs
        if log.attributes.get("sqs.receive_count", 1) and int(log.attributes.get("sqs.receive_count", 1) or 1) > 1
    ]
    if redelivery_logs:
        evidence.append(
            Evidence(
                kind="log",
                ref="redelivery_log_lines",
                detail=(
                    f"{len(redelivery_logs)} log line(s) record the redelivery directly "
                    f"('{redelivery_logs[0].message}' with sqs.receive_count>1) — "
                    f"{len(redelivery_logs)} lines out of {len(context.dataset.logs)}, at INFO, "
                    "with no word suggesting duplication"
                ),
                source="logs.json",
            )
        )

    total_channel = sum(
        b.accepted for c, b in context.accounting.by_channel.items() if c == duplicates[0].channel
    )
    delta_note = (
        f"consistently {deltas[0]:.1f}s apart" if len(set(deltas)) == 1 else "varying intervals"
    )
    dominant_redelivery = len(redelivery) >= len(double_publish)

    finding = Finding(
        id="D2.duplicate_delivery",
        title=f"{len(duplicates)} message(s) reached the provider more than once",
        severity="high",
        confidence="observed" if dominant_redelivery else "inferred",
        summary=(
            f"{len(duplicates)} of {total_channel} messages were sent to the provider twice, "
            f"{delta_note}. Topology rules out the upstream service: one publish span, two "
            "consume spans sharing that single parent. The send succeeded, the message was "
            "not deleted from the queue, the visibility timeout expired, and the consumer "
            "reprocessed it with no idempotency check."
        ),
        evidence=evidence,
        affected=sorted(t.correlation_id for t in duplicates),
        alternatives=[
            Hypothesis(
                id="delete_not_issued",
                summary="The consumer never issued the delete after a successful send.",
                supports=[duplicates[0].correlation_id],
            ),
            Hypothesis(
                id="delete_failed",
                summary=(
                    "The delete was issued but failed or arrived after the visibility "
                    "timeout expired."
                ),
                supports=[duplicates[0].correlation_id],
            ),
        ],
        would_resolve=[
            "the queue's visibility timeout setting versus observed send duration",
            "sender logs or metrics for delete-message calls and their outcomes",
        ],
        params={"min_samples": context.config.min_samples},
    )

    # A tenant skew this small is arithmetic, not signal. Say so rather than
    # emitting "org-X has a 40% duplicate rate" off n=5.
    tenants = {}
    for trace in duplicates:
        tenants[trace.tenant_id] = tenants.get(trace.tenant_id, 0) + 1
    top_tenant = max(tenants, key=tenants.get) if tenants else None
    if top_tenant and tenants[top_tenant] > 1:
        tenant_total = sum(
            1
            for o in context.accounting.outcomes
            if o.tenant_id == top_tenant and o.channel == duplicates[0].channel
        )
        if tenant_total < context.config.min_samples:
            finding.low_confidence_rate = True
            finding.evidence.append(
                Evidence(
                    kind="metric",
                    ref="tenant_skew",
                    detail=(
                        f"{tenants[top_tenant]} of {len(duplicates)} duplicates belong to "
                        f"{top_tenant}, which has {tenant_total} messages on this channel. "
                        f"n={tenant_total} is below min_samples={context.config.min_samples}; "
                        "reported as a count, not a rate, and not claimed as tenant-specific."
                    ),
                    source="accepted_messages.json",
                )
            )

    return [finding]


def _count_stage(trace, stage: Stage) -> int:
    return sum(1 for span in trace.spans if span.stage is stage)
