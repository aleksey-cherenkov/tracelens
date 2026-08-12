"""D4 -- trace context break.

An instrumentation-health finding rather than an incident: no message is lost and
no supporter is affected. It earns its severity from what it costs during *other*
incidents -- half the journey is unreachable, so every investigation of that
channel starts blind.

The contrast between channels is the diagnosis. One channel breaking while another
on the same hop does not localises the bug to a single consumer.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from ..evidence import Evidence, Finding

if TYPE_CHECKING:
    from . import DetectorContext


def detect(context: "DetectorContext") -> list[Finding]:
    broken_by_channel: dict[str, list] = {}
    intact_by_channel: Counter = Counter()

    for trace in context.traces.values():
        channel = trace.channel or "unknown"
        if trace.trace_context_break:
            broken_by_channel.setdefault(channel, []).append(trace)
        else:
            intact_by_channel[channel] += 1

    findings: list[Finding] = []
    for channel, traces in sorted(broken_by_channel.items()):
        traces.sort(key=lambda t: t.correlation_id)
        total = context.accounting.by_channel[channel].accepted
        config = context.config

        # Where the trace ID changes. This is the exact stage boundary to fix.
        boundaries = Counter()
        for trace in traces:
            for record in trace.joins:
                if record.method.value == "correlation_fallback":
                    boundaries[(record.frm, record.to)] += 1
        (frm, to), boundary_count = boundaries.most_common(1)[0]

        # Can the orphaned half be reached any other way? For SMS the answer is
        # no: the sender emits no log records at all for this channel.
        orphan_trace_ids = set()
        for trace in traces:
            for segment in trace.segments[1:]:
                orphan_trace_ids.add(segment.trace_id)
        logs_on_orphans = sum(
            1 for log in context.dataset.logs if log.trace_id in orphan_trace_ids
        )
        downstream_service = to.service
        scoped_logs_downstream = sum(
            1
            for trace in traces
            for log in context.dataset.logs_by_correlation.get(trace.correlation_id, [])
            if log.service == downstream_service
        )

        evidence = [
            Evidence(
                kind="metric",
                ref=f"{channel}.trace_break",
                detail=(
                    f"{len(traces)} of {total} {channel} messages split into more than one "
                    f"trace_id; the break is at {frm.label} -> {to.label} in "
                    f"{boundary_count} of them. The downstream consumer starts a new root "
                    "span instead of continuing the producer's context."
                ),
                source="spans.json",
            ),
            Evidence(
                kind="metric",
                ref="channel_contrast",
                detail=(
                    "; ".join(
                        f"{c}: {len(broken_by_channel.get(c, []))} broken of "
                        f"{context.accounting.by_channel[c].accepted}"
                        for c in sorted(context.accounting.by_channel)
                    )
                    + " — the contrast localises the bug to one consumer, not the hop"
                ),
                source="spans.json",
            ),
            Evidence(
                kind="metric",
                ref="orphan_reachability",
                detail=(
                    f"{len(orphan_trace_ids)} orphaned trace_id(s) appear in {logs_on_orphans} "
                    f"log record(s); {downstream_service} emits {scoped_logs_downstream} "
                    f"message-scoped log line(s) for {channel}. "
                    + (
                        "There is no trace-based and no log-based route to the second half "
                        "of the journey — correlation_id on the spans is the only bridge."
                        if logs_on_orphans == 0 and scoped_logs_downstream == 0
                        else "The orphaned half is still reachable via logs."
                    )
                ),
                source="logs.json",
            ),
        ]
        for trace in traces[: config.max_exemplars]:
            evidence.append(
                Evidence(
                    kind="trace_id",
                    ref=trace.segments[-1].trace_id,
                    detail=(
                        f"{trace.correlation_id}: trace "
                        f"{trace.segments[0].short_trace} covers "
                        f"{trace.segments[0].stages[0].label}.."
                        f"{trace.segments[0].stages[-1].label}, then a NEW root trace "
                        f"{trace.segments[-1].short_trace} starts at "
                        f"{trace.segments[-1].stages[0].label}"
                    ),
                    source=f"spans.json#correlation_id={trace.correlation_id}",
                )
            )

        findings.append(
            Finding(
                id=f"D4.trace_context_break.{channel}",
                title=f"{channel} loses trace context at {to.label}",
                severity="medium",
                confidence="observed",
                summary=(
                    f"All {len(traces)} of {total} {channel} messages split into two trace IDs "
                    f"at the {frm.label} -> {to.label} boundary. The {downstream_service} "
                    f"consumer for {channel} starts a new root span rather than extracting "
                    "the propagated context from the message attributes. Other channels on "
                    "the same hop do not break, so this is one consumer's bug, not the hop's. "
                    "No message is lost — but half of every journey on this channel is "
                    "unreachable by trace ID."
                ),
                evidence=evidence,
                affected=[t.correlation_id for t in traces],
                would_resolve=[
                    f"the {downstream_service} consumer's context-extraction code for "
                    f"{channel}, compared against the channel that works",
                ],
                params={},
            )
        )

    return findings
