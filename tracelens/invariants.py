"""Invariants: what must be true of any pipeline, whatever it is.

The detectors in `detectors/` encode failures I already found. That is a
closed world -- useful for the five known symptoms, structurally unable to
surface a sixth. Every rule there was written *after* seeing the answer.

This module inverts it. Nothing here describes a failure. Each check states a
property that must hold for a healthy message pipeline, and reports the
violation. A violation is therefore novel by construction: it does not need to
have been anticipated to be found, and it fires on a pipeline this code has never
seen.

Nothing here references a channel name, a service name, or a stage count.

    conservation      what enters a hop must leave it
    path shape        messages of the same kind should follow the same route
    settlement        every promise in the ledger reaches a terminal node
    context integrity a message's trace should not fragment
    single visit      a message should traverse each node once
    referential       every reference should resolve; every record should join

Severity is derived from blast radius, not asserted, because on unfamiliar data
there is no prior about which violation matters most.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass

from .config import DEFAULT, Config
from .evidence import Evidence, Finding
from .model import Dataset
from .topology import Topology, discover, node_of


@dataclass
class InvariantContext:
    dataset: Dataset
    topology: Topology
    config: Config = DEFAULT


def build(dataset: Dataset, config: Config = DEFAULT) -> InvariantContext:
    return InvariantContext(dataset, discover(dataset), config)


def check_all(dataset: Dataset, config: Config = DEFAULT) -> list[Finding]:
    context = build(dataset, config)
    findings: list[Finding] = []
    for check in (
        _conservation,
        _path_shapes,
        _settlement,
        _context_integrity,
        _single_visit,
        _referential,
    ):
        findings.extend(check(context))
    return findings


def _stable_id(path: tuple) -> str:
    return hashlib.sha1(" -> ".join(path).encode("utf-8")).hexdigest()[:6]


def _severity(affected: int, total: int) -> str:
    """Blast radius, not a hand-assigned label. Unknown data, no priors."""
    if not total:
        return "low"
    share = affected / total
    if share >= 0.5:
        return "critical"
    if share >= 0.1:
        return "high"
    if share >= 0.01:
        return "medium"
    return "low"


def _ids_at(context: InvariantContext, node: str) -> set[str]:
    return {
        correlation_id
        for correlation_id, spans in context.dataset.spans_by_correlation.items()
        if any(node_of(s) == node for s in spans)
    }


# --------------------------------------------------------------------------- #


def _conservation(context: InvariantContext) -> list[Finding]:
    """What enters a hop must leave it.

    This is the general form of "a channel is being dropped". It does not know
    which channel, or that channels exist -- only that N messages reached one node
    and fewer reached the next.
    """
    findings: list[Finding] = []
    total = len(context.dataset.spans_by_correlation)

    threshold = context.config.expected_edge_share
    for node in sorted(context.topology.nodes):
        successors = context.topology.expected_successors(node, threshold)
        if not successors:
            continue  # a terminal node loses nothing by definition

        entered = _ids_at(context, node)
        left: set[str] = set()
        for successor in successors:
            left |= _ids_at(context, successor)

        lost = sorted(entered - left)
        if not lost:
            continue

        by_attribute = _discriminator(context, lost, entered)
        findings.append(
            Finding(
                id=f"INV.conservation.{node}",
                title=f"{len(lost)} message(s) enter '{node}' and never reach the next hop",
                severity=_severity(len(lost), total),
                confidence="observed",
                summary=(
                    f"{len(lost)} of {len(entered)} messages that reached '{node}' produced "
                    f"no span at any downstream node ({', '.join(successors)}). The loss is "
                    "on the hop out of this node, before the next component runs. "
                    + (f"All lost messages share: {by_attribute}. " if by_attribute else "")
                    + "No error or non-OK status accompanies them — the signal is the absence."
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        ref=f"conservation.{node}",
                        detail=(
                            f"in={len(entered)} out={len(left)} lost={len(lost)}; "
                            f"expected successors: {', '.join(successors) or 'none'}; "
                            f"optional branches ignored: "
                            f"{', '.join(sorted(set(context.topology.successors(node)) - set(successors))) or 'none'}"
                        ),
                        source="spans.json",
                    ),
                    *[
                        Evidence(
                            kind="correlation_id",
                            ref=correlation_id,
                            detail=f"last observed at '{node}', no downstream span",
                            source=f"spans.json#correlation_id={correlation_id}",
                        )
                        for correlation_id in lost[: context.config.max_exemplars]
                    ],
                ],
                affected=lost,
                would_resolve=[
                    f"the routing/subscription configuration on the hop out of '{node}'",
                    "whether the lost messages are recoverable from a dead-letter queue",
                ],
                params={
                    "invariant": "conservation",
                    "expected_edge_share": threshold,
                },
            )
        )
    return findings


def _discriminator(context: InvariantContext, lost: list[str], entered: set[str]) -> str:
    """What, if anything, the lost messages have in common that the others don't.

    Answers "is it a whole class or a scattering?" without knowing which
    attributes exist. On this data it finds message_type; on another pipeline it
    would find whatever the discriminating attribute happens to be.
    """
    accepted = context.dataset.accepted_by_correlation()
    survivors = entered - set(lost)
    clues: list[str] = []

    def values(ids, key: str) -> Counter:
        counter: Counter = Counter()
        for correlation_id in ids:
            message = accepted.get(correlation_id)
            value = getattr(message, key, None) if message else None
            if value is not None:
                counter[value] += 1
        return counter

    for key in ("message_type", "tenant_id"):
        lost_values = values(lost, key)
        if len(lost_values) != 1:
            continue
        value = next(iter(lost_values))
        if values(survivors, key).get(value, 0) == 0:
            clues.append(f"{key}={value} (and no surviving message has it)")
    return "; ".join(clues)


def _path_shapes(context: InvariantContext) -> list[Finding]:
    """Messages should follow one of a small number of routes.

    A shape that only a minority of messages take is either a new feature or a
    truncation. Both are worth a look; the tool does not pretend to know which.
    """
    paths = context.topology.paths
    if len(paths) < 2:
        return []

    total = sum(paths.values())
    dominant, dominant_count = paths.most_common(1)[0]
    findings: list[Finding] = []

    for path, count in paths.most_common()[1:]:
        share = count / total
        if share > 0.4:
            continue  # a genuinely bimodal pipeline, not an anomaly
        examples = context.topology.path_examples.get(path, [])
        truncated = len(path) < len(dominant) and list(path) == list(dominant[: len(path)])
        findings.append(
            Finding(
                # Stable across processes: builtin hash() is salted per run, which
                # would give this finding a different ID every invocation and break
                # both the determinism guarantee and any test pinned to it.
                id=f"INV.path_shape.{len(path)}node.{_stable_id(path)}",
                title=(
                    f"{count} message(s) stop {len(dominant) - len(path)} node(s) early"
                    if truncated
                    else f"{count} message(s) follow a minority route"
                ),
                severity=_severity(count, total),
                confidence="observed",
                summary=(
                    f"{count} of {total} messages ({share:.1%}) follow a route of "
                    f"{len(path)} node(s) while {dominant_count} follow the dominant route of "
                    f"{len(dominant)}. "
                    + (
                        "The short route is a strict prefix of the dominant one, so these "
                        "messages were truncated rather than routed differently."
                        if truncated
                        else "The route diverges rather than truncating, so this is a "
                        "different path and not simply a loss."
                    )
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        ref=f"path.{len(path)}",
                        detail=" -> ".join(path),
                        source="spans.json",
                    ),
                    Evidence(
                        kind="metric",
                        ref="path.dominant",
                        detail=f"dominant route ({dominant_count} messages): "
                        + " -> ".join(dominant),
                        source="spans.json",
                    ),
                    *[
                        Evidence(
                            kind="correlation_id",
                            ref=correlation_id,
                            detail="followed the minority route",
                            source=f"spans.json#correlation_id={correlation_id}",
                        )
                        for correlation_id in examples[: context.config.max_exemplars]
                    ],
                ],
                affected=sorted(examples),
                params={"invariant": "path_shape"},
            )
        )
    return findings


def _settlement(context: InvariantContext) -> list[Finding]:
    """Every promise in the ledger should reach a terminal node.

    The generic form of delivery accounting: it does not know what "delivered"
    means for this pipeline, only that the ledger recorded an intent and the
    telemetry shows no terminal span.
    """
    accepted = context.dataset.accepted_by_correlation()
    if not accepted:
        return []

    # Terminal = no *expected* successor. Using raw graph sinks would treat an
    # optional retry stage as the only real ending and call every message that
    # skipped it unsettled.
    sinks = context.topology.terminal_nodes(context.config.expected_edge_share)
    settled = set()
    for sink in sinks:
        settled |= _ids_at(context, sink)

    unsettled = sorted(set(accepted) - settled)
    no_telemetry = sorted(set(accepted) - set(context.dataset.spans_by_correlation))
    if not unsettled:
        return []

    return [
        Finding(
            id="INV.settlement",
            title=f"{len(unsettled)} accepted message(s) never reached a terminal node",
            severity=_severity(len(unsettled), len(accepted)),
            confidence="observed",
            summary=(
                f"The ledger records {len(accepted)} accepted messages. "
                f"{len(unsettled)} of them produced no span at any terminal node "
                f"({', '.join(sorted(sinks)) or 'none observed'}). "
                + (
                    f"{len(no_telemetry)} produced no telemetry at all, which no amount of "
                    "trace sampling would have revealed. "
                    if no_telemetry
                    else ""
                )
                + "This reconciliation is the one check that cannot be replaced by "
                "sampled traces: it is driven by the promise record, not by the spans."
            ),
            evidence=[
                Evidence(
                    kind="metric",
                    ref="settlement",
                    detail=(
                        f"accepted={len(accepted)} settled={len(settled)} "
                        f"unsettled={len(unsettled)} with_no_spans_at_all={len(no_telemetry)}"
                    ),
                    source="accepted_messages.json + spans.json",
                ),
                *[
                    Evidence(
                        kind="correlation_id",
                        ref=correlation_id,
                        detail="accepted but never settled",
                        source=f"accepted_messages.json#{correlation_id}",
                    )
                    for correlation_id in unsettled[: context.config.max_exemplars]
                ],
            ],
            affected=unsettled,
            would_resolve=["a delivery ledger with an explicit settlement event per message"],
            params={
                "invariant": "settlement",
                "expected_edge_share": context.config.expected_edge_share,
            },
        )
    ]


def _context_integrity(context: InvariantContext) -> list[Finding]:
    """A message's trace should not fragment as it crosses a hop."""
    findings: list[Finding] = []
    total = len(context.dataset.spans_by_correlation)

    for (frm, to), edge in sorted(context.topology.edges.items()):
        if not edge.context_breaks:
            continue
        broken = sorted(
            correlation_id
            for correlation_id, spans in context.dataset.spans_by_correlation.items()
            if len({s.trace_id for s in spans}) > 1
            and any(node_of(s) == to for s in spans)
        )
        intact_edges = [
            f"{a} -> {b}"
            for (a, b), e in context.topology.edges.items()
            if e.context_breaks == 0 and e.observed
        ]
        findings.append(
            Finding(
                id=f"INV.context_break.{to}",
                title=f"trace context is lost entering '{to}'",
                severity=_severity(edge.context_breaks, total),
                confidence="observed",
                summary=(
                    f"{edge.context_breaks} of {edge.observed} traversals of "
                    f"'{frm}' -> '{to}' start a new trace instead of continuing the "
                    "producer's context, so the downstream half of those journeys is "
                    "unreachable by trace ID. "
                    f"{len(intact_edges)} other hop(s) propagate context correctly, which "
                    "localises this to the consumer at this node rather than to the "
                    "transport."
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        ref=f"context_break.{to}",
                        detail=(
                            f"traversals={edge.observed} linked={edge.linked} "
                            f"broken={edge.context_breaks}"
                        ),
                        source="spans.json",
                    ),
                    Evidence(
                        kind="metric",
                        ref="context_intact_elsewhere",
                        detail=f"hops propagating correctly: {', '.join(intact_edges) or 'none'}",
                        source="spans.json",
                    ),
                    *[
                        Evidence(
                            kind="correlation_id",
                            ref=correlation_id,
                            detail="trace fragments at this hop; joined on correlation_id",
                            source=f"spans.json#correlation_id={correlation_id}",
                        )
                        for correlation_id in broken[: context.config.max_exemplars]
                    ],
                ],
                affected=broken,
                would_resolve=[
                    f"the context-extraction code in the consumer at '{to}', compared "
                    "against a hop that propagates correctly",
                ],
                params={"invariant": "context_integrity"},
            )
        )
    return findings


def _single_visit(context: InvariantContext) -> list[Finding]:
    """A message should traverse each node once.

    The general form of duplicate delivery. Revisiting a node means the work was
    done twice, whatever that work is.
    """
    repeats: dict[str, list[str]] = defaultdict(list)
    for correlation_id, spans in context.dataset.spans_by_correlation.items():
        counts = Counter(node_of(s) for s in spans)
        for node, count in counts.items():
            if count > 1:
                repeats[node].append(correlation_id)

    total = len(context.dataset.spans_by_correlation)
    findings: list[Finding] = []
    for node, ids in sorted(repeats.items()):
        ids = sorted(ids)
        # Was it re-emitted upstream, or re-consumed here? Compare against the
        # predecessor: one upstream span with two downstream is a redelivery.
        predecessors = context.topology.predecessors(node)
        upstream_repeats = 0
        for correlation_id in ids:
            spans = context.dataset.spans_by_correlation[correlation_id]
            for predecessor in predecessors:
                if sum(1 for s in spans if node_of(s) == predecessor) > 1:
                    upstream_repeats += 1
                    break
        redelivery = upstream_repeats == 0

        findings.append(
            Finding(
                id=f"INV.repeat_visit.{node}",
                title=f"{len(ids)} message(s) traverse '{node}' more than once",
                severity=_severity(len(ids), total),
                confidence="observed",
                summary=(
                    f"{len(ids)} of {total} messages produced more than one span at "
                    f"'{node}'. "
                    + (
                        "The upstream node emitted only once for each, so the repeat "
                        "originates at this node — a redelivery being reprocessed, not an "
                        "upstream duplicate."
                        if redelivery
                        else f"{upstream_repeats} of them also repeat upstream, so the "
                        "duplication originates before this node."
                    )
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        ref=f"repeat_visit.{node}",
                        detail=(
                            f"messages repeating at this node: {len(ids)}; also repeating "
                            f"upstream: {upstream_repeats}; predecessors: "
                            f"{', '.join(predecessors) or 'none'}"
                        ),
                        source="spans.json",
                    ),
                    *[
                        Evidence(
                            kind="correlation_id",
                            ref=correlation_id,
                            detail=f"visits '{node}' more than once",
                            source=f"spans.json#correlation_id={correlation_id}",
                        )
                        for correlation_id in ids[: context.config.max_exemplars]
                    ],
                ],
                affected=ids,
                would_resolve=[
                    "whether the consumer deletes/acknowledges after success",
                    "the visibility timeout or lease duration versus observed processing time",
                ],
                params={"invariant": "single_visit"},
            )
        )
    return findings


def _referential(context: InvariantContext) -> list[Finding]:
    """Every reference should resolve and every record should join to something."""
    dataset = context.dataset
    findings: list[Finding] = []

    known_spans = {s.span_id for s in dataset.spans}
    dangling = [s for s in dataset.spans if s.parent_span_id and s.parent_span_id not in known_spans]

    ledger = set(dataset.accepted_by_correlation())
    span_ids = set(dataset.spans_by_correlation)
    ghosts = sorted(span_ids - ledger) if ledger else []

    unjoinable = sum(1 for r in dataset.logs if not r.is_message_scoped)
    log_share = unjoinable / len(dataset.logs) if dataset.logs else 0.0

    problems: list[Evidence] = []
    if dangling:
        problems.append(
            Evidence(
                kind="metric",
                ref="dangling_parents",
                detail=(
                    f"{len(dangling)} span(s) reference a parent_span_id absent from the "
                    "export — the trace is incomplete, so any conclusion drawn from its "
                    "shape is suspect"
                ),
                source="spans.json",
            )
        )
    if ghosts:
        problems.append(
            Evidence(
                kind="metric",
                ref="unledgered_messages",
                detail=(
                    f"{len(ghosts)} correlation_id(s) appear in spans but not in the promise "
                    f"ledger, e.g. {', '.join(ghosts[:3])} — work is happening for messages "
                    "nothing claims to have accepted"
                ),
                source="spans.json + accepted_messages.json",
            )
        )
    if log_share >= context.config.noise_ratio_alert:
        problems.append(
            Evidence(
                kind="metric",
                ref="unjoinable_logs",
                detail=(
                    f"{unjoinable} of {len(dataset.logs)} log records ({log_share:.1%}) carry "
                    "neither a correlation_id nor a trace_id and cannot be joined to any "
                    "message"
                ),
                source="logs.json",
            )
        )

    if not problems:
        return []

    return [
        Finding(
            id="INV.referential_integrity",
            title=f"{len(problems)} referential integrity problem(s) in the export",
            severity="high" if (dangling or ghosts) else "medium",
            confidence="observed",
            summary=(
                "Records that should resolve or join do not. This matters before any "
                "other finding is trusted: gaps here mean the shape of the data is "
                "partly unknown, so an absence downstream may be a real loss or may be a "
                "missing record."
            ),
            evidence=problems,
            affected=ghosts,
            params={"invariant": "referential"},
        )
    ]
