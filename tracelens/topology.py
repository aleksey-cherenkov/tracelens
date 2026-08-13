"""Discover the pipeline graph from the data instead of hardcoding it.

`model.py` names seven stages because that is what *this* pipeline has. That is
fine for rendering one trace and wrong as a foundation: add a stage, a channel or
a service and a hardcoded taxonomy is blind to it. Worse, it can only recognise
failures in the shape it already knows.

Nothing in this module knows the words "email", "sms", "push", "comms-ingest", or
how many stages there should be. Point it at a different export and it learns that
pipeline instead.

The trick that makes it channel-independent: span names embed attribute values
("publish email-queue", "send sms", "route push"). Substituting the value back out
gives a template -- "publish {message_type}-queue" -- so three channels collapse
onto one node, and a fourth channel added tomorrow lands on that same node rather
than looking like something new.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .model import Dataset, Span

# Identity-like attributes only. Templating on a status code would erase signal.
TEMPLATED_ATTRIBUTES = ("message_type", "tenant_id", "provider")


def templatize(span: Span) -> str:
    """'publish email-queue' + message_type=email -> 'publish {message_type}-queue'."""
    name = span.name
    for key in TEMPLATED_ATTRIBUTES:
        value = span.attributes.get(key)
        if isinstance(value, str) and value and value in name:
            name = name.replace(value, "{" + key + "}")
    return name


def node_of(span: Span) -> str:
    return f"{span.service}:{templatize(span)}"


@dataclass
class Edge:
    frm: str
    to: str
    observed: int = 0
    linked: int = 0
    """Resolved by parent/child rather than by time-order fallback."""

    @property
    def context_breaks(self) -> int:
        return self.observed - self.linked


@dataclass
class Topology:
    nodes: dict[str, int] = field(default_factory=dict)
    """node -> distinct messages observed at it."""

    edges: dict[tuple[str, str], Edge] = field(default_factory=dict)
    entry_nodes: set[str] = field(default_factory=set)
    paths: Counter = field(default_factory=Counter)
    """Ordered node sequence -> how many messages followed it."""

    path_examples: dict[tuple, list[str]] = field(default_factory=dict)

    def successors(self, node: str) -> list[str]:
        return [to for (frm, to) in self.edges if frm == node]

    def predecessors(self, node: str) -> list[str]:
        return [frm for (frm, to) in self.edges if to == node]

    def expected_successors(self, node: str, threshold: float = 0.5) -> list[str]:
        """Successors most messages actually go on to reach.

        The distinction that matters: a retry hop taken by 15% of messages is
        *optional*, and a message skipping it has lost nothing. A hop taken by 90%
        is *expected*, and a message skipping it has stopped. Without this,
        any optional trailing stage makes every message that skips it look
        dropped -- which is a false 'silent loss' on the most alarming finding
        the tool produces.

        Note the threshold cannot be near 1.0: the drop being detected itself
        drags the edge's share down. Half is the honest dividing line between
        "the normal route" and "a branch".
        """
        entered = self.nodes.get(node, 0)
        if not entered:
            return []
        return sorted(
            to
            for (frm, to), edge in self.edges.items()
            if frm == node and self.nodes.get(to, 0) / entered >= threshold
        )

    def terminal_nodes(self, threshold: float = 0.5) -> set[str]:
        """Nodes where a journey legitimately ends -- no expected successor."""
        return {n for n in self.nodes if not self.expected_successors(n, threshold)}

    @property
    def sinks(self) -> set[str]:
        return {n for n in self.nodes if not self.successors(n)}

    @property
    def dominant_path(self) -> tuple:
        return self.paths.most_common(1)[0][0] if self.paths else ()

    def as_dict(self) -> dict:
        return {
            "nodes": dict(sorted(self.nodes.items())),
            "edges": {
                f"{a} -> {b}": {"observed": e.observed, "context_breaks": e.context_breaks}
                for (a, b), e in sorted(self.edges.items())
            },
            "entry_nodes": sorted(self.entry_nodes),
            "paths": {
                " -> ".join(p): n for p, n in sorted(self.paths.items(), key=lambda kv: -kv[1])
            },
        }


def discover(dataset: Dataset) -> Topology:
    """Build the observed graph in one pass. No assumptions about depth or width."""
    topology = Topology()
    node_messages: dict[str, set[str]] = defaultdict(set)

    for correlation_id, spans in dataset.spans_by_correlation.items():
        ordered = sorted(spans, key=lambda s: (s.start_time, s.span_id))
        by_id = {s.span_id: s for s in ordered}

        sequence: list[str] = []
        for span in ordered:
            node = node_of(span)
            node_messages[node].add(correlation_id)
            if node not in sequence:
                sequence.append(node)

        if ordered:
            topology.entry_nodes.add(node_of(ordered[0]))

        # Prefer the real parent link; fall back to time-order adjacency, which is
        # exactly what a broken trace context leaves behind.
        for index, span in enumerate(ordered):
            parent = by_id.get(span.parent_span_id) if span.parent_span_id else None
            if parent is not None:
                frm, linked = node_of(parent), True
            elif index > 0:
                frm, linked = node_of(ordered[index - 1]), False
            else:
                continue
            to = node_of(span)
            if frm == to:
                continue  # a redelivery re-entering the same node
            edge = topology.edges.setdefault((frm, to), Edge(frm, to))
            edge.observed += 1
            edge.linked += int(linked)

        path = tuple(sequence)
        topology.paths[path] += 1
        topology.path_examples.setdefault(path, []).append(correlation_id)

    topology.nodes = {node: len(ids) for node, ids in sorted(node_messages.items())}
    return topology


def log_template(message: str) -> str:
    """Collapse a log line to its shape.

    Crude on purpose: this is the cheap deterministic version that runs
    everywhere, including in tests. The production version is the nightly LLM
    classification job (DESIGN section 10.5).
    """
    out: list[str] = []
    for token in message.split():
        if "@" in token:
            key, sep, _ = token.partition("=")
            out.append(f"{key}={{addr}}" if sep else "{addr}")
        elif any(character.isdigit() for character in token):
            key, sep, _ = token.partition("=")
            out.append(f"{key}={{n}}" if sep else "{n}")
        else:
            out.append(token)
    return " ".join(out)


def profile(dataset: Dataset, topology: Topology | None = None) -> dict:
    """A fingerprint of what this pipeline looks like right now.

    Shapes and cardinalities, never counts or rates: volume legitimately changes
    between environments and over time. What must not change silently is the
    *set* of things -- nodes, edges, status values, attribute keys, log templates.
    That is what a novelty diff compares.
    """
    topology = topology or discover(dataset)
    return {
        "services": sorted({s.service for s in dataset.spans}),
        "nodes": sorted(topology.nodes),
        "edges": sorted(f"{a} -> {b}" for (a, b) in topology.edges),
        "entry_nodes": sorted(topology.entry_nodes),
        "path_shapes": sorted(" -> ".join(p) for p in topology.paths),
        "span_kinds": sorted({s.kind for s in dataset.spans}),
        "span_statuses": sorted({s.status for s in dataset.spans}),
        "provider_statuses": sorted(
            {
                str(s.attributes[key])
                for s in dataset.spans
                for key in ("provider.status_code", "provider.final_status_code")
                if s.attributes.get(key) is not None
            }
        ),
        "span_attribute_keys": sorted({k for s in dataset.spans for k in s.attributes}),
        "log_attribute_keys": sorted({k for r in dataset.logs for k in r.attributes}),
        "log_levels": sorted({r.level for r in dataset.logs}),
        "log_templates": sorted({log_template(r.message) for r in dataset.logs}),
        "channels": sorted({m.message_type for m in dataset.accepted}),
        "services_deployed": sorted({d.service for d in dataset.deploys}),
    }


def diff_profiles(baseline: dict, current: dict) -> dict[str, dict[str, list]]:
    """What appeared and what vanished, per dimension.

    Both directions matter. Something new is the usual suspect during an incident;
    something that *stopped* appearing is how a silently disabled code path looks.
    """
    changes: dict[str, dict[str, list]] = {}
    for key in sorted(set(baseline) | set(current)):
        before = set(baseline.get(key) or [])
        after = set(current.get(key) or [])
        appeared, vanished = sorted(after - before), sorted(before - after)
        if appeared or vanished:
            changes[key] = {"appeared": appeared, "vanished": vanished}
    return changes
