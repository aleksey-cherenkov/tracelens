"""The timeline the model reads.

The design rests on this module, so the tests are about the two things that make
a slice useful rather than merely correct: the contrast journey, and changes
landing in the sequence at the right place.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tracelens.slices import Filter, render, select


def build(analysis, where, limit=3):
    return select(analysis.log, analysis.grouping, analysis.routes, where, max_journeys=limit)


@pytest.fixture
def minority(analysis):
    return min(analysis.routes.routes, key=lambda r: r.count)


# -- the contrast ------------------------------------------------------------ #


def test_a_slice_carries_a_contrast_from_the_most_common_route(analysis, minority):
    """The load-bearing property. Filter to the affected journeys and the model
    sees sequences that each end at the same place — nothing looks wrong, because
    that is simply what those journeys look like. The finding is 'these stop here
    and normal ones don't', and it needs both halves on the page."""
    chosen = build(analysis, Filter(route=minority.index))

    assert chosen.contrast is not None
    assert chosen.contrast.value in analysis.routes.dominant.journeys
    assert chosen.contrast.value not in {j.value for j in chosen.matched}

    text = "\n".join(render(chosen, analysis.routes))
    assert "contrast" in text and text.count("journey ") >= 2


def test_the_contrast_is_the_one_nearest_in_time(analysis, minority):
    """Nearest in time controls for anything that changed across the window, so a
    difference between the two is more likely to be about the filter than the day."""
    chosen = build(analysis, Filter(route=minority.index))
    anchor = min(j.start for j in chosen.matched)
    pool = [
        analysis.grouping.journeys[v]
        for v in analysis.routes.dominant.journeys
        if v not in {j.value for j in chosen.matched}
    ]
    assert abs((chosen.contrast.start - anchor).total_seconds()) == min(
        abs((j.start - anchor).total_seconds()) for j in pool
    )


def test_asking_about_the_normal_route_says_there_is_no_contrast(analysis):
    """Rather than silently comparing the common route against itself."""
    chosen = build(analysis, Filter(route=analysis.routes.dominant.index))
    assert chosen.contrast is None and "no contrast" in chosen.contrast_reason


# -- changes in the sequence ------------------------------------------------- #


def test_a_change_appears_in_the_timeline_at_its_timestamp(analysis):
    """A deploy read as a footnote is a deploy nobody weighs against the sequence.
    Rendered in place, 'this landed after the first affected record' is a position
    on the page rather than arithmetic someone has to trust."""
    deploys = analysis.log.of_kind("deploy")
    if not deploys:
        pytest.skip("export records no changes")

    moment = deploys[0].at
    lines = render(
        build(analysis, Filter(after=moment - timedelta(hours=6), before=moment + timedelta(hours=6))),
        analysis.routes,
    )
    assert any(line.strip().startswith("**") for line in lines)


def test_a_time_filter_looks_across_the_period_asked_about(analysis):
    """Anchoring to the matched journeys instead hides a deploy hours into the
    window the person actually asked about — which is the window they care about."""
    deploys = analysis.log.of_kind("deploy")
    if not deploys:
        pytest.skip("export records no changes")

    moment = deploys[0].at
    wide = build(analysis, Filter(after=moment - timedelta(days=1), before=moment + timedelta(days=1)))
    assert any(c.at == moment for c in wide.changes)


def test_silence_about_changes_is_never_read_as_absence(analysis):
    """Only deploys reach this tool. Config flips, feature flags and provider-side
    changes are invisible to it, and the output has to say so."""
    quiet = build(analysis, Filter(values=("nothing-matches-this",)))
    assert quiet.changes == []
    assert "no journeys match" in "\n".join(render(quiet, analysis.routes))


def test_a_change_is_never_called_a_cause(analysis):
    deploys = analysis.log.of_kind("deploy")
    if not deploys:
        pytest.skip("export records no changes")
    text = "\n".join(
        render(
            build(analysis, Filter(after=deploys[0].at - timedelta(hours=2), before=deploys[0].at)),
            analysis.routes,
        )
    ).lower()
    for word in ("caused", "because of", "responsible for", "due to the deploy"):
        assert word not in text


# -- shape and caps ---------------------------------------------------------- #


def test_the_cap_holds_and_is_reported(analysis):
    chosen = build(analysis, Filter(), limit=2)
    assert len(chosen.shown) == 2 and chosen.total > 2
    assert "rendered below" in "\n".join(render(chosen, analysis.routes))


def test_attributes_constant_within_a_journey_are_hoisted_once(analysis):
    """Nine repetitions removed from a ten-record journey. Worth it for a person
    reading and worth more for a prompt paying by the token."""
    value = next(iter(analysis.grouping.journeys))
    lines = render(build(analysis, Filter(values=(value,))), analysis.routes)
    header = next(line for line in lines if "constant throughout:" in line)

    hoisted = header.split(":", 1)[1].split(",")[0].split("=")[0].strip()
    assert not any(f"{hoisted}=" in line for line in lines if line.startswith("    "))


def test_a_time_only_filter_interleaves_rather_than_separating(analysis):
    """'Everything was slow last Tuesday' is a question about a period, not about
    a journey, and reading it needs one sequence rather than several."""
    window = analysis.log.window
    chosen = build(analysis, Filter(after=window[0], before=window[1]), limit=2)
    assert chosen.interleaved
    assert "one sequence" in "\n".join(render(chosen, analysis.routes))


def test_a_fragmented_identifier_is_marked_where_it_breaks(analysis):
    """The most consequential thing to see in a timeline: from this record on, a
    search by that identifier finds nothing, and nothing says so."""
    broken = [j for j in analysis.grouping.journeys.values() if len(j.other_ids("trace_id")) > 1]
    if not broken:
        pytest.skip("no fragmented journeys in this export")

    text = "\n".join(render(build(analysis, Filter(values=(broken[0].value,))), analysis.routes))
    assert "!" in text and "distinct trace identifiers" in text


def test_an_empty_result_says_so_rather_than_rendering_nothing(analysis):
    chosen = build(analysis, Filter(where={"does_not_exist": "at_all"}))
    assert chosen.matched == []
    assert "no journeys match" in "\n".join(render(chosen, analysis.routes))
