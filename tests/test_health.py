from __future__ import annotations

from tracelens.health import summarise


def test_three_error_rates_reported_separately(health):
    errors = health.errors
    assert (errors.span_status_errors, errors.total_spans) == (0, 273)
    assert (errors.provider_errors, errors.provider_calls) == (6, 40)
    assert (errors.delivery_failures, errors.accepted) == (4, 41)


def test_divergence_is_detected(health):
    """Zero error signal alongside four undelivered messages is the whole reason
    nobody noticed. Collapsing these into one 'error rate' hides it."""
    assert health.errors.diverges is True
    assert health.errors.span_status_rate == 0.0
    assert health.errors.delivery_failure_rate > 0


def test_no_percentiles_on_zero_variance_hops(health):
    for name in ("topic", "channel-queue"):
        latency = health.hops[name].latency
        assert latency.n == 37
        assert latency.has_variance is False
        assert latency.p95 is None and latency.p99 is None
        assert latency.variance_note == "none"


def test_percentiles_exist_where_values_actually_differ(health):
    email = health.end_to_end["email"]
    assert email.has_variance is True
    assert email.p95 is not None
    assert email.minimum == 990.0 and email.maximum == 4875.0


def test_nested_transitions_are_not_reported_as_hop_latency(health):
    """A nested child's offset is not a hop gap, so it never enters the sample."""
    for name in ("ingest:accept->publish", "orchestrator:consume->route"):
        assert health.hops[name].nested is True
        assert health.hops[name].latency.n == 0


def test_retries_and_redeliveries_are_never_summed(health):
    assert health.retries_provider == 18
    assert health.redeliveries == 3


def test_worst_hop_points_at_the_loss(health):
    worst = health.worst_hop()
    assert worst.name == "topic"
    assert worst.absent == 4


def test_summarise_handles_degenerate_input():
    empty = summarise([])
    assert empty.n == 0 and empty.median is None
    single = summarise([5.0])
    assert single.has_variance is False and single.p95 is None
