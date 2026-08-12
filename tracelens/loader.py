"""Load the telemetry export off disk."""

from __future__ import annotations

import json
from pathlib import Path

from .model import AcceptedMessage, Dataset, Deploy, LogRecord, Span, Symptom

FILES = {
    "spans": "spans.json",
    "logs": "logs.json",
    "deploys": "deploys.json",
    "accepted": "accepted_messages.json",
    "symptoms": "symptoms.json",
}


def find_data_dir(start: Path | None = None) -> Path:
    """Locate the data directory.

    The provided export nests as data/data/, so accept either layout rather than
    making the caller care.
    """
    root = Path(start) if start else Path(__file__).resolve().parent.parent
    candidates = [root, root / "data", root / "data" / "data"]
    for candidate in candidates:
        if (candidate / FILES["spans"]).exists():
            return candidate
    raise FileNotFoundError(
        f"could not find {FILES['spans']} in any of: {', '.join(str(c) for c in candidates)}"
    )


def _read(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_dataset(data_dir: Path | str | None = None) -> Dataset:
    directory = find_data_dir(Path(data_dir)) if data_dir else find_data_dir()

    raw_symptoms = _read(directory / FILES["symptoms"])
    symptom_list = raw_symptoms["symptoms"] if isinstance(raw_symptoms, dict) else raw_symptoms

    return Dataset(
        spans=[Span.from_json(r) for r in _read(directory / FILES["spans"])],
        logs=[LogRecord.from_json(r) for r in _read(directory / FILES["logs"])],
        deploys=[Deploy.from_json(r) for r in _read(directory / FILES["deploys"])],
        accepted=[AcceptedMessage.from_json(r) for r in _read(directory / FILES["accepted"])],
        symptoms=[Symptom(source=s.get("from", "unknown"), text=s["text"]) for s in symptom_list],
    )
