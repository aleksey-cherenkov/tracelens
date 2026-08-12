"""Detector registry.

Every detector is pure deterministic code returning Finding objects with
pre-resolved Evidence. The model never computes a number and never selects an ID.

Running all of them up front is deliberate rather than lazy: 273 spans is small
enough that full precomputation is free, it makes the evidence set fixed and
therefore auditable, and it means a triage answer cannot be reached by a path
that can't be reconstructed afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..accounting import Accounting
from ..config import DEFAULT, Config
from ..evidence import Finding
from ..health import Health
from ..join import LogicalTrace
from ..model import Dataset
from . import blindspot, drop, duplicate, provider, tracing


@dataclass
class DetectorContext:
    """Everything a detector may read. Deliberately a value object: detectors do
    no I/O, so swapping the file loader for a backend query layer leaves every
    detector and the whole AI layer untouched."""

    dataset: Dataset
    traces: dict[str, LogicalTrace]
    accounting: Accounting
    health: Health
    config: Config = DEFAULT


DETECTORS = [
    drop.detect,
    duplicate.detect,
    provider.detect,
    tracing.detect,
    blindspot.detect,
]


def run_all(context: DetectorContext) -> list[Finding]:
    findings: list[Finding] = []
    for detector in DETECTORS:
        findings.extend(detector(context))
    return sorted(findings, key=lambda f: f.rank_score, reverse=True)


def build_context(dataset: Dataset, config: Config = DEFAULT) -> DetectorContext:
    from ..accounting import account
    from ..health import compute
    from ..join import build_all

    traces = build_all(dataset, config)
    accounting = account(traces)
    return DetectorContext(
        dataset=dataset,
        traces=traces,
        accounting=accounting,
        health=compute(dataset, traces, accounting),
        config=config,
    )
