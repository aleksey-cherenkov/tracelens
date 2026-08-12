"""Assemble the evidence bundle handed to the model.

The invariant: context size is O(findings), not O(telemetry volume). A 10x traffic
increase must not change the prompt size. Everything here is an aggregate, a
finding, or a capped exemplar -- never a raw span dump.
"""

from __future__ import annotations

from ..config import DEFAULT, Config
from ..detectors import DetectorContext, run_all
from ..evidence import EvidenceBundle
from ..model import Dataset, fmt_ts


def pipeline_summary(context: DetectorContext) -> dict:
    accounting = context.accounting
    errors = context.health.errors
    window = context.dataset.window
    return {
        "window": {"from": fmt_ts(window[0]), "to": fmt_ts(window[1])},
        "topology": (
            "comms-ingest --topic--> comms-orchestrator --channel queue--> "
            "comms-sender --> provider"
        ),
        "accepted_messages": accounting.total,
        "reached_provider_once": accounting.delivered_once,
        "reached_provider_more_than_once": accounting.delivered_duplicate,
        "never_reached_provider": accounting.stopped,
        "provider_calls": accounting.provider_calls,
        "by_channel": {
            name: {
                "accepted": bucket.accepted,
                "delivered": bucket.delivered,
                "lost": bucket.lost,
                "duplicated": bucket.duplicated,
                "trace_intact": f"{bucket.trace_intact}/{bucket.accepted}",
            }
            for name, bucket in accounting.by_channel.items()
        },
        "error_rates": {
            "span_status_errors": f"{errors.span_status_errors}/{errors.total_spans}",
            "provider_errors": f"{errors.provider_errors}/{errors.provider_calls}",
            "delivery_failures": f"{errors.delivery_failures}/{errors.accepted}",
            "note": (
                "these three are reported separately on purpose; span status and "
                "delivery reality disagree"
            ),
        },
        "caveats": [
            f"lower environment: {accounting.total} messages over "
            f"{(window[1] - window[0]).days + 1} days, orders of magnitude below production",
            "async hop latency has zero variance across all messages, so hop percentiles "
            "carry no information",
            "a zero-traffic day is insufficient_data, never a drop — production sends on "
            "weekends even though this environment does not",
        ],
    }


def build_bundle(
    dataset: Dataset, complaint: str, config: Config = DEFAULT
) -> tuple[EvidenceBundle, DetectorContext]:
    """Run every detector up front rather than letting the model explore.

    273 spans makes full precomputation free, it fixes the evidence set so the
    answer is auditable, and it means the model cannot reach a conclusion by a
    path that can't be reconstructed afterwards. The tools exist for drill-down
    on findings that have *already* surfaced.
    """
    from ..detectors import build_context

    context = build_context(dataset, config)
    findings = run_all(context)

    bundle = EvidenceBundle(
        complaint=complaint,
        findings=findings,
        summary=pipeline_summary(context),
        deploys=[
            {
                "sha": d.sha,
                "service": d.service,
                "deployed_at": fmt_ts(d.deployed_at),
                "pr": d.pr,
                "title": d.title,
            }
            for d in sorted(dataset.deploys, key=lambda d: d.deployed_at)
        ],
    )
    return bundle, context
