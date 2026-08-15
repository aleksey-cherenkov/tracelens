# Decisions

What I got wrong, and what changed because of it. Kept because the corrections are
more informative than the result — most of them came from the tool catching me
rather than from me being careful.

---

## The two rewrites

**First version: five detectors.** One per problem I'd found by reading the export
— a rule that knew about channels, one that knew about redelivery counts, one that
did deploy arithmetic. 1,190 lines, well tested, accurate on all five.

It was worthless as an assistant. Every rule was written *after* I knew the answer,
so it could only re-find what I already knew. Point it at a sixth problem and it
produces nothing — and "no findings" reads identically to "healthy", which is
exactly what this export demonstrates (`status: OK` on 273 spans, four of them for
messages never delivered).

**Second version: invariants.** Properties I believed held of any pipeline —
conservation, path shape, single visit, completeness. Genuinely better: a
violation didn't have to be anticipated, and it found the push loss on a synthetic
payments pipeline it had never seen.

But every check carried a threshold I picked and a severity I assigned. On
unfamiliar data I couldn't defend either. `_severity(affected, total)` returning
CRITICAL because a share crossed 0.5 is code inventing a judgement.

**Third version: route table plus timelines.** The deterministic half groups,
orders, filters and renders. The model reads. A route table with counts says
everything the four invariants computed, in a form a person can check:

| The invariant | The row |
|---|---|
| conservation — 4 enter, 0 leave | route 4 ends six nodes early |
| single visit — duplicate delivery | route 5 visits a node twice |
| completeness — fewer sources | route 4 touches two services, others four |

~2,400 lines deleted across the two rewrites. I expected the cost to be the
mechanisms the detectors named -- and I was wrong about that. Live runs show the
model reaching the March 9 mechanism from the timeline alone. What is genuinely
lost is *guaranteed* coverage: a detector fires every time, a model reading is
likelier and not certain.

What it bought: the tool works on a system it has never seen, and that is
checkable rather than claimed.

---

## Corrections from re-reading my own code

**`model.py` declared a seven-stage pipeline.** The single worst decision in the
project — it meant the tool could only understand the one system I had already
read. Replaced by one flat `Event` type with identifiers in a dict, found by
suffix. `order_id` on an unseen system now works without anyone editing a list.

**I scored the correlation key with a formula I invented.** `coverage ×
multi_source_share × min(1, groups/10)`. It picked correctly here, which is the
problem: it looked principled and the constants were arbitrary. Now the key is
supplied or defaults to the highest-coverage identifier that groups across
services, and the whole candidate table prints so a wrong default is visible.

**Accounting, health rollups and a HTML report** existed to serve the detectors and
died with them. None of them answered a question anyone asked.

---

## Bugs the tool found in itself

**Conservation took the union over every expected successor.** A log line emitted
for all 60 journeys sat beside the real next hop, the union covered everyone, and a
drop affecting a third of traffic came out as *zero*. Found by the synthetic
payments fixture. Fixed by only checking handoffs across a service boundary —
within one service, a node some journeys skip is usually a logging difference.

**`min_samples=20` would have suppressed the most severe finding.** A 100% channel
outage has four examples. Fixed: existence claims are never gated on sample size,
only rates. The general lesson survived both rewrites — the current tool reports
`4 of 41`, never a bare percentage.

**`hash()` is salted per process**, so finding IDs changed every run and any test
pinned to one was meaningless. Fixed with `hashlib.sha1`.

**`read_dotenv` bound its default path at import time**, silently ignoring any
later override. **`sdk_available()` used `find_spec`**, which raises rather than
returning None when a parent package is missing.

**An optional retry stage made every journey that skipped it look lost.** Fixed
with a share threshold — an edge is the normal route only if at least half the
journeys at that node take it. It cannot sit near 1.0, because a real drop drags
the edge's own share down; the threshold has to be below the loss it detects.

**The route table truncated before the divergence.** Five routes agreeing for eight
nodes and diverging at the ninth all rendered identically. Fixed by printing the
shared opening once — the divergence is the only thing the table exists to show.

**A hand-rolled stemmer.** "settlement" reduced to "settl" while "settle" stayed
whole, so the two never matched and a complaint about payments not settling missed
the finding that answered it. Replaced with prefix comparison.

---

## Found by the model, not by me

**The first live call returned two hypotheses with the same finding ID.** Asked
for "at least two" when only one thing matched, it split one explanation into two
entries — the second restating an ambiguity already attached to the first. Two
entries read as two independent explanations when there is one. Fixed on both
sides: the validator merges duplicates, and the prompt now says padding is worse
than a single honest answer.

**The validator re-sorted hypotheses by severity.** Every complaint got answered
with the most severe finding regardless of relevance — a question about slow email
answered with the push outage. Fixed: ranking by fit to the question is the
model's job, and code owns only the labels it cannot inflate.

**`temperature: 0` would have 400'd on the first live call.** Sonnet 5 rejects
non-default sampling parameters.

**I misdiagnosed a 401 as an invalid key.** My sandbox MITM-proxies TLS and strips
credentials. The proof: a request with *no* key reached Anthropic and returned a
JSON error with a Request-Id; a request with any key returned plain-text
"Unauthorized" with no Request-Id. I had to correct myself publicly, and it is
precisely the misattribution-from-insufficient-evidence failure this project is
about. The error message now explains how to tell the two apart.

---

## The adversarial test, and the limitation that went away

*"Our webhooks stopped firing"* must return insufficient evidence -- webhooks are
not part of this platform, but they are adjacent to one of its channels, and that
channel happens to be entirely dead in this data.

**For weeks this test was green for the wrong reason.** It only ever ran against
the offline stand-in, which declines anything it cannot word-match. The guarantee
was being checked by the one implementation that could not fail it. Live, under
the previous design, the model answered with the push outage -- hedged, but with
a confident verdict.

**Under the timeline design it declines**, and the reason it gives is the one I
would want:

> The architecture description states the platform has exactly one inbound
> endpoint and three outbound integrations. It explicitly says the platform "does
> not call back into product services". There is no webhook concept anywhere in
> this system's documented shape or in the observed record kinds and services.

I do not think the prompt fixed this on its own. The likelier account is that
`PLATFORM.md` states a *boundary* -- what the platform does not do -- and the
route table gives the model a complete list of observed services to check that
boundary against. Neither is a rule about webhooks.

**One run is not a guarantee**, and this is the test I would least trust to a
single sample, because the failure mode is a model being agreeable rather than a
deterministic bug. The offline test still asserts only the stand-in's behaviour,
with a docstring saying so, so nobody reads it as the real guarantee.

**The A/B is now less informative than it was.** With `--no-platform-context` the
model also declines, but on different grounds -- it rules out the deploys on
timing and says it cannot find anything webhook-shaped, rather than citing a
documented boundary. So the architecture context is still doing something; it is
no longer the difference between answering and declining.

---

## Found while trimming

**The test suite took 70 seconds; 47 of them were one test.** The API-key scanner
walked the entire tree including the multi-megabyte export, reading `logs.json`
looking for a key. Scoped to the directories a key could plausibly be pasted into.

**That scanner also cried wolf.** Matching the bare `sk-ant-` prefix fired on a
deliberately synthetic key in another test. A check that cries wolf gets loosened
until it catches nothing, so it now requires a long run of key characters in one
literal — and there is a test for the test.

**The vocabulary check exempted every string literal**, which is where both real
leaks lived: a hardcoded service-name prefix in the CLI, and this pipeline's
vocabulary used as the example in a tool schema, which was going into the *prompt*.
Fixed to exempt only docstrings.

**The citation gate caught my own test fixtures.** Four tests failed after the
rewrite because they built a reply citing a route and a journey without ever
calling a tool — so nothing entered the index, and every citation was correctly
rejected. The gate was working on a test that forgot to show the model anything.

**113 tests against 3,400 lines of source, and 1,900 lines of docs.** For a
take-home that reads as generated, not owned. Cut to 90 tests and 5 documents, and
deleted the ones asserting that `sorted()` sorts. It is 96 now -- the six that came
back are the live-run failures below, each pinned so it cannot return.

---

## Scope decisions, on purpose

**No statistical anomaly detection.** With 41 journeys and zero-variance hop
timing, stated thresholds are more honest and far more debuggable than a model
fitted to nothing.

**No severity labels.** The output says `4 of 41`. Severity was code inventing a
judgement from a share, and a count is what an on-call engineer can act on.

**No novelty detection.** It compares against a baseline that does not exist, so
with one export the honest output is "no baseline recorded" — 176 lines and a
whole concept the reviewer would never see fire.

**Input quality stays rule-based**, and it is the only rule-based layer left. That
is inconsistent with the rest of the design and I know it. Computing the statistic
and letting the model draw the conclusion would be smaller and more uniform — it
would also make the limit advisory, and the limit is the point.

**No live query layer.** Addressed as design; `loader.py` is already the only I/O
boundary, so it is a small change rather than an aspirational one.

**No pipeline fixes.** This analyzes. The delivery ledger is a change to two
services, not to this repo.
