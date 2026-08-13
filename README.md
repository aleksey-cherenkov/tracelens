# Tracelens

Message pipeline trace analyzer — Bloomerang Platform Engineering take-home.

Three services chained over a topic into per-channel queues:

```
product ──▶ comms-ingest ──topic──▶ comms-orchestrator ──queue──▶ comms-sender ──▶ provider
```

`accepted_messages.json` is the promise ledger: 41 messages the platform returned
`202` for over 10 days (2026-03-02 → 03-11). This tool asks what happened to each
of them.

**Headline: the platform broke its `202` promise for 9.8% of messages and 100% of
one channel, and every span in the export reports `status: OK`.**

- [Setup](#setup) · [Commands](#commands)
- [What I found](#what-i-found) — F1–F7
- [What I'd fix first](#what-id-fix-first)
- [AI: in and out](#ai-in-and-out)
- [Going live](#going-live)
- [Unknown problems](#troubleshooting-problems-nobody-has-seen-yet) — the layers that work on data this code has never seen
- [What I cut](#what-i-cut)

Build decisions, join strategy, and the production data-access design are in
[`DESIGN.md`](DESIGN.md).

---

## Setup

```bash
pip install -e .            # Python 3.10+, one dependency (rich)
tracelens account           # Parts 1 and 3 need no API key at all
pytest                      # 112 tests, ~2s
```

Parts 1 and 3 — every finding, number, and command below — are pure deterministic
code with no model in the loop. Only Part 2 (triage) calls the API: **Claude
Sonnet 5 at `effort: medium`**, roughly $0.05–0.10 per run. Not Opus, because by
the time the model is called every number and rule-out is already computed and
what remains is semantic matching and explanation — see
[DESIGN §6.4](DESIGN.md#64-model-configuration) for the full argument, including
why `temperature: 0` is deliberately *not* set.

### API key

`tracelens keys` shows exactly what will happen before you spend anything:

```
$ tracelens keys
╭───────────────────────── triage credentials ──────────────────────────╮
│  API key                 not set                                      │
│  resolved from           none                                         │
│  anthropic SDK           not installed — run: pip install -e ".[ai]"  │
│  triage will use         offline stub                                 │
╰───────────────────────────────────────────────────────────────────────╯
```

Add or clear it in one command each. The key goes to a gitignored `.env` at the
repo root, so both actions are a single visible change to a single file rather
than a shell setting whose effect depends on which terminal you're in:

```bash
tracelens keys --set sk-ant-...     # write it
tracelens keys --clear              # remove it
tracelens keys --shells             # per-shell env commands (PowerShell, cmd, bash)
pip install -e ".[ai]"              # install the SDK
```

**Resolution order:** `--api-key` flag → `ANTHROPIC_API_KEY` in the environment →
`.env`. If the environment has a key, `--clear` says so rather than pretending it
worked. `--api-key sk-ant-...` on a single `triage` call overrides everything and
is never written to disk.

Keys are masked everywhere they appear (`sk-ant-…abcd (73 chars)`), `.env` is
gitignored, and a test asserts no committed transcript contains `sk-ant-`.

`examples/symptom-3.json` is a **live** `claude-sonnet-5` run; the rest are stub
runs, and every file says which in its own `note` field. If no key resolves,
triage falls back to the offline stub and labels its output `source: stub`. The stub is a deterministic keyword router that exercises the real
context assembly, tool surface, validator gate, and rendering — but anything it
gets right, it gets right because the detectors already did the work. That is the
point of the code/model split; it is not evidence of what the model would say.

### Recording transcripts

```bash
tracelens triage --symptom 3 --record     # writes examples/symptom-3.json
```

Each recorded file states in its own `note` field whether it came from a live
model call or the stub, so a reviewer can never mistake one for the other.
Transcripts capture the response, the validated result, tool-call count, and which
*source* the key resolved from — never the key.

## Commands

```
tracelens trace corr-0003          # waterfall + per-hop timing + join method per hop
tracelens trace corr-0005          # truncated path, terminal stage highlighted
tracelens account [--by channel|tenant|day]
tracelens health [--service X] [--hop Y]
tracelens findings [--severity critical] [--layer detector|invariant|novelty] [--quiet]
tracelens topology [--json]        # the pipeline graph, learned from the data
tracelens baseline [--save] [--against DIR]
tracelens logs [--corr ID] [--service X] [--grep P] [--show-suppressed] [--no-filter]
tracelens triage "we got the same email twice"
tracelens triage --symptom 3       # replay a complaint from symptoms.json
tracelens triage --symptom 3 --record --api-key sk-ant-...
tracelens triage --symptom 3 --effort high        # low|medium|high|xhigh|max
tracelens keys [--set KEY] [--clear] [--shells]
tracelens report --out report.html
```

`--plain` on any command drops colour for piping.

### The two views that make the worst findings obvious

```
$ tracelens trace corr-0003
  stage                 service      start            dur        trace    hop to next
  accept request        ingest       13:26:00.000    48ms ██     007edb4b    22ms  nested in parent
  publish to topic      ingest       13:26:00.022    19ms █      007edb4b   269ms  parent/child
  consume from topic    orchestrator 13:26:00.310    26ms █      007edb4b     8ms  nested in parent
  route to channel      orchestrator 13:26:00.318    14ms █      007edb4b     2ms  parent/child
  publish to channel q… orchestrator 13:26:00.334    17ms █      007edb4b   379ms  corr fallback
! consume from channel… sender       13:26:00.730    21ms █      00807464     4ms  parent/child
  call provider         sender       13:26:00.755   180ms ██████ 00807464
! trace context breaks: 2 distinct trace IDs. Joined on correlation_id instead.

$ tracelens trace corr-0005
  accept request        ingest       17:52:00.000    48ms ██████ 0083a696    22ms  nested in parent
  publish to topic      ingest       17:52:00.022    19ms ██     0083a696 STOPPED — nothing next
X never reached a provider — stopped after publish to topic in comms-ingest
```

The `!` marks where the trace ID changes (F4) and `STOPPED` marks where a message
died (F1). No prose required.

Every number below is reproducible from one of these commands; the command is
named next to each finding. `python scripts/verify_claims.py` recomputes all 101
of them from `data/` and exits non-zero on any mismatch — so the write-up cannot
silently drift from the data.

---

## What I found

Confidence is stated per finding. **Observed** = directly in the data.
**Inferred** = the most likely mechanism, not itself evidenced. **Ambiguous** =
the data supports more than one explanation and cannot separate them.

Integrity checks pass — no orphan spans in either direction, no duplicate ledger
IDs, `accepted_at` matches every `ACCEPT` span start, no dangling
`parent_span_id`, no log `trace_id` contradicting the spans. So the gaps below are
real signal, not export artifacts.

### F1 — Push is 100% lost between the topic and the orchestrator

**Critical · observed** · `tracelens findings --severity critical`

| Evidence | Value |
|---|---|
| Push accepted | 4 — `corr-0005`, `corr-0010`, `corr-0020`, `corr-0036` |
| Push spans in `comms-ingest` | 8 — accept + publish, both present for all 4 |
| Push spans in orchestrator or sender | **0** |
| `publish comms-topic` / `consume comms-queue` | 41 / **37** — the deficit is exactly the 4 push |
| `route push` / `publish push-queue` spans | **0** |
| Ingest logs `Published to topic type=push` | 4 |
| Orchestrator logs `Routing message type=push` | **0** |

Ingest publishes. The orchestrator never receives. The loss is on the topic→queue
subscription, before any orchestrator code runs.

**The reported framing is wrong, and the real problem is bigger.** Payments called
this "our donation campaign last week." The 4 messages span **three tenants**
(`org-4471` ×2, `org-1042`, `org-6614`) and the **entire window** (03-02, 03-03,
03-05, 03-10). Every push message from every tenant for the whole retention
period was lost. This is chronic and channel-wide, not one campaign.

**The provider is not the suspect.** SMS and push both go to AWS Pinpoint, and all
8 SMS messages delivered fine. Whatever drops push sits between the topic and the
orchestrator queue.

*Inferred:* an SNS subscription filter policy allowing `email` and `sms` but not
`push`. The data proves the loss location; it does not prove the mechanism.
Checking the filter policy is a console lookup, not a project.

### F2 — Duplicate email is queue redelivery, not double-publish

**High · observed (topology), inferred (cause)** · `tracelens trace corr-0014`

3 of 29 emails reached the provider twice: `corr-0014` (03-04), `corr-0022`
(03-06), `corr-0035` (03-10). Identical topology on all three:

| Stage | Spans |
|---|---|
| `publish email-queue` (orchestrator) | **1** |
| `consume email-queue` (sender) | **2** — same `parent_span_id` |
| `send email` (sender) | **2** |

One publish, two consumes. **The orchestrator did not double-publish.** The second
consume fires exactly **31.0 s** after the first, its send carries
`sqs.receive_count: 2`, and the first send had already returned
`provider.status_code: 202` in 240 ms.

So: send succeeded, message not deleted from the queue, ~30 s visibility timeout
expired, sender reprocessed with no idempotency check. Whether the delete failed
or was never issued isn't determinable here — the fix is the same either way.

**The redelivery was logged and nobody found it.** Each of the three emits
`Received message from queue` with `sqs.receive_count: 2` at `comms-sender`. Three
lines in 2,820, at `INFO`, with no word suggesting duplication. That is F7's cost
made concrete.

*Weak signal, stated as weak:* 2 of 3 belong to `org-5502`, which has 5 emails
total. n=3 — not significant, and the tool does not report it as tenant-specific.

### F3 — March 9 was provider throttling; the deploy blame is false, the recovery is genuinely ambiguous

**Medium · mixed** · `tracelens triage --symptom 3`

This is the one that isn't what it first appears.

Baseline `send email` is 233–240 ms (modal 235). During the incident every
affected send is exactly 4,120 ms with `provider.status_code: 429`,
`retry_count: 3`, `provider.final_status_code: 202` — corroborated by six
`WARN Provider returned 429, backing off (attempt 1 of 3)` log lines.

```
2026-03-09 09:00  corr-0026  4120 ms  429   ◀── onset
2026-03-09 10:05  corr-0031  4120 ms  429
2026-03-09 11:13  corr-0027  4120 ms  429
2026-03-09 14:00  ░ DEPLOY c52a0f9  comms-sender  PR #99  "Add SMS provider seam"
2026-03-09 15:39  corr-0029  4120 ms  429
2026-03-09 17:52  corr-0030  4120 ms  429
2026-03-10 09:00  corr-0032  4120 ms  429   ◀── last slow send
2026-03-10 10:00  ░ DEPLOY e18d773  comms-sender  PR #101 "Bump provider SDK"
2026-03-10 11:13  corr-0033   235 ms  202   ◀── recovered
```

**Falsified:** on-call blamed "the sender deploy that day." `c52a0f9` shipped at
14:00 — **five hours and three affected messages after onset**. It cannot have
caused it. That attribution is dead.

**Two further facts point away from anything sender-side:** all **5 of 5** emails
sent on 03-09 were throttled — not a subset — and the six affected messages span
**five of the six tenants in the dataset**. A regression selective by neither
tenant nor message is not a plausible reading.

**Not resolved:** `e18d773` "Bump provider SDK" lands at 03-10 10:00, **inside**
the recovery window (last bad send 09:00, first clean send 11:13). Two hypotheses
survive and this data cannot separate them:

- **H1** — SendGrid-side rate limiting for ~24 h that ended on its own; the SDK bump is coincidence.
- **H2** — the old SDK mishandled concurrency or client-side rate limiting, SendGrid throttled in response, and PR #101 fixed it.

The tool returns both, ranked, plus what would settle it: the `e18d773` diff. If
PR #101 changed connection pooling, concurrency, or retry config, H2 gains weight;
if it was a version bump with no behaviour change, H1 does.

**Nothing was lost.** All six reached the provider with `final_status_code: 202`.
Pure latency: end-to-end 990 ms → 4,875 ms, 4.9×.

### F4 — SMS loses trace context at the channel queue, and the second half is unreachable

**Medium · observed** · `tracelens trace corr-0003`

All 8 of 8 SMS messages split into two trace IDs:

```
trace 007edb4b   comms-ingest        POST /api/v1/messages     parent=null
trace 007edb4b   comms-ingest        publish comms-topic
trace 007edb4b   comms-orchestrator  consume comms-queue
trace 007edb4b   comms-orchestrator  route sms
trace 007edb4b   comms-orchestrator  publish sms-queue
trace 00807464   comms-sender        consume sms-queue         parent=null   ◀── new root
trace 00807464   comms-sender        send sms
```

The sender's SMS consumer starts a new root span instead of continuing the
producer's context. Email does this correctly (0 of 29 break) — which localises
the bug precisely to the SMS consumer.

**Worse than it looks:** the SMS sender emits **zero** log records. Scoped logs
for SMS exist only in `comms-ingest` (8) and `comms-orchestrator` (8). The 8
orphaned sender trace IDs appear in **no log line anywhere**. For SMS there is no
trace-based *and* no log-based route to the second half of the journey.
`correlation_id` on the spans is the only surviving bridge — which is why the
analyzer joins on correlation first (DESIGN §2).

### F5 — Errors and retries are structurally invisible

**High · observed** — *this is why F1–F3 went unnoticed* · `tracelens health`

| Signal | Value |
|---|---|
| Spans with `status != "OK"` | **0 of 273** |
| Log records at `ERROR` | **0 of 2,820** |
| Provider 429 responses | 6 — visible only in span attributes and `WARN` logs |
| Retries | 18 (6 spans × `retry_count: 3`) — no child spans, no error status |

Every span reports `OK`, including the six that took 4.1 s retrying against a
throttling provider and the three that sent a duplicate. **Any alert, SLO, or
dashboard built on span status or log level shows this pipeline as perfectly
healthy through every incident in this window.** A dropped channel produces no
error at all — just an absence of spans, and absence pages nobody.

### F6 — The one metric that would have caught F1 is hardcoded noise

**High · observed** · `tracelens logs --grep "queue depth"`

`queue depth metric recorded depth=N` appears 1,200 times, 400 per service. Every
value is `depth=0`, every record has `attributes: {}` — **no queue label** — and
`trace_id: null`. There is no push-queue emitter at all.

A working per-queue depth gauge would have shown a push backlog, or the absence of
a push queue, on day one. Instead it costs storage, contributes 42.6% of log
volume, and reports nothing.

### F7 — 95.7% of log volume is unsearchable

**Medium, high as a force multiplier · observed** · `tracelens logs`

| Category | Records | Share | Joinable? |
|---|---|---|---|
| `GET /health 200` | 1,200 | 42.6% | no |
| `queue depth metric` | 1,200 | 42.6% | no |
| `Polling queue: received N messages` | 300 | 10.6% | no |
| **Message-scoped** | **120** | **4.3%** | yes |

2,700 of 2,820 records carry `trace_id: null` and join to nothing. On-call's
complaint is precise, not hyperbole: the useful signal for a 10-day, 41-message
window is 120 lines. F2's redelivery evidence is 3 of those 120.

---

## Delivery accounting

`tracelens account`

| Outcome | Count | Share |
|---|---|---|
| Reached provider exactly once | 34 | 82.9% |
| Reached provider more than once | 3 | 7.3% |
| **Never reached provider** | **4** | **9.8%** |
| Provider calls issued | 40 | — |

| Channel | Accepted | Delivered | Lost | Duplicated | Trace intact |
|---|---|---|---|---|---|
| email | 29 | 29 | 0 | 3 | 29/29 |
| sms | 8 | 8 | 0 | 0 | **0/8** |
| push | 4 | **0** | **4** | 0 | n/a |

---

## What I'd fix first

Ordered by supporter impact per hour of work, not by severity label.

**1. Check the SNS subscription filter policy on `comms-topic` (F1).** Minutes,
in a console. It is the only finding where messages are being lost outright, it
affects an entire channel across every tenant, and if the filter is the cause the
fix is one policy edit. Do this before anything else because it is the highest
impact and the lowest cost in the list — and because it may already be recoverable
if the messages landed in a DLQ.

**2. Delete the message from the queue after a successful send, and add an
idempotency key at the sender (F2).** Duplicate donation receipts and volunteer
confirmations are a donor-trust problem, not an engineering-embarrassment problem.
Small, local change in one service.

**3. Emit real error signal (F5).** Set span status on non-2xx provider responses,
log `ERROR` on exhausted retries, and emit a child span per retry attempt. Until
this lands, every fix above is unverifiable — you cannot confirm a repair in a
system where success and failure look identical. This is what makes 1 and 2
*stick*, which is why it ranks above the cheaper log cleanup.

**4. Stop emitting the noise (F6, F7).** Drop successful health-probe logs, make
queue depth a real gauge with a `queue` label, move poll chatter to sampled
`DEBUG`. ~95.7% log volume reduction for roughly an afternoon of emission changes,
and it is a hard line-item saving that helps bankroll items 5 and 6.

**5. Fix SMS trace propagation (F4).** Extract the trace context from the SQS
message attributes in the SMS consumer, the way the email consumer already does.
Real, but it costs investigation time rather than delivering wrong outcomes to
supporters — and the correlation-first analyzer routes around it today.

**6. Build the delivery ledger (see [Going live](#going-live)).** The largest item
and the one that stops this recurring. Sequenced last because 1–4 are cheap and
immediate, and because item 4's savings make the case for it.

**Deliberately not first: the March 9 throttling (F3).** It self-resolved, lost
nothing, and the actionable follow-up is reading one PR diff. Ranking a resolved
latency blip above an ongoing 100% channel loss is exactly the misprioritisation
this pipeline's telemetry encourages.

---

## AI in and out

**Code owns every number.** All joins, counts, durations, rates, window
boundaries, affected ID sets, deploy timestamp comparisons, severity, and
confidence are computed deterministically. Parts 1 and 3 have no model in the loop
at all — every figure in this document is reproducible from a CLI command.

**The model owns language.** Mapping a vague complaint onto detectors, ranking
findings against what the person actually asked, and explaining the causal story.
"A couple of our supporters got the same confirmation email twice" → the duplicate
detector is a *semantic* match, not a keyword match, and code here would be a pile
of brittle regexes.

The model's job is selection, ranking, and explanation over a fixed evidence set.
It is never asked to compute, count, or recall an identifier.

**Stopping unsupported assertions** — four mechanisms, strongest first:

1. **Evidence-first construction.** Findings and citations are generated by code before the model is called. It selects from a fixed set and has no unreferenced IDs in its inputs to hallucinate from.
2. **Citation validator, a hard gate.** Every hypothesis must cite ≥1 evidence ref; every ref is checked against a citation index; any hypothesis with an unresolvable ref is **dropped from the output**. A code gate, not a prompt instruction.
3. **Code-owned confidence.** The response schema has no free-text confidence field. Confidence and severity are inherited from the cited finding, so the model cannot promote a guess by asserting certainty. It *does* own the order of hypotheses — ranking by fit to what was actually asked is the fuzzy work code does badly, and forcing a severity sort would answer every complaint with the push outage.
4. **Mandatory alternatives.** The schema requires ≥2 hypotheses or an explicit `insufficient_evidence` verdict, and forbids collapsing a finding that carries competing alternatives — this is what preserves F3's H1/H2 through to the output.

**What would have to be true for me to trust it during an incident** — it has to
be wrong *loudly* and in a way I can check in seconds:

- **Golden set.** The 5 symptoms have known answers (F1, F2, F3, F4, F7). Each is a test pinning the top hypothesis's finding ID and required citations.
- **Stability.** Each golden case runs 5×; the top-ranked finding ID must be identical every time. Note that this cannot be bought with `temperature: 0` — Sonnet 5 rejects any non-default sampling parameter with a 400. Determinism instead comes from shrinking what the model is allowed to decide (fixed evidence set, code-owned confidence, hard citation gate) and then *measuring* the variance. An analyzer that answers differently each run is worse than no analyzer at 3am, so instability is a reported defect rather than an accepted cost.
- **The adversarial case.** "Our webhooks stopped firing" — absent from the data — must return `insufficient_evidence`. This is the test that catches a model pattern-matching to the nearest finding.
- **The trap case.** Symptom 3 must rank throttling above the deploy, cite the 5-hour gap, *and* surface the recovery ambiguity. A run that confidently blames `c52a0f9` fails — and so does one that confidently credits `e18d773`.
- **Validator telemetry.** Rejected-citation count is logged per run. A non-zero rate is early warning of drift toward fabrication.

The load-bearing property: because the analysis has no model in it, the triage
layer can be wrong without corrupting a single number in this write-up.

---

## Going live

Full treatment in [`DESIGN.md`](DESIGN.md#10-going-live). The three things that
matter:

**You cannot sample an absence.** F1 — the most severe finding — has zero error
spans, zero `ERROR` logs, and no non-`OK` status. Its signal *is* the missing
span. Head sampling makes "no sender span for `corr-0005`" indistinguishable from
"not sampled." Tail sampling selects on error status and duration; a dropped
message has neither. **The finding that matters most is the one that disappears
the moment you make telemetry affordable**, and it fails silently in the
safe-looking direction — your dashboards get cleaner as reliability gets worse.

So delivery accounting must not be built on traces. It needs a **delivery
ledger**: two cheap durable rows per message (`message_accepted` at ingest,
`message_settled` at the sender with the provider outcome), keyed by
`correlation_id`, on its own 13-month retention, with a reconciliation job that
alerts on accepted-but-not-settled past SLA. That turns F1 from something
discovered a week later into a page within minutes, and it converts the pipeline's
central question from an expensive windowed scan into a primary-key lookup.
Everything else — latency, hop health, drill-down — is legitimately a sampled
tracing problem.

**There is no producer attribute, and that blocks everything multi-team.** The
complete attribute set across all 273 spans is `correlation_id`, `message_type`,
`tenant_id`, `messaging.system`, `http.status_code`, `provider`,
`provider.status_code`, `provider.final_status_code`, `retry_count`,
`sqs.receive_count`. Nothing identifies the producing team. `tenant_id` is the
*recipient* org and is orthogonal — all six tenants appear across multiple
channels.

So per-producer baselines, noisy-neighbour attribution, per-team alert routing,
and "is it me or is it the platform?" are not implementable against current
instrumentation. Add `producer.service` / `producer.team` at ingest and propagate
it alongside `correlation_id` — the same plumbing, already proven end-to-end.
Sequence this **before** rollups: you cannot backfill a dimension you never
recorded, and at 30-day retention every day it is missing is permanently lost.

**Costs get defended with arithmetic, not assertion.** Three levers, in order of
return: don't store the 95.7% noise; move accounting off traces onto the ledger;
precompute rollups so you pay one scan per period and read it thousands of times.
Then make the spend legible — the comparison that matters is not "$X/month" but
"$X per incident against a status quo of most of an engineer-day." Lead with what
it detects: this pipeline silently failed to deliver 9.8% of messages it returned
`202` for, and 100% of one channel, for at least ten days, while the existing
telemetry reported zero errors.

---

## Lower environment vs production

The honest framing is not "these numbers are small, don't extrapolate." It is
that **a lower environment and production fail in different classes**, so most of
what this dataset teaches does not transfer, and the parts that do are not the
parts that look most impressive.

**What transfers: correctness bugs.** F1 (a channel dropped at the subscription),
F4 (a consumer that doesn't propagate trace context), F5 (no error signal on
non-2xx), F6 (a gauge with no dimension label). These are wrong in every
environment. They were findable here precisely *because* the environment is quiet
— no contention to hide behind. Fix them and they stay fixed.

**What does not transfer: every threshold, rate, baseline and latency figure.**
`min_samples`, `slow_factor`, the 235 ms baseline, the 24-hour incident gap, the
"5 of 5 messages affected" blast radius — all calibrated on 41 synthetic messages
with zero hop-latency variance and one producer. Carrying any of them to
production would be worse than having no default, because they would look
authoritative.

**The failure classes differ, not just the volume.** Lower environments surface
logic errors under uniform, single-tenant, uncontended load. Production surfaces
*emergent* behaviour: noisy neighbours on a shared quota, hot partitions, poison
messages, retry storms, backpressure cascades, partial deploys, clock skew, one
tenant at 80% of volume. Not one of those is visible here, and no threshold tuned
here predicts them. The March 9 `429` is the clearest example — in production the
same symptom is at least as likely to be another team's burst against the shared
SendGrid quota as a provider-side limit, and this dataset cannot tell the
difference.

**The log noise is probably not waste.** 95.7% of records being health checks,
poll chatter and a queue-depth gauge reads like carelessness, but it is more
likely instrumentation someone added deliberately because it was useful in dev,
which was then never gated by environment. That changes the fix: it is
level-and-sampling per environment, not deletion. Deleting something a QA
engineer depends on is how an observability cleanup gets reverted.

**And it constrains what can be concluded at all:**

- **The zero-traffic weekend is a test-harness artifact.** 03-07 and 03-08 are a Saturday and Sunday with zero messages. **Production sends on weekends** — donors and supporters do. The tool never encodes "weekends are quiet": a zero-traffic bucket is `insufficient_data`, never a 100% drop.
- **Timing is synthetic.** Every async hop is identical across all messages — topic hop exactly 269.0 ms ×37, queue hop exactly 379.0 ms ×37, zero variance. Real queues jitter. Messages arrive in fixed daily slots (all 4 push at 17:52). Hop percentiles carry no information, so the tool doesn't compute them.
- **No infrastructure config.** No filter policies, visibility-timeout settings or DLQ configuration in the export, so F1's mechanism and F2's exact failure mode are inferences.
- **No rejected requests.** The ledger lists only 202s. Anything ingest rejected is invisible, so Payments' campaign may have been larger than 4 records show.

## Troubleshooting problems nobody has seen yet

The detectors above have a structural weakness worth stating plainly: **every one
of them was written after I already knew the answer.** D1–D5 are a closed world.
Point them at a new failure mode and they produce nothing — and "no findings"
looks exactly like "healthy", which is the same silence F5 is about.

So the tool runs three layers, and only the first is pipeline-specific:

| Layer | Knows about this pipeline? | Answers | On unfamiliar data |
|---|---|---|---|
| **Detectors** (`D*`) | Yes — rules written from known failures | *why*, with mechanism and cause | mostly silent |
| **Invariants** (`INV.*`) | No | *what* broke, never why | fully working |
| **Novelty** (`NOV.*`) | No | *what changed* since a baseline | fully working |

**Invariants state what must be true of any message pipeline** and report the
violation. Nothing in the layer names a channel, a service, or a stage count:

- *conservation* — what enters a hop must leave it
- *path shape* — messages should follow one of a few routes
- *settlement* — every promise in the ledger reaches a terminal node
- *context integrity* — a trace should not fragment mid-journey
- *single visit* — a message traverses each node once
- *referential* — every reference resolves, every record joins

A violation is novel by construction: it needs no rule and no prior example. On
this dataset the invariants independently rediscover F1, F2, F4 and F7 — from
different directions, which `tracelens findings` reports as corroboration.
Conservation even names the discriminating attribute (`message_type=push`) by
comparing lost against surviving messages, without knowing that channels exist.

**The topology is learned, not configured.** `tracelens topology` builds the graph
from the spans, templating attribute values out of span names so
`publish email-queue` and `publish sms-queue` collapse to
`publish {message_type}-queue` — meaning a fourth channel added tomorrow lands on
an existing node instead of looking like an anomaly.

**Novelty answers "what is different from last week?"**, in both directions.
Something new is the usual suspect mid-incident; something that *stopped*
appearing is what a silently disabled code path looks like, and it produces no
error at all.

```bash
tracelens topology                       # the graph, learned from the data
tracelens findings --layer invariant     # what broke, without any rule
tracelens baseline --save                # record a known-good fingerprint
tracelens baseline --against ../last-week/data
```

**Proof rather than assertion:** `tests/test_unfamiliar_dataset.py` builds a
synthetic five-stage payments pipeline — different services, different channel
names, a stage count this repo was not written around — injects a *mid-pipeline*
silent drop that no detector encodes, and asserts the invariants find it and name
the affected class. Writing that test found three real bugs the familiar dataset could never expose:
an optional retry stage made every message that skipped it look lost; a
path-shape finding built its ID from Python's per-process-salted `hash()`, so it
changed every run; and — worst — the detectors fired anyway on the foreign
pipeline, reporting all three payment rails as dropped, because no span mapped to
a known stage so every message looked undelivered. The detector layer is now
gated on whether its taxonomy actually recognises the data, and skipping it is
reported as an `ERR.taxonomy_mismatch` finding rather than passed off as silence.

The layers also fail independently — if one raises on an unfamiliar export the
other two still report, and the failure surfaces as an `ERR.*` finding rather
than as silence.

---

## What I cut

Scope decisions, stated on purpose:

- **No statistical anomaly detection.** With n=41 and zero-variance hop timing, thresholded rules are more honest and far more debuggable.
- **No production polish** — no auth, persistence, or packaging.
- **No live query layer** against a real backend. Addressed as design (DESIGN §10). The `Source` split is documented but only file loading is implemented; the loader is already the single I/O boundary, so it is a small change rather than an aspirational one.
- **No pipeline fixes.** This analyzes. The recommendations are prose, and the ledger is a change to `comms-ingest` and `comms-sender`, not to this repo.
- **HTML report is a view, not new analysis** — hand-rolled SVG, no CDN, no build step, opens from a clone with no network.

## Where AI was used building this

Claude wrote most of the code and the first draft of the analysis. What I changed
matters more than what it produced:

- **Rejected the naive hop formula.** `next.start − previous.end` returned −26 ms and −18 ms. The generated code would have printed those. They are correct parent/child nesting, not corruption, so transitions are now typed (DESIGN C1).
- **Caught an attempt-partitioning bug** that survived two drafts: `sqs.receive_count` is on the *send* span, absent on the redelivered *consume* span, so the documented filter would have let duplicates back into end-to-end latency — silently reintroducing the 32-second days the rule existed to prevent (C3, and `test_join.py::test_receive_count_is_absent_on_the_redelivered_consume`).
- **Caught a `min_samples` contradiction** that would have suppressed the headline finding: push is n=4 against a gate of 20. Existence claims are now never gated, only rates (`test_detectors.py::test_d1_fires_below_min_samples`).
- **Caught the validator re-sorting hypotheses by severity**, which answered every complaint — including "email was slow" — with the push outage. Relevance ranking is the model's job; the validator now preserves its order.
- **Rewrote the first analysis pass**, which confidently exonerated both deploys. The onset attribution is falsifiable; the recovery is not. That correction is now the tool's most important behaviour.
- **Cut three design documents to two.** The first draft was ~1,240 lines of prose against a 2–3 hour brief.
- **The first live run exposed two more.** The model returned two hypotheses bearing the *same* `finding_id` — padding to satisfy "return ≥2", with the second entry restating an ambiguity already attached to the first. The prompt demanded two hypotheses while the validator accepted one carrying alternatives; that gap is what produced the padding. Fixed on both sides: the prompt now says two *distinct* findings or one with alternatives, and the validator merges duplicates while unioning their citations. Separately, two of my own tests were pinned to the stub's exact wording — the live model wrote "postdates the onset by 5 hours" instead of the literal "ruled out", which is the same claim made better. Tests now assert substance and let the model choose its words.

`scripts/verify_claims.py` exists because of this: it recomputes all 101 cited
figures from `data/` and exits non-zero on drift, so no number in this write-up
depends on my having read a generated table carefully.
