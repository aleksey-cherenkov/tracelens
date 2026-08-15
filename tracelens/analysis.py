"""Everything the tool knows about one export, assembled once.

Four stages and no cleverness between them:

    load → group into journeys → build the route table → assess input quality

There is no findings layer. An earlier version had detectors encoding failures I
had already found by hand, then invariants encoding properties I thought were
universal. Both produced verdicts. What replaced them is a route table and a
timeline, which produce *data* — and the reading is done by whoever asked the
question, model or person.

The one thing this module still asserts is the quality defects, because a limit
that is merely computed is a limit nobody applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import journeys as journeys_module
from . import quality, routes, timeline
from .events import EventLog, format_time
from .loader import Export


@dataclass
class Analysis:
    log: EventLog
    grouping: journeys_module.Grouping
    routes: routes.Routes
    quality: quality.Quality
    timing: timeline.Timeline

    @property
    def limits(self) -> list[str]:
        return self.quality.limits

    def overview(self) -> dict:
        """The opening payload. Bounded by route count, not by traffic.

        This is the property that lets the design survive go-live unchanged: a
        route table is a dozen lines whether the export holds 41 journeys or 41
        million, and the slices that follow are capped individually.
        """
        window = self.log.window
        return {
            "window": (
                {"from": format_time(window[0]), "to": format_time(window[1])}
                if window
                else None
            ),
            "records": len(self.log),
            "services": sorted(self.log.sources),
            "record_kinds": sorted(self.log.kinds),
            "journeys": {
                "key": self.grouping.key,
                "count": len(self.grouping),
                "unjoined_records": self.grouping.unjoined,
                "key_candidates": [c.as_dict() for c in self.grouping.candidates],
                "note": (
                    "The key was not inferred from meaning; it was supplied or "
                    "defaulted to the highest-coverage identifier that groups "
                    "records across more than one service. The others are listed "
                    "so a wrong choice is visible."
                ),
            },
            "routes": self.routes.as_dict(),
            "timing": self.timing.as_dict(),
            "input_quality": self.quality.as_dict(),
        }


def analyse(log: EventLog, key: str | None = None) -> Analysis:
    grouping = journeys_module.build(log, key)
    route_table = routes.build(log, grouping.journeys)
    return Analysis(
        log=log,
        grouping=grouping,
        routes=route_table,
        quality=quality.assess(log, grouping),
        timing=timeline.build(log, grouping, node_of=route_table.node_of),
    )


def of_export(export: Export, key: str | None = None) -> Analysis:
    return analyse(export.log, key)
