from __future__ import annotations

from tracelens.accounting import Outcome
from tracelens.model import Stage


def test_delivery_funnel(accounting):
    assert accounting.total == 41
    assert accounting.delivered_once == 34
    assert accounting.delivered_duplicate == 3
    assert accounting.stopped == 4
    assert accounting.provider_calls == 40


def test_shares_match_the_writeup(accounting):
    assert round(accounting.share(accounting.delivered_once) * 100, 1) == 82.9
    assert round(accounting.share(accounting.delivered_duplicate) * 100, 1) == 7.3
    assert round(accounting.share(accounting.stopped) * 100, 1) == 9.8


def test_everything_lost_stopped_at_the_same_stage(accounting):
    assert dict(accounting.stopped_by_stage) == {Stage.PUBLISH_TOPIC: 4}
    assert accounting.stopped_ids() == ["corr-0005", "corr-0010", "corr-0020", "corr-0036"]


def test_per_channel(accounting):
    email = accounting.by_channel["email"]
    assert (email.accepted, email.delivered, email.lost, email.duplicated) == (29, 29, 0, 3)
    assert email.trace_intact == 29

    sms = accounting.by_channel["sms"]
    assert (sms.accepted, sms.delivered, sms.lost, sms.duplicated) == (8, 8, 0, 0)
    assert sms.trace_intact == 0, "every SMS message loses trace context"

    push = accounting.by_channel["push"]
    assert (push.accepted, push.delivered, push.lost) == (4, 0, 4)
    assert push.loss_rate == 1.0


def test_ledger_is_the_left_side_of_the_join(accounting, dataset):
    """Accounting must be driven by the promise ledger, not by the spans.

    A message that produced no telemetry at all still has to appear as a loss --
    which is exactly what sampling would hide in production.
    """
    assert accounting.total == len(dataset.accepted)


def test_duplicates_are_counted_as_delivered_not_lost(accounting):
    duplicates = [o for o in accounting.outcomes if o.outcome is Outcome.DELIVERED_DUPLICATE]
    assert {o.correlation_id for o in duplicates} == {"corr-0014", "corr-0022", "corr-0035"}
    assert all(o.provider_calls == 2 for o in duplicates)
