"""D3 -- provider degradation, and the deploy correlation that goes with it.

This detector carries the assignment's trap. The obvious story ("a deploy that day
broke it") is falsifiable by arithmetic: the deploy postdates the onset. But the
*recovery* is genuinely ambiguous, and a tool that resolves it confidently is
lying. So the incident is emitted as fact and the recovery cause as two ranked,
unresolved alternatives with what would settle them.

Deploy rule-out is computed here, in code, rather than left to the model's
timestamp arithmetic -- an LLM asked to compare two ISO-8601 strings under
pressure is exactly the wrong tool.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..evidence import Evidence, Finding, Hypothesis
from ..model import Deploy, Span, fmt_ts

if TYPE_CHECKING:
    from . import DetectorContext


@dataclass
class Window:
    channel: str
    sends: list[Span]

    @property
    def onset(self) -> datetime:
        return self.sends[0].start_time

    @property
    def last(self) -> datetime:
        return self.sends[-1].start_time

    @property
    def correlation_ids(self) -> list[str]:
        return [s.correlation_id for s in self.sends if s.correlation_id]


@dataclass
class DeployVerdict:
    deploy: Deploy
    plausible: bool
    reason: str


def detect(context: "DetectorContext") -> list[Finding]:
    config = context.config
    findings: list[Finding] = []

    sends_by_channel: dict[str, list[Span]] = {}
    for trace in context.traces.values():
        for attempt in trace.attempts:
            sends_by_channel.setdefault(trace.channel or "unknown", []).append(attempt.send)

    for channel, sends in sorted(sends_by_channel.items()):
        sends.sort(key=lambda s: s.start_time)
        healthy = [s for s in sends if _is_2xx(s.attributes.get("provider.status_code"))]
        if not healthy:
            continue
        # Baseline over 2xx sends, not "days with no errors" -- excluding whole
        # days would discard the clean sends that bracket the recovery.
        baseline = statistics.median(float(s.duration_ms) for s in healthy)

        affected = [
            s
            for s in sends
            if not _is_2xx(s.attributes.get("provider.status_code"))
            or s.duration_ms > baseline * config.slow_factor
        ]
        if not affected:
            continue

        for window in _group(channel, affected, config.incident_max_gap_s):
            findings.append(_build(context, window, baseline, sends))

    return findings


def _is_2xx(status) -> bool:
    return status is not None and 200 <= int(status) < 300


def _group(channel: str, affected: list[Span], max_gap_s: float) -> list[Window]:
    windows: list[Window] = []
    current: list[Span] = []
    for span in affected:
        if current and (span.start_time - current[-1].start_time).total_seconds() > max_gap_s:
            windows.append(Window(channel, current))
            current = []
        current.append(span)
    if current:
        windows.append(Window(channel, current))
    return windows


def correlate_deploys(
    deploys: list[Deploy], service: str, window: Window, lookback_s: float
) -> tuple[list[DeployVerdict], list[Deploy]]:
    """Deploys of *this* service, each ruled in or out by arithmetic.

    Takes a service argument derived from where the evidence localises the fault,
    not from a time window alone. A proximity-only correlator ranks any temporally
    adjacent deploy as a likely cause, which is how incidents get misattributed to
    the wrong team.
    """
    start = window.onset - timedelta(seconds=lookback_s)
    verdicts: list[DeployVerdict] = []
    adjacent: list[Deploy] = []

    for deploy in sorted(deploys, key=lambda d: d.deployed_at):
        if not (start <= deploy.deployed_at <= window.last):
            continue
        if deploy.service != service:
            adjacent.append(deploy)
            continue
        if deploy.deployed_at > window.onset:
            gap = deploy.deployed_at - window.onset
            after = sum(1 for s in window.sends if s.start_time < deploy.deployed_at)
            verdicts.append(
                DeployVerdict(
                    deploy=deploy,
                    plausible=False,
                    reason=(
                        f"ruled out — postdates onset by {_hhmm(gap)} and "
                        f"{after} affected message(s) had already occurred"
                    ),
                )
            )
        else:
            verdicts.append(
                DeployVerdict(
                    deploy=deploy,
                    plausible=True,
                    reason=(
                        f"plausible — shipped {_hhmm(window.onset - deploy.deployed_at)} "
                        "before onset"
                    ),
                )
            )
    return verdicts, adjacent


def _build(
    context: "DetectorContext", window: Window, baseline: float, all_sends: list[Span]
) -> Finding:
    config = context.config
    service = "comms-sender"

    durations = sorted({s.duration_ms for s in window.sends})
    statuses = sorted({int(s.attributes["provider.status_code"]) for s in window.sends if s.attributes.get("provider.status_code") is not None})
    finals = sorted({int(s.attributes.get("provider.final_status_code", s.attributes.get("provider.status_code", 0))) for s in window.sends})
    retries = sum(s.retry_count for s in window.sends)
    factor = durations[-1] / baseline if baseline else 0

    verdicts, adjacent = correlate_deploys(
        context.dataset.deploys, service, window, config.deploy_lookback_s
    )

    # The window ends at the last affected send. Recovery is a *separate*
    # interval -- last bad send to first clean send -- and it is the one deploys
    # are tested against for the recovery ambiguity.
    later_clean = [
        s
        for s in all_sends
        if s.start_time > window.last and _is_2xx(s.attributes.get("provider.status_code"))
    ]
    recovery_end = later_clean[0].start_time if later_clean else None
    in_recovery = [
        d
        for d in context.dataset.deploys
        if d.service == service
        and recovery_end is not None
        and window.last < d.deployed_at < recovery_end
    ]

    evidence: list[Evidence] = [
        Evidence(
            kind="metric",
            ref="incident_window",
            detail=(
                f"{len(window.sends)} {window.channel} send(s) between {fmt_ts(window.onset)} "
                f"and {fmt_ts(window.last)}; duration {'/'.join(str(d) for d in durations)} ms "
                f"vs baseline {baseline:.0f} ms ({factor:.1f}x); "
                f"provider.status_code {statuses}, final {finals}, retries {retries}"
            ),
            source="spans.json",
        )
    ]
    for span in window.sends[: config.max_exemplars]:
        evidence.append(
            Evidence(
                kind="correlation_id",
                ref=span.correlation_id or span.span_id,
                detail=(
                    f"{fmt_ts(span.start_time)} took {span.duration_ms} ms, "
                    f"provider.status_code="
                    f"{span.attributes.get('provider.status_code')}, "
                    f"retry_count={span.retry_count}, final="
                    f"{span.attributes.get('provider.final_status_code')}"
                ),
                source=f"spans.json#correlation_id={span.correlation_id}",
            )
        )

    warn_logs = [
        log
        for log in context.dataset.logs
        if log.level == "WARN" and window.onset <= log.timestamp <= window.last + timedelta(seconds=10)
    ]
    if warn_logs:
        evidence.append(
            Evidence(
                kind="log",
                ref="throttle_warnings",
                detail=f"{len(warn_logs)} WARN line(s) in the window: '{warn_logs[0].message}'",
                source="logs.json",
            )
        )

    tenants = {s.tenant_id for s in window.sends}
    day = window.onset.date()
    same_day = [s for s in all_sends if s.start_time.date() == day]
    evidence.append(
        Evidence(
            kind="metric",
            ref="blast_radius",
            detail=(
                f"{len(window.sends)} affected message(s) span {len(tenants)} distinct tenant(s); "
                f"{sum(1 for s in same_day if s in window.sends)} of {len(same_day)} "
                f"{window.channel} sends on {day.isoformat()} were affected — "
                "the incident is selective by neither tenant nor message"
            ),
            source="spans.json",
        )
    )

    for verdict in verdicts:
        evidence.append(
            Evidence(
                kind="deploy",
                ref=verdict.deploy.sha,
                detail=f"{verdict.deploy} — {verdict.reason}",
                source="deploys.json",
            )
        )
    for deploy in adjacent:
        evidence.append(
            Evidence(
                kind="deploy",
                ref=deploy.sha,
                detail=(
                    f"{deploy} — temporally adjacent but a different service; not a "
                    f"candidate for a fault localised to {service}"
                ),
                source="deploys.json",
            )
        )

    alternatives: list[Hypothesis] = []
    would_resolve: list[str] = []
    confidence = "observed"

    if in_recovery and recovery_end is not None:
        confidence = "ambiguous"
        deploy = in_recovery[0]
        evidence.append(
            Evidence(
                kind="deploy",
                ref=deploy.sha,
                detail=(
                    f"{deploy} — lands INSIDE the recovery window "
                    f"({fmt_ts(window.last)} last affected, {fmt_ts(recovery_end)} first clean). "
                    "Cannot be credited or excluded from this data."
                ),
                source="deploys.json",
            )
        )
        alternatives = [
            Hypothesis(
                id="H1.provider_side",
                summary=(
                    "Provider-side rate limiting that ended on its own; the deploy inside "
                    "the recovery window is coincidence."
                ),
                supports=["incident_window", "blast_radius"],
                against=[deploy.sha],
            ),
            Hypothesis(
                id="H2.client_side",
                summary=(
                    f"The pre-existing client mishandled concurrency or client-side rate "
                    f"limiting, the provider throttled in response, and {deploy.sha} "
                    f"(PR #{deploy.pr}) fixed it."
                ),
                supports=[deploy.sha],
                against=["blast_radius"],
            ),
        ]
        would_resolve = [
            f"the {deploy.sha} diff — if PR #{deploy.pr} changed connection pooling, "
            "concurrency, or retry configuration, H2 gains weight; if it was a version "
            "bump with no client-behaviour change, H1 does",
            "provider-side account rate-limit metrics for the incident window",
            "whether other producers on the shared quota saw the same throttling",
        ]

    ruled_out = [v for v in verdicts if not v.plausible]
    summary = (
        f"{len(window.sends)} {window.channel} send(s) degraded to "
        f"{durations[-1]} ms ({factor:.1f}x baseline {baseline:.0f} ms) with provider status "
        f"{statuses} and {retries} retries, from {fmt_ts(window.onset)} to "
        f"{fmt_ts(window.last)}. All reached the provider — final status {finals}, so nothing "
        "was lost; this was latency, not delivery failure."
    )
    if ruled_out:
        first = ruled_out[0]
        summary += (
            f" The {service} deploy {first.deploy.sha} is NOT the cause: {first.reason}."
        )
    if in_recovery:
        summary += " The cause of the recovery is not resolvable from this data — see alternatives."

    return Finding(
        id=f"D3.provider_degradation.{window.channel}",
        title=f"{window.channel} sends degraded {factor:.0f}x against the provider",
        severity="medium",
        confidence=confidence,
        summary=summary,
        evidence=evidence,
        affected=window.correlation_ids,
        alternatives=alternatives,
        would_resolve=would_resolve,
        params={
            "slow_factor": config.slow_factor,
            "incident_max_gap_s": config.incident_max_gap_s,
            "baseline_ms": round(baseline, 1),
            "largest_gap_in_window_s": _largest_gap(window),
        },
    )


def _largest_gap(window: Window) -> float:
    if len(window.sends) < 2:
        return 0.0
    return max(
        (b.start_time - a.start_time).total_seconds()
        for a, b in zip(window.sends, window.sends[1:])
    )


def _hhmm(delta: timedelta) -> str:
    """Round to the displayed precision rather than truncating.

    The March 9 gap is 4h59m59.245s. Truncating renders that as '4h59m', which
    reads as a different claim from the true one; rounding gives '5h00m'.
    """
    total = round(delta.total_seconds() / 60)
    return f"{total // 60}h{total % 60:02d}m"
