"""The timeline the model actually reads.

Filter to what the question is about, order by time, put the deploys in the
sequence. That is the whole idea, and it is what an engineer does by hand.

Two things this module adds that "filter and sort" does not give you on its own,
and both are the difference between a slice that answers a question and one that
looks fine:

**A contrast journey.** Filter to the four affected journeys and the model sees
four sequences that each end at the same place. Nothing looks wrong — that is
simply what those journeys look like, as far as it can tell. The finding is
*"these stop here and everything else doesn't"*, and it needs a normal journey on
the same page to be visible at all.

**Deploys in the sequence, not in a footnote.** A change that landed inside a
journey's own span is rendered inline at its timestamp. One that landed near the
slice but outside every journey gets its offset stated. Neither is called a cause.

Everything here is capped and the caps are reported, so a slice cannot quietly
become the whole export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .events import Event, EventLog, format_time
from .journeys import Grouping, Journey
from .routes import Routes

MAX_JOURNEYS = 3
"""Rendered in full when a slice is large. More than this and it stops being
readable, for a model as much as for a person."""

SHOW_ALL_BELOW = 6
"""...but a small slice is rendered whole.

A live run caught this: four journeys matched, three were shown, and the model
wrote out the three identifiers it had plus "the 4th match" -- correctly refusing
to invent the one it had not been given. Hiding a quarter of the evidence to save
four lines is a bad trade, and the honest workaround it produced is the tell."""

MAX_RECORDS = 60
"""Per journey. A journey longer than this is itself worth knowing about, so the
truncation is reported rather than silent."""

CHANGE_WINDOW = timedelta(minutes=45)
"""How far either side of a slice a change is still worth mentioning. Wide on
purpose: a narrow window quietly does the ruling-out for you, and hiding a
candidate is worse than listing one the reader dismisses in a second."""

MAX_ATTRIBUTES = 6

# Already shown in their own column, or already stated in the header.
HIDDEN_ATTRIBUTES = frozenset({"kind", "duration_ms", "duration", "elapsed_ms", "latency_ms"})


@dataclass(frozen=True)
class Filter:
    """What the question is about. Every field is optional; none is a default."""

    where: dict[str, str] = field(default_factory=dict)
    route: int | None = None
    after: datetime | None = None
    before: datetime | None = None
    values: tuple[str, ...] = ()

    @property
    def is_time_only(self) -> bool:
        return bool(self.after or self.before) and not (
            self.where or self.route or self.values
        )

    def describe(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.where.items())]
        if self.route:
            parts.append(f"route {self.route}")
        if self.values:
            parts.append(f"{len(self.values)} named journey(s)")
        if self.after:
            parts.append(f"after {format_time(self.after)}")
        if self.before:
            parts.append(f"before {format_time(self.before)}")
        return ", ".join(parts) or "everything"

    def matches(self, journey: Journey, routes: Routes) -> bool:
        if self.values and journey.value not in self.values:
            return False
        if self.where and not journey.matches(self.where):
            return False
        if self.after and journey.end < self.after:
            return False
        if self.before and journey.start > self.before:
            return False
        if self.route is not None:
            route = routes.of_journey(journey.value)
            if route is None or route.index != self.route:
                return False
        return True


@dataclass
class Change:
    at: datetime
    target: str
    detail: str
    name: str = ""

    def describe(self) -> str:
        return " ".join(part for part in (self.name, self.detail) if part)


@dataclass
class Slice:
    filter: Filter
    matched: list[Journey]
    shown: list[Journey]
    contrast: Journey | None
    contrast_reason: str
    changes: list[Change]
    interleaved: bool

    @property
    def total(self) -> int:
        return len(self.matched)

    def as_dict(self, routes: Routes) -> dict:
        return {
            "filter": self.filter.describe(),
            "matched_journeys": self.total,
            "rendered_journeys": len(self.shown),
            "timeline": render(self, routes),
            "contrast": self.contrast_reason,
            "note": (
                "Records in time order. `**` marks a recorded change. `!` marks a "
                "record whose trace identifier differs from the one before it. "
                "Nothing here is labelled a cause."
            ),
        }


# --------------------------------------------------------------------------- #


def _detail(event: Event, skip: frozenset = frozenset()) -> str:
    parts = [
        f"{k}={v}"
        for k, v in sorted(event.attributes.items())
        if k not in HIDDEN_ATTRIBUTES and k not in skip
    ]
    return ", ".join(parts[:MAX_ATTRIBUTES])


def constant_within(journey: Journey) -> dict[str, object]:
    """Attributes that never vary across this journey.

    Hoisted into the header instead of repeated on every row. On a ten-record
    journey that is nine repetitions of `message_type=email` removed — worth it
    for a person reading, and worth more for a prompt paying by the token.
    """
    seen: dict[str, set] = {}
    for event in journey.events:
        for key, value in event.attributes.items():
            if key in HIDDEN_ATTRIBUTES or not isinstance(value, (str, int, bool)):
                continue
            seen.setdefault(key, set()).add(value)
    return {k: next(iter(v)) for k, v in sorted(seen.items()) if len(v) == 1}


def _constant_across(journeys: list[Journey]) -> dict[str, object]:
    """Attributes that never vary across every rendered journey."""
    if not journeys:
        return {}
    shared = constant_within(journeys[0])
    for journey in journeys[1:]:
        other = constant_within(journey)
        shared = {k: v for k, v in shared.items() if other.get(k) == v}
    return shared


def changes_near(log: EventLog, start: datetime, end: datetime) -> list[Change]:
    found = []
    for event in log.of_kind("deploy"):
        if start - CHANGE_WINDOW <= event.at <= end + CHANGE_WINDOW:
            found.append(
                Change(
                    at=event.at,
                    target=str(event.attributes.get("service") or event.source),
                    detail=_detail(event),
                    name=event.name,
                )
            )
    return sorted(found, key=lambda c: c.at)


def pick_contrast(
    matched: list[Journey], grouping: Grouping, routes: Routes
) -> tuple[Journey | None, str]:
    """A journey from the dominant route, nearest in time to the slice.

    Nearest in time rather than first or random: it controls for anything that
    changed across the window, so a difference between the two is more likely to
    be about the filter than about the day.
    """
    dominant = routes.dominant
    if dominant is None or not matched:
        return None, "no contrast available -- nothing to compare against"

    excluded = {j.value for j in matched}
    pool = [
        grouping.journeys[v]
        for v in dominant.journeys
        if v in grouping.journeys and v not in excluded
    ]
    if not pool:
        return None, (
            f"no contrast shown -- these journeys are route {dominant.index}, the "
            "most common one, so there is no more-normal path to compare with"
        )

    anchor = min(j.start for j in matched)
    nearest = min(pool, key=lambda j: abs((j.start - anchor).total_seconds()))
    return nearest, (
        f"contrast is {nearest.value}, from route {dominant.index} "
        f"({dominant.count} journeys, the most common), chosen as the one nearest "
        "in time to this slice"
    )


def select(
    log: EventLog,
    grouping: Grouping,
    routes: Routes,
    where: Filter,
    max_journeys: int = MAX_JOURNEYS,
) -> Slice:
    matched = [j for j in grouping.journeys.values() if where.matches(j, routes)]
    matched.sort(key=lambda j: j.start)
    shown = matched if len(matched) <= SHOW_ALL_BELOW else matched[:max_journeys]

    contrast, reason = pick_contrast(matched, grouping, routes)

    # A filter that names a period is asking about that period, so changes are
    # looked for across it -- not only across the journeys that happened to
    # match. Anchoring to the journeys instead hides a deploy four hours into
    # the window the person actually asked about.
    if where.after or where.before:
        start = where.after or (log.window[0] if log.window else datetime.min)
        end = where.before or (log.window[1] if log.window else datetime.max)
    elif shown:
        start = min(j.start for j in shown)
        end = max(j.end for j in shown)
    else:
        start = end = log.window[0] if log.window else datetime.min

    return Slice(
        filter=where,
        matched=matched,
        shown=shown,
        contrast=contrast,
        contrast_reason=reason,
        changes=changes_near(log, start, end),
        interleaved=where.is_time_only,
    )


# --------------------------------------------------------------------------- #


def _row(event: Event, routes: Routes, mark: str, skip: frozenset = frozenset()) -> str:
    duration = event.duration_ms
    return (
        f"  {mark:2}{format_time(event.at)[11:23]}  "
        f"{event.source:<20} "
        f"{routes.node_of(event).split(':', 1)[-1]:<46} "
        f"{(f'{duration:.0f}ms' if duration is not None else ''):>8}  "
        f"{_detail(event, skip)}"
    ).rstrip()


def _change_row(change: Change) -> str:
    return (
        f"  **{format_time(change.at)[11:23]}  "
        f"{'CHANGE ' + change.target:<20} {change.describe()}"
    )


def _journey_lines(
    journey: Journey, routes: Routes, changes: list[Change], label: str
) -> list[str]:
    route = routes.of_journey(journey.value)
    header = f"{label} {journey.value}"
    if route:
        header += f"  (route {route.index}, {route.count} journeys took it)"
    lines = [header]

    constants = constant_within(journey)
    if constants:
        lines.append(
            "  constant throughout: "
            + ", ".join(f"{k}={v}" for k, v in constants.items())
        )
    skip = frozenset(constants)

    records = journey.events[:MAX_RECORDS]
    inline = [c for c in changes if journey.start <= c.at <= journey.end]

    rows: list[tuple[datetime, str]] = []
    previous_trace = None
    for event in records:
        trace = event.ids.get("trace_id", "")
        mark = ""
        if previous_trace and trace and trace != previous_trace:
            mark = "!"
        if trace:
            previous_trace = trace
        rows.append((event.at, _row(event, routes, mark, skip)))
    rows.extend((c.at, _change_row(c)) for c in inline)

    lines.extend(text for _, text in sorted(rows, key=lambda pair: pair[0]))

    if len(journey.events) > MAX_RECORDS:
        lines.append(f"  ... {len(journey.events) - MAX_RECORDS} more records not shown")

    distinct = journey.other_ids("trace_id")
    if len(distinct) > 1:
        lines.append(
            f"  ! {len(distinct)} distinct trace identifiers -- a search by trace "
            f"returns part of this journey, with no sign that it is a part"
        )
    return lines


def render(sl: Slice, routes: Routes) -> list[str]:
    lines: list[str] = [f"filter: {sl.filter.describe()}"]
    if not sl.matched:
        lines.append("no journeys match")
        return lines

    lines.append(
        f"{sl.total} journey(s) match"
        + (f", {len(sl.shown)} rendered below" if sl.total > len(sl.shown) else "")
    )

    if sl.interleaved:
        shared = _constant_across(sl.shown)
        lines.append("")
        if shared:
            lines.append(
                "constant across every record below: "
                + ", ".join(f"{k}={v}" for k, v in shared.items())
            )
        lines.append("all matching records, one sequence, in time order:")
        skip = frozenset(shared)
        rows: list[tuple[datetime, str]] = []
        for journey in sl.shown:
            for event in journey.events[:MAX_RECORDS]:
                rows.append(
                    (event.at, _row(event, routes, "", skip) + f"   [{journey.value}]")
                )
        rows.extend((c.at, _change_row(c)) for c in sl.changes)
        lines.extend(text for _, text in sorted(rows, key=lambda pair: pair[0]))

        if sl.contrast is not None:
            lines.append("")
            lines.extend(
                _journey_lines(sl.contrast, routes, sl.changes, "contrast -- journey")
            )
    else:
        for journey in sl.shown:
            lines.append("")
            lines.extend(_journey_lines(journey, routes, sl.changes, "journey"))

        if sl.contrast is not None:
            lines.append("")
            lines.extend(
                _journey_lines(sl.contrast, routes, sl.changes, "contrast -- journey")
            )

        outside = [
            c
            for c in sl.changes
            if not any(j.start <= c.at <= j.end for j in sl.shown)
        ]
        if outside:
            anchor = min(j.start for j in sl.shown)
            lines.append("")
            lines.append("changes near this slice but not inside any journey:")
            for change in outside:
                minutes = (anchor - change.at).total_seconds() / 60
                side = "before" if minutes >= 0 else "after"
                lines.append(
                    f"  ** {format_time(change.at)}  {change.target}  "
                    f"{abs(minutes):.0f} min {side} the earliest journey here -- "
                    f"{change.describe()}"
                )

    lines.append("")
    lines.append(sl.contrast_reason)
    if not sl.changes:
        lines.append(
            "no change was recorded near this slice, which is not the same as "
            "nothing having changed -- only deploys reach this tool"
        )
    return lines
