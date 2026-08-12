"""Join resolution, including the two cases the obvious join gets wrong."""

from __future__ import annotations

from collections import Counter

from tracelens.join import JoinMethod
from tracelens.model import Stage

PUSH = {"corr-0005", "corr-0010", "corr-0020", "corr-0036"}
DUPLICATES = {"corr-0014", "corr-0022", "corr-0035"}


def test_join_method_census(traces):
    counts = Counter(j.method for t in traces.values() for j in t.joins)
    assert counts[JoinMethod.PARENT_CHILD] == 218
    assert counts[JoinMethod.CORRELATION_FALLBACK] == 8
    assert counts[JoinMethod.ABSENT] == 4
    assert sum(counts.values()) == 230


def test_all_sms_fall_back_to_correlation_at_the_queue_hop(traces):
    sms = [t for t in traces.values() if t.channel == "sms"]
    assert len(sms) == 8
    for trace in sms:
        record = trace.join_for(Stage.PUBLISH_QUEUE)
        assert record.method is JoinMethod.CORRELATION_FALLBACK
        assert trace.trace_context_break


def test_no_email_loses_trace_context(traces):
    """The contrast is the diagnosis: same hop, one channel breaks, one doesn't."""
    email = [t for t in traces.values() if t.channel == "email"]
    assert len(email) == 29
    assert [t.correlation_id for t in email if t.trace_context_break] == []


def test_push_is_absent_after_publish_topic(traces):
    for correlation_id in PUSH:
        trace = traces[correlation_id]
        assert trace.join_for(Stage.PUBLISH_TOPIC).method is JoinMethod.ABSENT
        assert trace.terminal_stage is Stage.PUBLISH_TOPIC
        assert not trace.reached_provider


def test_nested_versus_sequential_typing(traces):
    """A naive next.start - previous.end gives -26ms and -18ms on the two nested
    transitions. Typing them stops a negative number reaching a dashboard."""
    trace = traces["corr-0001"]
    nested = {j.frm for j in trace.joins if j.nested}
    assert nested == {Stage.ACCEPT, Stage.CONSUME_TOPIC}

    for record in trace.joins:
        if record.nested:
            assert record.gap_ms > 0, "a nested offset must never be reported negative"

    gaps = {j.frm: j.gap_ms for j in trace.joins}
    assert gaps[Stage.PUBLISH_TOPIC] == 269.0
    assert gaps[Stage.PUBLISH_QUEUE] == 379.0


def test_async_hops_have_zero_variance(traces):
    """No percentile engine is justified: p50 == p95 == p99 == min == max."""
    topic = {t.join_for(Stage.PUBLISH_TOPIC).gap_ms for t in traces.values()
             if t.join_for(Stage.PUBLISH_TOPIC).method is not JoinMethod.ABSENT}
    queue = {t.join_for(Stage.PUBLISH_QUEUE).gap_ms for t in traces.values()
             if t.join_for(Stage.PUBLISH_QUEUE)}
    assert topic == {269.0}
    assert queue == {379.0}


def test_attempts_are_split_by_walking_back_from_the_send(traces):
    """Both consume spans share one publish parent, so walking forward cannot
    separate the attempts. Walking backwards from each send can."""
    for correlation_id in DUPLICATES:
        trace = traces[correlation_id]
        assert len(trace.attempts) == 2
        first, second = trace.attempts
        assert first.consume is not None and second.consume is not None
        assert first.consume.span_id != second.consume.span_id
        assert first.send.parent_span_id == first.consume.span_id
        assert second.send.parent_span_id == second.consume.span_id
        # The shared parent is the single publish, which is why forward-walking fails.
        assert first.consume.parent_span_id == second.consume.parent_span_id


def test_receive_count_is_absent_on_the_redelivered_consume(traces):
    """The trap C3 exists to avoid: filtering on 'receive_count == 1 or absent'
    would let the duplicate consume back in, because its attributes are empty."""
    trace = traces["corr-0014"]
    second = trace.attempts[1]
    assert second.receive_count == 2, "the send carries the counter"
    assert "sqs.receive_count" not in second.consume.attributes, "the consume does not"


def test_end_to_end_excludes_redeliveries(traces):
    """First-span-start to last-span-end would report ~32,000ms for these three
    and invent a latency incident on the days duplicates happened."""
    for correlation_id in DUPLICATES:
        assert traces[correlation_id].end_to_end_ms < 1200
