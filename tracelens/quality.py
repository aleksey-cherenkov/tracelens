"""What is wrong with the telemetry itself, and what that stops you concluding.

This runs before anything else, and it is the layer I would keep if I could only
keep one.

Real telemetry is partly broken. Fields are constant and therefore meaningless.
Context dies at a hop. Most records join to nothing. A tool that reads that and
still produces a confident root cause is doing the same thing a confident junior
does — and it is the failure this whole project is about.

So every check reports two things: the defect, and the **limit** it imposes. "No
record ever reports a failure status" is a fact. "Therefore absence of error is
not evidence of success in this data" is the part that changes what you do next,
and it is what the model is told binds it.

It is also the one rule-based component left, which is deliberate. Computing the
statistic and letting the model say what it prevents would be more consistent with
the rest of the design and would make the limit advisory. Writing it here is what
guarantees it appears at all.

Nothing here knows what this pipeline is.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .events import EventLog
from .evidence import Defect
from .journeys import Grouping

# A field whose value never varies carries no information. Below this many
# observations we say nothing — three identical values is not a pattern.
MIN_OBSERVATIONS = 20

# Share of records that must join to nothing before it is worth reporting.
UNJOINED_SHARE = 0.2

# Values that would indicate something went wrong, if any field ever took one.
FAILURE_HINTS = frozenset(
    {"error", "err", "fail", "failed", "failure", "fatal", "critical", "panic",
     "exception", "timeout", "rejected", "denied", "aborted", "cancelled"}
)

# Field names that suggest the field was *meant* to carry a failure signal. Used
# only to order evidence, never to decide anything — so an unfamiliar naming
# convention costs readability, not correctness.
SEVERITY_NAME_HINTS = ("level", "severity", "status", "state", "result", "outcome")

# Per-record identifiers can't fragment a journey; they are supposed to differ.
NOT_JOURNEY_SCOPED = frozenset({"span_id", "parent_span_id", "event_id", "record_id"})


@dataclass
class Quality:
    defects: list[Defect]

    @property
    def limits(self) -> list[str]:
        """Every constraint the data places on the analysis, deduplicated."""
        seen: list[str] = []
        for defect in self.defects:
            for limit in defect.limits:
                if limit not in seen:
                    seen.append(limit)
        return seen

    def as_dict(self) -> dict:
        return {
            "note": (
                "Defects in the telemetry itself. Each states what it prevents "
                "you concluding. Honour those limits: do not read a field as "
                "evidence when a limit says it cannot be."
            ),
            "limits": self.limits,
            "defects": [d.as_dict() for d in self.defects],
        }


def assess(log: EventLog, grouping: Grouping) -> Quality:
    defects: list[Defect] = []
    for check in (
        _uninformative_fields,
        _no_failure_signal,
        _unjoined_records,
        _fragmented_journeys,
    ):
        defects.extend(check(log, grouping))
    return Quality(defects=defects)


def _field_values(log: EventLog) -> dict[str, Counter]:
    values: dict[str, Counter] = defaultdict(Counter)
    for event in log.events:
        for key, value in event.attributes.items():
            if isinstance(value, (str, int, float, bool)):
                values[key][value] += 1
    return values


# --------------------------------------------------------------------------- #


def _uninformative_fields(log: EventLog, grouping: Grouping) -> list[Defect]:
    """A field that never varies cannot distinguish anything.

    This is how a status field that always says OK and a gauge that always says
    zero are the same defect — and why neither can support an alert.
    """
    defects: list[Defect] = []
    for key, counts in sorted(_field_values(log).items()):
        observations = sum(counts.values())
        if observations < MIN_OBSERVATIONS or len(counts) != 1:
            continue

        only = next(iter(counts))
        looks_healthy = str(only).strip().lower() in {"ok", "success", "healthy", "0", "0.0"}

        limits = [
            f"`{key}` is constant at {only!r} across all {observations:,} records, "
            "so it cannot distinguish anything and no alert built on it can ever fire"
        ]
        if looks_healthy:
            limits.append(
                f"in particular, a healthy-looking `{key}` is not evidence that "
                "anything worked — the field would read the same if everything failed"
            )

        defects.append(
            Defect(
                id=f"Q.uninformative.{key}",
                title=f"`{key}` never varies — it is recorded but measures nothing",
                detail=(
                    f"Every one of {observations:,} records carrying `{key}` reports "
                    f"the same value, {only!r}. A field with one value cannot separate "
                    "good from bad."
                ),
                evidence=[f"{observations:,} observations, 1 distinct value: {only!r}"],
                limits=limits,
                would_resolve=[
                    f"whether `{key}` is populated at emission, or hardcoded"
                ],
                params={"min_observations": MIN_OBSERVATIONS},
            )
        )
    return defects


def _no_failure_signal(log: EventLog, grouping: Grouping) -> list[Defect]:
    """Is there any field, anywhere, that would say something went wrong?"""
    if len(log.events) < MIN_OBSERVATIONS:
        return []

    values = _field_values(log)
    if any(
        str(value).strip().lower() in FAILURE_HINTS
        for counts in values.values()
        for value in counts
        if isinstance(value, str)
    ):
        return []

    # Which fields *look* like they were meant to carry it? A small string-valued
    # enum is the shape of a severity or status field. Naming them turns an
    # abstract complaint into somewhere to go and look.
    shaped = {
        key: sorted(str(v) for v in counts)
        for key, counts in values.items()
        if 2 <= len(counts) <= 8
        and sum(counts.values()) >= MIN_OBSERVATIONS
        and all(isinstance(v, str) for v in counts)
    }
    ordered = sorted(
        shaped.items(),
        key=lambda item: (
            not any(hint in item[0].lower() for hint in SEVERITY_NAME_HINTS),
            item[0],
        ),
    )

    return [
        Defect(
            id="Q.no_failure_signal",
            title="nothing in this data ever reports a failure",
            detail=(
                f"Across {len(log.events):,} records, no field takes a value meaning "
                "something went wrong — no error, no failure, no timeout, no "
                "rejection. Either this system never failed in this window, or "
                "failures are not being recorded. This data cannot distinguish the two."
            ),
            evidence=[
                f"0 of {len(log.events):,} records carry any value in "
                f"{sorted(FAILURE_HINTS)[:6]}…",
                *[f"`{key}` only ever takes {seen}" for key, seen in ordered[:4]],
            ],
            limits=[
                "no finding here can be based on an error signal, because there is "
                "none — everything must be inferred from structure, absence and "
                "timing instead",
                "any existing alerting built on error status or log level is blind "
                "in this window by construction",
            ],
            would_resolve=[
                "whether failures are caught and swallowed before reaching telemetry"
            ],
        )
    ]


def _unjoined_records(log: EventLog, grouping: Grouping) -> list[Defect]:
    """Records carrying no correlation key support no per-journey claim."""
    if not grouping.key or not log.events or not grouping.unjoined:
        return []

    share = grouping.unjoined / len(log.events)
    if share < UNJOINED_SHARE:
        return []

    orphans = [e for e in log.events if grouping.key not in e.ids]
    by_name = Counter(e.name for e in orphans)
    by_source = Counter(e.source for e in orphans)

    return [
        Defect(
            id="Q.unjoined_records",
            title=f"{share:.0%} of records carry no `{grouping.key}` and join to nothing",
            detail=(
                f"{grouping.unjoined:,} of {len(log.events):,} records have no "
                f"`{grouping.key}`, so they cannot be tied to any journey. They can be "
                "counted in aggregate but can never answer 'what happened to this one'. "
                f"The remaining {len(log.events) - grouping.unjoined:,} records are the "
                "entire investigable surface."
            ),
            evidence=[
                f"by service: {dict(by_source.most_common(5))}",
                *[f"{count:,} records named {name!r}" for name, count in by_name.most_common(4)],
            ],
            limits=[
                f"{share:.0%} of this data can never support a claim about a specific "
                "journey — only about volume",
                "any search starting from an identifier misses those records entirely, "
                "however relevant they are",
            ],
            would_resolve=[
                f"why these emit without `{grouping.key}` — whether they are outside a "
                "request context, or the context is not propagated to the logger"
            ],
            params={"unjoined_share": UNJOINED_SHARE},
        )
    ]


def _fragmented_journeys(log: EventLog, grouping: Grouping) -> list[Defect]:
    """A secondary identifier that changes mid-journey is a broken propagation.

    Generic: it checks every other id key against the chosen one, so it finds a
    broken trace context without knowing that traces exist.
    """
    if not grouping.key or not grouping.journeys:
        return []

    defects: list[Defect] = []
    for key in sorted(log.id_keys()):
        if key == grouping.key or key in NOT_JOURNEY_SCOPED:
            continue

        carrying = [j for j in grouping.journeys.values() if j.other_ids(key)]
        broken = [j for j in carrying if len(j.other_ids(key)) > 1]
        if not broken or not carrying:
            continue

        breaks: Counter = Counter()
        for journey in broken:
            seen: set[str] = set()
            for event in journey.events:
                value = event.ids.get(key)
                if value is None:
                    continue
                if seen and value not in seen:
                    breaks[f"{event.source}:{event.name}"] += 1
                seen.add(value)

        where = ", ".join(name for name, _ in breaks.most_common(2)) or "an unclear point"
        defects.append(
            Defect(
                id=f"Q.fragmented.{key}",
                title=(
                    f"`{key}` changes mid-journey in {len(broken)} of {len(carrying)} "
                    "journeys"
                ),
                detail=(
                    f"{len(broken)} journeys carry more than one `{key}` value, so anyone "
                    f"searching by `{key}` sees only part of them. The break appears at "
                    f"{where}. The other {len(carrying) - len(broken)} keep one value "
                    "throughout, so this is not how the transport behaves in general — "
                    "something specific is not propagating it."
                ),
                evidence=[
                    f"{len(broken)}/{len(carrying)} journeys fragment",
                    f"first divergence at {dict(breaks.most_common(3))}",
                ],
                affected=[j.value for j in broken],
                limits=[
                    f"for these journeys `{key}` reaches only part of the path — any "
                    f"tool or dashboard keyed on `{key}` shows a fragment and gives no "
                    "indication that it is one"
                ],
                would_resolve=[
                    "the context-propagation code at the point of divergence, compared "
                    "against a path that keeps one value"
                ],
            )
        )
    return defects
