"""Tunable thresholds.

Every number in here is a parameter rather than a literal buried in a detector,
because all of them were chosen against a 41-message lower-environment export and
none of them should survive contact with production unexamined. Detectors print
the parameter value alongside any finding that depended on it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- baselines and gating -------------------------------------------------
    min_samples: int = 20
    """Minimum n before a *rate* is reported or a detector fires *on a rate*.

    Never gates existence/count findings. A message the platform promised and did
    not deliver is a finding at n=1: it is a claim about that message, not a claim
    about a population. See DESIGN section 4.
    """

    # --- join -----------------------------------------------------------------
    correlation_join_window_s: float = 60.0
    """How far ahead to look for the next stage when the parent/child link is
    broken and we fall back to joining on correlation_id."""

    # --- D3 provider degradation ---------------------------------------------
    slow_factor: float = 3.0
    """A send is 'affected' above this multiple of the channel baseline."""

    incident_max_gap_s: float = 24 * 3600.0
    """Affected sends further apart than this start a new incident window.

    The overnight gap inside the March 9 incident is 15h08m, so anything below
    that splits one incident into two and changes the deploy arithmetic. The
    value is printed in the finding rather than left implicit.
    """

    deploy_lookback_s: float = 24 * 3600.0
    """How far before a window's onset to consider a deploy as a candidate cause."""

    # --- D5 blind spots -------------------------------------------------------
    noise_ratio_alert: float = 0.5
    """Unjoinable share of log volume above which the noise itself is a finding."""

    # --- presentation ---------------------------------------------------------
    max_exemplars: int = 5
    """Fully-rendered examples per finding handed to the model. Context size must
    be O(findings), not O(telemetry volume)."""


DEFAULT = Config()
