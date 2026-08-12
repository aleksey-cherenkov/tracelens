"""Delivery accounting against the promise ledger.

accepted_messages.json is the authoritative list of messages the platform
returned 202 for. This module answers the only question that really matters:
of the messages we promised, how many reached a provider, and where did the rest
stop?

Deliberately a left join *from* the ledger, not from the spans. A message that
produced no telemetry at all must still appear as a loss -- which is exactly the
failure mode that sampling would hide in production.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum

from .join import LogicalTrace
from .model import Stage


class Outcome(str, Enum):
    DELIVERED_ONCE = "delivered_once"
    DELIVERED_DUPLICATE = "delivered_duplicate"
    STOPPED = "stopped"

    @property
    def label(self) -> str:
        return {
            Outcome.DELIVERED_ONCE: "reached provider exactly once",
            Outcome.DELIVERED_DUPLICATE: "reached provider more than once",
            Outcome.STOPPED: "never reached provider",
        }[self]


@dataclass(frozen=True)
class MessageOutcome:
    correlation_id: str
    channel: str | None
    tenant_id: str | None
    outcome: Outcome
    terminal_stage: Stage
    attempts: int
    provider_calls: int
    trace_intact: bool

    @property
    def stopped_at(self) -> Stage | None:
        return self.terminal_stage if self.outcome is Outcome.STOPPED else None


@dataclass
class ChannelAccounting:
    channel: str
    accepted: int = 0
    delivered: int = 0
    lost: int = 0
    duplicated: int = 0
    trace_intact: int = 0

    @property
    def loss_rate(self) -> float:
        return self.lost / self.accepted if self.accepted else 0.0


@dataclass
class Accounting:
    outcomes: list[MessageOutcome] = field(default_factory=list)
    by_channel: dict[str, ChannelAccounting] = field(default_factory=dict)
    stopped_by_stage: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def delivered_once(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome is Outcome.DELIVERED_ONCE)

    @property
    def delivered_duplicate(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome is Outcome.DELIVERED_DUPLICATE)

    @property
    def stopped(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome is Outcome.STOPPED)

    @property
    def provider_calls(self) -> int:
        return sum(o.provider_calls for o in self.outcomes)

    def share(self, count: int) -> float:
        return count / self.total if self.total else 0.0

    def stopped_ids(self, channel: str | None = None) -> list[str]:
        return [
            o.correlation_id
            for o in self.outcomes
            if o.outcome is Outcome.STOPPED and (channel is None or o.channel == channel)
        ]

    def duplicate_ids(self) -> list[str]:
        return [
            o.correlation_id
            for o in self.outcomes
            if o.outcome is Outcome.DELIVERED_DUPLICATE
        ]

    def broken_trace_ids(self, channel: str | None = None) -> list[str]:
        return [
            o.correlation_id
            for o in self.outcomes
            if not o.trace_intact and (channel is None or o.channel == channel)
        ]


def account(traces: dict[str, LogicalTrace]) -> Accounting:
    result = Accounting()
    per_channel: dict[str, ChannelAccounting] = defaultdict(
        lambda: ChannelAccounting(channel="unknown")
    )

    for correlation_id in sorted(traces):
        trace = traces[correlation_id]
        provider_calls = len(trace.attempts)
        if provider_calls == 0:
            outcome = Outcome.STOPPED
        elif provider_calls == 1:
            outcome = Outcome.DELIVERED_ONCE
        else:
            outcome = Outcome.DELIVERED_DUPLICATE

        channel = trace.channel or "unknown"
        record = MessageOutcome(
            correlation_id=correlation_id,
            channel=channel,
            tenant_id=trace.tenant_id,
            outcome=outcome,
            terminal_stage=trace.terminal_stage,
            attempts=provider_calls,
            provider_calls=provider_calls,
            trace_intact=not trace.trace_context_break,
        )
        result.outcomes.append(record)

        bucket = per_channel[channel]
        bucket.channel = channel
        bucket.accepted += 1
        if outcome is Outcome.STOPPED:
            bucket.lost += 1
            result.stopped_by_stage[trace.terminal_stage] += 1
        else:
            bucket.delivered += 1
        if outcome is Outcome.DELIVERED_DUPLICATE:
            bucket.duplicated += 1
        if record.trace_intact:
            bucket.trace_intact += 1

    result.by_channel = dict(sorted(per_channel.items()))
    return result
