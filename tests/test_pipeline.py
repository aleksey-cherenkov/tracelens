"""Loading, grouping, routes, quality, timing — against the real export.

These test *properties*, not the seven problems I found by hand. A test asserting
"the push channel is dropped" would be testing my memory rather than the tool and
would pass just as happily on a version that hardcoded the answer.
"""

from __future__ import annotations

import json

import pytest

from tracelens import journeys, quality, timeline
from tracelens.analysis import analyse
from tracelens.events import EventLog, normalise
from tracelens.loader import load


# -- events and loading ------------------------------------------------------ #


def test_identifiers_and_timestamps_are_found_by_suffix_not_by_name():
    """The rule has to work on fields nobody listed. `order_id` and `accepted_at`
    are the test: one appears nowhere in this codebase, and the other was a real
    miss that silently dropped 41 records until the loader reported the skip."""
    event = normalise({"accepted_at": "2026-01-01T00:00:00Z", "order_id": "o-1", "n": 4}, "x")
    assert event.ids == {"order_id": "o-1"}
    assert event.attributes == {"n": 4}, "the time field must not leak into data"


def test_an_unusable_record_is_dropped_rather_than_raising():
    """An unfamiliar export contains shapes this does not expect, and one odd
    record must not take down the run."""
    assert normalise({"service": "x", "name": "y"}, "span") is None
    assert normalise("not a dict", "span") is None


def test_nothing_is_silently_skipped(export):
    """The most dangerous failure here: a dropped record means every downstream
    absence has two causes and no way to tell them apart. So skips are reported
    with the reason, and this asserts there are none."""
    assert export.skipped == {}
    assert sum(export.counts.values()) == len(export.log)


def test_one_unreadable_file_does_not_take_down_the_run(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "spans.json").write_text(
        json.dumps([{"timestamp": "2026-01-01T00:00:00Z", "service": "a", "name": "b"}]),
        encoding="utf-8",
    )
    (data / "broken.json").write_text("{not json", encoding="utf-8")

    found = load(tmp_path)
    assert "broken.json" in found.skipped
    assert len(found.log) == 1


def test_a_stray_json_at_the_root_does_not_win(tmp_path):
    """A repo root usually has a stray .json in it, and picking that over the real
    export is a silent wrong answer rather than a loud one."""
    (tmp_path / "baseline.json").write_text("{}", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    for name in ("spans.json", "logs.json"):
        (data / name).write_text("[]", encoding="utf-8")
    (data / "spans.json").write_text(
        json.dumps([{"timestamp": "2026-01-01T00:00:00Z", "name": "b"}]), encoding="utf-8"
    )
    assert load(tmp_path).directory == data


# -- grouping ---------------------------------------------------------------- #


def test_the_key_is_supplied_or_defaulted_never_scored(log, grouping):
    """No formula picks this. Two structural disqualifications, then highest
    coverage — and the whole table is printed so a wrong default is one glance
    from being seen."""
    usable = [c for c in grouping.candidates if not c.disqualified]
    assert grouping.key == max(usable, key=lambda c: c.coverage).key
    assert len(grouping.candidates) > len(usable), "rejections must be kept, not dropped"

    other = next(c for c in usable if c.key != grouping.key)
    assert journeys.build(log, other.key).key == other.key, "--key must override"


def test_an_identifier_that_labels_records_is_disqualified(grouping):
    """One record per group labels records; it does not join them."""
    single = [c for c in grouping.candidates if c.median_group_size <= 1]
    assert single and all(c.disqualified for c in single)


def test_an_empty_log_groups_nothing_rather_than_guessing():
    empty = journeys.build(EventLog([]))
    assert empty.key is None and not empty.journeys


# -- routes ------------------------------------------------------------------ #


def test_records_from_every_source_land_on_the_same_route(routes, grouping):
    """The reason for one Event type and for learning the substitution vocabulary
    by value rather than per record: a log line carrying no attributes at all has
    to collapse the same way a span does, or logs and spans build two graphs."""
    assert routes.vocabulary
    assert len(routes.routes[0].sources) > 2
    assert len(routes.routes) < len(grouping.journeys) / 4, "one route per journey says nothing"


def test_the_table_shows_where_routes_diverge(routes):
    """Five routes agreeing for eight nodes and diverging at the ninth all
    truncate before the divergence unless the shared opening is printed once —
    and the divergence is the only thing the table exists to show."""
    from tracelens.routes import common_prefix, render

    rendered = "\n".join(render(routes))
    assert common_prefix(routes) and "all journeys start:" in rendered
    for route in routes.routes:
        assert route.ends_at.split(":", 1)[-1][:20] in rendered


def test_work_done_twice_is_visible_as_a_repeated_node(routes):
    """The general form of duplicate delivery, without knowing what the work is."""
    assert any(r.repeats for r in routes.routes)


# -- quality ----------------------------------------------------------------- #


def test_a_constant_field_is_reported_with_what_it_prevents(log, grouping):
    """The trap this layer exists for: a field reading OK on every record,
    including records for work that never completed."""
    result = quality.assess(log, grouping)
    assert result.defects
    assert all(d.limits for d in result.defects), "a defect without a limit is a complaint"
    assert any(
        "not evidence that anything worked" in limit
        for d in result.defects
        for limit in d.limits
    )


def test_the_limits_are_stated_in_code_not_left_to_the_reader(analysis):
    """The design choice being tested. Computing the statistic and leaving the
    conclusion to whoever reads it would make the limit advisory; writing it here
    is what guarantees it appears at all."""
    assert analysis.limits
    assert len(analysis.limits) == len(set(analysis.limits)), "limits must be deduplicated"
    assert "cannot" in " ".join(analysis.limits).lower()


def test_a_field_below_the_observation_floor_is_not_reported(log, grouping):
    """Three identical values is not a pattern."""
    for defect in quality.assess(log, grouping).defects:
        if defect.id.startswith("Q.uninformative."):
            assert int(defect.evidence[0].split()[0].replace(",", "")) >= quality.MIN_OBSERVATIONS


# -- timing ------------------------------------------------------------------ #


def test_timing_describes_and_claims_nothing(analysis):
    """No threshold, no verdict. 'Longest journey' is an observation; 'slow' and
    'degraded' are judgements that need an SLO this tool has not been given."""
    payload = dict(analysis.timing.as_dict())
    disclaimer = payload.pop("note")
    text = str(payload).lower()
    for verdict in ("slow", "degraded", "breach", "violation", "unacceptab"):
        assert verdict not in text, f"the timing layer asserted '{verdict}'"
    assert "nothing here is called slow" in disclaimer


def test_percentiles_are_nearest_rank_and_suppressed_below_the_floor():
    """With three samples an interpolated p95 is a number invented between two
    observations. Show the observations instead."""
    small = timeline.Distribution("x", [1.0, 2.0, 3.0])
    assert not small.reliable and "too few for percentiles" in small.describe()

    big = timeline.Distribution("x", [float(n) for n in range(1, 101)])
    assert big.p50 in big.samples and big.p95 in big.samples


def test_nodes_are_ranked_by_total_time_not_percentile(analysis):
    """A 40ms node called 3,000 times is a better target than a 500ms node called
    twice, and ranking by percentile hides that every time."""
    totals = [d.total_ms for d in analysis.timing.busiest(3)]
    assert totals == sorted(totals, reverse=True)


# -- assembly ---------------------------------------------------------------- #


def test_the_opening_payload_is_bounded_by_routes_not_by_traffic(analysis, log):
    """The property that lets this survive go-live unchanged: a route table is a
    dozen lines whether the export holds 41 journeys or 41 million."""
    tripled = EventLog(sorted(log.events * 3, key=lambda e: e.at))
    one = len(json.dumps(analysis.overview()))
    many = len(json.dumps(analyse(tripled).overview()))
    assert many < one * 1.5, f"overview grew {many / one:.2f}x when traffic grew 3x"


def test_the_analysis_is_deterministic(log):
    """An analyzer that answers differently each run is worse than none."""
    runs = [[r.nodes for r in analyse(log).routes.routes] for _ in range(3)]
    assert all(run == runs[0] for run in runs)


@pytest.mark.parametrize(
    "command",
    [["quality", "--quiet"], ["routes"], ["slice", "--route", "1", "--limit", "1"]],
)
def test_every_command_runs(command):
    from tracelens.cli import main

    assert main(["--plain", *command]) == 0
