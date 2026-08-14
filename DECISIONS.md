# Decisions

What I got wrong and changed, in the order I found it. Kept separate so
[`README.md`](README.md) and [`DESIGN.md`](DESIGN.md) can state conclusions
without carrying the history.

Named for architecture decision records rather than "history": the value is the
*reasoning*, not the chronology.

---

## Corrections from re-reading my own first pass

**C1 — Naive hop latency produces negative numbers.**
`next.start − previous.end` gives `ACCEPT → PUBLISH_TOPIC = −26 ms`. Not
corruption: `ACCEPT` runs 48 ms from `.000` and its child starts at `.022`,
*inside* it. → Transitions are typed `NESTED` / `SEQUENTIAL`. A health view
showing "−26 ms" loses the reader on first contact.

**C2 — Hop latency has literally zero variance.** Every topic hop is exactly
269.0 ms; every queue hop 379.0 ms. → No percentile engine. Reporting "p99: 379ms"
implies a distribution that doesn't exist; `variance: none` is printed instead.

**C3 — Duplicates poison end-to-end latency.** First-span to last-span gives
31,988 ms for three messages, inventing a 32-second day. → Latency is defined over
the first attempt only. **The trap:** `sqs.receive_count` is on the *send* span
and absent on the redelivered *consume* span, so the obvious filter doesn't
exclude it. Attempts are partitioned by walking backwards from each send.

**C4 — The March 9 recovery is ambiguous and I nearly overclaimed it.** My first
read was "throttling, both deploys exonerated, done." Wrong on the second half:
`e18d773` lands *inside* the recovery window. The onset attribution is falsified;
the recovery is not resolvable. → Findings carry competing sub-hypotheses the tool
is forbidden from collapsing. This is the single most important behaviour in the
tool.

**C5 — Deploy correlation needs a service constraint.** The orchestrator deployed
03-04 11:00; the first duplicate is 15:39 the same day. A proximity-only
correlator blames it — and the span topology disproves it. → `correlate_deploys()`
takes a service derived from where evidence localises the fault. Other services'
deploys are shown as "adjacent, ruled out by $reason" rather than hidden.

---

## Bugs found while building

**`min_samples` would have deleted the headline finding.** The spec said "no
detector fires on a rate below `min_samples` (default 20)". Push is n=4. Read
literally, the gate suppresses F1 entirely. → Existence claims are never gated,
only rates. `test_d1_fires_below_min_samples` sets `min_samples=1000` and asserts
push still surfaces.

**The validator re-sorted hypotheses by severity**, so *every* complaint —
including "email was slow on March 9th" — was answered with the push outage.
→ Order is the model's; code owns only the labels.

**`rank_score` let a well-cited medium outrank a thinly cited critical.** Was
`severity × support`. → Now a tuple, severity primary.

**Two doc numbers were wrong once code existed.** The join table said 224 (that's
spans-with-a-parent; stage transitions are 218/8/4), and the deploy gap said
5h03m (it's 4h59m59s, rounded to 5h00m rather than truncated to 4h59m).

**`read_dotenv` bound its default path at import time**, freezing the `.env`
location so any override was silently ignored.

**`sdk_available()` used `find_spec`, which raises** for a module imported without
a `__spec__`. A probe for an optional dependency should never be the thing that
throws.

**The insufficient-evidence branch returned before recording**, so the adversarial
case — one of the golden tests — could never be saved as a transcript.

---

## Found by pointing it at a pipeline it had never seen

`tests/test_unfamiliar_dataset.py` builds a five-stage payments pipeline
(`api-gateway → fraud-check → ledger-writer → settlement → bank-adapter`, rails
`wire`/`ach`/`card`) with a *mid-pipeline* drop no detector encodes.

**An optional retry stage made every message that skipped it look lost.** The
conservation invariant treated any successor as mandatory. → An edge is *expected*
only if ≥50% of messages at that node take it. The threshold can't sit near 1.0,
because a real drop drags the edge's own share down.

**A path-shape finding built its ID from Python's `hash()`**, which is salted per
process — so the ID changed every run, silently breaking determinism.

**Worst: the detectors fired anyway and reported all three payment rails as
dropped.** No span mapped to a known stage, so every message looked undelivered.
My original test only asserted `affected_count <= total` — trivially true, caught
nothing. → The detector layer is gated on `stage_coverage`, and skipping it
surfaces as `ERR.taxonomy_mismatch` rather than as silence.

---

## Found by the first live model call

**The model returned two hypotheses with the same `finding_id`** — padding to
satisfy "return ≥2", the second restating an ambiguity already attached to the
first. Its own `why_this_rank` admitted it: *"Surfaced per rule 4."* The cause was
mine: the prompt demanded two hypotheses while the validator already accepted one
carrying alternatives. → Prompt says two *distinct* findings or one with
alternatives; validator merges duplicates and unions their citations.

**Two of my tests were pinned to the stub's exact wording.** The live model wrote
"postdates the onset by 5 hours" instead of the literal "ruled out" — the same
claim, better phrased. → Tests assert substance and let the model choose its words.

**A 401 was misdiagnosed as a bad key.** The sandbox I was testing from
MITM-proxies TLS and blocks credentialed calls, returning a plain-text
`Unauthorized`. A genuine API 401 returns a JSON body with a `Request-Id`. → The
error message now distinguishes the two cases, which is exactly the
misattribution-from-insufficient-evidence failure this project is about.

---

## The adversarial test passed against the stub and failed against the model

Running all five symptoms live: every one ranked its expected finding first, no
fabricated citations, no duplicate `finding_id`s, and symptom 4 reached an
invariant finding on its own. Then the adversarial case — *"our webhooks stopped
firing"* — came back as `D1.channel_drop.push`, **CRITICAL**, confident. Its own
justification: *"the most direct match for 'webhooks stopped firing' since push is
the only channel losing 100% of its messages."*

There are no webhooks in this platform.

**The test had been green for weeks because it only ever ran against the stub**,
whose keyword router genuinely finds nothing and declines. The honesty guarantee
was being verified by the one implementation that could not fail it. That is worse
than having no test, because it bought confidence.

### Root cause: context starvation I designed in

The model was not reasoning badly. It was under-informed. It sees findings derived
from telemetry and nothing else — no statement of what the platform *is*, which
channels exist as a product concept, or what a webhook would even mean here. Given
only "push is 100% lost" and a complaint about webhooks, conflating them is a
defensible inference from what it was shown.

The deeper tension: I withheld everything but code-computed evidence on the theory
that less context means less hallucination surface. True — and it is also why the
model cannot tell whether a complaint is about this system at all. **The same
withholding that prevents invention prevents grounding.**

The resolution is that architecture and product description are a *different
category* from per-incident evidence: stable, human-maintained, checkable by
reading. They can be added without weakening the citation gate, and they are
fixed-size, so the O(findings) invariant holds. → `PLATFORM.md`, loaded into the
prompt by `prompts.platform_context()`, +775 tokens.

### Tested, not assumed — and it half worked

A/B against the live API, `--no-platform-context` versus default:

| Complaint | Without context | With context |
|---|---|---|
| the CSV export job is failing | — | **insufficient evidence** |
| our Salesforce sync stopped last night | — | **insufficient evidence** |
| our webhooks stopped firing | `D1` critical, unhedged | `D1` critical, **hedged** |
| symptom 1 (push) | `D1` correct | `D1` correct |
| symptom 3 (March 9) | `D3` correct | `D3` correct |

Two clearly unrelated subjects now get declined, and the model cites the
architecture when explaining why: *"this platform's architecture … is a
message-delivery pipeline … and has no CSV export functionality."* No regression
on the controls.

Webhooks still gets answered. But the answer changed shape: it now opens *"If
'webhooks' refers to push notifications (the outbound channel closest to a
webhook-style fire-and-forget call)…"* and its first `would_resolve` entry is
*"Confirmation of what 'webhooks' refers to in this system — the architecture doc
states there is no webhook concept … so this term may not map directly onto any
pipeline component."* It read the doc, understood there are no webhooks, and chose
to answer conditionally anyway.

**Which may be correct.** A webhook and a push notification are both outbound
fire-and-forget calls to a remote endpoint. An on-call engineer told "our webhooks
stopped" while knowing push is 100% dead would probably say the same thing: *we
don't have webhooks — did you mean push? Because push is completely broken.* That
is more useful than a refusal.

What is wrong is the **presentation**. The verdict is `hypotheses`, the label is
CRITICAL, and the caveat lives in prose. Someone skimming sees a confident answer
to a question about something that does not exist here. The term-mapping
assumption belongs in the verdict, not in a paragraph.

→ Left as a documented limitation rather than patched, because the fix is
unverified and this project's whole argument is against shipping untested
hypotheses as solutions. The golden test now uses genuinely out-of-scope subjects
(verified live); the webhooks case is pinned separately in
`test_semantically_adjacent_term_is_a_known_limitation`, with the live behaviour
described in the docstring so it cannot be quietly forgotten.

**What I would try next, in order:** promote the scope judgement to a required
schema field so it lands in the verdict rather than the prose; then, if that is
not enough, a code-side check that a hypothesis naming a term absent from the
platform vocabulary is downgraded rather than dropped — downgraded, because
dropping it would lose the genuinely useful "did you mean push?" answer.

---

## Scope decisions, on purpose

**Three design documents cut to two, then to three of different shape.** The first
draft was ~1,240 lines of prose against a brief that says 2–3 hours; the second
grew back to ~1,540 once the three-layer architecture landed. Conclusions now live
in README and DESIGN; this file holds the history that was bloating them.

**Open questions removed.** An earlier DESIGN ended with seven "open questions for
review", which reads as undecided. The brief rewards deliberate scope cuts, so
they became "What I cut".

**The stub is not the model, and says so.** It is a keyword matcher with a token-
overlap fallback. Anything it gets right, it gets right because the detectors
already did the work — which is the point of the split, but not evidence about the
model. Every recorded transcript states in its own `note` which it is.
