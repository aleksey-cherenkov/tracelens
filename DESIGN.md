# Design

A guide to reading the code. Findings are in [`README.md`](README.md); what I got
wrong on the way is in [`DECISIONS.md`](DECISIONS.md).

6,169 lines across 27 files. Start with §1 — it walks one message through the
whole thing, naming every module it touches. After that the sections answer
questions independently, so jump to whichever you have.

- [1. Follow one message](#1-follow-one-message)
- [2. How does it join records that don't connect?](#2-how-does-it-join-records-that-dont-connect)
- [3. How does it find something nobody predicted?](#3-how-does-it-find-something-nobody-predicted)
- [4. What does a finding actually contain?](#4-what-does-a-finding-actually-contain)
- [5. How do you stop the model inventing evidence?](#5-how-do-you-stop-the-model-inventing-evidence)
- [6. Which numbers are decisions, not facts?](#6-which-numbers-are-decisions-not-facts)
- [7. What does this cost in production?](#7-what-does-this-cost-in-production)
- [8. What do the tests actually check?](#8-what-do-the-tests-actually-check)
- [9. File reference](#9-file-reference)

---

## 1. Follow one message

`corr-0003` is an SMS. It's the useful one to trace because it breaks halfway
through — its trace ID changes at the sender, so it exercises the fallback path.

```
tracelens trace corr-0003
```

**`loader.load_dataset()`** reads five JSON files into one `Dataset`. That's the
only place the code touches disk — swapping to a real backend means changing this
file and nothing else.

**`model.classify_stage(span)`** maps each span onto a `Stage` enum
(`ACCEPT`, `PUBLISH_TOPIC`, … `SEND_PROVIDER`). It keys on `(service, kind, name
prefix)` rather than exact names, because the channel is baked into the name
(`publish sms-queue`). An unrecognised span returns `None` instead of raising.

**`join.build_trace()`** groups spans by `correlation_id` and walks the expected
stage order. At each step `_resolve_join()` asks how the two spans connect:

- downstream `parent_span_id` resolves upstream, same trace → `PARENT_CHILD`
- it doesn't → `CORRELATION_FALLBACK` (this is where `corr-0003` lands)
- nothing downstream exists → `ABSENT`, and the message stopped here

The result is a `LogicalTrace`. For `corr-0003` it has two `segments` (two trace
IDs), six `joins`, and `trace_context_break == True`.

**`accounting.account()`** left-joins the ledger against those traces. Driving it
*from* the ledger matters — a message that produced no telemetry at all still
shows up as lost, which is exactly what sampling would hide in production.

**`analysis.analyse()`** then runs three layers over the same data and merges the
findings. `corr-0003` shows up twice: once from `D4` (the SMS trace-break
detector) and once from `INV.context_break` (the generic invariant). That overlap
is reported as corroboration, not deduplicated.

**`cli.cmd_trace()`** renders it. The `!` marks the row where the trace ID
changes; `corr-0005` renders `STOPPED` instead, because it never reached the
provider.

---

## 2. How does it join records that don't connect?

**Join on `correlation_id`, not `trace_id`.** Grouping by trace loses 8 of 41
messages — every SMS starts a fresh trace at the sender — and can't represent a
message that just stops.

The interesting part is that *how* each hop resolved is recorded, not just
whether it did:

```python
class JoinMethod(str, Enum):
    PARENT_CHILD = "parent_child"           # 218 here
    CORRELATION_FALLBACK = "correlation_fallback"   # 8, all SMS
    ABSENT = "absent"                       # 4, all push
```

That's 230 stage transitions: 37 complete messages × 6, plus 4 push × 2 before
they stop. (Not to be confused with "spans that have a parent", which is 224 and
tells you nothing about how a hop resolved.)

`CORRELATION_FALLBACK` isn't a silent degradation — it sets `trace_context_break`
on the trace, which is what answers the CRM team's question about where the rest
of their SMS trace went.

**Two subtleties in `join.py` worth knowing before you read it.**

*Attempts are built backwards.* When a message is redelivered, both `consume`
spans share the *same* `publish` parent, so walking forward can't tell you which
consume belongs to which attempt. `_build_attempts()` starts from each
`SEND_PROVIDER` span and follows `parent_span_id` up:

```python
parent = by_id.get(send.parent_span_id)
consume = parent if parent and parent.stage is Stage.CONSUME_QUEUE else None
```

*Transitions are typed.* `ACCEPT` runs 48ms from `.000` and its child starts at
`.022` — *inside* it. So `next.start − previous.end` gives −26ms. `_resolve_join()`
marks that case `nested` and reports the child's offset within the parent instead.
Only the two async broker hops carry real queue latency.

---

## 3. How does it find something nobody predicted?

Every detector rule was written *after* I knew the answer. Point them at a new
failure and they find nothing — and "no findings" looks identical to "healthy",
which is F5 all over again.

So `analysis.analyse()` runs three layers:

| Layer | Knows this pipeline | Answers | On unfamiliar data |
|---|---|---|---|
| `detectors/` → `D*` | yes | *why* — mechanism and cause | skipped, with a stated reason |
| `invariants.py` → `INV.*` | no | *what* broke | works |
| `novelty.py` → `NOV.*` | no | *what changed* | works |

### Detectors — `tracelens/detectors/`

One module each, all with the same shape: `detect(context) -> list[Finding]`.

| File | Fires on | Here |
|---|---|---|
| `drop.py` | accepted vs `terminal_stage`, per channel | push 4/4, critical |
| `duplicate.py` | more than one `SEND_PROVIDER` per correlation | 3, all redelivery |
| `provider.py` | non-2xx or slow sends, grouped into windows, then deploy correlation | 6 messages, `c52a0f9` ruled out |
| `tracing.py` | more than one trace ID per message | 8/8 SMS, 0/29 email |
| `blindspot.py` | status-vs-reality gap, noise ratio, dead gauges | 0/273 vs 4/41 |

`provider.py` is the longest (365 lines) and the one worth reading closely — it
contains `correlate_deploys()`, which takes a **service** argument derived from
where the evidence localises the fault. A proximity-only correlator would blame
the orchestrator deploy for the duplicates; the span topology disproves it.

### Invariants — `invariants.py`

Six properties that must hold of *any* message pipeline. Nothing here names a
channel, a service, or a stage count.

| Function | Property |
|---|---|
| `_conservation` | what enters a hop must leave it |
| `_path_shapes` | messages follow one of a few routes |
| `_settlement` | every ledger promise reaches a terminal node |
| `_context_integrity` | a trace must not fragment mid-journey |
| `_single_visit` | a message traverses each node once |
| `_referential` | references resolve, records join |

A violation is novel by construction — it needs no rule and no prior example.

Three details make this work rather than just sound good:

*Severity is derived, not assigned.* `_severity(affected, total)` returns
critical/high/medium/low from blast radius, because on unfamiliar data there's no
prior about what matters.

*`_discriminator()` names the lost class.* Given the lost set and the surviving
set, it finds an attribute value present in all of the former and none of the
latter. That produces `message_type=push` here without the code knowing channels
exist. "Whole class or scattering?" is the first question anyone asks about silent
loss.

*Optional branches aren't losses.* A retry stage taken by 15% of messages must not
make the other 85% look dropped. So `Topology.expected_successors()` counts an
edge only if ≥ `expected_edge_share` (0.5) of messages at that node take it. The
threshold can't sit near 1.0 — a real drop drags the edge's own share down, so it
has to be below the loss it's meant to detect.

### Topology — `topology.py`

`discover()` builds the graph from spans in one pass. The trick that makes it
channel-independent is `templatize()`:

```python
def templatize(span: Span) -> str:
    """'publish email-queue' + message_type=email -> 'publish {message_type}-queue'"""
    name = span.name
    for key in ("message_type", "tenant_id", "provider"):
        value = span.attributes.get(key)
        if isinstance(value, str) and value and value in name:
            name = name.replace(value, "{" + key + "}")
    return name
```

Three channels collapse onto one node, and a fourth added tomorrow lands there
too. Without this, every new tenant would look like a new pipeline.

### Novelty — `novelty.py`

`profile()` fingerprints the pipeline — services, nodes, edges, route shapes,
statuses, attribute keys, log templates, channels. `diff_profiles()` compares it
against a saved baseline.

Shapes and cardinalities only, never counts. A fingerprint that moved with traffic
would flag every busy Monday.

Both directions matter. Something new is the usual suspect during an incident.
Something that *stopped* appearing is what a silently disabled code path looks
like, produces no error, and is much harder to spot — so a vanished node is
escalated to critical while a new log template is low.

### The gate that stops detectors lying

`analysis.stage_coverage()` measures how many spans `classify_stage()` recognises.
Under 60% and the detector layer doesn't run at all — it emits
`ERR.taxonomy_mismatch` instead.

Without that gate, on a foreign pipeline no span maps to a known stage, every
message looks undelivered, and `D1` confidently reports that *every* channel is
being dropped. Confidently wrong is worse than silent.

Layers also run independently: one that raises becomes an `ERR.*` finding rather
than silence.

---

## 4. What does a finding actually contain?

Everything the AI layer is allowed to say comes from here. `evidence.py`:

```python
@dataclass(frozen=True)
class Evidence:
    kind: EvidenceKind   # correlation_id | trace_id | span | log | deploy | metric
    ref: str             # the citable identifier
    detail: str          # human-readable, pre-rendered
    source: str          # e.g. "spans.json#span_id=00000000000a744a"

@dataclass
class Finding:
    id: str              # "D1.channel_drop.push"
    title: str
    severity: Severity   # critical | high | medium | low
    confidence: Confidence   # observed | inferred | ambiguous
    summary: str
    evidence: list[Evidence]
    affected: list[str]        # correlation_ids
    alternatives: list[Hypothesis]   # competing explanations code must not collapse
    would_resolve: list[str]         # what data would settle it
    params: dict[str, object]        # thresholds this finding depended on
```

`alternatives` and `would_resolve` are the two fields that carry the March 9
ambiguity all the way to the output. `params` is why every threshold prints its
own value next to the finding it produced.

`detail` being pre-rendered is deliberate: the model never formats a number.

`CitationIndex` collects every `ref` from every finding. It's the thing that makes
the validator a real gate rather than a request.

---

## 5. How do you stop the model inventing evidence?

### The split

| Code owns | Model owns |
|---|---|
| Every join, count, duration, rate, window | Mapping a vague complaint onto detectors |
| Which IDs are affected | Ordering hypotheses by fit to what was asked |
| Deploy comparison and rule-out logic | Explaining the causal story |
| Severity, confidence, alternatives | Judging when nothing matches |

One boundary is easy to get backwards: code owns the severity and confidence
*labels*, the model owns the *order*. An early draft re-sorted by severity, which
answered "email was slow on March 9th" with the push outage.

### The path — `tracelens/triage/`

```
complaint
  ├─▶ context.build_bundle()   run all layers -> EvidenceBundle + CitationIndex
  ├─▶ engine.triage()          API call, 5 tools, max 8 iterations
  ├─▶ validator.validate()     drop bad citations, merge duplicate finding_ids,
  │                            attach code-owned confidence
  └─▶ TriageResult, or insufficient_evidence
```

**`tools.py`** exposes five read-only drill-downs: `list_findings`,
`get_finding_evidence`, `get_trace`, `query_messages`, `get_deploys`. Every
response is capped and marked `truncated: true` with `N more not shown`. There's
no `run_query`, no write path, no bulk span dump — so the model can't author an
expensive scan, and every conclusion traces back to a named function you can re-run
by hand.

**`validator.py`** is the hard gate. A hypothesis citing a ref that isn't in the
index is dropped entirely, and the rejection is logged. Confidence and severity are
overwritten from the cited `Finding`, so the model claiming certainty changes
nothing.

**Insufficient evidence is a first-class result.** If nothing intersects the
complaint, the tool returns what it checked and what would be needed. A triage
tool that always produces an answer trains people to ignore it.

### Context size

**O(findings), not O(telemetry).** A 10× traffic increase must not change the
prompt. Measured: findings payload ~4.6K tokens, whole request ~6.1K, against
~160K for raw spans and logs. `test_live_path.py` asserts this by growing the
message count 5× and checking the payload barely moves.

### Model choice

`claude-sonnet-5`, `effort: medium`, ~$0.05 a run.

Not Opus — by the time the model is called every number and rule-out is already
computed, so what's left is semantic matching and explanation. Paying 2.5× per
token for a stronger reasoner to compensate for a weak evidence layer is the wrong
trade.

Not Haiku — holding onto a genuine ambiguity under pressure to sound decisive is
where a mid-tier model earns its cost.

Determinism comes from shrinking what the model may decide, then measuring whether
the answer moves across repeated runs.

---

## 6. Which numbers are decisions, not facts?

All of them live in `config.py`, and each prints its value in the finding it
produced. Going live is "re-tune these nine", not "find the constants".

| Parameter | Default | What it governs |
|---|---|---|
| `min_samples` | 20 | minimum n before a **rate** is reported |
| `expected_edge_share` | 0.5 | when an edge counts as the normal route vs a branch |
| `min_stage_coverage` | 0.6 | when the detector layer is trusted at all |
| `slow_factor` | 3.0 | how much slower than baseline counts as affected |
| `incident_max_gap_s` | 24h | when two slow sends are one incident or two |
| `correlation_join_window_s` | 60 | how far ahead to look when the trace breaks |
| `noise_ratio_alert` | 0.5 | unjoinable log share worth flagging |
| `max_exemplars` | 5 | rendered examples per finding |
| `deploy_lookback_s` | 24h | how far before onset a deploy is a candidate |

Two of these have non-obvious reasoning:

**`min_samples` gates rates, never counts.** `D1` fires on push at n=4 because
"these 4 named messages stopped" is a claim about *those messages*, not about a
population. Reading the gate the other way deletes the most severe finding in the
dataset — `test_d1_fires_below_min_samples` sets it to 1000 and asserts push still
surfaces.

**`incident_max_gap_s` has to clear the overnight gap.** The March 9 incident
spans 15h08m with no email sends in between. Anything under that splits one
incident into two and changes the deploy arithmetic.

Other definitions worth knowing:

- **End-to-end latency** is `ACCEPT.start → SEND_PROVIDER.end` of the *first attempt only*. Measuring to the last span gives 31,988ms for the three duplicates and invents a latency incident.
- **Percentiles** are computed only where distinct values exist. Every async hop has zero variance, so `variance: none` prints instead of a fake p99.
- **Error rate is three numbers, never collapsed:** `span_status_errors` 0/273, `provider_errors` 6/40, `delivery_failures` 4/41. The gap between the first and last is the headline.
- **Retries and redeliveries are different phenomena** — 18 and 3 — and are never summed.

---

## 7. What does this cost in production?

The absence-sampling argument and the log storage tiers are in
[README § Going live](README.md#going-live). This section is what that doesn't
cover.

### Data access — five options

| Option | Verdict |
|---|---|
| **A** on-demand queries | needed for drill-down, insufficient alone — accounting becomes a full scan |
| **B** delivery ledger | **build this.** The only option that detects an absence |
| **C** rollups | second. Fixed cost, and the only way baselines outlive 30-day retention |
| **D** tail sampling | good for cost, **only after B** — adopting it first makes F1 undetectable |
| **E** warehouse mirror | reject. You've rebuilt the observability backend; C gets most of it |

Tiered: ledger (exact, 13 months, negligible) → rollups (fixed) → sampled traces
(bounded) → raw queries (metered). Detectors declare which tier they need, and the
rule is **detect cheap, confirm expensive**.

### What it costs — `python scripts/cost_model.py`

Per-message bytes are measured from `data/`. Prices are assumptions, declared at
the top of the script so they can be replaced with real vendor rates and re-run.

| Per message | Bytes | |
|---|---|---|
| spans | 2,624 | |
| logs that join to a message | 742 | the ones worth keeping hot |
| **logs that join to nothing** | **10,866** | **76% of all telemetry** |
| delivery ledger, 2 rows | 318 | **2.2%** of telemetry |

The ledger — the thing that actually answers *did we deliver it* — costs 2.2% of
what the telemetry costs. That single ratio is the whole argument for Option B.

At 2M messages/day, with $2.50/GB-month indexed and $0.023/GB-month columnar:

| | Monthly |
|---|---|
| everything indexed 30d (upper bound) | $1,988 |
| "how many did we deliver", hourly dashboard, full scan | $2,796 |
| after: hot 7d scoped logs only | $24 |
| after: warm 30d spans + scoped logs, columnar | $4 |
| after: cold 13mo ledger + rollups | $5 |
| same dashboard, from the ledger | ~$0 |

Every ratio is volume-independent; only the dollars move. The framing that
matters: **the noise is 76% of the volume and removing it is an afternoon**, and
**accounting stops being a scan** — that's a decision someone can approve, unlike
"improve observability."

Remaining levers, in order of return: rollups, then tail sampling once the ledger
proves itself; detect on aggregates and confirm on ≤5 exemplars; lead every query
with an indexed dimension rather than a regex.

For LLM tokens: O(findings) context, prompt-cache the static preamble, Haiku for
routing, batch the offline work, hard-cap the tool loop.

Guardrails: Tier-3 commands print estimated scan volume first; a per-session budget
returns "budget exhausted — here are the questions I didn't get to"; and a fired
finding snapshots its raw evidence, so a postmortem six weeks later still has it
after retention has passed.

### Defending the bill

Instrument the tool's own spend by detector — you can't defend a budget you can't
itemise, and the first such report usually names one detector as most of it.

Quote cost *per incident* against a status quo of most of an engineer-day. One
engineer-day exceeds a generous monthly query budget; that ratio is the sentence
for the Slack message.

Lead with what it detects: 9.8% of messages silently undelivered, for ten days,
while every dashboard read healthy. Then sequence the noise reduction first so the
saving pays for the ledger — which turns "approve new spend" into "reallocate
existing waste," a much easier meeting.

### Five teams on one pipeline

Two filtering problems needing opposite treatment. Unrelated services in a shared
backend → **filter hard** at the query predicate. Other teams' messages *inside*
this pipeline → **partition, don't discard**; they're the same signal from a
different source, and throwing them away destroys the comparison that answers "is
it us or the platform?"

All of it is blocked on the missing `producer.service` attribute. Once it exists:
elevated for one producer → their config; elevated across all → shared
infrastructure. Also needed then: per-producer quotas, PII redaction in the log
viewer (recipient addresses appear in logs), and escalation to platform on-call
when a finding spans three or more producers.

### Sequencing

Week 1, point it at the backend with tight scope and ship the log-emission fixes,
so the saving lands first. Weeks 2–3, add `producer.service` — before rollups,
because you can't backfill a dimension. Month 1, the delivery ledger and its
reconciliation job. Quarter, rollups then tail sampling then the
is-it-us-or-them detector.

### What changes in the code

`loader.py` becomes a `Source` protocol — it's already the only I/O boundary.
Detectors declare `required_tier`. The join moves to streaming, chunked by
`correlation_id`. `Finding.affected` becomes `affected_count` plus exemplars.
Thresholds gain per-producer overrides. The ledger writer is a change to two
services, not to this repo.

Because the analysis has no model in it and all evidence is code-generated,
swapping the data source doesn't touch the AI layer at all.

---

## 8. What do the tests actually check?

112 tests, ~2s. The two worth reading first are `test_unfamiliar_dataset` and
`test_live_path` — they're the ones that check the properties I'd otherwise only
be asserting.

| Suite | Checks |
|---|---|
| `test_loader` | no orphans in either direction, `accepted_at` == `ACCEPT.start` ×41, no dangling parents |
| `test_join` | the 218/8/4 census; nested-vs-sequential typing; attempts split backwards from the send; e2e excludes redeliveries |
| `test_accounting` | 34/3/4 funnel, 40 provider calls, per-channel breakdown |
| `test_health` | three error rates stay separate; no percentiles on zero-variance hops |
| `test_detectors` | pinned correlation IDs; D1 fires below `min_samples`; D3's overnight gap stays one incident |
| `test_deploy_correlation` | `c52a0f9` ruled out with its reason; the orchestrator deploy isn't blamed for the duplicates |
| `test_triage_validator` | fabricated citation dropped; duplicate `finding_id` merged; adversarial complaint → insufficient |
| `test_live_path` | the real model loop against a fake SDK — all 5 tools execute, asserted confidence is overridden, the 8-iteration cap raises, the prompt carries findings not raw telemetry |
| `test_unfamiliar_dataset` | a **5-stage pipeline this code has never seen**: mid-pipeline drop found and its class named, optional branch not mistaken for a loss, detectors skipped rather than fabricating |
| `test_keys` | resolution order, masking, `.env` round-trip, no key in any committed transcript |

`scripts/verify_claims.py` is separate from the test suite: it recomputes all 101
figures quoted in the docs from `data/` and exits non-zero if any drifted.

---

## 9. File reference

| File | Lines | Contents |
|---|---|---|
| `model.py` | 330 | `Span`, `LogRecord`, `Deploy`, `AcceptedMessage`, `Dataset`; `Stage` enum; `classify_stage()` |
| `loader.py` | 52 | `load_dataset()` — the only disk I/O |
| `join.py` | 347 | `LogicalTrace`, `JoinMethod`, `Attempt`; `build_trace()`, `build_all()` |
| `accounting.py` | 161 | `Outcome`, `Accounting`; `account()` |
| `health.py` | 252 | `LatencySummary`, `ErrorRates`, `HopHealth`; `compute()` |
| `topology.py` | 235 | `Topology`, `templatize()`, `discover()`, `profile()`, `diff_profiles()` |
| `invariants.py` | 572 | six `_check` functions; `check_all()` |
| `novelty.py` | 179 | `save_baseline()`, `check()`, `compare_datasets()` |
| `analysis.py` | 213 | `analyse()`, `stage_coverage()`, `Analysis.corroborated()` |
| `evidence.py` | 188 | `Evidence`, `Finding`, `Hypothesis`, `CitationIndex`, `EvidenceBundle` |
| `detectors/` | 1,190 | D1–D5 plus the registry |
| `triage/` | 1,004 | `context`, `tools`, `prompts`, `engine`, `validator`, `stub` |
| `config.py` | 70 | every tunable threshold |
| `keys.py` | 153 | API key resolution and masking |
| `cli.py` | 932 | 10 commands |
| `report.py` | 291 | single-file HTML, inline SVG, no CDN |
| `scripts/verify_claims.py` | 142 | recomputes all 101 quoted figures from `data/` |
| `scripts/cost_model.py` | 186 | per-message footprint measured, prices assumed |
