"""Novelty: what changed, rather than what is wrong.

Detectors need a rule. Invariants need a property to violate. Some real problems
break neither: a new provider status code nobody handles, a log line from a code
path that should be unreachable, a stage that quietly stopped appearing. Nothing
is *violated* -- the pipeline is simply not the pipeline it was.

So the third layer compares the current fingerprint against a recorded one and
reports the difference in both directions. Something new is the usual suspect
during an incident. Something that stopped appearing is what a silently disabled
code path looks like, and it is the harder of the two to notice by eye.

This layer is deliberately dumb. It has no opinion about whether a change is bad
-- only that it happened, and that an engineer mid-incident should see it. Set
against a baseline from before the incident, "what is different?" is often the
whole investigation.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DEFAULT, Config
from .evidence import Evidence, Finding
from .model import Dataset
from .topology import diff_profiles, profile

DEFAULT_BASELINE = Path("baseline.json")

# How much a change in each dimension matters. Structural changes rank above
# vocabulary changes: a vanished node is an outage, a new log template is usually
# just a deploy.
SEVERITY_BY_DIMENSION = {
    "services": "critical",
    "nodes": "critical",
    "edges": "high",
    "entry_nodes": "high",
    "path_shapes": "high",
    "channels": "high",
    "span_statuses": "high",
    "provider_statuses": "high",
    "span_attribute_keys": "medium",
    "log_attribute_keys": "medium",
    "span_kinds": "medium",
    "log_levels": "medium",
    "services_deployed": "low",
    "log_templates": "low",
}

VANISHING_IS_WORSE = {"services", "nodes", "edges", "channels", "path_shapes", "entry_nodes"}


def save_baseline(dataset: Dataset, path: Path | str = DEFAULT_BASELINE) -> Path:
    path = Path(path)
    path.write_text(json.dumps(profile(dataset), indent=2, sort_keys=True) + "\n", "utf-8")
    return path


def load_baseline(path: Path | str = DEFAULT_BASELINE) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def check(
    dataset: Dataset,
    baseline: dict | None = None,
    baseline_path: Path | str = DEFAULT_BASELINE,
    config: Config = DEFAULT,
) -> list[Finding]:
    if baseline is None:
        baseline = load_baseline(baseline_path)

    current = profile(dataset)

    if baseline is None:
        # Not a failure. On unfamiliar data the honest output is "here is what
        # this pipeline looks like, I have nothing to compare it to yet".
        return [
            Finding(
                id="NOV.no_baseline",
                title="no baseline recorded — nothing to compare against",
                severity="low",
                confidence="observed",
                summary=(
                    "Novelty detection needs a recorded fingerprint of a known-good "
                    "period. None exists, so this run establishes what the pipeline looks "
                    f"like now: {len(current['nodes'])} node(s), {len(current['edges'])} "
                    f"edge(s), {len(current['path_shapes'])} route shape(s), "
                    f"{len(current['log_templates'])} log template(s). Save it with "
                    "`tracelens baseline --save`, then a later run reports what changed."
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        ref=f"profile.{dimension}",
                        detail=f"{len(values)} distinct: {_sample(values)}",
                        source="spans.json + logs.json",
                    )
                    for dimension, values in sorted(current.items())
                    if values
                ][:12],
                affected=[],
                params={"layer": "novelty"},
            )
        ]

    changes = diff_profiles(baseline, current)
    if not changes:
        return []

    findings: list[Finding] = []
    for dimension, delta in changes.items():
        appeared, vanished = delta["appeared"], delta["vanished"]
        severity = SEVERITY_BY_DIMENSION.get(dimension, "medium")
        if vanished and dimension in VANISHING_IS_WORSE:
            severity = "critical"

        parts = []
        if appeared:
            parts.append(f"{len(appeared)} appeared")
        if vanished:
            parts.append(f"{len(vanished)} no longer present")

        findings.append(
            Finding(
                id=f"NOV.{dimension}",
                title=f"{dimension.replace('_', ' ')}: {', '.join(parts)}",
                severity=severity,
                confidence="observed",
                summary=(
                    f"The set of {dimension.replace('_', ' ')} differs from the baseline. "
                    + (f"New: {_sample(appeared)}. " if appeared else "")
                    + (
                        f"Gone: {_sample(vanished)}. Something that stopped appearing is "
                        "how a disabled or unreachable code path looks, and it produces no "
                        "error. "
                        if vanished
                        else ""
                    )
                    + "This layer reports the change without judging it — a deploy and an "
                    "incident look identical here, which is why it is evidence rather than "
                    "a verdict."
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        ref=f"novelty.{dimension}.{'appeared' if group == appeared else 'vanished'}",
                        detail=f"{label}: {', '.join(group[:10])}"
                        + (f" (+{len(group) - 10} more)" if len(group) > 10 else ""),
                        source="baseline.json vs current export",
                    )
                    for group, label in ((appeared, "appeared"), (vanished, "vanished"))
                    if group
                ],
                affected=[],
                would_resolve=[
                    "the deploy history for the window between the baseline and now",
                ],
                params={"layer": "novelty", "dimension": dimension},
            )
        )
    return findings


def compare_datasets(before: Dataset, after: Dataset, config: Config = DEFAULT) -> list[Finding]:
    """Diff two exports directly -- 'last week versus this week'."""
    return check(after, baseline=profile(before), config=config)


def _sample(values, limit: int = 6) -> str:
    values = list(values)
    head = ", ".join(str(v) for v in values[:limit])
    return head + (f" (+{len(values) - limit} more)" if len(values) > limit else "")
