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
