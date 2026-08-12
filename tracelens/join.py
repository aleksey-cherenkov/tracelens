"""Assemble spans into one LogicalTrace per message.

The primary key is correlation_id, not trace_id. The obvious join -- group by
trace_id -- fails on 8 of 41 messages here (every SMS message starts a new root
span at the sender) and cannot represent a truncated path at all. correlation_id
is present on 100% of spans and survives every hop.

Each stage transition records *how* it was resolved, because the join method is
itself the diagnosis for "where does the rest of my SMS trace go?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .config import DEFAULT, Config
from .model import (
    STAGE_ORDER,
    AcceptedMessage,
    Dataset,
    Span,
    Stage,
)


class JoinMethod(str, Enum):
    PARENT_CHILD = "parent_child"
    """Downstream span's parent_span_id resolves to the upstream span."""

    CORRELATION_FALLBACK = "correlation_fallback"
    """Trace context was lost. Joined on correlation_id + stage order + time."""

    ABSENT = "absent"
    """No downstream span exists. The message stopped here."""


@dataclass(frozen=True)
class JoinRecord:
    frm: Stage
    to: Stage
    method: JoinMethod
    gap_ms: float | None = None
    """Wall-clock gap between upstream end and downstream start.

    Only meaningful for a SEQUENTIAL transition (the two async broker hops). For a
    NESTED transition this is the child's offset *within* the parent and is
    reported as an offset, never as latency -- naively subtracting a parent's end
    from a child's start yields negative numbers (-26 ms, -18 ms here).
    """

    nested: bool = False

    @property
    def kind(self) -> str:
        return "nested" if self.nested else "sequential"


@dataclass
class Attempt:
    """One delivery attempt: the chain reachable by walking parent_span_id
    upward from a SEND_PROVIDER span until it leaves the sender.

    Both consume spans of a redelivered message share the *same* publish parent,
    so walking forward from the publish cannot say which consume belongs to which
    attempt. Walking backwards from the send can: send.parent_span_id resolves to
    exactly one consume.
    """

    index: int
    send: Span
    consume: Span | None
    receive_count: int

    @property
    def start_time(self) -> datetime:
        return (self.consume or self.send).start_time

    @property
    def is_first(self) -> bool:
        return self.index == 0

    @property
    def provider_status(self) -> int | None:
        raw = self.send.attributes.get("provider.status_code")
        return int(raw) if raw is not None else None

    @property
    def provider_final_status(self) -> int | None:
        raw = self.send.attributes.get(
            "provider.final_status_code", self.send.attributes.get("provider.status_code")
        )
        return int(raw) if raw is not None else None

    @property
    def succeeded(self) -> bool:
        status = self.provider_final_status
        return status is not None and 200 <= status < 300


@dataclass
class TraceSegment:
    """A contiguous run of stages sharing one trace_id."""

    trace_id: str
    stages: list[Stage]

    @property
    def short_trace(self) -> str:
        return self.trace_id[-8:]


@dataclass
class LogicalTrace:
    correlation_id: str
    accepted: AcceptedMessage | None
    spans: list[Span]
    stage_spans: dict[Stage, Span]
    attempts: list[Attempt] = field(default_factory=list)
    segments: list[TraceSegment] = field(default_factory=list)
    joins: list[JoinRecord] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    # -- identity --------------------------------------------------------------

    @property
    def channel(self) -> str | None:
        if self.accepted:
            return self.accepted.message_type
        for span in self.spans:
            if span.message_type:
                return span.message_type
        return None

    @property
    def tenant_id(self) -> str | None:
        if self.accepted:
            return self.accepted.tenant_id
        for span in self.spans:
            if span.tenant_id:
                return span.tenant_id
        return None

    # -- progress --------------------------------------------------------------

    @property
    def terminal_stage(self) -> Stage:
        """Furthest stage actually reached."""
        reached = [s for s in STAGE_ORDER if s in self.stage_spans]
        return reached[-1] if reached else Stage.ACCEPT

    @property
    def reached_provider(self) -> bool:
        return Stage.SEND_PROVIDER in self.stage_spans

    @property
    def stopped_at(self) -> Stage | None:
        """Where the message died, or None if it reached the provider."""
        return None if self.reached_provider else self.terminal_stage

    @property
    def trace_context_break(self) -> bool:
        return len(self.segments) > 1

    @property
    def is_duplicate(self) -> bool:
        return len(self.attempts) > 1

    # -- timing ----------------------------------------------------------------

    @property
    def first_attempt(self) -> Attempt | None:
        return self.attempts[0] if self.attempts else None

    @property
    def end_to_end_ms(self) -> float | None:
        """ACCEPT.start -> SEND_PROVIDER.end of the *first attempt only*.

        Measuring first-span-start to last-span-end instead would report ~32,000 ms
        for every redelivered message and invent a latency incident on the days
        duplicates happened.
        """
        accept = self.stage_spans.get(Stage.ACCEPT)
        attempt = self.first_attempt
        if not accept or not attempt:
            return None
        return (attempt.send.end_time - accept.start_time).total_seconds() * 1000

    def join_for(self, frm: Stage) -> JoinRecord | None:
        return next((j for j in self.joins if j.frm is frm), None)


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def _expected_path(channel: str | None) -> list[Stage]:
    """Every message follows the same seven stages regardless of channel."""
    return list(STAGE_ORDER)


def _build_attempts(spans: list[Span]) -> list[Attempt]:
    by_id = {s.span_id: s for s in spans}
    sends = sorted(
        (s for s in spans if s.stage is Stage.SEND_PROVIDER),
        key=lambda s: (s.start_time, s.span_id),
    )
    attempts: list[Attempt] = []
    for index, send in enumerate(sends):
        parent = by_id.get(send.parent_span_id) if send.parent_span_id else None
        consume = parent if parent and parent.stage is Stage.CONSUME_QUEUE else None
        # receive_count lives on the send span, not the consume span -- the
        # redelivered consume carries empty attributes.
        attempts.append(
            Attempt(
                index=index,
                send=send,
                consume=consume,
                receive_count=send.receive_count,
            )
        )
    return attempts


def _build_segments(spans: list[Span]) -> list[TraceSegment]:
    segments: list[TraceSegment] = []
    for span in sorted(spans, key=lambda s: (s.start_time, s.span_id)):
        stage = span.stage
        if stage is None:
            continue
        if segments and segments[-1].trace_id == span.trace_id:
            segments[-1].stages.append(stage)
        else:
            segments.append(TraceSegment(trace_id=span.trace_id, stages=[stage]))
    return segments


def _resolve_join(
    upstream: Span,
    downstream: Span | None,
    frm: Stage,
    to: Stage,
    by_id: dict[str, Span],
) -> JoinRecord:
    if downstream is None:
        return JoinRecord(frm=frm, to=to, method=JoinMethod.ABSENT)

    parent = by_id.get(downstream.parent_span_id) if downstream.parent_span_id else None
    linked = parent is not None and parent.span_id == upstream.span_id
    same_trace = downstream.trace_id == upstream.trace_id
    method = (
        JoinMethod.PARENT_CHILD
        if linked and same_trace
        else JoinMethod.CORRELATION_FALLBACK
    )

    # A transition is NESTED when the child starts before its parent ends and
    # both are in the same service: a synchronous call inside a span, not a hop.
    nested = (
        downstream.start_time < upstream.end_time and downstream.service == upstream.service
    )
    gap_ms = (
        (downstream.start_time - upstream.start_time).total_seconds() * 1000
        if nested
        else (downstream.start_time - upstream.end_time).total_seconds() * 1000
    )
    return JoinRecord(
        frm=frm, to=to, method=method, gap_ms=round(gap_ms, 1), nested=nested
    )


def build_trace(
    correlation_id: str,
    spans: list[Span],
    accepted: AcceptedMessage | None,
    config: Config = DEFAULT,
) -> LogicalTrace:
    ordered = sorted(spans, key=lambda s: (s.start_time, s.span_id))
    by_id = {s.span_id: s for s in ordered}

    # First span seen at each stage. Later spans at the same stage are
    # redeliveries and belong to attempts, not to the primary path.
    stage_spans: dict[Stage, Span] = {}
    unmapped = 0
    for span in ordered:
        stage = span.stage
        if stage is None:
            unmapped += 1
            continue
        stage_spans.setdefault(stage, span)

    trace = LogicalTrace(
        correlation_id=correlation_id,
        accepted=accepted,
        spans=ordered,
        stage_spans=stage_spans,
        attempts=_build_attempts(ordered),
        segments=_build_segments(ordered),
    )

    path = _expected_path(trace.channel)
    window = timedelta(seconds=config.correlation_join_window_s)
    for frm, to in zip(path, path[1:]):
        upstream = stage_spans.get(frm)
        if upstream is None:
            break
        downstream = stage_spans.get(to)
        if downstream is not None and downstream.start_time + window < upstream.start_time:
            downstream = None  # too far away to be this message's next stage
        trace.joins.append(_resolve_join(upstream, downstream, frm, to, by_id))
        if downstream is None:
            break

    if unmapped:
        trace.anomalies.append(f"{unmapped} span(s) did not map to a known stage")
    if trace.accepted is None:
        trace.anomalies.append("spans exist but no accepted_messages record")
    if trace.trace_context_break:
        broken = trace.joins[len(trace.segments[0].stages) - 1 : len(trace.segments[0].stages)]
        where = broken[0].to.label if broken else "unknown stage"
        trace.anomalies.append(f"trace context lost before {where}")
    if trace.is_duplicate:
        trace.anomalies.append(f"{len(trace.attempts)} delivery attempts")

    return trace


def build_all(dataset: Dataset, config: Config = DEFAULT) -> dict[str, LogicalTrace]:
    """One LogicalTrace per correlation_id, from both directions.

    Messages with no spans and spans with no promise record both need to appear:
    the first is an outright loss, the second is a ledger gap.
    """
    accepted_index = dataset.accepted_by_correlation()
    spans_index = dataset.spans_by_correlation

    traces: dict[str, LogicalTrace] = {}
    for correlation_id in sorted(set(accepted_index) | set(spans_index)):
        traces[correlation_id] = build_trace(
            correlation_id,
            spans_index.get(correlation_id, []),
            accepted_index.get(correlation_id),
            config,
        )
    return traces
