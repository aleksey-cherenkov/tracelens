"""The three layers, run together.

    DETECTORS   rules for failures already understood.  Closed world: precise,
                explains mechanism and cause, cannot surface a sixth failure.
    INVARIANTS  properties that must hold of any pipeline.  Open world: a
                violation is novel by construction, but says only *what* broke.
    NOVELTY     differences from a recorded baseline.  No opinion at all, just
                "this is not the pipeline it was".

The ordering is deliberate and it is the answer to "how would this help with a
problem nobody has seen before". Layer 1 alone is a demo: it re-finds what its
author already knew. Layers 2 and 3 carry no knowledge of this pipeline, so they
are what still works on an export this code has never been pointed at.

Where they overlap, that is a feature: a conservation violation and a channel-drop
detector firing on the same messages is corroboration from independent directions,
and the detector supplies the mechanism the invariant cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import invariants, novelty
from .accounting import Accounting, account
from .config import DEFAULT, Config
from .detectors import DetectorContext, run_all
from .evidence import Finding
from .health import Health, compute
from .join import LogicalTrace, build_all
from .model import Dataset
from .topology import Topology, discover

LAYERS = ("detector", "invariant", "novelty")


def stage_coverage(dataset: Dataset) -> float:
    """Share of spans the hardcoded Stage taxonomy recognises.

    A proxy for "is this the pipeline model.py was written for?". High on the
    real export, near zero on anything else.
    """
    if not dataset.spans:
        return 1.0
    mapped = sum(1 for span in dataset.spans if span.stage is not None)
    return mapped / len(dataset.spans)


def layer_of(finding: Finding) -> str:
    if finding.id.startswith("INV."):
        return "invariant"
    if finding.id.startswith("NOV."):
        return "novelty"
    return "detector"


@dataclass
class Analysis:
    dataset: Dataset
    traces: dict[str, LogicalTrace]
    accounting: Accounting
    health: Health
    topology: Topology
    findings: list[Finding] = field(default_factory=list)

    def by_layer(self, layer: str) -> list[Finding]:
        return [f for f in self.findings if layer_of(f) == layer]

    @property
    def counts(self) -> dict[str, int]:
        return {layer: len(self.by_layer(layer)) for layer in LAYERS}

    def corroborated(self) -> list[tuple[Finding, list[Finding]]]:
        """Findings from different layers naming overlapping messages.

        Two layers reaching the same set from independent directions is the
        strongest signal the tool produces, and it is worth surfacing explicitly
        rather than leaving a reader to notice the repeated IDs.
        """
        pairs: list[tuple[Finding, list[Finding]]] = []
        for detector in self.by_layer("detector"):
            if not detector.affected:
                continue
            overlap = [
                other
                for other in self.by_layer("invariant")
                if other.affected and set(other.affected) & set(detector.affected)
            ]
            if overlap:
                pairs.append((detector, overlap))
        return pairs


def analyse(
    dataset: Dataset,
    config: Config = DEFAULT,
    baseline_path: Path | str | None = None,
    include_novelty: bool = True,
) -> Analysis:
    traces = build_all(dataset, config)
    accounting = account(traces)
    health = compute(dataset, traces, accounting)
    context = DetectorContext(
        dataset=dataset,
        traces=traces,
        accounting=accounting,
        health=health,
        config=config,
    )

    findings: list[Finding] = []

    # The detectors are built on the hardcoded Stage taxonomy in model.py. On a
    # pipeline that taxonomy does not describe, classify_stage() returns None for
    # most spans, every message then looks like it never reached a provider, and
    # D1 cheerfully reports that *every* channel is being dropped. Confidently
    # wrong is worse than silent, so they are gated on whether the taxonomy
    # actually fits -- and the gate is reported, not hidden.
    coverage = stage_coverage(dataset)
    taxonomy_fits = coverage >= config.min_stage_coverage
    if taxonomy_fits:
        try:
            findings.extend(run_all(context))
        except Exception as exc:  # pragma: no cover - defensive by intent
            findings.append(_layer_failed("detectors", exc))
    else:
        findings.append(_taxonomy_mismatch(coverage, config))

    try:
        findings.extend(invariants.check_all(dataset, config))
    except Exception as exc:  # pragma: no cover - defensive by intent
        findings.append(_layer_failed("invariants", exc))

    if include_novelty:
        try:
            findings.extend(
                novelty.check(
                    dataset,
                    baseline_path=baseline_path or novelty.DEFAULT_BASELINE,
                    config=config,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive by intent
            findings.append(_layer_failed("novelty", exc))

    findings.sort(key=lambda f: f.rank_score, reverse=True)
    return Analysis(
        dataset=dataset,
        traces=traces,
        accounting=accounting,
        health=health,
        topology=discover(dataset),
        findings=findings,
    )


def _taxonomy_mismatch(coverage: float, config: Config) -> Finding:
    from .evidence import Evidence

    return Finding(
        id="ERR.taxonomy_mismatch",
        title="detector layer skipped — this is not the pipeline it was written for",
        severity="medium",
        confidence="observed",
        summary=(
            f"The hardcoded stage taxonomy recognises only {coverage:.0%} of spans "
            f"(threshold {config.min_stage_coverage:.0%}), so the detectors were not run. "
            "They encode failures specific to one pipeline and, applied here, would "
            "report every message as undelivered simply because no span maps to a known "
            "stage. The invariant and novelty layers carry no such assumption and ran "
            "normally — read those. Extending the taxonomy in model.py, or relying on "
            "the general layers, are both valid; silently emitting confident nonsense "
            "is not."
        ),
        evidence=[
            Evidence(
                kind="metric",
                ref="stage_coverage",
                detail=(
                    f"{coverage:.1%} of spans mapped to a known stage; detectors require "
                    f"{config.min_stage_coverage:.0%}"
                ),
                source="tracelens/model.py:classify_stage",
            )
        ],
        affected=[],
    )


def _layer_failed(layer: str, exc: Exception) -> Finding:
    from .evidence import Evidence

    return Finding(
        id=f"ERR.{layer}",
        title=f"the {layer} layer failed on this dataset",
        severity="high",
        confidence="observed",
        summary=(
            f"{type(exc).__name__}: {exc}. Reported rather than swallowed — a layer "
            "that silently produces nothing looks identical to a clean bill of health, "
            "which is the failure mode this whole tool exists to prevent."
        ),
        evidence=[
            Evidence(
                kind="metric",
                ref=f"error.{layer}",
                detail=f"{type(exc).__name__}: {exc}",
                source="tracelens",
            )
        ],
        affected=[],
    )
