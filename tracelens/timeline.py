"""How long things took, and when. Descriptive only.

Latency is the one signal every telemetry system has, whatever it is monitoring,
which is why this layer exists at all.

It is also where confident tools go wrong. A p99 of 340ms is not a problem; it is
a number. Whether it is a problem depends on an SLO this tool has never been told.
So nothing here declares anything slow. There is no threshold, no "degraded", no
severity. It reports the distribution, where the time went, and where the shape
changed relative to the rest of the data — and leaves the judgement to the person
who knows what the target was.

The one thing it does assert is *shape*: if one node's distribution is bimodal, or
if a node's share of total time moved sharply within the window, that is a fact
about the data rather than an opinion about the system.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .journeys import Grouping
from .events import Event, EventLog, format_time

# Below this many samples a percentile is theatre. Report the raw values instead.
MIN_SAMPLES = 8


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank. No interpolation: with 12 samples an interpolated p99 is a
    number invented between two observations, and inventing numbers is the thing
    this project is against."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * len(ordered) + 0.5) - 1))
    return ordered[index]


@dataclass
class Distribution:
    label: str
    samples: list[float]

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def total_ms(self) -> float:
        return sum(self.samples)

    @property
    def p50(self) -> float:
        return percentile(self.samples, 0.50)

    @property
    def p95(self) -> float:
        return percentile(self.samples, 0.95)

    @property
    def maximum(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def reliable(self) -> bool:
        return self.count >= MIN_SAMPLES

    @property
    def spread(self) -> float:
        """max/p50. A large ratio means the average is describing nobody."""
        return self.maximum / self.p50 if self.p50 else 0.0

    def describe(self) -> str:
        if not self.reliable:
            shown = ", ".join(f"{v:.0f}" for v in sorted(self.samples))
            return f"{self.count} samples: {shown} ms (too few for percentiles)"
        return (
            f"n={self.count}, p50 {self.p50:.0f}ms, p95 {self.p95:.0f}ms, "
            f"max {self.maximum:.0f}ms"
        )

    def as_dict(self) -> dict:
        payload = {"label": self.label, "count": self.count, "description": self.describe()}
        if self.reliable:
            payload |= {
                "p50_ms": round(self.p50, 1),
                "p95_ms": round(self.p95, 1),
                "max_ms": round(self.maximum, 1),
                "spread": round(self.spread, 1),
            }
        return payload


@dataclass
class Timeline:
    """Where the time went, at three resolutions."""

    per_node: dict[str, Distribution]
    end_to_end: Distribution
    window: tuple[datetime, datetime] | None
    longest_journeys: list[tuple[str, float]]
    """Ranked by duration. "Longest", not "slowest" -- one is an observation and
    the other is a verdict this layer is not entitled to."""

    def busiest(self, limit: int = 8) -> list[Distribution]:
        """Nodes ranked by total time spent, not by p95.

        Total time is what you would actually gain by fixing it. A node with a
        500ms p95 called twice is a worse target than a 40ms node called 3,000
        times, and ranking by percentile hides that every time.
        """
        return sorted(self.per_node.values(), key=lambda d: d.total_ms, reverse=True)[:limit]

    def widest_spread(self, limit: int = 5) -> list[Distribution]:
        """Nodes where the distribution is worth looking at rather than summarising."""
        return sorted(
            (d for d in self.per_node.values() if d.reliable and d.spread >= 3),
            key=lambda d: d.spread,
            reverse=True,
        )[:limit]

    def as_dict(self) -> dict:
        return {
            "window": (
                [format_time(self.window[0]), format_time(self.window[1])]
                if self.window
                else None
            ),
            "end_to_end": self.end_to_end.as_dict(),
            "busiest_nodes": [d.as_dict() for d in self.busiest()],
            "widest_spread": [d.as_dict() for d in self.widest_spread()],
            "longest_journeys": [
                {"journey": value, "duration_ms": round(ms, 1)}
                for value, ms in self.longest_journeys
            ],
            "note": (
                "Descriptive only. No threshold has been applied and nothing here "
                "is called slow -- that judgement needs an SLO this tool has not "
                "been given."
            ),
        }


def node_label(event: Event, node_of=None) -> str:
    return node_of(event) if node_of else event.node


def build(log: EventLog, grouping: Grouping, node_of=None) -> Timeline:
    """node_of lets routes.py supply templated node names, so two channels
    publishing to their own queues collapse into one distribution."""
    per_node: dict[str, list[float]] = defaultdict(list)
    for event in log.events:
        duration = event.duration_ms
        if duration is not None:
            per_node[node_label(event, node_of)].append(duration)

    journeys = grouping.journeys
    end_to_end = [j.duration_ms for j in journeys.values() if len(j.events) > 1]
    longest = sorted(
        ((j.value, j.duration_ms) for j in journeys.values() if len(j.events) > 1),
        key=lambda pair: pair[1],
        reverse=True,
    )[:5]

    return Timeline(
        per_node={
            label: Distribution(label=label, samples=samples)
            for label, samples in sorted(per_node.items())
        },
        end_to_end=Distribution(label="end to end", samples=end_to_end),
        window=log.window,
        longest_journeys=longest,
    )
