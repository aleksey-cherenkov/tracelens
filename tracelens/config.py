"""Tunable thresholds.

Every number here is a parameter rather than a literal buried in a check, because
all of them were chosen against a 41-journey lower-environment export and none of
them should survive contact with production unexamined. Findings print the
parameter value alongside any conclusion that depended on it, so a reader can see
which numbers the answer rests on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    min_samples: int = 20
    """Minimum n before a *rate* is reported. Never gates existence findings.

    A message the platform promised and did not deliver is a finding at n=1: it
    is a claim about that message, not about a population. An earlier version
    gated everything on this and would have suppressed a 100% channel outage
    because it only had four examples.
    """

    expected_edge_share: float = 0.5
    """Share of journeys at a node that must traverse an edge for it to count as
    the expected route rather than an optional branch.

    Guards the conservation invariant against false positives: a retry or fallback
    stage taken by a minority is not a hop everyone else failed to reach. Cannot
    be set near 1.0, because a real drop drags the edge's own share down -- the
    threshold has to sit below the loss it is meant to detect.
    """

    max_exemplars: int = 5
    """Fully-rendered examples per finding handed to the model. Context size must
    be O(findings), not O(telemetry volume)."""


DEFAULT = Config()
