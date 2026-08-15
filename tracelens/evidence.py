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
        self._refs.update(str(r) for r in refs if r)

    def add_journeys(self, values: Iterable[str]) -> None:
        self.add(values)

    def add_defects(self, defects: Iterable[Defect]) -> None:
        for defect in defects:
            self._refs.add(defect.id)
            self._refs.update(defect.affected)

    def knows(self, ref: str) -> bool:
        return str(ref) in self._refs

    @property
    def refs(self) -> set[str]:
        return set(self._refs)

    def __len__(self) -> int:
        return len(self._refs)
