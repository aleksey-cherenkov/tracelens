"""What a defect is, and what the model is allowed to cite.

Two small types and one gate.

`Defect` is the only thing this tool asserts. Everything else it produces — routes,
timelines, distributions — is data, and the model reads it. A defect is different:
it is a claim about the *telemetry*, and it carries a `limits` list saying what
that defect prevents you concluding. That list is the reason this file exists.

`SliceIndex` is what makes the citation rule enforceable rather than aspirational.
The model now reads raw records, so it can no longer be limited to citing
pre-computed findings. What it can be limited to is identifiers that actually
appeared in a slice it was given — checked in code, in validator.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Defect:
    """Something wrong with the input, and what it stops you concluding."""

    id: str
    title: str
    detail: str

    limits: list[str] = field(default_factory=list)
    """The important field.

    Telemetry is usually partly broken, and a tool that produces a confident
    answer over broken input is doing the thing this project exists to avoid.
    "No record ever reports a failure" is a fact; "therefore absence of error is
    not evidence of success here" is what changes what you do next.
    """

    evidence: list[str] = field(default_factory=list)
    """Pre-rendered lines a person can check by hand. The model never formats a
    number that appears here."""

    affected: list[str] = field(default_factory=list)
    would_resolve: list[str] = field(default_factory=list)
    params: dict[str, object] = field(default_factory=dict)
    """Thresholds this defect depended on, printed so they are never implicit."""

    def as_dict(self, exemplars: int = 5) -> dict:
        payload = {
            "defect": self.id,
            "title": self.title,
            "detail": self.detail,
            "limits": list(self.limits),
        }
        if self.evidence:
            payload["evidence"] = list(self.evidence)
        if self.affected:
            payload["affected_journeys"] = self.affected[:exemplars]
            payload["affected_count"] = len(self.affected)
        if self.would_resolve:
            payload["would_resolve"] = list(self.would_resolve)
        if self.params:
            payload["params"] = dict(self.params)
        return payload


class SliceIndex:
    """Every identifier the model was actually shown.

    Grows as slices are returned. A hypothesis citing anything outside it is
    dropped in validator.py -- the model cannot introduce a journey identifier,
    because none of its inputs contained an unreferenced one.
    """

    def __init__(self) -> None:
        self._refs: set[str] = set()

    def add(self, refs: Iterable[str]) -> None:
        """Store each identifier, and the value half of any `key=value` token.

        A deploy renders as `sha=c52a0f9` in a timeline. The model may quote the
        whole token or just the sha, and both are honest citations of something
        it was shown. Two live runs failed in opposite directions before this
        indexed both forms -- normalise once here rather than guessing at the
        gate.
        """
        for ref in refs:
            text = str(ref).strip().strip(".,;:()[]")
            if not text:
                continue
            self._refs.add(text)
            _, sep, value = text.partition("=")
            if sep and value:
                self._refs.add(value)

    def add_journeys(self, values: Iterable[str]) -> None:
        self.add(values)

    def add_defects(self, defects: Iterable[Defect]) -> None:
        for defect in defects:
            self._refs.add(defect.id)
            self._refs.update(defect.affected)

    def add_overview(self, overview: dict) -> None:
        """Everything visible in the opening payload.

        This was missed and it broke the gate on the first live run. The route
        table is in the first prompt, so the model can cite `route-4` having
        never called a tool -- and the index, populated only by tool calls,
        rejected it as fabricated. Nine of twelve answers were thrown away for
        citing something they had genuinely been shown.

        The rule is simply: if it was in front of the model, it is citable.
        """
        self.add(overview.get("services") or [])
        self.add(overview.get("record_kinds") or [])
        self.add([(overview.get("journeys") or {}).get("key") or ""])
        for route in (overview.get("routes") or {}).get("routes") or []:
            self._refs.add(str(route.get("route", "")))
            self.add(route.get("exemplars") or [])
        for defect in (overview.get("input_quality") or {}).get("defects") or []:
            self._refs.add(str(defect.get("defect", "")))
            self.add(defect.get("affected_journeys") or [])
        self._refs.discard("")

    def knows(self, ref: str) -> bool:
        return str(ref) in self._refs

    @property
    def refs(self) -> set[str]:
        return set(self._refs)

    def __len__(self) -> int:
        return len(self._refs)
