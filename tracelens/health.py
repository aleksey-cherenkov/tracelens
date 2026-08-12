"""Per-service and per-hop health: throughput, latency, errors, retries.

Two things here are deliberate and both are corrections of an earlier draft.

1. Percentiles are computed only where distinct values exist. Every async hop in
   this export has *zero* variance (269.0 ms x37, 379.0 ms x37), so reporting
   "p99: 379 ms" would imply a distribution that does not exist.

2. The error rate is three separate numbers, never one. Collapsing them is
   precisely how a pipeline that dropped 9.8% of its messages reported itself as
   perfectly healthy: span status and delivery reality disagree completely.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .accounting import Accounting
from .join import JoinMethod, LogicalTrace
from .model import HOPS, SERVICES, Dataset, Stage


@dataclass(frozen=True)
class LatencySummary:
    n: int
    minimum: float | None
    median: float | None
    maximum: float | None
    p95: float | None
    p99: float | None
    distinct: int

    @property
    def has_variance(self) -> bool:
        return self.distinct > 1

    @property
    def variance_note(self) -> str:
        return "none" if self.n and not self.has_variance else ""


def summarise(values: list[float]) -> LatencySummary:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return LatencySummary(0, None, None, None, None, None, 0)
    distinct = len(set(clean))
    # Percentiles only where a distribution actually exists.
    p95 = p99 = None
    if distinct > 1:
        p95 = _percentile(clean, 0.95)
        p99 = _percentile(clean, 0.99)
    return LatencySummary(
        n=len(clean),
        minimum=clean[0],
        median=statistics.median(clean),
        maximum=clean[-1],
        p95=p95,
        p99=p99,
        distinct=distinct,
    )


def _percentile(sorted_values: list[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


@dataclass
class ServiceHealth:
    service: str
    spans: int = 0
    messages: int = 0
    span_status_errors: int = 0
    retries: int = 0
    log_records: int = 0
    scoped_logs: int = 0
    durations: list[float] = field(default_factory=list)

    @property
    def latency(self) -> LatencySummary:
        return summarise(self.durations)


@dataclass
class HopHealth:
    name: str
    frm: Stage
    to: Stage
    asynchronous: bool
    observed: int = 0
    absent: int = 0
    fallback_joins: int = 0
    gaps: list[float] = field(default_factory=list)
    nested: bool = False

    @property
    def latency(self) -> LatencySummary:
        return summarise(self.gaps)

    @property
    def loss(self) -> int:
        return self.absent

    @property
    def needs_attention(self) -> str | None:
        """One-line reason this hop is the problem, or None."""
        if self.absent:
            return f"{self.absent} message(s) never arrived"
        if self.fallback_joins:
            return f"{self.fallback_joins} message(s) lost trace context here"
        return None


@dataclass
class ErrorRates:
    """Three numbers that must never be collapsed into one."""

    span_status_errors: int
    total_spans: int
    provider_errors: int
    provider_calls: int
    delivery_failures: int
    accepted: int

    @property
    def span_status_rate(self) -> float:
        return self.span_status_errors / self.total_spans if self.total_spans else 0.0

    @property
    def provider_error_rate(self) -> float:
        return self.provider_errors / self.provider_calls if self.provider_calls else 0.0

    @property
    def delivery_failure_rate(self) -> float:
        return self.delivery_failures / self.accepted if self.accepted else 0.0

    @property
    def diverges(self) -> bool:
        """True when the telemetry claims health the delivery record contradicts."""
        return self.span_status_errors == 0 and self.delivery_failures > 0


@dataclass
class Health:
    services: dict[str, ServiceHealth]
    hops: dict[str, HopHealth]
    errors: ErrorRates
    end_to_end: dict[str, LatencySummary]
    throughput: dict[str, Counter]
    retries_provider: int
    redeliveries: int

    def worst_hop(self) -> HopHealth | None:
        candidates = [h for h in self.hops.values() if h.needs_attention]
        if not candidates:
            return None
        return max(candidates, key=lambda h: (h.absent, h.fallback_joins))


def compute(
    dataset: Dataset, traces: dict[str, LogicalTrace], accounting: Accounting
) -> Health:
    services = {name: ServiceHealth(service=name) for name in SERVICES}
    for span in dataset.spans:
        bucket = services.setdefault(span.service, ServiceHealth(service=span.service))
        bucket.spans += 1
        bucket.durations.append(float(span.duration_ms))
        if span.status != "OK":
            bucket.span_status_errors += 1
        bucket.retries += span.retry_count
    for trace in traces.values():
        for service in {s.service for s in trace.spans}:
            services[service].messages += 1
    for log in dataset.logs:
        bucket = services.setdefault(log.service, ServiceHealth(service=log.service))
        bucket.log_records += 1
        if log.is_message_scoped:
            bucket.scoped_logs += 1

    hops = {
        hop.name: HopHealth(
            name=hop.name, frm=hop.frm, to=hop.to, asynchronous=hop.asynchronous
        )
        for hop in HOPS
    }
    hop_by_stage = {hop.frm: hops[hop.name] for hop in HOPS}
    for trace in traces.values():
        for record in trace.joins:
            hop = hop_by_stage.get(record.frm)
            if hop is None:
                continue
            hop.nested = record.nested
            if record.method is JoinMethod.ABSENT:
                hop.absent += 1
                continue
            hop.observed += 1
            if record.method is JoinMethod.CORRELATION_FALLBACK:
                hop.fallback_joins += 1
            # Only a real elapsed gap is latency. A nested transition is a child
            # starting inside its parent and its "gap" is an offset, not a hop.
            if not record.nested and record.gap_ms is not None:
                hop.gaps.append(record.gap_ms)

    provider_calls = 0
    provider_errors = 0
    retries_provider = 0
    redeliveries = 0
    e2e: dict[str, list[float]] = defaultdict(list)
    for trace in traces.values():
        for attempt in trace.attempts:
            provider_calls += 1
            retries_provider += attempt.send.retry_count
            if attempt.receive_count > 1:
                redeliveries += 1
            status = attempt.provider_status
            if status is not None and not 200 <= status < 300:
                provider_errors += 1
        value = trace.end_to_end_ms
        if value is not None:
            e2e[trace.channel or "unknown"].append(value)

    errors = ErrorRates(
        span_status_errors=sum(s.span_status_errors for s in services.values()),
        total_spans=len(dataset.spans),
        provider_errors=provider_errors,
        provider_calls=provider_calls,
        delivery_failures=accounting.stopped,
        accepted=accounting.total,
    )

    throughput: dict[str, Counter] = defaultdict(Counter)
    for trace in traces.values():
        if trace.accepted:
            day = trace.accepted.accepted_at.date().isoformat()
            throughput[trace.channel or "unknown"][day] += 1

    return Health(
        services=services,
        hops=hops,
        errors=errors,
        end_to_end={k: summarise(v) for k, v in sorted(e2e.items())},
        throughput={k: v for k, v in sorted(throughput.items())},
        retries_provider=retries_provider,
        redeliveries=redeliveries,
    )
