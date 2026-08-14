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
pytest                       # 114 tests, ~3s
```

Parts 1 and 3 have **no model in the loop** — every number below comes from
deterministic code. Only triage calls the API (`claude-sonnet-5`,
`effort: medium`, ~$0.05/run).

**You don't need a key to see what the model said.** All six live transcripts are
committed, so `tracelens triage --symptom 3` replays the real answer and labels it
with the model that produced it. A complaint with no recorded transcript falls
back to an offline keyword stub and says `source: stub`. `tracelens keys` shows
which you'll get before you spend anything.

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

Two scripts back the write-up: `verify_claims.py` recomputes all 101 figures in
this document from `data/` and fails on any drift; `cost_model.py` derives the
per-message telemetry footprint from `data/` and applies stated price assumptions
you can replace.

---

## What I found

Everything below was seen directly in the data. The one exception is called out:
why the March 9 slowdown *ended* is still an open question.

### F1 — Push notifications never arrive

All four push messages were accepted and published. None reached the orchestrator.

Ingest logged 41 publishes, the orchestrator logged 37 consumes, and the gap is
exactly those four. Nothing reports an error — no failed span, no ERROR log. The
only evidence is an absence.

Payments called this a one-off with "our donation campaign last week." It isn't.
The four come from three different tenants, spread across the whole window. Every
push message in the export was lost.

Push and SMS use the same provider, and SMS works fine, so the provider isn't at
fault. The break is between the topic and the queue.

### F2 — The duplicate emails are the queue redelivering

Three of 29 emails went out twice.

Each one shows a single publish and two deliveries. The second fires 31 seconds
after the first and is tagged `receive_count: 2`. The first send had already
succeeded.

So the app didn't send twice. The message was never deleted from the queue, the
visibility timeout expired, and the sender picked it up again.

### F3 — March 9 was the provider throttling us

Six sends took about 4.1 seconds instead of the usual 235ms, hit `429` rate
limits, and retried. All of them eventually succeeded, so nothing was lost.

The deploy people suspected shipped five hours *after* the slowdown started. That
theory doesn't hold.

**Why it recovered is still open.** A different deploy — an SDK bump — landed in
the middle of the recovery window. Either the rate limit expired on its own, or
the old SDK was causing it and the update fixed it. This data can't tell them
apart. The code change would.

One thing is clear: it wasn't us sending too fast. All five emails that day were
throttled, across five different tenants, not a burst from one sender.

### The rest

| | Finding | Evidence |
|---|---|---|
| **F4** | SMS loses its trace ID partway through | 8 of 8 SMS traces split in two; email never does (0 of 29). The sender logs nothing for SMS either, so there's no fallback trail |
| **F5** | Errors never show up as errors | 0 of 273 spans marked failed, 0 of 2,820 logs at ERROR — despite real rate limits and retries. Only visible inside span attributes |
| **F6** | The queue-depth metric is hardcoded | Reports `depth=0` all 1,200 times, with no queue name and no trace ID. Nothing emits it for the push queue at all |
| **F7** | 96% of logs match nothing | 2,700 of 2,820 lines have no correlation ID and no trace ID. About 120 lines in the whole window are usable |

F5 is why nobody caught the other six. Any dashboard built on span status or log
level shows this pipeline as healthy straight through every incident here.

### The numbers — `tracelens account`

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

Bugs and anything customer-facing come first. That's a business call as much as an
engineering one, so I'd set the order with them rather than alone.

**Push loss and duplicate emails, now.** Push being dropped is a bug, but it's an
internal team reporting it. Supporters getting the same email twice is the one they
actually feel — it's annoying, and it chips away at trust in the product.

**Fix the logging on those two paths while you're in there.** We have a good idea
what's broken in each case, but part of it is still hypothesis. Tightening the logs
and traces around those features is cheap when you're already in the code, and it's
how you confirm the fix actually worked.

**Then the proper overhaul** — real error signal and the delivery ledger. Worth
doing, and it's the difference between diagnosing this by hand and being told. Just
not ahead of a defect dropping messages today.

**The noise, investigate before deleting.** I don't know why it's there. It might
be an uptime check, or how ops spots an unhealthy instance getting swapped out.
Worth an afternoon to find out. If it's useful, the answer isn't deleting it — it's
a viewer that hides it by default so devs can find the message they're chasing.

**SMS trace propagation last.** It costs investigation time, not wrong outcomes,
and the tool already routes around it. Same for March 9 — it resolved itself and
lost nothing.

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

**The model also gets architecture and product context** — `PLATFORM.md`, about
775 tokens, fixed size. Without it, it can't tell whether a complaint is even
about this system. That's a different category from per-incident evidence: stable,
human-maintained, checkable by reading, so it grounds the model without weakening
the citation gate.

### What it actually said — `tracelens triage --symptom 3`

No API key needed to see this. All six live transcripts are committed, and the
tool replays the matching one, labelled with the model that produced it.

```
source: replayed live claude-sonnet-5, effort=medium run from examples/symptom-3.json

╭──────────────── #1  D3.provider_degradation.email ─────────────────╮
│ Email sends did slow down starting 2026-03-09T09:00:00.755Z: 6     │
│ sends degraded to 4120ms vs a 235ms baseline (17.5x), all hitting  │
│ provider 429 (rate-limited) responses with 18 retries before       │
│ eventually succeeding (final status 202) — so nothing was lost,    │
│ but latency spiked for about a day. The suspected cause, the       │
│ comms-sender deploy c52a0f9 (PR #99, 'Add SMS provider seam') at   │
│ 14:00 that day, is explicitly ruled out: it postdates the onset of │
│ the slowdown by 5 hours, and 3 of the 6 affected messages had      │
│ already occurred before it deployed. The data cannot determine     │
│ what actually ended the incident — it recovered by                 │
│ 2026-03-10T11:13, and a different sender deploy, e18d773 (PR       │
│ #101, 'Bump provider SDK') at 2026-03-10T10:00, landed right       │
│ inside that recovery window.                                       │
╰───────────────────────── MEDIUM / ambiguous ───────────────────────╯
  unresolved — both survive the evidence:
    - H1.provider_side: Provider-side rate limiting that ended on its
      own; the deploy inside the recovery window is coincidence.
    - H2.client_side: The pre-existing client mishandled concurrency
      or client-side rate limiting, the provider throttled in
      response, and e18d773 (PR #101) fixed it.
  ruled out:
    - The comms-sender deploy c52a0f9 caused the email slowdown.
      The slowdown began at 2026-03-09T09:00:00.755Z, 5 hours before
      this deploy, and 3 of the 6 affected messages had already
      occurred before it went out.
```

That's the trap case passing: throttling first, the deploy killed by arithmetic,
and the recovery ambiguity intact. Severity and confidence are code-owned — the
model can't promote `ambiguous` to `observed`.

**How I'd know it was wrong:** the 5 symptoms are a golden set pinned to finding
IDs; out-of-scope complaints must return insufficient evidence; symptom 3 must
rank throttling first, cite the 5-hour gap, *and* keep the ambiguity.

**What running it live actually showed.** All five symptoms pass — correct finding
first, no fabricated citations, and symptom 4 reached an invariant finding on its
own. Two out-of-scope complaints ("the CSV export job is failing", "our Salesforce
sync stopped") correctly decline and cite the architecture when explaining why.

But *"our webhooks stopped firing"* still gets answered with the push outage. The
model hedges — "if 'webhooks' refers to push notifications" — and flags the
terminology mismatch as the first thing to resolve. That's arguably right: a
webhook and a push are both outbound fire-and-forget calls, and push really is
100% dead. What's wrong is that the verdict still reads CRITICAL with the caveat
buried in prose.

And that test was green for weeks, because it only ever ran against the offline
stub, which declines anything it can't keyword-match. **The guarantee was being
checked by the one implementation that couldn't fail it.**
[`DECISIONS.md`](DECISIONS.md) has the A/B and what I'd try next.

All six live transcripts are in `examples/`, and every file states whether it came
from the model or the stub.

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

**You can't sample an absence.** F1 has no error span and nothing marked failed.
The signal *is* the missing span. Head sampling makes "no sender span" look
identical to "not sampled." Tail sampling keeps errors and slow traces, and a
dropped message is neither.

So the worst problem here is the one that disappears as soon as you make telemetry
affordable — and it disappears quietly. Your dashboards get cleaner as reliability
gets worse.

**Accounting moves off traces onto a ledger.** Two rows per message: accepted at
ingest, settled at the sender, keyed by `correlation_id`, kept 13 months. A job
alerts on anything accepted but never settled. That turns the push outage from a
week-late discovery into a page in minutes.

**How logs get stored and retrieved.** Three tiers, picked by how each gets read.

*Hot* is 7 days in the search backend. It holds only records carrying a
`correlation_id` or `trace_id`, indexed on correlation ID, service and time —
that's how every incident query starts. It's the expensive tier, so it holds the
least.

*Warm* is 30 days of Parquet in object storage, partitioned by `date/service`.
Scanned instead of indexed, roughly a tenth the cost per GB. This answers "how
many did we deliver last month."

*Cold* is 13 months of ledger and daily rollups only. Kilobytes a day, so
retention stops being a budget conversation.

**The arithmetic, because "improve observability" persuades nobody.** Measured
from `data/`: the logs that join to nothing are **76% of all telemetry bytes**,
and the delivery ledger — the thing that actually answers *did we deliver it* —
costs **2.2%** of what the telemetry costs. At 2M messages/day, "how many did we
deliver last month" as an hourly dashboard is a full scan; from the ledger it's a
primary-key aggregate. `python scripts/cost_model.py` shows the working and takes
your own vendor rates.

Two rules keep it cheap. Logs explain one message — they never count things, and
counting is the ledger's job, so nothing ever scans raw logs for a number.
Everything is structured JSON with a stable field set, because free-text logs
force full-text indexes and those are what get expensive. Health probes and poll
lines never enter any tier at all — they become metrics at emission, and that
alone is 95.7% of current log volume.

**Nothing says which team sent a message.** The full attribute set is
`correlation_id`, `message_type`, `tenant_id`, `messaging.system`,
`http.status_code`, `provider`, `provider.status_code`,
`provider.final_status_code`, `retry_count`, `sqs.receive_count`. `tenant_id` is
who receives it, not who sent it.

Until `producer.service` is stamped at ingest and carried through, nothing
multi-team works — no per-producer baselines, no noisy-neighbour attribution, no
answer to "is it us or the platform?" Add it before building rollups. You can't
backfill a dimension you never recorded.

---

## What this data can and can't tell you

A lower environment and production don't fail the same way. It's not that the
numbers here are small — the problems are a different kind.

**What transfers** are the correctness bugs: push loss, SMS trace propagation,
missing error signal, the fake metric. Those are wrong everywhere. They were
findable here *because* it's quiet, with no load to hide behind.

**What doesn't** is every threshold in the tool. The 235ms baseline, the incident
window, the minimum sample size — all tuned on 41 synthetic messages from one
producer with zero timing variance. A threshold that looks calibrated but isn't is
worse than none.

Production's problems are emergent, and none of them can happen here: noisy
neighbours on a shared quota, hot partitions, poison messages, retry storms, one
tenant at 80% of volume. March 9 is the clearest case — in production that `429`
is at least as likely to be another team's burst, and this data can't tell.

A few smaller things. The zero-traffic weekend is a test harness artifact, not a
pattern, so the tool never assumes weekends are quiet. Hop timings are identical
across every message, so percentiles would be fiction and aren't computed. And
there's no infrastructure config in the export, which is why the subscription
filter and the missed queue delete are likely causes rather than facts.

---

## What I cut

- **No statistical anomaly detection.** With n=41 and zero-variance timing, thresholded rules are more honest and far more debuggable.
- **No live query layer.** Addressed as design; the loader is already the only I/O boundary, so it's a small change rather than an aspirational one.
- **No production polish** — no auth, persistence, packaging.
- **No pipeline fixes.** This analyzes; the ledger is a change to two services, not to this repo.
