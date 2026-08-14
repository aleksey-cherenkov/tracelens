# Tracelens

Message pipeline trace analyzer — Bloomerang Platform Engineering take-home.

```
product ──▶ comms-ingest ──topic──▶ comms-orchestrator ──queue──▶ comms-sender ──▶ provider
```

`accepted_messages.json` is the promise ledger: 41 messages the platform returned
`202` for over 10 days. This tool asks what happened to each one.

**The platform broke its `202` promise for 9.8% of messages and 100% of one
channel — and every span in the export reports `status: OK`.**

[`DESIGN.md`](DESIGN.md) — how it's built · [`DECISIONS.md`](DECISIONS.md) — what
I got wrong and changed

---

## Run it

```bash
pip install -e ".[ai,dev]"   # Python 3.10+; base dep is just rich
tracelens findings           # everything, three layers
pytest                       # 112 tests, ~2s
```

Parts 1 and 3 have **no model in the loop** — every number below comes from
deterministic code. Only triage calls the API (`claude-sonnet-5`,
`effort: medium`, ~$0.05/run). `tracelens keys` shows whether a live call will
happen before you spend anything; `tracelens keys --set|--clear` manages a
gitignored `.env`. Without a key, triage falls back to an offline stub and says
so.

```
tracelens trace corr-0003            # waterfall, per-hop timing, join method per hop
tracelens trace corr-0005            # truncated path — where a message died
tracelens account [--by channel|tenant|day]
tracelens health [--service X] [--hop Y]
tracelens findings [--layer detector|invariant|novelty] [--severity critical]
tracelens topology                   # the graph, learned from the data
tracelens baseline --save | --against DIR
tracelens logs [--corr ID] [--grep P] [--show-suppressed]
tracelens triage --symptom 3 [--record] [--effort high]
tracelens report --out report.html
```

`python scripts/verify_claims.py` recomputes all 101 figures in this document
from `data/` and exits non-zero on any mismatch.

---

## What I found

Three labels show how sure each finding is: **Observed** = seen directly in the
data. **Inferred** = the likely cause, but not proven. **Mixed** = part of the
finding is solid, part is still an open question.

| | Finding | What the data shows | How sure |
|---|---|---|---|
| **F1** | **Every push notification goes missing** on its way from the topic to the orchestrator | All 4 accepted push messages get published, then none of them ever arrive downstream. The ingest service logs 41 publishes; the orchestrator only logs 37 consumes — push accounts for the entire 4-message gap | Observed |
| **F2** | The "duplicate emails" are the queue **redelivering**, not the app sending twice | 3 of 29 emails went out twice. Each case traces back to one publish and two deliveries from the same message — the second delivery fires 31 seconds after the first, tagged as a redelivery (`receive_count: 2`) | Observed |
| **F3** | The March 9 slowdown was the **email provider throttling us**, not the deploy some people suspected | 6 sends that day took ~4.1s instead of the usual ~235ms, with `429` (rate-limit) errors and retries — but all eventually succeeded. The deploy some pointed to actually shipped 5 hours *after* the slowdown started | Mixed |
| **F4** | SMS messages **lose their trace ID** partway through | Every single SMS trace (8/8) splits into two disconnected trace IDs instead of one continuous one; email never does this (0/29). The sending service also logs nothing for SMS, so there's no log trail to fall back on either | Observed |
| **F5** | Errors don't show up as errors anywhere | Not one span (0 of 273) is marked as failed, and not one log line (0 of 2,820) is at ERROR level — even though rate-limit errors and retries did happen. They're only visible if you dig into span attributes | Observed |
| **F6** | The queue-depth metric — which should have caught the missing push messages — is **fake/hardcoded** | It reports `depth=0` every single time, 1,200 times in a row, with no queue name attached and no trace ID. There's no metric at all for the push queue specifically | Observed |
| **F7** | **96% of all logs can't be matched to anything** | 2,700 of 2,820 log lines have no correlation ID and no trace ID, so they can't be tied to a specific message. Across the whole time window, only about 120 log lines are actually usable for investigation | Observed |

**About F1 — the story people were told is wrong.** The Payments team described
this as a one-off problem with "our donation campaign last week." It isn't: the
4 lost messages come from **three different tenants** and are spread across the
**entire time window**, not one campaign. Push and SMS both go through the same
provider (AWS Pinpoint), and SMS works fine — so the provider isn't at fault.
The real problem is the connection between the topic and the queue.

**About F3 — the headline is right, one detail underneath isn't settled.**
Some people suggested the deploy *caused* the slowdown, but that doesn't hold up —
it shipped after the slowdown already started. But *why things went back to normal* is genuinely
unclear: a different deploy (an SDK version bump) landed right in the middle of
the recovery window. Two explanations both fit the data and this data can't tell
them apart: either the provider's rate-limiting simply expired on its own, or the
old SDK was actually causing the problem and the update fixed it. Looking at the
code change itself would settle it. What we can say for sure: it wasn't caused by
anything on our side sending too fast — every single throttled email that day
(5 of 5) came from different tenants (5 of the 6 total), not a burst from one
sender.

### Delivery accounting — `tracelens account`

| Outcome | Count | Share |
|---|---|---|
| Reached provider once | 34 | 82.9% |
| Reached more than once | 3 | 7.3% |
| **Never reached provider** | **4** | **9.8%** |

| Channel | Accepted | Lost | Duplicated | Trace intact |
|---|---|---|---|---|
| email | 29 | 0 | 3 | 29/29 |
| sms | 8 | 0 | 0 | **0/8** |
| push | 4 | **4** | 0 | n/a |

---

## What I'd fix first

Bugs and anything customer-facing go first — that's a business call as much as
an engineering one, so I'd confirm this ordering with them rather than treat it
as mine alone to set.

1. **F1 and F2, now — both are live bugs.** F1 (push silently lost) came in as
   an internal report from Payments, not a supporter complaint, but it's still a
   bug and a bad one: 100% of a channel, every tenant, and the fix is checking
   the SNS subscription filter policy in a console — minutes, possibly
   recoverable from a DLQ today. F2 (duplicate email) is the one supporters
   actually notice — a second donation receipt is annoying and erodes trust in
   the product every time it lands — and the fix (delete-after-send +
   idempotency key) is small and local. Neither should wait on anything below.
2. **Fix the logging on those two features while you're in there.** F1's
   mechanism and F2's cause are both still partly hypothesis, not proof — the
   table above marks F2 "observed / inferred cause," and the SNS theory hasn't
   been confirmed in the console yet. Rather than waiting on a general error-signal
   overhaul before touching either bug, tighten the logs/traces on just these two
   code paths as part of the fix — enough to actually confirm the bug is gone,
   for a fraction of the cost of doing it system-wide.
3. **F5 done properly, and the delivery ledger, are real — but they're next, not
   now.** Comprehensive error signal and a durable ledger (see below) are the
   difference between diagnosing this class of bug by hand and having the
   system tell you outright. Worth doing. Not worth bumping ahead of an actual
   defect that's dropping messages today.
4. **F6/F7 (the noise) — investigate before deleting.** My first instinct was
   "stop emitting it," but I don't actually know why it's there, and deleting
   something with no idea what it's for is how you lose a signal someone
   depends on — it could easily be an uptime check, or how ops notices an
   unhealthy instance getting swapped. Worth an afternoon to find out who reads
   it and why. If it's genuinely dead, cut it; if it's serving ops, the right
   fix is a viewer that filters this noise out by default for devs chasing a
   message, with the option to filter back in for whoever actually needs it.
5. **F4 (SMS trace propagation) stays low.** It costs investigation time, not
   wrong outcomes, and the correlation-first join already routes around it.

**Not on this list: F3.** It self-resolved, lost nothing, and the follow-up is
reading one PR diff. A resolved latency blip doesn't outrank two live bugs.

---

## AI: in and out

**Code owns every number.** Joins, counts, durations, rates, window boundaries,
affected IDs, deploy arithmetic, severity, confidence. Parts 1 and 3 have no model
at all.

**The model owns language** — mapping a vague complaint onto detectors, ranking by
relevance, explaining the causal story. "A couple of supporters got the same
confirmation email twice" → the duplicate detector is a *semantic* match, not a
keyword one.

**Four honesty mechanisms, strongest first:**

1. **Evidence-first.** Findings and citations are generated before the model is called; it selects from a fixed set and has no unreferenced IDs to hallucinate from.
2. **Citation validator — a hard gate.** Any hypothesis citing a ref absent from the index is *dropped*. Code, not prompt.
3. **Confidence is inherited**, never asserted. The model does own hypothesis *order* — relevance ranking is the fuzzy work code does badly, and forcing a severity sort would answer every complaint with the push outage.
4. **Mandatory alternatives.** Two distinct findings, or one carrying alternatives, or `insufficient_evidence`. This is what preserves F3's ambiguity.

**How I'd know it was wrong:** the 5 symptoms are a golden set pinned to finding
IDs; an adversarial complaint ("our webhooks stopped firing") must return
insufficient evidence; symptom 3 must rank throttling first, cite the 5-hour gap,
*and* keep the ambiguity. Stability is **measured** — each case runs repeatedly
and the top-ranked finding ID must not move. Ranking instability is a reported
defect, not an accepted cost.

`examples/symptom-3.json` is a **live** run; the rest are stub runs, each file
saying which in its `note`.

---

## Problems nobody has seen yet

Every detector rule was written *after* I knew the answer. That's a closed world:
point it at a new failure and it produces nothing — and "no findings" looks
exactly like "healthy", which is F5 again. So the tool runs three layers and only
the first is pipeline-specific.

| Layer | Knows this pipeline? | Answers | On unfamiliar data |
|---|---|---|---|
| **Detectors** `D*` | yes | *why* — mechanism and cause | skipped, with a reason |
| **Invariants** `INV.*` | no | *what* broke | fully working |
| **Novelty** `NOV.*` | no | *what changed* | fully working |

**Invariants** state what must hold of *any* pipeline — conservation across a hop,
path shape, settlement against the ledger, context integrity, single visit,
referential integrity — and report violations. A violation is novel by
construction. On this data they independently rediscover F1, F2, F4 and F7, which
`tracelens findings` reports as corroboration. Conservation even names the
discriminating attribute (`message_type=push`) without knowing channels exist.

**Topology is learned**, not configured: attribute values are templated out of
span names, so `publish email-queue` and `publish sms-queue` collapse to
`publish {message_type}-queue` and a fourth channel lands on an existing node.

`tests/test_unfamiliar_dataset.py` proves it — a five-stage payments pipeline with
a mid-pipeline drop no detector encodes. Writing it found three real bugs
([`DECISIONS.md`](DECISIONS.md)).

---

## Going live

30-day retention, queries cost money, five teams produce into this pipeline.
Full treatment in [DESIGN §7](DESIGN.md#7-going-live). Four things matter:

**You cannot sample an absence.** F1 has zero error spans and no non-`OK` status —
its signal *is* the missing span. Head sampling makes "no sender span" identical
to "not sampled"; tail sampling selects on error and duration, and a dropped
message has neither. The most severe finding is the one that disappears the moment
you make telemetry affordable, and it fails in the safe-looking direction: your
dashboards get cleaner as reliability gets worse.

**So accounting moves off traces onto a delivery ledger** — two durable rows per
message (`accepted` at ingest, `settled` at the sender), keyed by
`correlation_id`, on its own 13-month retention, with a job that alerts on
accepted-but-not-settled past SLA. That turns F1 from a week-late discovery into a
page within minutes.

**How logs get stored and retrieved.** Three tiers, chosen by how they'll be read.
*Hot* (7 days, in the search backend) holds only message-scoped records — the ones
carrying `correlation_id` or `trace_id` — indexed on `correlation_id`, `service`
and time, because that is how every incident query starts. *Warm* (30 days,
columnar object storage, Parquet partitioned by `date/service`) is scanned rather
than indexed and costs roughly a tenth as much per GB; it backs aggregate
questions like "how many did we deliver last month". *Cold* (13 months) keeps only
the delivery ledger and daily rollups — kilobytes per day, so retention stops
being a cost conversation. Operational chatter never enters any tier: health
probes and poll lines become metrics at emission, which is where the 95.7%
reduction comes from. The rule that makes it affordable is that **logs are for
explaining a specific message, not for counting** — counting is what the ledger
and rollups are for, so no query ever needs to scan raw logs to produce a number.
Everything is structured JSON with a stable field set, because free-text logs force
full-text indexes, and full-text indexes are what make log storage expensive.

**There is no producer attribute today.** The complete span attribute set is
`correlation_id`, `message_type`, `tenant_id`, `messaging.system`,
`http.status_code`, `provider`, `provider.status_code`,
`provider.final_status_code`, `retry_count`, `sqs.receive_count`. Nothing
identifies the producing team; `tenant_id` is the *recipient*. So per-producer
baselines, noisy-neighbour attribution and "is it me or the platform?" are not
implementable. Add `producer.service` at ingest and propagate it alongside
`correlation_id` — and sequence it **before** rollups, because you cannot backfill
a dimension you never recorded.

---

## Lower environment vs production

Not "the numbers are small" — **they fail in different classes.**

**Transfers:** correctness bugs. F1, F4, F5, F6 are wrong in every environment and
were findable here *because* it's quiet, with no contention to hide behind.

**Does not transfer:** every threshold, rate and baseline. `min_samples`,
`slow_factor`, the 235ms baseline, the 24h incident gap — all calibrated on 41
synthetic messages with zero hop-latency variance and one producer. A threshold
that *looks* calibrated is worse than an absent one.

**Production's failures are emergent** and cannot occur here: noisy neighbours on
a shared quota, hot partitions, poison messages, retry storms, backpressure,
partial deploys, one tenant at 80% of volume. The March 9 `429` is the clearest
case — in production it's at least as likely to be another team's burst.

**The log noise is probably not waste.** Health checks and poll chatter are more
likely instrumentation that was useful in dev and never gated by environment. The
fix is level-and-sampling per environment, not deletion — deleting what QA depends
on is how a cleanup gets reverted.

Also: the zero-traffic weekend is a test-harness artifact (production sends on
weekends, so the tool never encodes "weekends are quiet"); hop timing has zero
variance, so no percentiles are computed; and there is no infrastructure config in
the export, so F1's mechanism and F2's failure mode are inferences.

---

## What I cut

- **No statistical anomaly detection.** With n=41 and zero-variance timing, thresholded rules are more honest and far more debuggable.
- **No live query layer.** Addressed as design; the loader is already the only I/O boundary, so it's a small change rather than an aspirational one.
- **No production polish** — no auth, persistence, packaging.
- **No pipeline fixes.** This analyzes; the ledger is a change to two services, not to this repo.
