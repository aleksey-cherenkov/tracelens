"""Integrity invariants.

These matter because they establish that the gaps found later are real signal
rather than export artifacts. If spans were missing at random, "push has no
sender span" would prove nothing.
"""

from __future__ import annotations


def test_dataset_shape(dataset):
    assert len(dataset.accepted) == 41
    assert len(dataset.spans) == 273
    assert len(dataset.logs) == 2820
    assert len(dataset.deploys) == 4
    assert len(dataset.symptoms) == 5


def test_no_duplicate_ledger_ids(dataset):
    ids = [m.correlation_id for m in dataset.accepted]
    assert len(ids) == len(set(ids))


def test_no_orphans_in_either_direction(dataset):
    ledger = {m.correlation_id for m in dataset.accepted}
    spans = {s.correlation_id for s in dataset.spans if s.correlation_id}
    assert spans - ledger == set(), "spans reference messages absent from the ledger"
    assert ledger - spans == set(), "ledger messages produced no spans at all"


def test_accepted_at_matches_accept_span(dataset, traces):
    from tracelens.model import Stage

    for message in dataset.accepted:
        accept = traces[message.correlation_id].stage_spans[Stage.ACCEPT]
        assert accept.start_time == message.accepted_at


def test_no_dangling_parent_span_ids(dataset):
    known = {s.span_id for s in dataset.spans}
    dangling = [
        s for s in dataset.spans if s.parent_span_id and s.parent_span_id not in known
    ]
    assert dangling == []


def test_no_log_contradicts_its_spans(dataset):
    by_correlation: dict[str, set[str]] = {}
    for span in dataset.spans:
        if span.correlation_id:
            by_correlation.setdefault(span.correlation_id, set()).add(span.trace_id)

    contradictions = [
        log
        for log in dataset.logs
        if log.correlation_id
        and log.trace_id
        and log.trace_id not in by_correlation.get(log.correlation_id, set())
    ]
    assert contradictions == []


def test_handles_nested_data_layout(dataset):
    """The export ships as data/data/. The loader accepts either layout rather
    than making the caller care, so the reviewer can diff against the original."""
    assert dataset.spans, "loader found no spans"
