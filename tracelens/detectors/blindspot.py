"""D5 -- observability blind spots.

Three checks that all answer the same question: why did nobody notice?

(a) span status versus delivery reality
(b) log noise ratio and unjoinable share
(c) gauge metrics that are constant or undimensioned

This is the detector that explains the others. A pipeline where every span reports
OK and no log is at ERROR will look perfectly healthy on any dashboard built from
status or level -- through every incident in the window.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from ..evidence import Evidence, Finding

if TYPE_CHECKING:
    from . import DetectorContext

# Recognised operational chatter. Deliberately a denylist: an unrecognised log
# line is more likely to be interesting, not less, so anything unmatched stays
# visible. Patterns live here rather than in the filter logic so they can move to
# a config file without touching code.
NOISE_PATTERNS: list[tuple[str, str]] = [
    ("health_check", r"^GET /health"),
    ("queue_depth_gauge", r"^queue depth metric"),
    ("poll_chatter", r"^Polling queue"),
]

GAUGE_PATTERN = re.compile(r"^(?P<name>[a-z ]+?) (?:metric )?recorded (?P<dim>\w+)=(?P<value>\S+)")


def classify_log(message: str) -> str | None:
    for name, pattern in NOISE_PATTERNS:
        if re.match(pattern, message):
            return name
    return None


def detect(context: "DetectorContext") -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_status_divergence(context))
    findings.extend(_log_noise(context))
    findings.extend(_broken_gauges(context))
    return findings


def _status_divergence(context: "DetectorContext") -> list[Finding]:
    errors = context.health.errors
    if not errors.diverges:
        return []

    levels = Counter(log.level for log in context.dataset.logs)
    retry_spans = [
        a.send for t in context.traces.values() for a in t.attempts if a.send.retry_count
    ]

    evidence = [
        Evidence(
            kind="metric",
            ref="span_status_errors",
            detail=(
                f"{errors.span_status_errors} of {errors.total_spans} spans report a status "
                "other than OK"
            ),
            source="spans.json",
        ),
        Evidence(
            kind="metric",
            ref="error_logs",
            detail=(
                f"{levels.get('ERROR', 0)} of {len(context.dataset.logs)} log records are at "
                f"ERROR (levels present: "
                + ", ".join(f"{k}={v}" for k, v in sorted(levels.items()))
                + ")"
            ),
            source="logs.json",
        ),
        Evidence(
            kind="metric",
            ref="delivery_failures",
            detail=(
                f"{errors.delivery_failures} of {errors.accepted} accepted messages never "
                f"reached a provider, and {errors.provider_errors} of {errors.provider_calls} "
                "provider calls returned a non-2xx status"
            ),
            source="accepted_messages.json + spans.json",
        ),
    ]
    if retry_spans:
        total_retries = sum(s.retry_count for s in retry_spans)
        evidence.append(
            Evidence(
                kind="metric",
                ref="invisible_retries",
                detail=(
                    f"{total_retries} retries across {len(retry_spans)} span(s) — recorded only "
                    "as a retry_count attribute, with no child span and no error status"
                ),
                source="spans.json",
            )
        )

    return [
        Finding(
            id="D5.status_divergence",
            title="Span status and log level report health the delivery record contradicts",
            severity="high",
            confidence="observed",
            summary=(
                f"Every one of {errors.total_spans} spans reports status OK and "
                f"{levels.get('ERROR', 0)} log records are at ERROR — including the spans that "
                "retried against a throttling provider and the ones that sent a duplicate. "
                f"Meanwhile {errors.delivery_failures} accepted messages never reached a "
                "provider. Any alert, SLO, or dashboard built on span status or log level "
                "shows this pipeline as healthy through every incident in this window. A "
                "dropped message produces no error at all — only an absence of spans, and "
                "absence pages nobody."
            ),
            evidence=evidence,
            affected=sorted(context.accounting.stopped_ids()),
            would_resolve=[],
            params={},
        )
    ]


def _log_noise(context: "DetectorContext") -> list[Finding]:
    logs = context.dataset.logs
    if not logs:
        return []

    categories: Counter = Counter()
    for log in logs:
        categories[classify_log(log.message) or "message_scoped_or_unknown"] += 1

    unjoinable = sum(1 for log in logs if not log.is_message_scoped)
    scoped = len(logs) - unjoinable
    ratio = unjoinable / len(logs)
    if ratio < context.config.noise_ratio_alert:
        return []

    evidence = [
        Evidence(
            kind="metric",
            ref="unjoinable_share",
            detail=(
                f"{unjoinable} of {len(logs)} log records ({ratio:.1%}) carry neither a "
                f"correlation_id nor a trace_id and cannot be joined to anything. "
                f"Message-scoped records: {scoped} ({scoped / len(logs):.1%})."
            ),
            source="logs.json",
        )
    ]
    for name, count in categories.most_common():
        if name == "message_scoped_or_unknown":
            continue
        evidence.append(
            Evidence(
                kind="metric",
                ref=f"noise.{name}",
                detail=f"{name}: {count} records ({count / len(logs):.1%} of volume)",
                source="logs.json",
            )
        )

    suppressible = sum(c for n, c in categories.items() if n != "message_scoped_or_unknown")
    return [
        Finding(
            id="D5.log_noise",
            title=f"{ratio:.1%} of log volume cannot be joined to a message",
            severity="medium",
            confidence="observed",
            summary=(
                f"{unjoinable} of {len(logs)} records have no correlation_id and no trace_id. "
                f"{suppressible} of them match known operational patterns "
                f"({', '.join(n for n, _ in categories.most_common() if n != 'message_scoped_or_unknown')}) "
                f"and are suppressible at emission, leaving {scoped} useful lines for the whole "
                "window. Grep is not being used wrong — there is almost nothing to grep. Note "
                "the multiplier: evidence for other findings is buried in those few useful "
                "lines."
            ),
            evidence=evidence,
            affected=[],
            would_resolve=[],
            params={"noise_ratio_alert": context.config.noise_ratio_alert},
        )
    ]


def _broken_gauges(context: "DetectorContext") -> list[Finding]:
    """A gauge wearing a log's clothing, and a broken one at that."""
    gauges: dict[str, list] = {}
    for log in context.dataset.logs:
        match = GAUGE_PATTERN.match(log.message)
        if match:
            gauges.setdefault(match.group("name").strip(), []).append((log, match))

    findings: list[Finding] = []
    for name, entries in sorted(gauges.items()):
        values = {m.group("value") for _, m in entries}
        dimensions = {k for log, _ in entries for k in log.attributes}
        services = Counter(log.service for log, _ in entries)
        if len(values) > 1 and dimensions:
            continue  # a working gauge

        problems = []
        if len(values) == 1:
            problems.append(f"every one of {len(entries)} records reports the same value ({values.pop()})")
        if not dimensions:
            problems.append("no dimension label on any record, so per-queue values cannot be distinguished")
        if all(log.trace_id is None for log, _ in entries):
            problems.append("trace_id is null on every record, so it joins to nothing")

        findings.append(
            Finding(
                id=f"D5.broken_gauge.{name.replace(' ', '_')}",
                title=f"'{name}' is emitted {len(entries)} times and measures nothing",
                severity="high",
                confidence="observed",
                summary=(
                    f"'{name}' appears {len(entries)} times ("
                    + ", ".join(f"{s} x{c}" for s, c in sorted(services.items()))
                    + "). "
                    + "; ".join(problems).capitalize()
                    + ". This is the one signal that would have shown a per-queue backlog — "
                    "or the absence of a queue — directly. Instead it costs storage, "
                    f"contributes {len(entries) / len(context.dataset.logs):.1%} of log volume, "
                    "and reports nothing."
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        ref=f"gauge.{name.replace(' ', '_')}",
                        detail=(
                            f"{len(entries)} records, {len(values) if values else 1} distinct "
                            f"value(s), attribute keys present: {sorted(dimensions) or 'none'}"
                        ),
                        source="logs.json",
                    ),
                    Evidence(
                        kind="log",
                        ref=f"gauge.{name.replace(' ', '_')}.example",
                        detail=f"example record: '{entries[0][0].message}' attributes={entries[0][0].attributes}",
                        source="logs.json",
                    ),
                ],
                affected=[],
                would_resolve=[],
                params={},
            )
        )
    return findings
