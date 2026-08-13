# Design

Companion to [`README.md`](README.md). That document says what the data shows;
this one says what gets built, why, and how it survives production.

---

## 1. Five things my first pass got wrong

I ran an adversarial pass over my own findings before designing. These changed the
design, so they lead.

**C1 — Naive hop latency produces negative numbers.** `next.start − previous.end`
gives `ACCEPT → PUBLISH_TOPIC = −26.0 ms` and `CONSUME_TOPIC → ROUTE = −18.0 ms`.
Not corruption — correct parent/child nesting. `ACCEPT` runs 48 ms from `.000`;
its child `PUBLISH_TOPIC` starts at `.022`, *inside* it. Subtracting a parent's end
from a child's start is meaningless.

→ **Stage transitions are typed.** `NESTED` — child starts before the parent ends,
same service — reports the child's offset *within* the parent and is never called
latency. `SEQUENTIAL` — child starts after the parent ends — reports a real gap.
Of the six transitions, two are nested (`ACCEPT→PUBLISH_TOPIC`,
`CONSUME_TOPIC→ROUTE`), and of the four sequential ones only the two **async
broker hops** cross a service boundary and carry queue latency; the other two are
small in-service gaps (2 ms, 4 ms). Only the broker hops feed hop-latency health.
A health view showing "−26 ms" loses the reader's trust on first contact, which is
the worst outcome for an incident tool.

**C2 — Hop latency has literally zero variance.** Every topic hop is exactly
269.0 ms across 37 messages; every queue hop exactly 379.0 ms. p50 = p95 = p99 =
min = max.

→ **No percentile engine for hop latency.** Reporting "p99: 379 ms" implies a
distribution that doesn't exist. Health reports `n`, `min`, `median`, `max`, plus
an explicit `variance: none` marker. Percentiles are computed only where distinct
values exist — in this dataset, only `SEND_PROVIDER`.

**C3 — Duplicates poison end-to-end latency.** Measuring first-span-start to
last-span-end gives 31,988 ms for `corr-0014`, `corr-0022`, `corr-0035`, because
the redelivered copy lands 31 s later. That would make 03-04 and 03-06 look like
32-second days — inventing a latency incident and burying the real one.

→ **End-to-end is defined over the first delivery attempt only.** Note the trap:
`sqs.receive_count` is set on the *send* span but **not** on the redelivered
`consume email-queue` span, whose attributes are `{}`. A filter of
"`receive_count == 1` or absent" therefore does *not* exclude the duplicate
consume. Attempts are instead partitioned by grouping each correlation's
`SEND_PROVIDER` spans in `start_time` order and taking the first.

**Attaching the rest of an attempt.** Both `consume email-queue` spans share the
*same* `publish email-queue` parent, so walking *forward* from the publish cannot
say which consume belongs to which attempt. Walk **backwards from the send**
instead: `send.parent_span_id` resolves to exactly one consume, and that consume
is that attempt's. Formally, an `Attempt` is the chain reachable by following
`parent_span_id` upward from a `SEND_PROVIDER` span until it leaves the sender.
Everything upstream of the channel queue is shared by all attempts and belongs to
the trace, not to an attempt.

**C4 — The March 9 recovery is ambiguous and I nearly overclaimed it.** My first
read was "throttling, both deploys exonerated, done." Wrong on the second half:
`e18d773` lands at 03-10 10:00, inside the recovery window (last bad send 09:00,
first clean 11:13). The *onset* attribution is falsified; the *recovery*
attribution is not resolvable.

→ **Findings carry a confidence enum and may carry competing sub-hypotheses the
tool is forbidden from collapsing.** D3 emits the incident as fact and the
recovery cause as two ranked alternatives with a `would_resolve` field. This is
the single most important behaviour in the tool: the brief's stated trap is a
misattribution, and the characteristic failure of an LLM triage layer is confident
misattribution.

**C5 — Deploy correlation needs a service constraint or it invents a story.** The
orchestrator deployed 03-04 11:00; the first duplicate is `corr-0014` at 15:39 the
same day. A proximity-only correlator ranks that as likely cause — temporally
adjacent, same day, upstream service. It is wrong: one `publish email-queue` span,
two `consume email-queue` spans sharing that one parent. The orchestrator emitted
once. And only 1 of 5 messages that day duplicated; a deploy regression would not
be so selective.

→ **`correlate_deploys()` takes a `service` argument derived from where the
evidence localises the fault, not from a time window.** Deploys of other services
are reported as "temporally adjacent, ruled out by $reason" rather than silently
omitted — surfacing the near-miss shows the reasoning instead of hiding it.

---

## 2. Join strategy

**Primary key is `correlation_id`. `trace_id` is evidence about instrumentation
health, not the identity of a message.**

The obvious join — group by `trace_id` — fails on 8 of 41 messages (every SMS) and
cannot represent the 4 push truncations at all. `correlation_id` is on 100% of
spans and 100% of message-scoped logs and survives every hop.

```python
@dataclass
class LogicalTrace:
    correlation_id: str
    accepted: AcceptedMessage | None      # None => span with no promise record
    attempts: list[Attempt]               # 1 normally, 2 when redelivered
    segments: list[TraceSegment]          # contiguous runs sharing a trace_id
    joins: list[JoinRecord]               # how each stage transition resolved
    terminal_stage: Stage                 # furthest stage reached
    anomalies: list[str]
```

Each transition records **how** it was joined, because Part 1.1 asks for it and
because the join method *is* the diagnosis for symptom 4:

| `JoinMethod` | Meaning | Count here |
|---|---|---|
| `PARENT_CHILD` | downstream `parent_span_id` resolves upstream, same `trace_id` | 218 |
| `CORRELATION_FALLBACK` | trace broken — joined on `correlation_id` + stage order + time | 8 (all SMS, queue hop) |
| `ABSENT` | no downstream span; the message stopped here | 4 (all push, topic hop) |

These count **stage transitions on the primary path** — 230 in total: 37 complete
messages × 6 transitions, plus 4 push × 2 before they stop. Not to be confused
with "spans having a parent" (224), which counts redelivery spans and says nothing
about how a hop was resolved.

`CORRELATION_FALLBACK` is not a silent degradation — it sets `trace_context_break`
and feeds detector D4. That field is the tool's answer to "where does the rest of
my SMS trace go?"

**Fallback order** when `PARENT_CHILD` fails:

1. Same `correlation_id`, expected next stage, downstream `start_time ≥` upstream `start_time`, within 60 s → `CORRELATION_FALLBACK`.
2. Multiple candidates (redelivery) → order by `start_time` and assign to attempts positionally, per C3 — not by `sqs.receive_count`, which is absent on the consume span.
3. No candidate → `ABSENT`, and `terminal_stage` is the last stage reached.

No heuristic join on `(tenant_id, message_type, time)` — inventing one would risk
fabricating links. If `correlation_id` were ever missing, the honest output is an
unjoinable fragment, reported as such.

---

## 3. Module layout

```
tracelens/
  model.py         # Span/LogRecord/Deploy/AcceptedMessage, Stage & Hop taxonomy
  loader.py        # Source protocol; FileSource (handles nested data/data/)
  join.py          # LogicalTrace assembly, JoinMethod resolution, attempts
  accounting.py    # promise ledger -> delivery funnel, terminal-stage census
  health.py        # per-service / per-hop throughput, latency, errors, retries
  topology.py      # graph learned from spans; attribute-templated node names
  invariants.py    # properties that must hold of ANY pipeline -> INV.* findings
  novelty.py       # fingerprint diff against a baseline -> NOV.* findings
  analysis.py      # runs the three layers in isolation; reports corroboration
  detectors/       # one module per detector, each -> list[Finding]
  evidence.py      # Evidence, Finding, EvidenceBundle, citation index
  triage/
    context.py     # complaint -> scoped EvidenceBundle
    tools.py       # detectors exposed as tool definitions
    prompts.py     # system + user templates
    engine.py      # Anthropic call, tool loop, orchestration
    validator.py   # rejects hypotheses citing IDs absent from the bundle
    stub.py        # recorded transcript for offline runs
  cli.py           # rich terminal UI
  report.py        # single-file self-contained HTML
```

`model.py` and `loader.py` exist from the scaffold; `model.py`'s `Hop` definition
needs C1 applied before anything else is built.

---

## 4. Metric definitions

Vague metrics are how the original team got here, so these are stated exactly.

**Delivery accounting** — left join `accepted_messages` → `LogicalTrace`. Outcome
is `DELIVERED_ONCE`, `DELIVERED_DUPLICATE`, or `STOPPED_AT_<stage>`. A message
reached the provider iff a `SEND_PROVIDER` span exists;
`provider.final_status_code` (falling back to `provider.status_code`) determines
acceptance.

**End-to-end latency** — `ACCEPT.start` → `SEND_PROVIDER.end` of the first
attempt only (C3). Per channel, per day.

**Stage transitions** (C1) — `NESTED` reports child offset within parent;
`SEQUENTIAL` reports the gap.

**Latency summary** (C2) — `n`, `min`, `median`, `max` always; percentiles only
when `len(set(values)) > 1`; `variance: none` otherwise.

**Error rate — three separate numbers**, because collapsing them is exactly how
F5 stayed hidden:

1. `span_status_errors` — spans with `status != OK`. **0/273.**
2. `provider_errors` — `SEND_PROVIDER` with non-2xx `provider.status_code`. **6/40.**
3. `delivery_failures` — accepted messages with no provider call. **4/41.**

The gap between (1) and (3) is itself a headline finding, so health prints all
three adjacently with the divergence called out.

**Retries** — `sum(retry_count)` over `SEND_PROVIDER` (18), plus redelivery count
from `sqs.receive_count > 1` (3). Different phenomena, never summed.

**Throughput** — messages per service per bucket, **no calendar assumptions**. A
zero-message bucket is `insufficient_data`, never a drop and never an expected
weekend.

**Baselines**, in priority order:

1. **Hour-of-week seasonality** — same hour, same weekday, trailing 4 weeks. Handles weekend-quiet *and* weekend-busy without knowing which applies, and survives lower-env → prod unchanged.
2. **Trailing window** — fewer than 4 comparable periods (true in this 10-day export) falls back to a trailing window of the same channel's healthy observations, marked `low_confidence`.
3. **Minimum-n gate — applies to rates, never to counts.** No *rate* is reported, and no detector fires *on a rate*, below `min_samples` (default 20). This is what stops "org-5502 has a 40% duplicate rate" (n=5) from being emitted as a finding.

**The gate must not suppress absolute-count findings, and this is the sharpest
edge in the whole spec.** D1 fires on push at n=4 and D4 fires on SMS at n=8. Both
are below the gate; both are real. A message the platform promised and did not
deliver is a finding at n=1 — you do not need statistical support to claim that a
specific message is missing, because it is not a claim about a population. So:

| Claim type | Example | Gate |
|---|---|---|
| **Existence / count** — "these 4 named messages stopped at `PUBLISH_TOPIC`" | D1, D2, D4 | **Never gated.** Fires at n ≥ 1. |
| **Rate over a population** — "push has a 100% loss rate", "org-5502 duplicates at 40%" | severity scoring, tenant breakdowns | Gated at `min_samples`. Below it, print the raw count and `sample too small to rate`. |

D1's rule is therefore "count of messages whose `terminal_stage` < `SEND_PROVIDER`,
grouped by channel and terminal stage" — a count, ungated. The *rate* it prints
alongside (4/4 = 100%) is labelled `low_confidence` because n=4 < 20. The finding
fires either way. Getting this backwards silently deletes the most severe finding
in the dataset, which is exactly the failure mode this tool exists to prevent.

Every baseline here lands in tier 2 with `low_confidence`, and that label is
printed. Production uses tier 1 through the same code path — seasonality does not
need re-solving at go-live.

---

## 5. Detectors

Every detector is pure deterministic code returning `Finding` objects with
pre-resolved `Evidence`. The model never computes a number or selects an ID.

```python
@dataclass(frozen=True)
class Evidence:
    kind: Literal["correlation_id","trace_id","span","log","deploy","metric"]
    ref: str        # the citable identifier
    detail: str     # human-readable, pre-rendered
    source: str     # e.g. "spans.json#span_id=00000000000a744a"

@dataclass(frozen=True)
class Finding:
    id: str         # "D1.channel_drop.push"
    title: str
    severity: Literal["critical","high","medium","low"]
    confidence: Literal["observed","inferred","ambiguous"]
    summary: str
    evidence: list[Evidence]
    affected: list[str]
    alternatives: list[Hypothesis]   # competing explanations, may be empty
    would_resolve: list[str]         # what data would settle it
```

| ID | Rule | Output on this data |
|---|---|---|
| **D1** channel drop | Per channel, accepted vs `terminal_stage`. Flag any stopped-rate > 0, grouped by terminal stage. | `push` 4/4 stop at `PUBLISH_TOPIC`. Critical, observed. Tenant and date spread reported to counter the single-campaign framing. |
| **D2** duplicate delivery | `SEND_PROVIDER` count > 1 per correlation. Classify: >1 `PUBLISH_QUEUE` ⇒ double-publish; 1 publish + n consumes ⇒ redelivery. Report inter-attempt delta and `sqs.receive_count`. | 3 findings, all redelivery, Δ = 31.0 s, `receive_count: 2`. Explicitly rules out double-publish (C5). |
| **D3** provider degradation | Baseline `SEND_PROVIDER` per channel from sends with a 2xx `provider.status_code`. Mark a send *affected* if non-2xx **or** duration > `slow_factor` × baseline (default 3). Group affected sends into windows (below). Then `correlate_deploys(service="comms-sender", window)`. | 03-09 09:00 → 03-10 09:00, 6 messages, 429 / retry 3 (17.5× baseline). `c52a0f9` **ruled out — postdates onset by 5h00m**. Recovery emits H1/H2 with `confidence: ambiguous` (C4). |
| **D4** trace context break | Per trace, count distinct `trace_id`s and root spans. Report the exact stage boundary, and whether the orphan is reachable via logs. | 8/8 SMS break at the queue hop; orphan trace IDs in 0 log lines. Email 0/29 — the contrast localises the bug to the SMS consumer. |
| **D5** blind spots | (a) `status != OK` rate vs `delivery_failures` rate; (b) log noise ratio and unjoinable share; (c) gauge metrics with a single constant value or no dimension label. | (a) 0/273 vs 4/41; (b) 95.7% noise, 2,700 unjoinable; (c) queue depth constant 0 across 1,200 records, no queue label. |

**D3 window grouping, stated exactly** — the assertion "03-09 09:00 → 03-10 09:00"
is only reachable under a specific rule, so it needs writing down. Affected sends
are sorted by `start_time` and split into a new window whenever the gap to the
previous affected send exceeds `max_gap` (default **24 h**). The largest gap in
this incident is the overnight 03-09 17:52 → 03-10 09:00, which is 15h08m —
inside the default, so the six stay one incident. A `max_gap` under 15h08m splits
them into two windows and the deploy arithmetic changes, so the parameter is
printed in the finding rather than left implicit.

Two guards on the same rule:

- **Baseline is per channel, over 2xx sends only.** Not "days with no non-2xx" — that would discard `corr-0033` and `corr-0035`, clean sends on 03-10, and leave the baseline blind to the recovery.
- **A window boundary is not a recovery claim.** The window *ends* at the last affected send (03-10 09:00). The recovery *window* — last bad send to first clean send, 09:00 → 11:13 — is a separate field, and it is the one deploys are tested against for the H1/H2 ambiguity.

`correlate_deploys(service, window)` returns deploys of *that* service in the
window plus a lookback, each annotated `plausible` or `ruled_out(reason)`. Other
services' deploys go in a separate `adjacent_other_services` list so the near-miss
is visible rather than hidden (C5).

---

## 5.1 The closed-world problem, and the two layers that fix it

Every rule in §5 was written after I already knew the answer. That makes the
detector catalog a **closed world**: precise on the five known symptoms,
structurally unable to surface a sixth. Point it at a pipeline it wasn't written
for and it produces nothing — and "no findings" is indistinguishable from
"healthy", which is exactly the silence F5 is about.

Two further layers carry no knowledge of this pipeline at all.

| Layer | Pipeline-specific? | Answers | On unfamiliar data |
|---|---|---|---|
| **Detectors** `D*` | yes | *why* — mechanism, cause, ruled-out alternatives | mostly silent |
| **Invariants** `INV.*` | no | *what* broke, never why | fully working |
| **Novelty** `NOV.*` | no | *what changed* since a baseline | fully working |

### 5.2 Invariants — properties, not failures

Each check states something that must hold of *any* message pipeline and reports
the violation. A violation is novel by construction: it needs no rule and no
prior example.

| Invariant | Property | Generalises |
|---|---|---|
| `conservation` | what enters a hop must leave it | D1, for any hop and any class |
| `path_shape` | messages follow one of a few routes | truncation vs divergence |
| `settlement` | every ledger promise reaches a terminal node | delivery accounting |
| `context_integrity` | a trace must not fragment mid-journey | D4 |
| `single_visit` | a message traverses each node once | D2 |
| `referential` | references resolve, records join | the integrity checks |

Three details make these work rather than merely sound good.

**Severity is derived, not assigned.** On unfamiliar data there is no prior about
which violation matters, so severity comes from blast radius — the share of
messages affected.

**Conservation names the discriminating attribute.** Given the lost set and the
surviving set, it looks for an attribute value present in all of the former and
none of the latter. On this data that yields `message_type=push` without the code
knowing channels exist; on another pipeline it finds whatever discriminates
there. "Is this a whole class or a scattering?" is the first question anyone asks
about a silent loss, and it is answerable generically.

**Optional branches are not losses — and this is subtle.** A retry stage taken by
15% of messages must not make the other 85% look dropped. So an edge counts as
*expected* only if at least `expected_edge_share` (default 0.5) of the messages at
a node traverse it, and a node is *terminal* when it has no expected successor.
The threshold cannot sit near 1.0: a real drop drags the edge's own share down, so
it has to be below the loss it is meant to detect. Half is the honest line between
"the normal route" and "a branch". Getting this wrong produced a false silent-loss
finding on the very first unfamiliar dataset — see §5.4.

### 5.3 Topology is learned, not configured

`model.py` hardcodes seven stages because that is what this pipeline has. It is
kept for rendering a single trace legibly, and it is **not** the foundation.
`topology.py` derives the graph from the spans in one pass.

The trick that makes it channel-independent: span names embed attribute values
(`publish email-queue`, `send sms`). Substituting the value back out gives
`publish {message_type}-queue`, so three channels collapse to one node — and a
fourth channel added tomorrow lands on that node instead of registering as
novelty. Without templating, every new tenant or channel would look like a new
pipeline.

### 5.4 Novelty — what changed, in both directions

Some real problems violate nothing. A new provider status code nobody handles, a
log line from a path that should be unreachable, a stage that quietly stopped
appearing — the pipeline is simply not the pipeline it was.

So the third layer records a **fingerprint** — services, nodes, edges, entry
points, route shapes, span kinds and statuses, provider statuses, attribute keys,
log levels, log templates, channels — and diffs it against a stored baseline.
Deliberately shapes and cardinalities, never counts: volume legitimately changes
between environments and over time, and a fingerprint that moved with traffic
would flag every busy Monday.

Both directions matter. Something appearing is the usual suspect during an
incident. Something *vanishing* is what a silently disabled code path looks like,
it produces no error, and it is much harder to notice by eye — so a vanished
node, service or channel is escalated to `critical` while an appearing log
template is `low`.

This layer has no opinion about whether a change is bad. A deploy and an incident
look identical to it. That is why it emits evidence rather than verdicts.

### 5.5 How I know the general layers actually generalise

Asserting that code is topology-agnostic is cheap. `tests/test_unfamiliar_dataset.py`
builds a synthetic **five**-stage payments pipeline — `api-gateway → fraud-check →
ledger-writer → settlement → bank-adapter`, rails named `wire`/`ach`/`card`, no
deploys — and injects two faults neither the detectors nor the invariants were
written against:

1. a **mid-pipeline** silent drop (one rail swallowed at `fraud-check`, not at the first hop as in the real data), and
2. an **optional retry stage** reached by a minority of messages.

The tests assert that conservation finds the drop and names `message_type=ach`,
that settlement reconciles to exactly the 20 affected messages, that the retry
stage is reported as a *divergent* route rather than a truncation, that no layer
raises, and that finding IDs are stable across runs.

Writing it found two real bugs that the familiar dataset could never have exposed:
the optional retry stage made every message skipping it look lost (fixed by the
expected-edge rule in §5.2), and a path-shape finding built its ID from Python's
`hash()`, which is salted per process — so the ID changed on every invocation,
silently breaking both the determinism guarantee and any test pinned to it.

**Layers fail independently.** `analysis.py` runs each in isolation; if one raises
on an unfamiliar export the others still report, and the failure surfaces as an
`ERR.*` finding. A layer that silently produces nothing looks exactly like a clean
bill of health, which is the failure mode this whole tool exists to prevent.

### 5.6 Where the layers overlap, that is corroboration

On this dataset the invariants independently rediscover F1, F2, F4 and F7. That
is not redundancy worth removing: two layers reaching the same message set from
independent directions is the strongest signal the tool produces, so
`tracelens findings` reports it explicitly rather than leaving a reader to notice
repeated IDs. The detector then supplies the mechanism the invariant cannot —
*what* broke from the invariant, *why* from the detector.

---

## 6. AI triage

### 6.1 The split

| Computed in code | Handed to the model |
|---|---|
| Every join, count, duration, rate, window boundary | Mapping a vague complaint onto detectors |
| Which correlation and trace IDs are affected | Ordering hypotheses by fit to what was actually asked |
| Deploy timestamp comparison and rule-out logic | Explaining the causal story in plain language |
| Severity, confidence, alternatives | Judging when the complaint matches nothing |

**One boundary worth stating precisely, because getting it wrong is subtle.** Code
owns the severity and confidence *labels* and the global ranking of findings. The
model owns the *order of hypotheses in an answer*. Those are different things: an
early draft re-sorted the validated hypotheses by severity, which meant every
complaint — including "email was slow on March 9th" — was answered with the push
outage, because push is the most severe finding in the dataset. Relevance to the
question asked is exactly the fuzzy judgement the model is here for, so the
validator preserves its order and constrains only what it may claim.

Everything on the left must be identical on every run — during an incident, an
analyzer that returns a different number each time is worse than none. Everything
on the right is fuzzy language work where code would be brittle regexes.

Rationale, honesty mechanisms, and the how-would-I-know-it's-wrong tests are in
[README § AI in and out](README.md#ai-in-and-out).

### 6.2 Flow

```
complaint
   ├─▶ context.py  run ALL detectors (cheap: 273 spans) -> EvidenceBundle
   │                + pipeline summary + deploy list + citation index
   ├─▶ engine.py   Anthropic call; tools = drill-downs over surfaced findings
   │                -> structured JSON: ranked hypotheses with evidence refs
   ├─▶ validator.py  drop any hypothesis citing a ref absent from the index
   └─▶ ranked output, or insufficient_evidence
```

**Tool surface**, fixed at five. Every one is read-only, bounded, and backed by a
named analysis function I can re-run by hand:

| Tool | Arguments | Returns |
|---|---|---|
| `list_findings` | — | `[{finding_id, title, severity, confidence, affected_count, exemplars[≤5]}]` |
| `get_finding_evidence` | `finding_id: str` | `{finding_id, evidence: [{kind, ref, detail, source}], alternatives, would_resolve}` — ≤20 items, truncation marked |
| `get_trace` | `correlation_id: str` | `{correlation_id, channel, tenant_id, terminal_stage, stages: [{stage, service, start, duration_ms, join_method}], attempts, anomalies}` |
| `query_messages` | `channel?`, `tenant_id?`, `terminal_stage?`, `since?`, `until?`, `limit=20` | `{total, returned, messages: [{correlation_id, channel, tenant_id, terminal_stage, accepted_at}]}` — `total` is exact even when `returned` is capped |
| `get_deploys` | `service?`, `since?`, `until?` | `[{sha, service, deployed_at, pr, title}]` |

Every response is a JSON object, hard-capped, and carries `truncated: true` with
`N more not shown` when the cap bites — so the model always knows it is seeing a
sample. Errors return `{error: "..."}` rather than raising, so a bad tool call
costs one iteration instead of the run. There is deliberately no `run_query`, no
write path, and no tool that returns raw spans or raw log lines in bulk.

Running all detectors up front rather than letting the model explore is
deliberate: full precomputation is free at this size, it makes the evidence set
fixed and auditable, and the model cannot reach a conclusion by a path I can't
reconstruct afterwards.

**The insufficient-evidence path is first-class.** If no finding's affected set
intersects the complaint's scope, the tool returns what *was* checked, what would
be needed, and no hypothesis. A triage tool that always produces an answer trains
people to ignore it.

### 6.3 Context strategy

**The invariant: context size is O(findings), not O(telemetry volume).** A 10×
traffic increase must not change the prompt size. This is the one property that
makes the AI layer survive go-live unchanged.

| Payload | ≈ tokens |
|---|---|
| `spans.json` + `logs.json` raw (658 KB) | ~160 K |
| Spans + the 120 *scoped* logs only | ~35 K |
| **Computed findings + capped exemplars** (measured) | **~4.6 K** |
| — plus system prompt and framing, i.e. the whole request | ~6.1 K |

Measured, not estimated: `test_triage_validator.py` asserts the findings payload
stays under 40 KB, and the figures above come from
`len(json.dumps(bundle.as_prompt_payload()))`. An earlier draft of this document
claimed ~2 K from a back-of-envelope guess; the real number is a little over
twice that, because pre-rendered evidence `detail` strings are verbose by design —
they exist so the model never formats a number itself.

Even on this toy dataset, dumping raw telemetry is near the practical limit — and
only becomes viable after a noise filter that is itself a code-side decision the
model shouldn't make. At production volume it is off by orders of magnitude,
permanently. This is not a bigger-context-window problem.

Three levels of assembly: **findings and aggregates** (always sent, fixed size);
**capped exemplars** (≤5 rendered traces per finding, plus exact counts —
`affected_count: 12431, exemplars: [corr-0005, …]`, so the model reasons over five
concrete cases and cites the count for scale); **on-demand drill-down** (tools,
every response hard-truncated with an explicit `N more not shown` marker).

**Deliberately not raw-query tools.** No `run_query(sql)`. Analysis-level tools buy
three things: the model cannot author an expensive full scan, every conclusion
traces to a named function I can re-run by hand, and the tool surface is small
enough to describe accurately in the prompt. Flexibility is the trade.

**Batch anything that is many independent items with no deadline; keep anything a
human waits on synchronous.** Incident triage is synchronous — nobody waits on a
batch queue at 3am. Log-template classification, weekly digests, and embedding
generation go to the Batch API at roughly half price.

**Bounded two-phase agent**, not open-ended:

```
1 ROUTE     cheap model or embeddings: complaint -> {scope, window, candidate
            detectors, match_confidence}.  match_confidence == none -> stop, $0
2 ASSEMBLE  pure code, parallel: detectors over scope -> bundle + citation index,
            exemplars prefetched concurrently rather than by serial tool calls
3 REASON    strong model, temp 0, structured output, max 8 tool iterations
4 VALIDATE  pure code: drop unresolvable citations, attach code-owned confidence
```

Phase 1 is the cost gate. Phase 2 being code rather than agent turns six serial
round-trips into one parallel fetch — most of the latency saving in the design,
and it is what makes the evidence set auditable.

### 6.4 Model configuration

`claude-sonnet-5`, `effort: medium`, tool use, structured JSON output, via
`ANTHROPIC_API_KEY`. `--stub` (auto-engaged with no key) runs offline; recorded
transcripts are committed under `examples/`.

**Why Sonnet 5 and not Opus 5.** The split in §6.1 is the whole argument: by the
time the model is called, every join, count, duration, window boundary, affected
ID and deploy rule-out is already computed. What is left is semantic matching
("a couple of supporters got the same confirmation email twice" → the duplicate
detector), ranking by relevance, and explaining a causal story in plain language.
That is not a frontier reasoning problem. Opus 5 costs 2.5× per token ($5/$25 per
MTok versus $2/$10) to do a job that was deliberately made easy — and paying for
a stronger reasoner to compensate for a weak evidence layer would be exactly the
wrong trade. If the golden set showed Sonnet collapsing F3's ambiguity, the fix
is a better gate, not a bigger model.

**Why not Haiku 4.5.** Cheaper again ($1/$5), and genuinely the right choice for
the *routing* phase and for nightly log-template classification (§10.5). But the
one behaviour that matters most here — preserving a genuine ambiguity instead of
collapsing it into a confident answer — is a judgement call under pressure to
sound decisive, and that is where a mid-tier model earns its cost. Haiku also has
a 200k context against Sonnet's 1M, which is irrelevant today at ~6K tokens but
constrains the production path where bundles carry more findings.

**Cost.** Roughly $0.05–0.10 per triage run (~18K input tokens across the tool
round-trip, ~2K output). A full golden-set validation pass — 6 cases × 5 runs — is
under $2. Cheap enough that the honesty tests can run on every change, which is
the point of keeping them executable.

**No `temperature`, and this is not an oversight.** Sonnet 5, Opus 5 and Fable 5
return a **400 on any non-default `temperature`, `top_p` or `top_k`** — on every
request, whether or not thinking is active. An earlier draft of this document
specified `temperature: 0` for determinism and the code sent it; the first live
call would have failed outright.

The deeper point is that determinism was never really available from a sampling
parameter. It comes from **shrinking the model's decision surface**: the evidence
set is fixed and code-generated, confidence and severity are inherited rather than
asserted, citations are checked against an index, and the schema forbids
collapsing a finding that carries alternatives. What remains variable is wording
and ordering, so stability is **measured** — each golden case runs 5× and the
top-ranked finding ID must be identical — rather than assumed. That is a better
guarantee than a parameter would have given, because it tests the property we
actually care about instead of a proxy for it.

**`effort: medium` rather than the `high` default.** Effort governs total token
spend *including how many tool calls get made*, and the tools here only drill into
findings that have already surfaced. Medium is the honest setting for a model
selecting from a prepared evidence set; the golden set is the instrument for
moving it either way, and `--effort` exposes it per run so that sweep is one
command rather than a code change.

---

## 7. CLI

```
tracelens trace corr-0003          # waterfall, per-stage timing, join method per hop
tracelens trace corr-0005          # truncated path, terminal stage highlighted
tracelens account [--by channel|tenant|day]
tracelens health [--service X] [--hop Y]
tracelens findings [--severity critical]
tracelens logs [--corr ID] [--service X] [--grep P] [--show-suppressed] [--no-filter]
tracelens triage "we got the same email twice"
tracelens triage --symptom 3
tracelens report --out report.html
```

`trace` renders a `rich` tree with a proportional duration bar, a `⚡` where the
trace ID changes, and a red terminal marker where the path stops. This is the view
that makes F1 and F4 obvious in one screen.

### 7.1 Log viewer

120 useful lines out of 2,820 is the difference between grep working and grep
being abandoned, and on-call named it directly. Two decisions matter more than the
filtering.

**Denylist, not allowlist.**

| Tier | Rule | Default |
|---|---|---|
| `scoped` | has `correlation_id` or `trace_id` | **shown** |
| `operational` | matches a known-noise pattern (`GET /health`, `queue depth metric`, `Polling queue`) | suppressed |
| `unknown` | matches nothing | **shown** |

The `unknown` tier is the point. An allowlist silently hides every new log line
the moment someone ships one — and an unfamiliar log line is *more* likely to be
interesting, not less. A denylist degrades safely. Patterns live in config, not
code, so on-call can add one without a release.

**Suppression is always visible and reversible.** Every invocation footers with
`2,700 lines suppressed — 1,200 health, 1,200 queue-depth gauge, 300 poll`;
`--show-suppressed` and `--no-filter` restore them. A filter that hides its own
existence is how you lose the one line that mattered. This is also why the viewer
never filters by *level*: the six `429` lines are `WARN`, most useful lines are
`INFO`, and 1,500 `DEBUG` records are pure noise — level is nearly uncorrelated
with usefulness here.

**The viewer is a workaround.** The permanent fix is at emission and it is cheap:
stop logging successful health probes (1,200); make queue depth a real gauge with
a `queue` label (1,200, and currently a broken gauge wearing a log's clothing —
constant `0`, no dimension, F6); move poll chatter to sampled `DEBUG` (300). 95.7%
reduction, one afternoon. The viewer's second job is to *measure* the noise ratio
so that argument comes with a number attached.

**One derived metric worth alerting on:** the unjoinable share — log records with
no `correlation_id` and no `trace_id`, 95.7% today. A sudden rise means someone
shipped code that logs outside a trace context. That is an instrumentation
regression caught before the next incident rather than during it.

---

## 8. HTML report

Single self-contained file, hand-rolled inline SVG, no CDN and no build step — it
must open from a clone with no network. Sections: delivery funnel, per-channel
accounting, latency over time annotated with deploy markers and the throttling
window, findings by severity, recorded triage transcripts. Committed so a reviewer
sees the analysis without running anything.

---

## 9. Tests

| Suite | Asserts |
|---|---|
| `test_loader.py` | integrity invariants: no orphans either direction, no duplicate ledger IDs, `accepted_at` == `ACCEPT.start` ×41, no dangling `parent_span_id`, no log `trace_id` contradicting spans |
| `test_join.py` | 8 SMS `CORRELATION_FALLBACK`; 4 push `ABSENT`; 224 `PARENT_CHILD`; nested-vs-sequential typing (C1) |
| `test_accounting.py` | 37/41 delivered, 4 stopped at `PUBLISH_TOPIC`, 40 provider calls; e2e excludes redeliveries **including the consume span that carries no `receive_count`** (C3) |
| `test_health.py` | three error rates reported separately; no percentiles on zero-variance hops (C2) |
| `test_detectors.py` | pinned IDs — D1 `{0005,0010,0020,0036}`, D2 `{0014,0022,0035}`, D3 `{0026,0027,0029,0030,0031,0032}` |
| `test_deploy_correlation.py` | `c52a0f9` ruled out with the 5-hour reason; orchestrator deploy not attributed to D2 (C5) |
| `test_triage_validator.py` | fabricated citation dropped; duplicate finding_id merged; adversarial complaint → insufficient evidence |
| `test_live_path.py` | the real model loop against a fake SDK: all five tools execute, no sampling parameter is sent, model-asserted confidence is overridden, the 8-iteration cap raises |
| `test_unfamiliar_dataset.py` | **a five-stage pipeline this code has never seen**: a mid-pipeline drop is found and its class named, an optional branch is not mistaken for a loss, no layer raises, IDs are stable (§5.5) |
| `test_keys.py` | key resolution order, masking, `.env` round-trip, no key in any committed transcript |

The golden-set tests make the honesty guarantees executable rather than claimed.

---

## 10. Going live

30-day retention, queries cost money, five teams produce into this pipeline.

### 10.1 You cannot sample an absence

F1 has zero error spans, zero `ERROR` logs, no non-`OK` status. Its signal *is* the
missing `consume comms-queue` span. Apply the two standard cost levers:

| Lever | Effect on F1 |
|---|---|
| **Head sampling** (keep 5%) | "No sender span for `corr-0005`" becomes indistinguishable from "not sampled." The detector cannot fire. |
| **Tail sampling** (keep errors + slow) | Tail rules select on error status and duration. A dropped message has neither — it has *nothing*. The sampler drops it for the same reason the alerting misses it. |

The finding that matters most is precisely the one that disappears the moment you
make telemetry affordable — and it fails silently in the safe-looking direction:
dashboards get cleaner as reliability gets worse.

**So delivery accounting must not be built on traces.** Everything else — latency,
hop health, drill-down — legitimately is.

#### 10.1.1 Lower environment and production fail in different classes

Worth separating from the volume argument, because they are not the same point
and the volume one is the weaker of the two.

A lower environment runs uniform synthetic traffic through one producer with no
contention. It surfaces **correctness** bugs — a filter policy that drops a
channel, a consumer that doesn't propagate context, a gauge with no dimension.
Those are wrong everywhere and fixing them here fixes them in production.

Production surfaces **emergent** behaviour that simply cannot occur here: noisy
neighbours on a shared quota, hot partitions, poison messages, retry storms,
backpressure cascades, partial and canary deploys, clock skew across many hosts,
one tenant at 80% of volume. No threshold calibrated on this export predicts any
of them, and a threshold that *looks* calibrated is worse than an absent one
because it carries false authority.

Two consequences for this design:

- **Everything numeric here is a parameter with a printed value, never a literal.** `config.py` exists so that the go-live conversation is "re-tune these nine numbers against real traffic", not "find where the constants are buried".
- **The general layers matter more in production than the detectors do.** Detectors encode failures already understood, and production's failures are mostly the ones nobody has understood yet. Invariants and novelty are the parts of §5.1 that survive the environment change intact.

There is also a claim in F7 worth softening. 95.7% of log volume being health
checks, poll chatter and a queue-depth gauge reads as waste, but it is more likely
instrumentation someone added because it was genuinely useful in dev and which was
never gated by environment. The fix is level-and-sampling per environment, not
deletion — deleting something QA depends on is how an observability cleanup gets
reverted a week later.

## 10.2 What else breaks

| Prototype assumption | Production reality |
|---|---|
| Load all spans, join in Python | Millions/day. Streaming, or the join pushed into the backend. |
| Run all detectors on every invocation | Each becomes one or more paid queries. A recurring bill. |
| Evidence lists every affected `correlation_id` | "Affected: 12,431" cannot go in a context window. Capped exemplars + counts. |
| One producer, no contention | Five teams share the topic, queues, and provider quota. The March 9 `429` might be another team's burst. |
| History is whatever is in the file | 30 days, then gone. The push bug ran the whole 10-day sample; a chronic issue can age out before anyone connects the dots. |
| Thresholds tuned on n=41 | Minimum-sample gates and per-producer baselines. |

### 10.3 Data access — five options

| Option | Cost profile | Answers well | Verdict |
|---|---|---|---|
| **A** On-demand passthrough | Zero fixed, unbounded variable | Narrow lookups: "show me trace X" | Necessary for drill-down, insufficient alone. Accounting becomes a full scan — the most valuable question is the most expensive. |
| **B** Delivery ledger | ~2 small rows/message vs 7 spans + 3 logs. Storage cost, not query cost. | Exactly what traces answer badly: promised vs delivered, what's missing now, the real SLO | **Build this.** The only option that detects absence. |
| **C** Precomputed rollups | Fixed — one scan per period, amortised over every read | Health trends, which-hop-needs-attention, seasonality baselines | Build second. Dimensions must be chosen before you need them. |
| **D** Tail sampling + exemplars | The big lever — ~10× span reduction | Preserves the tails incidents care about; exemplars keep aggregates clickable | Yes for cost, **only after B**. Adopting D without B actively makes F1 undetectable. |
| **E** Full warehouse mirror | Duplicates the telemetry bill in storage | Unlimited retention, arbitrary SQL | Reject for now. You have rebuilt the observability backend; C captures most of the benefit for a fraction of the effort. |

**Option B in detail.** Two cheap durable events written out-of-band from
telemetry: `message_accepted` at ingest, `message_settled` at the sender with the
provider outcome. Keyed by `correlation_id` in a dedicated store (Postgres,
DynamoDB, or a Kafka topic landed in the warehouse) with its **own 13-month
retention**. A continuous reconciliation job flags any accepted message with no
settlement past a channel-specific SLA.

It is exact and unsampled, so §10.1 is solved; it survives 30-day retention; a push
backlog pages within minutes instead of running unnoticed for ten days; and it
becomes the source of truth for a delivery SLO you can put in front of product
teams. Costs: a new production dependency in two services, a migration, and
dual-write drift that needs its own reconciliation against provider receipts.
Someone has to own it.

### 10.4 Tiered hybrid

```
Tier 0  Delivery ledger (B)   exact, unsampled, 13-month     cost: negligible
        └─ accounting, absence detection, SLO, alerting
Tier 1  Rollups (C)           producer × channel × hop × hr  cost: fixed
        └─ health, trends, seasonality baselines
Tier 2  Sampled traces (D)    tail-based, 100% of errors     cost: bounded
        └─ drill-down, waterfalls, per-hop diagnosis
Tier 3  Raw queries (A)       on demand, scoped, budgeted    cost: metered
        └─ incident forensics on specific correlation IDs
```

Detectors declare which tier they need. The rule is **detect cheap, confirm
expensive**: fire on Tier 0/1 evidence, then spend Tier 2/3 to confirm the
mechanism on a bounded set of exemplars.

| Detector | Tier | Note |
|---|---|---|
| D1 channel drop | 0 | Ledger reconciliation. Becomes an *alert*, not an investigation. |
| D2 duplicate | 0 → 3 | Ledger detects >1 settlement; raw query confirms redelivery vs double-publish on ~5 exemplars. |
| D3 provider degradation | 1 → 3 | Rollup histograms detect the window; raw query pulls status codes. |
| D4 trace break | 1 | Rollup counts distinct `trace_id` per `correlation_id`. An instrumentation-health metric, not an incident. |
| D5 blind spots | 1 | Noise ratio and status-vs-reality divergence are aggregates by nature. |

Note what happens to D1: it stops being something an engineer discovers a week
later and becomes a page within minutes. That is the whole return on this work.

### 10.5 Cost — decreasing it

Two budgets, never conflated: **backend queries** and **LLM tokens**.

Queries, in order of return-on-effort:

1. **Don't store the noise.** You pay to ingest, store, index, *and* scan 95.7% junk. ~20× reduction in log spend for one afternoon of emission changes.
2. **Move accounting onto the ledger** (B), then **rollups** (C), then **tail sampling** (D) once B makes it safe. Removes the most expensive recurring query class, converts variable cost to fixed, then cuts span volume ~10×.
3. **Detect on aggregates, confirm on exemplars.** Never scan raw data to *find* a problem — scan it to confirm one the rollups flagged, on ≤5 correlation IDs.
4. **Bound every query on an indexed dimension first** — time, then `service`, then `producer`, then `channel`. Never lead with a regex. Often the difference between a partition scan and a full scan.

Tokens: context stays O(findings) (§6.3); prompt-cache the static preamble, which
is re-sent on every incident follow-up; Haiku for routing and log classification,
the strong model only for ranking; batch the offline work; hard-cap the tool loop
at 8 iterations, because an unbounded agent is an unbounded bill.

**Guardrails.** Tier-3 commands print estimated scan volume and need confirmation
above a threshold (`--dry-run` for the plan alone). Per-session budget of 10
Tier-3 queries; on exhaustion the tool returns what it has plus "budget exhausted
— here are the questions I did not get to," which is more honest than silently
truncated evidence. Default scope 24 h and the current producer. And **freeze
incident evidence** — snapshot a finding's raw spans and logs, a few KB, so a
postmortem written six weeks later still has them despite 30-day retention.

### 10.6 Cost — defending it

Cost defence fails when it is an assertion. Make it arithmetic.

1. **Instrument the tool's own spend** — bytes scanned, queries, tokens, tagged by detector and user. You cannot defend a budget you cannot itemise, and the first run of this report almost always names one detector responsible for most of the bill.
2. **Quote cost per question answered.** Not "$X/month" but "$X per incident against a status quo of most of an engineer-day." One engineer-day exceeds a generous monthly query budget. That ratio is the sentence for the Slack message.
3. **Lead with the loss it detects.** 9.8% of messages returned `202` and never delivered, 100% of one channel, ten days. At production volume that is undelivered donation receipts and volunteer confirmations — donor-trust weight, not engineering embarrassment.
4. **Let the noise fix pay for the tool.** Sequence the 95.7% log reduction first so it bankrolls the ledger and rollups. Turns "approve new spend" into "reallocate existing waste."
5. **Show the counterfactual.** Every finding was invisible to existing alerting — 0 non-`OK` spans, 0 `ERROR` logs. Current spend buys telemetry that caught none of them. That is the argument for *re-shaping* spend, not adding to it.

### 10.7 Five producers on one pipeline

Two filtering problems that need **opposite** treatments.

**Unrelated services in a shared backend** hold telemetry for the whole company
and are genuinely irrelevant. → **Filter hard**, at the query predicate:
`service IN (comms-ingest, comms-orchestrator, comms-sender)` as a leading indexed
predicate. Correct scoping and cost control are the same action here.

**Other teams' messages inside the comms pipeline** are not noise — the four other
teams are *producers* calling `POST /api/v1/messages`, so their messages are the
same signal from a different source. → **Partition, don't discard.** Discarding
them destroys the two most valuable production capabilities: the cross-producer
comparison that answers "is it me or is it the platform?", and noisy-neighbour
detection on the shared quota. Default the *view* to one producer; keep the
*baseline* across all of them.

**The rule: filter out other services, scope across other producers.** A tool that
treats both as noise answers every incident with "your team's traffic looks fine"
while the platform burns.

**The blocker: there is no producer attribute today.** See
[README § Going live](README.md#going-live) for the full attribute inventory and
the `producer.service` recommendation. It must be sequenced before rollups —
rollup dimensions cannot be added retroactively once raw data has aged out.

Once it exists:

- **Is it me or is it the platform?** A detector compares a producer's rate against the cross-producer baseline for the same hop and window. Elevated for **one** producer → their payloads, config, tenant, filter policy. Elevated across **all** → shared infrastructure or provider. This is the question every one of the five complaints implicitly asks, and routing incidents correctly is most of the "most of a day to figure out what happened" cost. Applied to March 9: all five producers throttled ⇒ shared SendGrid quota; one producer ⇒ that team's send rate, and the fix is a per-producer quota, not an SDK bump.
- **Noisy neighbours.** Per-producer rate limits and a fair-share policy at the sender, plus "which producer consumed the quota" on every throttling finding.
- **Access and PII.** Logs contain recipient addresses (`Sending email to recipient-N@example.org`). Findings must be scoped so a team sees only its own traffic, and the log viewer must redact recipient identifiers by default. A compliance requirement before it is a UX one, and it does not exist in the single-tenant prototype.
- **Shared-fate alerting.** Page by producer; escalate to platform on-call when a finding spans three or more producers, so five teams don't independently debug one shared failure.

### 10.8 Phasing

**Week 1 — stop the bleeding, cost nothing.** Point the tool at the backend with
Option A and a tight scope. Ship the log-emission fixes — 95.7% reduction, landing
first deliberately so the saving bankrolls what follows. Confirm F1's mechanism
against the actual SNS filter policy: a console lookup, not a project.

**Weeks 2–3 — record the dimension you cannot backfill.** Add `producer.service` /
`producer.team` at ingest and propagate it. Small work with a hard deadline: every
day it is missing is a day of telemetry that can never answer "whose traffic is
this?", and at 30 days the gap becomes permanent.

**Month 1 — make absence detectable.** Delivery ledger (B) and its reconciliation
job, alerting on accepted-but-not-settled past SLA, per producer per channel. This
is the item that would have caught the push drop on day one.

**Quarter — make it cheap and historical.** Rollups (C) on
`producer × channel × hop`, then tail sampling (D) now that B makes it safe. Add
the is-it-me-or-the-platform detector. Migrate triage to tiered escalation with
budgets, and stand up the spend-by-detector report.

**Ongoing.** Golden-set tests run against recorded production evidence bundles, so
the honesty guarantees keep being verified after the data stops looking like this
export.

### 10.9 What changes in the code

Most of this is additive rather than a rewrite:

| Change | Effort |
|---|---|
| `loader.py` → `Source` protocol with `FileSource` and `BackendSource` | Small — the loader is already the only I/O boundary |
| Detectors declare `required_tier` and accept a scope | Small — they already take a `Dataset` and return `Finding`s |
| Join moves to streaming, chunked by `correlation_id`, or pushed server-side | Medium — `LogicalTrace` assembly is the part written for 273 spans |
| `Finding.affected` → `affected_count` + `exemplars` | Small |
| Thresholds → config with per-producer overrides and min-sample gates | Small — already parameterised |
| Ledger writer in `comms-ingest` and `comms-sender` | Medium, and **not in this repo** — a change to the services |

The design choice paying off here: because Parts 1 and 3 have no model in the loop
and all evidence is code-generated, swapping the data source does not touch the AI
layer at all. The triage engine consumes an `EvidenceBundle` and neither knows nor
cares whether it came from a file or a paid query.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Detector rules overfit to 41 messages | Thresholds are parameters, not literals; §5 states each rule in general terms; tests pin outputs rather than internals |
| Model ranks the deploy first on symptom 3 | Deploy rule-out computed in code and injected as a finding, not left to the model's timestamp arithmetic |
| Rich tables unreadable at narrow widths | Fixed 100-col layout, `--plain` for piping |
| Scope creep on the HTML report | Hand-rolled SVG, hard cap of four charts; the report is a view over existing computations, no new analysis |
