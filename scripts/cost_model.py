#!/usr/bin/env python3
"""What this telemetry costs, and what each fix is worth.

Every per-message figure is measured from data/ rather than guessed. Every price
is an assumption, stated in one place at the top so it can be replaced with real
vendor numbers and re-run.

    python scripts/cost_model.py                 # default: 2M messages/day
    python scripts/cost_model.py --per-day 10000000
    python scripts/cost_model.py --hot-gb-month 3.50

The point is not the exact dollar figure -- it is that the argument is arithmetic
rather than assertion. "Improve observability" persuades nobody; "the noise is 76%
of our telemetry volume and removing it costs an afternoon" is a decision someone
can approve.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracelens.loader import find_data_dir  # noqa: E402

# --------------------------------------------------------------------------- #
# ASSUMPTIONS — replace with real numbers and re-run.
# --------------------------------------------------------------------------- #

DEFAULT_MESSAGES_PER_DAY = 2_000_000
"""Production volume. Not derivable from a 41-message lower-env export, so it is
a parameter. The conclusions below are ratios and hold at any volume; only the
absolute dollars move."""

HOT_GB_MONTH = 2.50
"""Indexed search backend, per GB retained per month. Covers ingest, indexing and
storage. Datadog/Elastic-class pricing is in this range; yours will differ."""

WARM_GB_MONTH = 0.023
"""Columnar object storage (S3-class). Roughly 100x cheaper than indexed."""

SCAN_PER_TB = 5.00
"""Athena-class per-TB-scanned query pricing."""

HOT_DAYS, WARM_DAYS, COLD_DAYS = 7, 30, 395
ACCOUNTING_REFRESHES_PER_DAY = 24
"""How often "how many did we deliver" gets asked. One hourly dashboard. This is
the number that matters, because today that question is a full scan and after the
ledger it is a primary-key aggregate."""


@dataclass
class Footprint:
    """Bytes per message, measured from the export."""

    spans: float
    scoped_logs: float
    noise_logs: float
    ledger: float

    @property
    def telemetry(self) -> float:
        return self.spans + self.scoped_logs + self.noise_logs

    @property
    def useful(self) -> float:
        return self.spans + self.scoped_logs


def measure() -> Footprint:
    directory = find_data_dir()
    spans = json.loads((directory / "spans.json").read_text())
    logs = json.loads((directory / "logs.json").read_text())
    accepted = json.loads((directory / "accepted_messages.json").read_text())
    count = len(accepted)

    def size(records) -> int:
        return sum(len(json.dumps(r)) for r in records)

    scoped = [
        r for r in logs if r.get("trace_id") or r.get("attributes", {}).get("correlation_id")
    ]
    noise = [r for r in logs if r not in scoped]

    # Two rows per message: accepted at ingest, settled at the sender.
    ledger_row = {
        "correlation_id": "corr-0001",
        "message_type": "email",
        "tenant_id": "org-1042",
        "at": "2026-03-02T09:00:00.000Z",
        "event": "settled",
        "provider_status": 202,
    }

    return Footprint(
        spans=size(spans) / count,
        scoped_logs=size(scoped) / count,
        noise_logs=size(noise) / count,
        ledger=2 * len(json.dumps(ledger_row)),
    )


GB = 1024**3


def monthly(bytes_per_day: float, days_retained: int, gb_month: float) -> float:
    """Steady state: `days_retained` days of data sitting there, billed monthly."""
    return (bytes_per_day * days_retained / GB) * gb_month


def report(footprint: Footprint, per_day: int) -> None:
    def line(label: str, value: str, note: str = "") -> None:
        print(f"  {label:<44}{value:>14}  {note}")

    print("\nMEASURED PER MESSAGE  (from data/, not assumed)")
    line("spans", f"{footprint.spans:,.0f} B")
    line("logs that join to a message", f"{footprint.scoped_logs:,.0f} B")
    line("logs that join to nothing", f"{footprint.noise_logs:,.0f} B",
         f"{footprint.noise_logs / footprint.telemetry:.0%} of all telemetry")
    line("total telemetry", f"{footprint.telemetry:,.0f} B")
    line("delivery ledger (2 rows)", f"{footprint.ledger:,.0f} B",
         f"{footprint.ledger / footprint.telemetry:.1%} of telemetry")

    print(f"\nAT {per_day:,} MESSAGES/DAY")
    daily_all = footprint.telemetry * per_day
    daily_useful = footprint.useful * per_day
    daily_ledger = footprint.ledger * per_day
    line("telemetry produced per day", f"{daily_all / GB:,.0f} GB")
    line("after dropping the noise", f"{daily_useful / GB:,.0f} GB",
         f"{1 - daily_useful / daily_all:.0%} less")

    print("\nMONTHLY STORAGE — worst case: everything indexed, nothing tiered")
    today = monthly(daily_all, WARM_DAYS, HOT_GB_MONTH)
    line(f"all telemetry, indexed, {WARM_DAYS}d", f"${today:,.0f}",
         "upper bound, not necessarily today's bill")

    print("\nMONTHLY STORAGE — noise removed, then tiered")
    hot = monthly(footprint.scoped_logs * per_day, HOT_DAYS, HOT_GB_MONTH)
    warm = monthly(daily_useful, WARM_DAYS, WARM_GB_MONTH)
    cold = monthly(daily_ledger, COLD_DAYS, WARM_GB_MONTH)
    line(f"hot: message-scoped logs only, {HOT_DAYS}d indexed", f"${hot:,.0f}")
    line(f"warm: spans + scoped logs, {WARM_DAYS}d columnar", f"${warm:,.0f}")
    line(f"cold: ledger + rollups, {COLD_DAYS}d", f"${cold:,.0f}",
         "13 months of exact delivery history")
    line("total", f"${hot + warm + cold:,.0f}",
         f"{1 - (hot + warm + cold) / today:.0%} less than today")

    print("\nQUERY COST — the recurring bill, and the part the ledger removes")
    naive = (daily_all * WARM_DAYS / (1024**4)) * SCAN_PER_TB
    per_month = naive * ACCOUNTING_REFRESHES_PER_DAY * 30
    line("one 'how many did we deliver' full scan", f"${naive:,.2f}",
         f"{WARM_DAYS}d of raw telemetry")
    line(f"as an hourly dashboard ({ACCOUNTING_REFRESHES_PER_DAY}/day)",
         f"${per_month:,.0f}/mo", "one question, asked on a schedule")
    line("same answer from the ledger", "~$0", "indexed primary-key aggregate")

    print("\nWHAT EACH FIX IS WORTH")
    line("1. stop emitting the noise",
         f"{footprint.noise_logs / footprint.telemetry:.0%} of volume",
         "an afternoon of emission changes")
    line("2. move accounting to the ledger", "removes the scan class",
         f"ledger costs {footprint.ledger / footprint.telemetry:.1%} of telemetry")
    line("3. tier what remains", f"~{1 - (hot + warm + cold) / today:.0%} of storage",
         "hot holds the least, not the most")
    print()
    line("net", f"${today + per_month:,.0f}/mo -> ${hot + warm + cold:,.0f}/mo",
         "storage + one scheduled question")

    print(f"\n  Assumptions: hot ${HOT_GB_MONTH}/GB/mo, warm ${WARM_GB_MONTH}/GB/mo, "
          f"scan ${SCAN_PER_TB}/TB.")
    print("  Volume is a parameter. Every ratio above is volume-independent.\n")


def main() -> int:
    global HOT_GB_MONTH

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-day", type=int, default=DEFAULT_MESSAGES_PER_DAY)
    parser.add_argument("--hot-gb-month", type=float, default=HOT_GB_MONTH)
    args = parser.parse_args()

    HOT_GB_MONTH = args.hot_gb_month
    report(measure(), args.per_day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
