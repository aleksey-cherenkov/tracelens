# Design

A guide to reading the code. What I found by hand is in
[`DISCOVERY.md`](DISCOVERY.md); what I got wrong on the way is in
[`DECISIONS.md`](DECISIONS.md).

About 3,300 lines across 18 modules. §0 is the decision everything else
follows from.

- [0. Why there are no rules](#0-why-there-are-no-rules)
- [1. Follow one journey](#1-follow-one-journey)
- [2. Routes, and why node naming is the hard part](#2-routes-and-why-node-naming-is-the-hard-part)
- [3. Slices — what the model reads](#3-slices--what-the-model-reads)
- [4. Keeping the model honest](#4-keeping-the-model-honest)
- [5. Going to production](#5-going-to-production)
- [6. Tests](#6-tests)

---

## 0. Why there are no rules

The first version had five detectors, one per problem I'd found by reading the
export. A `drop.py` that knew about channels, a `duplicate.py` that knew about
redelivery counts, a `provider.py` that did deploy arithmetic. 1,190 lines, well
tested, and every rule written *after* I already knew the answer.

That is a closed world. It re-finds what its author knew and nothing else — and
when it finds nothing, that reads identically to "healthy", which is the failure
this export is full of: `status: OK` on 273 spans, four of them for messages
never delivered.

The second version replaced them with *invariants* — properties I believed held
of any pipeline. Conservation, path shape, single visit, completeness. Better,
because a violation didn't have to be anticipated. But each carried a threshold I
picked and a severity I assigned, and on unfamiliar data I couldn't defend either.

**The third version reports data and lets a model read it.** A route table with
counts says everything the four invariants computed:

| The invariant | The row |
|---|---|
| conservation — 4 enter, 0 leave | route 4 ends six nodes early |
| path shape — a minority route | the table, ordered by count |
| single visit — duplicate delivery | route 5 visits a node twice |
| completeness — fewer sources | route 4 touches two services, others touch four |

What's left is ~2,000 lines of mechanical work — group, order, filter, render —
plus one rule-based layer I chose to keep (§4).

**What it cost, and what I got wrong about the cost.** I expected to lose the
mechanisms the detectors named -- "the visibility timeout expired and the queue
redelivered" -- because no general *check* can reach them. The check half holds:
the timing layer shows a 17.5x spread and refuses to interpret it.

But a live run found the March 9 mechanism anyway. Handed the timeline, the model
reads `provider.status_code=429` and `retry_count=3` against a contrast journey at
235ms and names provider rate limiting, then rules out the suspected deploy on two
grounds -- that it postdates onset, and that its title describes a different
channel. Both are on the page; no rule had to anticipate either.

What is genuinely lost is *guaranteed* coverage. A detector fires every time; a
model reading a timeline is likelier and not certain. That is the trade, and it is
the honest version of it.

**What it bought.** `tests/test_no_vocabulary.py` parses every module and fails if
an executable line mentions anything about this export — not a service, not a
channel, not the field that ties records together. Comments may discuss the data
freely; code may not depend on it. It found two real leaks the first time it ran.

---

## 1. Follow one journey

```
tracelens trace corr-0003
```

**`loader.load()`** reads every `*.json` in the directory. No filename is special
and none is required; the filename becomes the record `kind`. If a file parses to
nothing that is *reported*, not swallowed — which is how a real bug surfaced: one
file's timestamp field wasn't recognised, 41 records vanished, and the skip
message named the exact fix.

**`events.normalise()`** turns each record into one `Event`: `at`, `source`,
`name`, `kind`, `ids`, `attributes`. `ids` is a *dict*, and a field lands in it by
suffix (`_id`, `_key`, `_uuid`) rather than by name — so `order_id` on an unseen
system is picked up without anyone editing a list. Time fields work the same way,
which is what the skip message taught.

**`journeys.build()`** groups by the correlation key. The key is *supplied or
defaulted, never scored* — an earlier version ranked candidates with a formula I
invented, which is code making a judgement call. What's kept is counting:

```
 identifier         coverage  groups  services/group  median size
 correlation_id          14%      41             3.8           11  USED
 trace_id                13%      49             2.3           10
 span_id                  9%     273             1.0            1  identifies a single record
```

Two structural disqualifications — one record per group is a label not a join; no
group crossing a service boundary can't show a handoff failing — then highest
coverage. The table prints so a wrong default is one glance from being seen.

**`routes.build()`** derives the paths. **`quality.assess()`** produces the limits.
**`slices.select()`** renders the timeline. **`analysis.analyse()`** wires the four
together and is 90 lines with no logic in it.

---

## 2. Routes, and why node naming is the hard part

A route is a journey's node sequence. Building them is fifteen lines. Making the
node names *collapse correctly* is the only clever part, and without it there are
41 routes — one per journey — and the table says nothing.

Two passes, in order:

**Learned substitution.** `learn_vocabulary()` finds low-cardinality attribute
values that appear inside record names, then substitutes them back out:

```
publish email-queue  →  publish {message_type}-queue
```

Keyed by *value*, not per record, and that is the point. A span carries the
attribute and is named `send email`; the log line "Routing message type=email"
carries no attributes at all but is the same place in the system. Learning the
vocabulary once from the whole export and applying it everywhere is what lets logs
and spans land on the same route — the entire reason for having one `Event` type.

**Structural collapse, behind two guards.** Addresses, hex and numbers vary per
record and are replaced *inside* the token, not instead of it — replacing the
token turns `POST /api/v1/messages` into `POST {n}` and throws away the label.

But replacing the digit run is not always right either, and the guards are why.

*A digit glued to a letter is part of a name, not a value.* `depth=0`,
`attempt 1 of 3` and `returned 429` are readings; `v1`, `s3`, `h2` are names.
Collapsing the second kind merges things that are genuinely different — a broken
v2 rollout sharing a node with v1 is invisible.

*A collapse has to earn itself by merging something.* `learn_names` builds every
candidate, then keeps only those that more than one name lands on. On this export
the address rule folds 29 names into one; the digit rule folds nothing. Applying
it anyway cost real signal: `Provider returned 429` became `returned {n}`, hiding
the status code, and `queue depth recorded depth=0` became `depth={n}` — hiding
that a gauge is hardcoded to zero, which is itself a finding.

Both guards are the same principle the vocabulary learner already uses:
**substitute what varies, keep what does not.**

Rendering factors out the opening every route shares. Five routes agreeing for
eight nodes and diverging at the ninth all truncate before the divergence
otherwise — and the divergence is the only thing the table exists to show.

---

## 3. Slices — what the model reads

`slices.select()` filters journeys by attribute, route, time window or identifier,
and renders one timeline. Two things it adds beyond filter-and-sort, both
load-bearing:

**A contrast journey**, from the most common route, chosen as the one nearest in
time. Filter to four affected journeys and a reader sees four sequences that each
end at the same place — nothing looks wrong, because that is what those journeys
look like. The finding is *"these stop here and normal ones don't"*, and it needs
both halves on the page. Nearest in time rather than arbitrary controls for
anything that changed across the window.

**Changes in the sequence**, not in a footnote. A deploy inside a journey's span
renders inline at its timestamp; one near the slice but outside every journey gets
its offset stated. Neither is called a cause, and when there is none the output
says that is *not* the same as nothing having changed — only deploys reach this
tool.

Two render modes, because the question decides which is readable: per-journey when
filtering by attribute, interleaved when filtering by time. "Everything was slow
last Tuesday" is a question about a period, not a journey.

Everything is capped and the caps are reported. Attributes constant within a
journey are hoisted to a header — nine repetitions removed from a ten-record
journey, which matters more for a prompt paying by the token than for a person.

---

## 4. Keeping the model honest

### Input quality — the one rule-based layer

`quality.py` runs first and reports two things per defect: the defect, and what it
prevents concluding.

| Check | On this export |
|---|---|
| a field that never varies | `status` is `OK` on all 273 records |
| no failure signal anywhere | `level` only ever reaches `WARN` |
| records that join to nothing | 86% |
| an identifier that fragments mid-journey | `trace_id`, 8 of 41 |

Those limits go into the prompt and the system prompt says they bind. Keeping this
rule-based is inconsistent with the rest of the design and I know it — computing
the statistic and letting the model draw the conclusion would be smaller and more
uniform. It would also make the limit advisory, and the limit is the point.

### The tool surface

Three read-only views: `list_routes`, `get_slice`, `get_journey`. No
`run_query(sql)`, no write path, no way to ask for everything. The model cannot
author an expensive scan, every answer traces to a named function you can re-run
by hand, and the surface is small enough to describe accurately in the prompt.

The model drives which slice it reads. That is a deliberate reversal of the
previous design, where code chose the evidence and the model selected from it —
choosing what to look at *is* the troubleshooting.

### The gate

`validator.py` drops any hypothesis citing an identifier that was never returned
by a tool. Two failure shapes, both real: a fabricated identifier, and prose
dressed as a citation ("the timeline above"), which a naive substring check waves
through. One bad reference kills the whole hypothesis — partial credit would let a
fabrication ride along beside a real citation.

The schema requires two hypotheses on different evidence, or one plus the
explanation it cannot be separated from, or `insufficient_evidence`. "It did not
happen" and "it was not recorded" are identical in telemetry, and collapsing that
silently is the worst thing this could do.

**Where this is weaker than the previous design.** The model reads raw records
now, so the gate guarantees an identifier is real but not that a number is right.
Mitigation is upstream: counts and percentiles are already in the payload, so it
has no reason to derive one.

### Bounded context

The opening payload is a route table — a dozen lines whether the export holds 41
journeys or 41 million — and each slice is capped. That's the property that lets
this survive go-live unchanged, and `test_pipeline.py` pins it by tripling the
traffic and asserting the payload barely moves.

### Model choice

`claude-sonnet-5`, `effort: medium`, ~$0.05 a run. Not Opus: the work is reading a
timeline and explaining it, not deriving anything. Not Haiku: holding onto a
genuine ambiguity under pressure to sound decisive is where a mid-tier model earns
its cost. The loop is capped at 8 iterations — an unbounded agent is an unbounded
bill.

---

## 5. Going to production

The absence-sampling argument, the delivery ledger, the storage tiers and the cost
arithmetic are in [README § Going live](README.md#going-live). What that doesn't
cover:

**Data access.** On-demand queries are needed for drill-down but insufficient
alone — accounting becomes a full scan. A delivery ledger is the only option that
detects an absence, so it's first. Rollups second: fixed cost, and the only way
baselines outlive 30-day retention. Tail sampling is good for cost but *only after
the ledger*, because adopting it first makes the push loss undetectable. A
warehouse mirror rebuilds the observability backend for little gain.

**Five teams on one pipeline** is two filtering problems needing opposite
treatment. Unrelated services in a shared backend → filter hard at the query
predicate. Other teams' messages *inside* this pipeline → partition, don't
discard; they're the same signal from a different source, and throwing them away
destroys the comparison that answers "is it us or the platform?" All of it is
blocked on the missing `producer.service` attribute.

**What changes in the code.** `loader.py` becomes a `Source` protocol — it's
already the only I/O boundary. Grouping moves to streaming, chunked by correlation
value. Slices gain a scan-volume estimate before they run. Because the analysis
has no model in it and every number is code-generated, swapping the data source
doesn't touch the AI layer at all.

---

## 6. Tests

96 tests, under two seconds. Four files, and the split is deliberate:

| File | Checks |
|---|---|
| `test_pipeline.py` | properties of loading, grouping, routes, quality and timing — never "the push channel is dropped", which would test my memory rather than the tool |
| `test_slices.py` | the contrast is present, changes land in order and are never called causes, caps hold |
| `test_ai.py` | the citation gate against four failure shapes, the tool surface, and the live path against a fake SDK — no key, no network |
| `test_no_vocabulary.py` | no executable line in any module mentions this export |

Two of these test the tests, which sounds precious and isn't. The vocabulary check
originally exempted every string literal, which is where both real leaks lived. The
key scanner originally matched the bare `sk-ant-` prefix and fired on a
deliberately fake key in another test — a check that cries wolf gets loosened until
it catches nothing.

`scripts/cost_model.py` is separate from the suite: it derives the per-message
telemetry footprint from `data/` and applies stated price assumptions you can
replace with real vendor rates.
