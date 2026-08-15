"""What paths journeys actually take, and how many took each.

The whole analysis layer, in one table:

     20  POST → accepted → publish-topic → consume → route → publish-queue → consume → sending-log → send
      8  POST → accepted → publish-topic → consume → route → publish-queue → consume → send
      6  POST → ... → send → provider-429-backoff
      4  POST → accepted → publish-topic → published-to-topic-log
      3  POST → ... → send → received-from-queue

An earlier version computed conservation, path shape, single visit and
completeness as separate checks, each with a threshold and a severity. All four
are legible above: route 4 stops six nodes early, route 5 visits `consume` twice,
route 4 touches two services where the others touch four. The table is the
finding, and a person or a model reads it.

The one piece of cleverness that has to stay is node naming. Without it there are
41 routes, one per record, and the table says nothing.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .events import Event, EventLog
from .journeys import Journey

# A value must be one of a small set to be worth substituting. Templating a status
# code would erase the signal; a high-cardinality id would template every name
# down to nothing.
MAX_VOCABULARY_SIZE = 12

# Shorter values match inside ordinary words ("id" inside "invalid").
MIN_VALUE_LENGTH = 3

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
# A digit run that is *not* glued to a letter. `depth=0`, `attempt 1 of 3` and
# `returned 429` are values a record reports; `v1`, `s3`, `h2`, `utf8` are part of
# a name. Collapsing the second kind merges things that are genuinely different --
# `/api/v1` and `/api/v2` are separate endpoints, and a broken v2 rollout sharing
# a node with v1 is invisible.
DIGITS = re.compile(r"(?<![A-Za-z])\d+")


def learn_vocabulary(log: EventLog) -> dict[str, str]:
    """Build value -> {attribute} for low-cardinality values that appear in names.

    Keyed by *value*, not by record, and that is the point. A span carries
    `message_type=email` and is named "send email"; the log line "Routing message
    type=email" carries no attributes at all, but it is the same place in the
    system. Learning the vocabulary once from the whole export and applying it
    everywhere is what lets logs and spans land on the same route.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for event in log.events:
        for key, value in event.attributes.items():
            if isinstance(value, str) and len(value) >= MIN_VALUE_LENGTH:
                seen[key].add(value)

    vocabulary: dict[str, str] = {}
    for key, values in sorted(seen.items()):
        if not 1 < len(values) <= MAX_VOCABULARY_SIZE:
            continue
        if not any(value in event.name for value in values for event in log.events):
            continue
        for value in values:
            vocabulary.setdefault(value, "{" + key + "}")
    return vocabulary


def substitute(name: str, vocabulary: dict[str, str]) -> str:
    # Longest first, so a value containing another is not half-replaced.
    for value in sorted(vocabulary, key=len, reverse=True):
        if value in name:
            name = name.replace(value, vocabulary[value])
    return name


def collapse(text: str) -> str:
    """The most aggressive form of a name: every per-record *value* removed.

    A *candidate*, not a decision. Whether it gets used is settled by
    `learn_names`, which only accepts a collapse that actually merges something.

    Two rules, and the second is the subtle one. A digit standing alone is a
    reading -- `depth=0`, `attempt 1 of 3`, `returned 429` -- and collapsing it
    is right as soon as it varies. A digit glued to a letter is part of a name:
    `v1`, `s3`, `h2`. Those never earn a collapse, because the thing they name is
    genuinely a different thing.
    """
    text = EMAIL.sub("{addr}", text)
    text = HEX.sub("{hex}", text)
    return DIGITS.sub("{n}", text)


def learn_names(log: EventLog, vocabulary: dict[str, str]) -> dict[str, str]:
    """Decide, per name, whether collapsing it is worth what it hides.

    Collapsing is only ever justified by what it *merges*. On this export the
    email-address rule folds 29 names into one, which is the difference between a
    route table and a list of records. The digit rule folds nothing -- every
    group it produces has exactly one member -- and it costs real signal:

        Provider returned 429, backing off  ->  Provider returned {n}, backing off
        queue depth metric recorded depth=0 ->  queue depth metric recorded depth={n}

    The second is the worst of them. A gauge hardcoded to zero is a finding, and
    the collapse hides precisely the zero. The first hides the status code that
    says what went wrong.

    So a collapse is applied only where more than one name lands on it. Same rule
    the vocabulary learner already uses: substitute what varies, keep what does
    not.

    This is the second of two guards and they answer different questions.
    `collapse` decides what *could* be a value; this decides whether hiding it
    buys anything. A version number fails the first test and would fail this one
    too on today's data -- but only by luck, because there is one version. The
    letter-glued rule is what makes it right for the reason rather than by
    accident.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for event in log.events:
        if event.kind == "deploy":
            continue
        substituted = substitute(event.name, vocabulary)
        seen[collapse(substituted)].add(substituted)

    names: dict[str, str] = {}
    for collapsed, originals in seen.items():
        for original in originals:
            names[original] = collapsed if len(originals) > 1 else original
    return names


@dataclass
class Route:
    """One distinct path, and the journeys that took it."""

    index: int
    nodes: tuple[str, ...]
    journeys: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.journeys)

    @property
    def ends_at(self) -> str:
        return self.nodes[-1] if self.nodes else ""

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for node in self.nodes:
            source = node.split(":", 1)[0]
            if source not in seen:
                seen.append(source)
        return seen

    @property
    def repeats(self) -> list[str]:
        """Nodes this route visits more than once — a duplicate, whatever the
        work at that node happens to be."""
        return sorted(n for n, c in Counter(self.nodes).items() if c > 1)

    def as_dict(self, exemplars: int = 3) -> dict:
        payload = {
            "route": f"route-{self.index}",
            "journeys": self.count,
            "nodes": list(self.nodes),
            "ends_at": self.ends_at,
            "services": self.sources,
            "exemplars": self.journeys[:exemplars],
        }
        if self.repeats:
            payload["visited_twice"] = self.repeats
        return payload


@dataclass
class Routes:
    vocabulary: dict[str, str]
    names: dict[str, str] = field(default_factory=dict)
    routes: list[Route] = field(default_factory=list)

    def node_of(self, event: Event) -> str:
        """Where in the system this happened, variable parts removed.

        Only the parts that genuinely vary: `names` was learned by checking which
        collapses merge anything, so a number that never changes stays visible.
        """
        substituted = substitute(event.name, self.vocabulary)
        return f"{event.source}:{self.names.get(substituted, substituted)}"

    def path_of(self, journey: Journey) -> tuple[str, ...]:
        sequence: list[str] = []
        for event in journey.events:
            if event.kind == "deploy":
                continue
            node = self.node_of(event)
            # Consecutive repeats are one visit; a return after going elsewhere
            # is a second, which is how a redelivery stays visible.
            if not sequence or sequence[-1] != node:
                sequence.append(node)
        return tuple(sequence)

    @property
    def dominant(self) -> Route | None:
        return self.routes[0] if self.routes else None

    @property
    def total(self) -> int:
        return sum(r.count for r in self.routes)

    def of_journey(self, value: str) -> Route | None:
        return next((r for r in self.routes if value in r.journeys), None)

    def by_index(self, index: int) -> Route | None:
        return next((r for r in self.routes if r.index == index), None)

    def as_dict(self) -> dict:
        return {
            "note": (
                "Every distinct path journeys took, most common first. A route "
                "that ends earlier than the others is where journeys stopped; a "
                "route with a repeated node did that work twice."
            ),
            "vocabulary": dict(sorted(self.vocabulary.items())),
            "collapsed_names": sorted({v for k, v in self.names.items() if k != v}),
            "total_journeys": self.total,
            "routes": [r.as_dict() for r in self.routes],
        }


def build(log: EventLog, journeys: dict[str, Journey]) -> Routes:
    vocabulary = learn_vocabulary(log)
    result = Routes(vocabulary=vocabulary, names=learn_names(log, vocabulary))

    paths: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for value, journey in journeys.items():
        paths[result.path_of(journey)].append(value)

    ordered = sorted(paths.items(), key=lambda item: (-len(item[1]), item[0]))
    result.routes = [
        Route(index=position, nodes=nodes, journeys=sorted(values))
        for position, (nodes, values) in enumerate(ordered, 1)
    ]
    return result


def common_prefix(routes: Routes) -> tuple[str, ...]:
    """The opening every route shares."""
    paths = [r.nodes for r in routes.routes]
    if len(paths) < 2:
        return ()
    shared: list[str] = []
    for step in zip(*paths):
        if len(set(step)) != 1:
            break
        shared.append(step[0])
    return tuple(shared)


def render(routes: Routes, width: int = 96) -> list[str]:
    """The table as text, for the terminal and for the prompt.

    The shared opening is printed once. Without that, five routes that agree for
    eight nodes and diverge at the ninth all truncate before the divergence, and
    the table shows nothing — which is the only thing it exists to show.
    """
    shared = common_prefix(routes)
    lines: list[str] = []
    if shared:
        lines.append(
            "all journeys start: "
            + " -> ".join(n.split(":", 1)[-1] for n in shared)
        )
        lines.append("")

    for route in routes.routes:
        rest = [n.split(":", 1)[-1] for n in route.nodes[len(shared) :]]
        path = " -> ".join(rest) or "(ends here)"
        # Elide the middle, never the tail. Routes diverge at the end far more
        # often than at the start, and the end is where a journey stopped.
        if len(path) > width and len(rest) > 3:
            path = " -> ".join([rest[0], f"...{len(rest) - 3} more...", rest[-2], rest[-1]])
        marks = " (x2) visits a node twice" if route.repeats else ""
        lines.append(
            f"{route.index:>3}. {route.count:>4}  ...-> {path}{marks}"
        )
    return lines
