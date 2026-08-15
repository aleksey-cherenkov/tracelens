"""Group records into journeys.

A journey is every record sharing one value of the correlation key, in time order.
That's it — no graph, no stage model, no expected ordering.

**The key is not discovered.** An earlier version scored every candidate
identifier with a formula and picked a winner. That was code making a judgement
call using a number I invented, which is the thing this project keeps arguing
against. In a real deployment you know your key; it's in a config file.

So `candidates()` *counts*, and the counting is genuinely useful:

 candidate         coverage   groups   sources/group   median size
 correlation_id         14%       41             3.0             9
 trace_id               13%       49             2.3             6
 tenant_id              12%        6             3.0            60
 span_id                 9%      273             1.0             1

Then `--key` picks one, or `default_key()` applies two disqualifying filters and
takes the highest coverage of what's left. Both the choice and the table are
printed, so a wrong default is visible rather than silent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .events import PER_RECORD_IDS, Event, EventLog


@dataclass(frozen=True)
class Candidate:
    """What an identifier looks like as a join key. Counted, not judged."""

    key: str
    coverage: float
    groups: int
    sources_per_group: float
    median_group_size: float
    disqualified: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "coverage": round(self.coverage, 3),
            "groups": self.groups,
            "sources_per_group": round(self.sources_per_group, 1),
            "median_group_size": self.median_group_size,
            "disqualified": self.disqualified,
        }


@dataclass
class Journey:
    """Every record sharing one correlation value, in time order."""

    key: str
    value: str
    events: list[Event] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.events[0].at

    @property
    def end(self) -> datetime:
        return self.events[-1].at

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start).total_seconds() * 1000

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for event in self.events:
            if event.source not in seen:
                seen.append(event.source)
        return seen

    def other_ids(self, name: str) -> set[str]:
        """Distinct values of another id key across this journey.

        How a broken context propagation shows itself: one journey, several
        trace IDs, so a search by trace ID returns a fragment with no indication
        that it is one.
        """
        return {e.ids[name] for e in self.events if name in e.ids}

    def values_of(self, name: str) -> set:
        return {e.attributes[name] for e in self.events if name in e.attributes}

    def matches(self, where: dict[str, str]) -> bool:
        for name, wanted in where.items():
            seen = {str(v) for v in self.values_of(name)} | self.other_ids(name)
            if str(wanted) not in seen:
                return False
        return True


def candidates(log: EventLog) -> list[Candidate]:
    """Every identifier, with the numbers that decide whether it joins anything.

    Two disqualifications, both structural rather than tuned:

      * one record per group -- that labels records, it does not join them
      * no group spans more than one source -- that is a per-service field, and
        a journey that never crosses a boundary cannot show you a handoff failing
    """
    total = len(log.events)
    if not total:
        return []

    found: list[Candidate] = []
    for key in sorted(log.id_keys()):
        groups: dict[str, list[Event]] = defaultdict(list)
        for event in log.events:
            if key in event.ids:
                groups[event.ids[key]].append(event)
        if not groups:
            continue

        sizes = sorted(len(g) for g in groups.values())
        median = sizes[len(sizes) // 2]
        sources = [len({e.source for e in g}) for g in groups.values()]

        disqualified = ""
        if key in PER_RECORD_IDS or median <= 1:
            disqualified = "identifies a single record, not a journey"
        elif max(sources) <= 1:
            disqualified = "no group spans more than one source"

        found.append(
            Candidate(
                key=key,
                coverage=sum(len(g) for g in groups.values()) / total,
                groups=len(groups),
                sources_per_group=sum(sources) / len(groups),
                median_group_size=median,
                disqualified=disqualified,
            )
        )

    return sorted(found, key=lambda c: (bool(c.disqualified), -c.coverage))


def default_key(log: EventLog) -> str | None:
    """Highest coverage among identifiers that actually join. Not a score.

    Deliberately dumb, and printed alongside the full table so overriding it with
    `--key` is one glance away.
    """
    usable = [c for c in candidates(log) if not c.disqualified]
    return max(usable, key=lambda c: c.coverage).key if usable else None


def group(log: EventLog, key: str) -> dict[str, Journey]:
    buckets: dict[str, list[Event]] = defaultdict(list)
    for event in log.events:
        if key in event.ids:
            buckets[event.ids[key]].append(event)
    return {
        value: Journey(key=key, value=value, events=sorted(events, key=lambda e: e.at))
        for value, events in sorted(buckets.items())
    }


@dataclass
class Grouping:
    key: str | None
    journeys: dict[str, Journey]
    candidates: list[Candidate]
    unjoined: int
    """Records carrying no value for the key. Reported rather than dropped
    silently: a large share here is itself a finding about the telemetry."""

    def __len__(self) -> int:
        return len(self.journeys)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "journeys": len(self.journeys),
            "unjoined_records": self.unjoined,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def build(log: EventLog, key: str | None = None) -> Grouping:
    chosen = key or default_key(log)
    journeys = group(log, chosen) if chosen else {}
    return Grouping(
        key=chosen,
        journeys=journeys,
        candidates=candidates(log),
        unjoined=sum(1 for e in log.events if not chosen or chosen not in e.ids),
    )
