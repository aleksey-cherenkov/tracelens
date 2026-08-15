"""One record type for everything.

Spans, log lines, deploys and metric samples are all the same shape once you stop
caring what they were called: something happened, somewhere, at a time, carrying
some identifiers and some attributes.

The previous version of this file declared a seven-stage pipeline. That was the
single worst decision in the project -- it meant the tool could only understand
the one system I had already read. There is no Stage enum here, no expected
ordering, and no assumption about how many services exist or what they do.

The one thing that matters is `ids`: a *dict*, not fixed fields. Real telemetry
correlates on whatever that team happened to choose -- correlation_id, request_id,
order_id, job_id, session_id -- and different subsystems in the same company
disagree. Which key actually correlates is discovered in correlate.py rather than
declared here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# Field names that mean "when". Checked in order; the first that parses wins.
TIME_KEYS = ("timestamp", "start_time", "time", "at")

# ...plus anything with one of these suffixes, so `accepted_at`, `deployed_at`,
# `occurred_at` and `published_time` all work without being listed. A suffix rule
# for the same reason ID_SUFFIXES is one: every team names these slightly
# differently, and a tool that needs editing per export is a tool nobody uses
# twice.
TIME_SUFFIXES = ("_at", "_time", "_timestamp", "_ts")

# Field names that mean "who emitted this".
SOURCE_KEYS = ("service", "source", "component", "app", "logger", "emitter")

# Field names that mean "what happened".
NAME_KEYS = ("name", "message", "event", "operation", "title", "msg")

# Anything ending in one of these is treated as an identifier rather than data.
# Deliberately a suffix rule: it catches order_id and job_id on a system this code
# has never seen, without anybody adding them to a list.
ID_SUFFIXES = ("_id", "Id", "_key", "_uuid")

# Identifiers that are per-record rather than per-journey. They still get kept --
# span_id and parent_span_id are how parent/child links are observed -- but
# correlate.py must never choose one as the grouping key.
PER_RECORD_IDS = frozenset({"span_id", "parent_span_id", "event_id", "record_id"})


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class Event:
    """Something that happened, somewhere, at a time."""

    at: datetime
    source: str
    name: str
    kind: str
    """Which file or stream it came from -- span, log, deploy. Descriptive only;
    no behaviour keys off it except where a record genuinely has no correlation."""

    ids: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        for key in ("duration_ms", "duration", "elapsed_ms", "latency_ms"):
            value = self.attributes.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    @property
    def node(self) -> str:
        """Where in the system this happened. Filled in by topology.py, which
        templates variable parts out of the name; this is the raw fallback."""
        return f"{self.source}:{self.name}"

    def __hash__(self) -> int:
        return hash((self.at, self.source, self.name, self.kind, id(self.attributes)))


def normalise(record: dict, kind: str) -> Event | None:
    """Turn one raw JSON record into an Event.

    Returns None rather than raising when a record has no usable timestamp: an
    unfamiliar export will contain shapes this does not expect, and one odd record
    must not take down the run.
    """
    if not isinstance(record, dict):
        return None

    time_fields = [k for k in TIME_KEYS if k in record] + [
        k for k in record if k.endswith(TIME_SUFFIXES) and k not in TIME_KEYS
    ]
    moment = next((t for k in time_fields if (t := parse_time(record[k]))), None)
    if moment is None:
        return None

    source = next((str(record[k]) for k in SOURCE_KEYS if record.get(k)), kind)
    name = next((str(record[k]) for k in NAME_KEYS if record.get(k)), kind)

    # Identifiers live at the top level, inside `attributes`, or both.
    ids: dict[str, str] = {}
    attributes: dict[str, Any] = {}

    def sort_field(key: str, value: Any) -> None:
        if value is None:
            return
        if any(key.endswith(suffix) for suffix in ID_SUFFIXES):
            ids[key] = str(value)
        else:
            attributes[key] = value

    for key, value in record.items():
        if key in time_fields or key in SOURCE_KEYS or key in NAME_KEYS:
            continue
        if key == "attributes" and isinstance(value, dict):
            for inner_key, inner_value in value.items():
                sort_field(inner_key, inner_value)
            continue
        sort_field(key, value)

    return Event(at=moment, source=source, name=name, kind=kind, ids=ids, attributes=attributes)


def normalise_all(records: Iterable[dict], kind: str) -> list[Event]:
    events = (normalise(record, kind) for record in records)
    return sorted((e for e in events if e is not None), key=lambda e: e.at)


@dataclass
class EventLog:
    """Everything that was loaded, in time order."""

    events: list[Event] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    @property
    def sources(self) -> set[str]:
        return {e.source for e in self.events}

    @property
    def kinds(self) -> set[str]:
        return {e.kind for e in self.events}

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        if not self.events:
            return None
        return self.events[0].at, self.events[-1].at

    def id_keys(self) -> set[str]:
        return {key for e in self.events for key in e.ids}

    def of_kind(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]
