# Design

How it's built and why. Findings are in [`README.md`](README.md); the record of
what I got wrong along the way is in [`DECISIONS.md`](DECISIONS.md).

---

## 1. Module map

```
tracelens/
  model.py       Span/Log/Deploy/AcceptedMessage; the 7-stage Stage enum
  loader.py      disk -> Dataset (handles the nested data/data/ layout)
  join.py        LogicalTrace per correlation_id; JoinMethod; Attempt
  accounting.py  promise ledger -> delivery funnel
  health.py      per-service and per-hop throughput, latency, errors, retries
  topology.py    graph LEARNED from spans; attribute-templated node names
  invariants.py  properties any pipeline must satisfy -> INV.* findings
  novelty.py     fingerprint diff against a baseline -> NOV.* findings
  analysis.py    runs the three layers in isolation; reports corroboration
  detectors/     D1-D5, one module each -> Finding
  evidence.py    Evidence, Finding, EvidenceBundle, CitationIndex
  triage/        context, tools, prompts, engine, validator, stub
  cli.py         rich terminal UI
  report.py      single-file HTML, inline SVG, no CDN
```

`model.py`'s `Stage` enum is the one pipeline-specific foundation. It is kept for
rendering a trace legibly and is deliberately **not** what the general layers
stand on — see §3.

---

## 2. Join strategy

**Primary key is `correlation_id`. `trace_id` is evidence about instrumentation
health, not identity.** Grouping by `trace_id` fails on 8 of 41 messages and
cannot represent a truncated path at all.

Every stage transition records *how* it resolved, because the join method **is**
the diagnosis for symptom 4:

| `JoinMethod` | Meaning | Count |
|---|---|---|
| `PARENT_CHILD` | downstream `parent_span_id` resolves upstream, same trace | 218 |
| `CORRELATION_FALLBACK` | trace broken — joined on correlation + stage order + time | 8 (all SMS) |
| `ABSENT` | no downstream span; the message stopped | 4 (all push) |

230 transitions total: 37 complete × 6, plus 4 push × 2. (Not "spans with a
parent", which is 224 and says nothing about how a hop resolved.)

**Attempts are partitioned by walking backwards from the send.** Both consume
spans of a redelivered message share the *same* publish parent, so walking forward
cannot separate them; `send.parent_span_id` resolves to exactly one consume.

**Transitions are typed.** `NESTED` (child starts before parent ends, same
service) reports an offset within the parent and is never called latency —
otherwise the naive formula prints `−26 ms`. Only the two async broker hops carry
queue latency.

---

## 3. Three layers

Every detector rule was written after I knew the answer — a closed world, unable
to surface a sixth failure. Two further layers know nothing about this pipeline.

| Layer | Pipeline-specific | Answers | Unfamiliar data |
|---|---|---|---|
| **Detectors** `D*` | yes | *why* — mechanism, cause, ruled-out alternatives | skipped with a stated reason |
| **Invariants** `INV.*` | no | *what* broke | fully working |
| **Novelty** `NOV.*` | no | *what changed* | fully working |

### Detectors

| ID | Rule | Output here |
|---|---|---|
| **D1** channel drop | accepted vs `terminal_stage` per channel; fires on a **count**, never a rate | push 4/4 at `PUBLISH_TOPIC`, critical |
| **D2** duplicate | >1 `SEND_PROVIDER` per correlation; classify by publish-vs-consume counts | 3 redeliveries, Δ 31.0s; rules out double-publish |
| **D3** provider degradation | baseline from 2xx sends; affected if non-2xx **or** > `slow_factor` × baseline; group by `max_gap`; then `correlate_deploys(service, window)` | 6 messages, 17.5×; `c52a0f9` ruled out; H1/H2 ambiguous |
| **D4** trace break | distinct trace IDs per message; locate the boundary; check log reachability | 8/8 SMS, email 0/29 |
| **D5** blind spots | status-vs-reality divergence; noise ratio; constant/undimensioned gauges | 0/273 vs 4/41; 95.7%; queue depth |

### Invariants

Nothing here names a channel, service or stage count. Severity is derived from
blast radius, because on unfamiliar data there is no prior about what matters.

| Invariant | Property | Generalises |
|---|---|---|
| `conservation` | what enters a hop must leave it | D1, for any hop and class |
| `path_shape` | messages follow one of a few routes | truncation vs divergence |
| `settlement` | every ledger promise reaches a terminal node | delivery accounting |
| `context_integrity` | a trace must not fragment mid-journey | D4 |
| `single_visit` | a message traverses each node once | D2 |
| `referential` | references resolve, records join | integrity checks |

**Conservation names the discriminating attribute** — an attribute value present
in every lost message and no surviving one. That yields `message_type=push` here
without knowing channels exist. "Whole class or scattering?" is the first question
anyone asks about silent loss, and it is answerable generically.

**Optional branches are not losses.** An edge counts as *expected* only if
≥ `expected_edge_share` (0.5) of messages at a node traverse it; a node is
*terminal* when it has no expected successor. The threshold cannot sit near 1.0 —
a real drop drags the edge's own share down, so it must sit below the loss it
detects.

**Detectors are gated on `stage_coverage`.** If the hardcoded taxonomy recognises
under 60% of spans, they don't run and say so via `ERR.taxonomy_mismatch`.
Ungated, they report *every* channel as dropped on a foreign pipeline, because no
span maps to a known stage.

### Topology and novelty

`topology.py` derives the graph in one pass. Span names embed attribute values
(`publish email-queue`), so substituting the value back gives
`publish {message_type}-queue` — three channels collapse to one node and a fourth
lands there too. Without templating, every new tenant would look like a new
pipeline.

`novelty.py` records a **fingerprint** — services, nodes, edges, route shapes,
statuses, attribute keys, log templates, channels — and diffs it against a
baseline. Shapes and cardinalities only, never counts: a fingerprint that moved
with traffic would flag every busy Monday. Both directions matter; a *vanished*
node is escalated to critical because a silently disabled path produces no error
and is the harder one to notice.

**Layers fail independently.** A layer that raises surfaces as `ERR.*` rather than
silence, because silence is indistinguishable from health.

---

## 4. Metric definitions

- **End-to-end latency** — `ACCEPT.start → SEND_PROVIDER.end` of the **first attempt only**. Measuring to the last span gives 31,988 ms for the three duplicates and invents a latency incident. Note `sqs.receive_count` is on the *send* span and absent on the redelivered consume, so filtering on it does not work.
- **Latency summary** — `n`, `min`, `median`, `max` always; percentiles **only** where distinct values exist. Every async hop has zero variance, so `variance: none` is printed instead of a fake p99.
- **Error rate — three numbers, never collapsed.** `span_status_errors` 0/273 · `provider_errors` 6/40 · `delivery_failures` 4/41. The gap between the first and last is the headline.
- **Retries** — 18 from `retry_count`, plus 3 redeliveries from `sqs.receive_count`. Different phenomena, never summed.
- **Minimum-n gate applies to rates, never counts.** D1 fires at n=4 against `min_samples=20` because "these 4 named messages stopped" is a claim about *those messages*, not a population. Gating it would delete the most severe finding in the dataset.
- **No calendar assumptions.** A zero-traffic bucket is `insufficient_data`, never a drop.

Every number above is a `config.py` parameter with its value printed in the
finding, so go-live is "re-tune these nine" rather than "find the constants".

---

## 5. AI triage

### The split

| Code | Model |
|---|---|
| Every join, count, duration, rate, window | Mapping a vague complaint onto detectors |
| Which IDs are affected | Ordering hypotheses by fit to what was asked |
| Deploy comparison and rule-out logic | Explaining the causal story |
| Severity, confidence, alternatives | Judging when nothing matches |

One boundary worth stating precisely: code owns the severity and confidence
*labels*; the model owns the *order of hypotheses*. An early draft re-sorted by
severity, which answered "email was slow on March 9th" with the push outage.

### Flow

```
complaint
  ├─▶ context.py   run all layers -> EvidenceBundle + citation index
  ├─▶ engine.py    Anthropic call; 5 read-only drill-down tools; max 8 iterations
  ├─▶ validator.py drop unresolvable citations; merge duplicate finding_ids;
  │                attach code-owned confidence
  └─▶ ranked output, or insufficient_evidence
```

**Tools** (all bounded, all backed by a named function): `list_findings`,
`get_finding_evidence`, `get_trace`, `query_messages`, `get_deploys`. Every
response is hard-capped with `truncated: true` and `N more not shown`. No
`run_query`, no write path, no bulk raw spans — so the model cannot author an
expensive scan and every conclusion traces to something re-runnable by hand.

**Insufficient evidence is first-class.** If nothing intersects the complaint's
scope, the tool returns what *was* checked and what would be needed. A triage tool
that always produces an answer trains people to ignore it.

### Context strategy

**Context size is O(findings), not O(telemetry volume).** A 10× traffic increase
must not change the prompt. Measured: findings payload ~4.6K tokens, whole request
~6.1K, against ~160K for raw spans + logs. Three levels: aggregates and findings
(always), ≤5 exemplars per finding plus exact counts, and on-demand drill-down.

### Model configuration

`claude-sonnet-5`, `effort: medium`. Not Opus: by the time the model is called
every number and rule-out is computed, so what remains is semantic matching and
explanation — paying 2.5× per token for a stronger reasoner to compensate for a
weak evidence layer is the wrong trade. Not Haiku: preserving a genuine ambiguity
under pressure to sound decisive is where a mid-tier model earns its cost. ~$0.05
per run; a 30-run golden-set pass is under $2.

**Determinism comes from shrinking the model's decision surface** — fixed
evidence, inherited confidence, a hard citation gate — and is then *measured*
across repeated runs. That tests the property we care about (does the answer
move?) rather than a proxy for it.

---

## 6. Log viewer

**Denylist, not allowlist.** `scoped` (has correlation or trace) and `unknown`
(matches nothing) are shown; `operational` (health, queue depth, poll) is
suppressed. The `unknown` tier is the point: an allowlist hides every new log line
the moment someone ships one, and an unfamiliar line is *more* likely to matter.

**Suppression is visible and reversible** — every invocation footers with what was
hidden. It never filters by *level*, because the six `429` lines are `WARN` and
most useful lines are `INFO`.

The viewer is a workaround; the fix is at emission (§7). Its second job is to
*measure* the noise ratio so the argument comes with a number.

---

## 7. Going live

### You cannot sample an absence

| Lever | Effect on F1 |
|---|---|
| Head sampling (5%) | "no sender span" becomes indistinguishable from "not sampled" |
| Tail sampling (errors + slow) | a dropped message has neither — it has *nothing* |

So delivery accounting must not be built on traces. Everything else — latency, hop
health, drill-down — legitimately is.

### Data access

| Option | Verdict |
|---|---|
| **A** on-demand queries | necessary for drill-down, insufficient alone — accounting becomes a full scan |
| **B** delivery ledger | **build this.** The only option that detects absence |
| **C** rollups | second. Fixed cost, and the only way seasonality baselines outlive 30-day retention |
| **D** tail sampling | yes for cost, **only after B** — adopting D first makes F1 undetectable |
| **E** warehouse mirror | reject. You have rebuilt the observability backend; C captures most of it |

**Tiered:** ledger (exact, 13 months, negligible) → rollups (fixed) → sampled
traces (bounded) → raw queries (metered). Detectors declare a tier; the rule is
**detect cheap, confirm expensive**.

### Log storage and retrieval

Three tiers, chosen by how the data will be read.

**Hot — 7 days, search backend, indexed.** Only message-scoped records, indexed on
`correlation_id`, `service` and time, because every incident query starts with one
of those. This is the tier that costs real money, so it holds the least data.

**Warm — 30 days, columnar object storage.** Parquet partitioned by
`date/service`, scanned rather than indexed, roughly a tenth the cost per GB.
Backs aggregate questions and postmortems written weeks later.

**Cold — 13 months, ledger and daily rollups only.** Kilobytes per day, so
retention stops being a cost conversation at all.

Three rules make it affordable. **Operational chatter never enters any tier** —
health probes and poll lines become metrics at emission, which is the 95.7%
reduction. **Logs explain a specific message; they never count** — counting is what
the ledger and rollups are for, so no query scans raw logs to produce a number.
And **everything is structured JSON with a stable field set**, because free-text
logs force full-text indexes and those are what make log storage expensive.

Retrieval follows the same shape: `correlation_id` is an indexed point lookup in
hot, a partition-pruned scan in warm, and never needed in cold. Every query leads
with time, then `service` — never a regex.

### Cost

**Decreasing**, by return on effort: don't store the noise (~20× on log spend);
move accounting to the ledger; rollups, then tail sampling once B is proven;
detect on aggregates and confirm on ≤5 exemplars; bound every query on an indexed
dimension first. For tokens: O(findings) context, prompt caching on the static
preamble, Haiku for routing, batch the offline work, hard-cap the tool loop.

**Guardrails:** Tier-3 commands print estimated scan volume; a per-session budget
returns "budget exhausted — here are the questions I did not get to"; and a fired
finding snapshots its raw evidence, so a postmortem six weeks later still has it.

**Defending it:** instrument the tool's own spend by detector; quote cost *per
incident* against a status quo of most of an engineer-day; lead with the 9.8%
silent delivery gap; and sequence the noise reduction first so it bankrolls the
rest — that turns "approve new spend" into "reallocate existing waste".

### Five producers

Two filtering problems needing **opposite** treatments. Unrelated services in a
shared backend → **filter hard** at the query predicate. Other teams' messages
*inside* this pipeline → **partition, don't discard**: they're the same signal
from a different source, and discarding them destroys the cross-producer
comparison that answers "is it me or the platform?". Default the *view* to one
producer; keep the *baseline* across all.

Blocked on the missing `producer.service` attribute (README § Going live). Once it
exists: elevated for one producer → their config; elevated across all → shared
infrastructure. Also needed: per-producer quotas, PII redaction in the log viewer
(recipient addresses appear in logs), and escalation to platform on-call when a
finding spans three or more producers.

### Phasing

**Week 1** — point at the backend with tight scope; ship the log-emission fixes so
the saving lands first; confirm F1 against the actual filter policy.
**Weeks 2–3** — add `producer.service` and propagate it, before rollups, because
you cannot backfill a dimension you never recorded.
**Month 1** — the delivery ledger and its reconciliation job.
**Quarter** — rollups, then tail sampling, then the is-it-me detector.

### What changes in the code

`loader.py` → a `Source` protocol (already the only I/O boundary). Detectors
declare `required_tier`. Join moves to streaming, chunked by `correlation_id`.
`Finding.affected` → `affected_count` + exemplars. Thresholds gain per-producer
overrides. The ledger writer is a change to two services, **not** to this repo.

Because Parts 1 and 3 have no model in the loop and all evidence is
code-generated, swapping the data source does not touch the AI layer at all.

---

## 8. Tests — 112, ~2s

| Suite | Asserts |
|---|---|
| `test_loader` | integrity invariants: no orphans either direction, `accepted_at` == `ACCEPT.start` ×41, no dangling parents |
| `test_join` | 218/8/4 join census; nested-vs-sequential typing; attempts split by walking back from the send; e2e excludes redeliveries |
| `test_accounting` | 34/3/4 funnel, 40 provider calls, per-channel |
| `test_health` | three error rates separate; no percentiles on zero-variance hops |
| `test_detectors` | pinned IDs; D1 fires below `min_samples`; D3's overnight gap stays one incident |
| `test_deploy_correlation` | `c52a0f9` ruled out with its reason; orchestrator deploy not attributed to D2 |
| `test_triage_validator` | fabricated citation dropped; duplicate finding_id merged; adversarial → insufficient |
| `test_live_path` | the real model loop against a fake SDK: all 5 tools execute, asserted confidence overridden, 8-iteration cap raises, prompt carries findings not raw telemetry |
| `test_unfamiliar_dataset` | **a 5-stage pipeline this code has never seen**: mid-pipeline drop found and its class named, optional branch not mistaken for loss, detectors skipped not fabricating |
| `test_keys` | resolution order, masking, `.env` round-trip, no key in any transcript |

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Detector rules overfit to 41 messages | Thresholds are parameters; tests pin outputs not internals; the general layers carry no such assumption |
| Model ranks the deploy first on symptom 3 | Rule-out computed in code and injected as a finding, not left to the model's arithmetic |
| Invariants produce noise at production cardinality | Severity is blast-radius derived; `expected_edge_share` suppresses optional branches |
| Novelty flags every deploy | It reports change without judging it — evidence, never a verdict |
