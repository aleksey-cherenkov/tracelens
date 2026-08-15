"""Load an export off disk into one flat list of events.

No file is required and no filename is special. Every `*.json` in the directory is
read, and the filename becomes the event `kind` -- so an export containing
`payments.json` or `audit.json` loads without anybody editing this file. That is
the whole point: the tool should not need to be taught what files a team happens
to produce.

Two filenames are treated differently, and only for display: `symptoms.json`
holds the complaints being investigated rather than telemetry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog, normalise_all

SYMPTOM_FILE = "symptoms.json"

# Used only to locate the directory, never to decide what to parse.
ANCHORS = ("spans.json", "logs.json", "events.json")


@dataclass(frozen=True)
class Symptom:
    source: str
    text: str


@dataclass
class Export:
    log: EventLog
    symptoms: list[Symptom]
    directory: Path
    counts: dict[str, int]
    skipped: dict[str, str]
    """file -> why nothing usable came out of it. Surfaced rather than swallowed:
    a file that silently contributed nothing looks exactly like a file that was
    not there."""


def find_data_dir(start: Path | None = None) -> Path:
    """Locate the data directory. The provided export nests as data/data/, so
    accept either layout rather than making the caller care."""
    root = Path(start) if start else Path(__file__).resolve().parent.parent
    candidates = [c for c in (root, root / "data", root / "data" / "data") if c.is_dir()]

    # Deepest first: a repo root usually has a stray .json in it, and picking that
    # over the actual export is a silent wrong answer rather than a loud one.
    for candidate in reversed(candidates):
        if any((candidate / anchor).exists() for anchor in ANCHORS):
            return candidate
    for candidate in reversed(candidates):
        if len(list(candidate.glob("*.json"))) > 1:
            return candidate
    raise FileNotFoundError(f"no JSON export found at or below {root}")


def _records(payload) -> list[dict]:
    """Accept a bare list, or a dict wrapping one under any key."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def load(data_dir: Path | str | None = None) -> Export:
    directory = find_data_dir(Path(data_dir) if data_dir else None)

    events = []
    symptoms: list[Symptom] = []
    counts: dict[str, int] = {}
    skipped: dict[str, str] = {}

    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            skipped[path.name] = f"unreadable: {exc}"
            continue

        records = _records(payload)
        if path.name == SYMPTOM_FILE:
            symptoms = [
                Symptom(source=r.get("from", "unknown"), text=r.get("text", ""))
                for r in records
                if r.get("text")
            ]
            continue

        kind = path.stem.rstrip("s") if path.stem.endswith("s") else path.stem
        parsed = normalise_all(records, kind)
        counts[path.name] = len(parsed)
        if records and not parsed:
            skipped[path.name] = (
                f"{len(records)} record(s), none with a recognisable timestamp — "
                "add its time field to events.TIME_KEYS"
            )
        events.extend(parsed)

    return Export(
        log=EventLog(sorted(events, key=lambda e: e.at)),
        symptoms=symptoms,
        directory=directory,
        counts=counts,
        skipped=skipped,
    )
