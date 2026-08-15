"""The model's tool surface: three read-only, bounded views.

The model drives which slice it reads. That's the change from the previous
design, where code guessed which findings a complaint meant and handed over a
fixed set. Now the opening payload is a route table — a dozen lines however much
traffic there is — and the model asks for the timeline it wants.

Deliberately views rather than queries. There is no `run_query(sql)`, no write
path, and no way to ask for everything. That buys three things: the model cannot
author an expensive scan, every answer traces to a named function you can re-run
by hand, and the surface is small enough to describe accurately in the prompt.

Every returned identifier is added to the `SliceIndex`, which is what the
validator checks citations against.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..analysis import Analysis
from ..events import parse_time
from ..evidence import SliceIndex
from ..slices import Filter, select
from ..slices import render as render_slice

MAX_JOURNEYS = 3

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_routes",
        "description": (
            "Every distinct path journeys took, with how many took each. Start "
            "here. A route that ends earlier than the others is where journeys "
            "stopped; a route with a repeated node did that work twice."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_slice",
        "description": (
            "The timeline for a set of journeys: every record in time order, "
            "recorded changes inline at their timestamps, plus one contrast "
            "journey from the most common route so a difference is visible. "
            "Filter by attribute value, by route number, or by time window. "
            "Capped and the cap is reported."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "where": {
                    "type": "object",
                    "description": (
                        "attribute name to value, e.g. {\"region\": \"eu-west\"}. "
                        "Use only attribute names and values that appeared in the "
                        "overview — this tool knows nothing about your system's "
                        "vocabulary beyond what the data showed."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "route": {"type": "integer", "description": "a route number from list_routes"},
                "after": {"type": "string", "description": "ISO timestamp"},
                "before": {"type": "string", "description": "ISO timestamp"},
                "journeys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "specific journey identifiers",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_journey",
        "description": (
            "One journey in full: every record in time order, with its route and "
            "any recorded change that landed inside its span."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
]


class ToolBox:
    """Executes tool calls against a precomputed analysis. No I/O, no queries."""

    def __init__(self, analysis: Analysis, index: SliceIndex) -> None:
        self.analysis = analysis
        self.index = index
        self.calls: list[tuple[str, dict]] = []

    def run(self, name: str, arguments: dict[str, Any]) -> dict:
        self.calls.append((name, dict(arguments)))
        handler: Callable[..., dict] | None = getattr(self, f"_{name}", None)
        if handler is None:
            return {"error": f"unknown tool '{name}'"}
        try:
            return handler(**arguments)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # a bad call costs one iteration, not the run
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- tools -----------------------------------------------------------------

    def _list_routes(self) -> dict:
        payload = self.analysis.routes.as_dict()
        for route in self.analysis.routes.routes:
            self.index.add(route.journeys)
            self.index.add([f"route-{route.index}"])
        return payload

    def _get_slice(
        self,
        where: dict | None = None,
        route: int | None = None,
        after: str | None = None,
        before: str | None = None,
        journeys: list[str] | None = None,
    ) -> dict:
        moment_after = parse_time(after) if after else None
        moment_before = parse_time(before) if before else None
        if after and moment_after is None:
            return {"error": f"could not parse after={after!r} as a timestamp"}
        if before and moment_before is None:
            return {"error": f"could not parse before={before!r} as a timestamp"}

        chosen = select(
            self.analysis.log,
            self.analysis.grouping,
            self.analysis.routes,
            Filter(
                where={str(k): str(v) for k, v in (where or {}).items()},
                route=route,
                after=moment_after,
                before=moment_before,
                values=tuple(journeys or ()),
            ),
            max_journeys=MAX_JOURNEYS,
        )

        self.index.add(j.value for j in chosen.shown)
        self.index.add([j.value for j in chosen.matched])
        if chosen.contrast is not None:
            self.index.add([chosen.contrast.value])
        self.index.add(
            part for change in chosen.changes for part in change.describe().split()
        )
        return chosen.as_dict(self.analysis.routes)

    def _get_journey(self, value: str) -> dict:
        journey = self.analysis.grouping.journeys.get(value)
        if journey is None:
            available = list(self.analysis.grouping.journeys)[:5]
            return {"error": f"no journey '{value}'", "for_example": available}

        chosen = select(
            self.analysis.log,
            self.analysis.grouping,
            self.analysis.routes,
            Filter(values=(value,)),
            max_journeys=1,
        )
        self.index.add([value])
        if chosen.contrast is not None:
            self.index.add([chosen.contrast.value])

        route = self.analysis.routes.of_journey(value)
        return {
            "journey": value,
            "route": route.index if route else None,
            "records": len(journey.events),
            "duration_ms": round(journey.duration_ms, 1),
            "services": journey.sources,
            "distinct_trace_ids": len(journey.other_ids("trace_id")),
            "timeline": render_slice(chosen, self.analysis.routes),
        }
