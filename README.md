# Tracelens

A troubleshooting assistant for telemetry — Bloomerang Platform Engineering
take-home.

**It knows nothing about this pipeline.** No service name, no channel, no stage
count. It works out the shape of the system from the export, states what is wrong
with the telemetry before it states anything about the system, and hands a model a
timeline to read rather than a verdict to agree with.

[`DISCOVERY.md`](DISCOVERY.md) — what I found reading the data by hand ·
[`DESIGN.md`](DESIGN.md) — how it's built ·
[`DECISIONS.md`](DECISIONS.md) — what I got wrong and changed

```bash
pip install -e ".[ai,dev]"   # Python 3.10+; base dep is just rich
tracelens quality            # start here: what's wrong with the input
tracelens routes             # every path work took, and how many took each
pytest                       # 96 tests, ~2s
```

---

## The idea

An engineer troubleshooting by hand does three things: pull the records that
relate to the complaint, sort them by time, and put the deploys in the same
sequence. Then they read. Everything before "then they read" is mechanical, and
everything after it is judgement.

So the deterministic half does exactly that much, and the model does the reading.

```
tracelens ask "we queued push notifications and none went out"
```

The model gets a route table, the stated limits, and a tool that returns a
timeline for any filter. It reads and answers.

---

## What that looks like

**Routes** — every distinct path, with counts. The shared opening is printed
once, because five routes agreeing for eight nodes and diverging at the ninth all
look identical otherwise.

```
all journeys start: accepted_message → POST /api/v{n}/messages → Accepted message
                    request type={message_type} → publish comms-topic

  1.   20  …→ consume comms-queue → …4 more… → Sending {message_type} to {addr} → send {message_type}
  2.    8  …→ consume comms-queue → …3 more… → consume {message_type}-queue → send {message_type}
  3.    6  …→ consume comms-queue → …5 more… → send {message_type} → Provider returned {n}, backing off
  4.    4  …→ Published to topic type={message_type}
  5.    3  …→ consume comms-queue → …7 more… → send {message_type} ⟲ visits a node twice
```

Route 4 stops six nodes early. Route 5 does work twice. Nothing computed that —
they're rows in a table.

**A slice** — the timeline, with a normal journey beside it.

```
tracelens slice --where message_type=push --limit 1

journey corr-0005  (route 4, 4 journeys took it)
  constant throughout: http.status_code=202, message_type=push, status=OK
    17:52:00.000  accepted_message     accepted_message
    17:52:00.000  comms-ingest         POST /api/v{n}/messages           48ms
    17:52:00.022  comms-ingest         publish comms-topic               19ms
    17:52:00.041  comms-ingest         Published to topic type={message_type}

contrast — journey corr-0004  (route 1, 20 journeys took it)
    15:39:00.022  comms-ingest         publish comms-topic               19ms
    15:39:00.310  comms-orchestrator   consume comms-queue               26ms   ← route 4 has none of this
    15:39:00.334  comms-orchestrator   publish {message_type}-queue      17ms
    15:39:00.730  comms-sender         consume {message_type}-queue      21ms
    15:39:00.755  comms-sender         send {message_type}              235ms
```

**The contrast is not a nicety.** Filter to the four affected journeys alone and
nothing looks wrong — that is simply what those journeys look like. The finding is
*"these stop here and normal ones don't"*, and it needs both halves on the page.

**Changes go in the sequence.** Asking about March 9 puts the deploy inline:

```
    09:00:00.755  comms-sender    send {message_type}         4120ms   [corr-0026]
    10:05:00.755  comms-sender    send {message_type}         4120ms   [corr-0031]
 ** 14:00:00.000  CHANGE comms-sender   Add SMS provider seam pr=99, sha=c52a0f9
```

The deploy postdates both slow sends by five hours. That's a position on the page,
not arithmetic anyone has to trust.

---

## Input quality is the first output

Real telemetry is partly broken, and a confident answer built on broken input is
the failure this whole export demonstrates. So the first thing the tool reports is
what's wrong with the data — and, for each defect, **what it stops you concluding**.

```
`status` never varies — it is recorded but measures nothing
  Every one of 273 records carrying `status` reports the same value, 'OK'.

  limit: no alert built on it can ever fire
  limit: a healthy-looking `status` is not evidence that anything worked —
         the field would read the same if everything failed
```

Four checks, none naming a field: a field that never varies, no failure signal
anywhere, records that join to nothing, an identifier that fragments mid-journey.
On this export they find that `status` is always OK, `level` never reaches ERROR,
86% of records join to nothing, and `trace_id` breaks in 8 of 41 journeys.

Those limits travel into the prompt, and the model is told they bind: if a field
is constant, it may not read that field as evidence of health.

**This is the one rule-based component left**, which is a real inconsistency. I
kept it because computing the statistic and leaving the conclusion to the reader
makes the limit advisory, and the limit is the point.

---

## Four concepts

| | |
|---|---|
| **events** | a span, log line, deploy and ledger row are the same shape: something happened, somewhere, at a time. Identifiers live in a dict and are found by suffix (`_id`, `_at`), so `order_id` on an unseen system works without anyone adding it to a list |
| **journeys** | records sharing a correlation value, in time order |
| **routes** | distinct node sequences, with counts |
| **slices** | a filtered timeline, plus a contrast, plus changes inline |

**The key is supplied, not discovered.** An earlier version scored every candidate
identifier with a formula I invented and picked a winner. In a real deployment you
know your key. What's kept is the counting:

```
 identifier         coverage  groups  services/group  median size
 correlation_id          14%      41             3.8           11  USED
 trace_id                13%      49             2.3           10
 tenant_id               12%       6             4.0           70
 span_id                  9%     273             1.0            1  identifies a single record
```

Two structural disqualifications, then highest coverage, and the whole table is
printed so a wrong default is one glance from being seen. `--key` overrides.

**Node names are learned.** Values that appear inside names get substituted back
out — `publish email-queue` becomes `publish {message_type}-queue` — so three
channels collapse onto one node and a fourth lands there too. Which values get
substituted is itself discovered. That's why log lines and spans land on the same
route: the log "Routing message type=email" carries no attributes at all.

---

## AI: in and out

**Code owns every number.** Grouping, counts, durations, distributions, and the
limits. **The model owns the reading** — which slice to look at, what the
difference between it and the contrast means, and whether the question is even
about this system.

Four gates, all in code:

1. **Bounded surface.** Three read-only tools: `list_routes`, `get_slice`, `get_journey`. No `run_query`, no write path, no way to ask for everything.
2. **Slice-scoped citations.** Any identifier in the answer must have appeared in a slice the model was actually given. Anything else is dropped and the rejection is reported.
3. **Mandatory alternatives.** Two hypotheses on different evidence, or one plus the explanation it can't be separated from, or `insufficient_evidence`. "It did not happen" and "it was not recorded" are identical in telemetry.
4. **No severity.** Nothing is labelled CRITICAL. The output says `4 of 41`.

**Where the gate is weaker than the last design, and I'd say so out loud:** the
model now reads raw records, so the citation check guarantees an identifier is
real but not that a number is right. The mitigation is upstream — counts and
percentiles are already in the payload, so it has no reason to derive one.

**The opening payload is bounded by route count, not traffic.** A route table is a
dozen lines whether the export holds 41 journeys or 41 million. That's the
property that lets this survive go-live unchanged, and a test pins it.

**What it actually did.** `scripts/live_check.ps1` (or `.sh`) puts twelve
questions to the model and writes the output to `examples/`. Those transcripts are
committed, so a reviewer with no API key can read what the model said rather than
taking my word for it. Two of them corrected claims I had written in
[`DISCOVERY.md`](DISCOVERY.md) about what the tool could reach.

**The failure that taught me the most.** *"Our webhooks stopped firing"* must
return insufficient evidence. That test was green for weeks — and only ever ran
against the offline stand-in, which declines anything it can't word-match.
**The guarantee was being checked by the one implementation that couldn't fail
it.** Live, under the previous design, the model answered with the push outage.

It declines now, citing the boundary `PLATFORM.md` states rather than any rule
about webhooks. One run is not a guarantee, and this is the case I'd least trust
to a single sample — the failure mode is a model being agreeable, not a
deterministic bug. [`DECISIONS.md`](DECISIONS.md) has the full account.

---

## Going live

30-day retention, queries cost money, five teams produce into this pipeline. Full
treatment in [DESIGN §5](DESIGN.md). Four things matter:

**You can't sample an absence.** The push loss has no error span and nothing
marked failed — the signal *is* the missing record. Head sampling makes "no sender
span" identical to "not sampled"; tail sampling keeps errors and slow traces, and
a dropped message is neither. So the worst problem here is the one that disappears
as soon as you make telemetry affordable, and it disappears quietly.

**Accounting moves off traces onto a ledger.** Two rows per message — accepted at
ingest, settled at the sender — keyed by correlation ID, kept 13 months. A job
alerts on anything accepted but never settled. That turns the push outage from a
week-late discovery into a page in minutes.

**Three storage tiers, picked by how each gets read.** *Hot* is 7 days in the
search backend holding only records that carry a correlation or trace ID, indexed
on correlation ID, service and time — that's how every incident query starts, and
it's the expensive tier so it holds the least. *Warm* is 30 days of Parquet in
object storage partitioned by `date/service`, scanned rather than indexed, roughly
a tenth the cost per GB. *Cold* is 13 months of ledger and rollups only.

**The arithmetic, because "improve observability" persuades nobody.** Measured
from `data/`: records that join to nothing are **76% of all telemetry bytes**, and
the delivery ledger costs **2.2%** of what the telemetry costs.
`python scripts/cost_model.py` shows the working and takes your own vendor rates.
Sequencing the noise reduction first makes the saving pay for the ledger, which
turns "approve new spend" into "reallocate existing waste."

**Nothing says which team sent a message.** `tenant_id` is who receives it. Until
`producer.service` is stamped at ingest, nothing multi-team works — no
per-producer baselines, no noisy-neighbour attribution, no answer to "is it us or
the platform?" Add it before building rollups; you can't backfill a dimension you
never recorded.

---

## What this data can and can't tell you

**What transfers** are the correctness bugs — push loss, the trace break, the
missing error signal. Those are wrong everywhere, and they were findable here
*because* it's quiet, with no load to hide behind.

**What doesn't** is every threshold: `MIN_OBSERVATIONS` at 20, the 8-sample floor
before a percentile is reported, the 45-minute change window. All chosen against
41 synthetic journeys with zero timing variance. Each is a named constant and
prints alongside anything that depended on it.

Production's problems are emergent and none can happen here: noisy neighbours on a
shared quota, hot partitions, poison messages, retry storms. March 9 is the
clearest case — in production that `429` is at least as likely to be another
team's burst, and this data cannot tell.

---

## What I cut

- **A detector layer**, and everything built to serve it — 2,400 lines encoding the five failures I'd already found by hand. It was the most impressive-looking and least useful part of the repo: every rule was written *after* I knew the answer, so it could only re-find what I already knew, and "no findings" reads identically to "healthy". [`DECISIONS.md`](DECISIONS.md) has the full account.
- **An invariants layer** that replaced it. The route table says the same things in a form a person can read.
- **Correlation-key discovery, severity labels, novelty detection.** Each was code making a judgement I couldn't defend.
- **No statistical anomaly detection.** With 41 journeys and zero-variance timing, stated thresholds are more honest and far more debuggable.
- **No production polish** — no auth, no persistence, no packaging.
