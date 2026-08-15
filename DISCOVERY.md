# What I found by reading the data

This is the human half of the assignment: seven findings I reached by hand, before
writing any analysis code. It is kept separate from the tool on purpose.

**The tool does not know any of this.** No module names a channel, a service, or a
failure mode. If you point `tracelens` at this export it re-derives some of the
list below from general properties, misses parts of it, and says which parts it
cannot support. That gap is the honest measure of what the tool is worth, and
folding these findings into it as rules would have hidden the gap while making the
output look better.

The right-hand column of the last table says which of these the tool found on its
own.

---

## F1 — Push notifications never arrive

All four push messages were accepted and published. None reached the orchestrator.

Ingest logged 41 publishes, the orchestrator logged 37 consumes, and the gap is
exactly those four. Nothing reports an error — no failed span, no ERROR log. The
only evidence is an absence.

Payments called this a one-off with "our donation campaign last week." It isn't.
The four come from three different tenants, spread across the whole window. Every
push message in the export was lost.

Push and SMS use the same provider, and SMS works fine, so the provider isn't at
fault. The break is between the topic and the queue — most likely a subscription
filter, though nothing in the export proves that.

## F2 — The duplicate emails are the queue redelivering

Three of 29 emails went out twice.

Each shows a single publish and two deliveries. The second fires 31 seconds after
the first and is tagged `receive_count: 2`. The first send had already succeeded.

So the app didn't send twice. The message was never deleted from the queue, the
visibility timeout expired, and the sender picked it up again.

## F3 — March 9 was the provider throttling us

Six sends took about 4.1 seconds instead of the usual 235ms, hit `429` rate
limits, and retried. All eventually succeeded, so nothing was lost.

The deploy people suspected shipped five hours *after* the slowdown started. That
theory doesn't hold.

**Why it recovered is still open.** A different deploy — an SDK bump — landed in
the middle of the recovery window. Either the rate limit expired on its own, or
the old SDK was causing it and the update fixed it. This data cannot tell them
apart. The code change would.

One thing is clear: it wasn't us sending too fast. All five emails that day were
throttled, across five different tenants, not a burst from one sender.

## F4 — SMS loses its trace ID partway through

8 of 8 SMS journeys split into two traces at the sender's consume. Email never
does — 0 of 29. The sender also logs nothing for SMS, so there is no fallback
trail: search by trace ID and half the journey is simply absent, with no
indication that it is half.

This is why the join is on `correlation_id` rather than `trace_id`. It is also the
one finding that changes how you *use* every other tool you own.

## F5 — Errors never show up as errors

0 of 273 spans marked failed. 0 of 2,820 logs at ERROR. Despite real rate limits,
real retries, and four messages silently lost.

The `429`s are visible only inside span attributes. `status` reads `OK` on every
span in the export, including the ones for messages that were never delivered.

F5 is why nobody caught the other six. Any dashboard built on span status or log
level shows this pipeline as healthy straight through every incident here.

## F6 — The queue-depth metric is hardcoded

Reports `depth=0` all 1,200 times, with no queue name and no trace ID. Nothing
emits it for the push queue at all — so the one metric that would have shown F1 is
both fake and absent.

## F7 — 96% of logs match nothing

2,700 of 2,820 lines carry no correlation ID and no trace ID. About 120 lines in
the whole window are usable for investigating a specific message.

---

## The numbers

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

## What the tool finds on its own

Run `tracelens routes` and compare. Nothing below was written into it.

| | Found? | How, without being told |
|---|---|---|
| **F1** push loss | **yes** | Route 4 — four journeys ending six nodes early, where no other route ends. `tracelens slice --where message_type=push` puts a normal journey beside them and the missing three services are visible in one screen. |
| **F2** duplicates | **yes** | Route 5 revisits two nodes. A journey returning to a node it already left did that work twice, whatever the work is. |
| **F3** March 9 | **yes**, and further than I expected | I predicted the mechanism was out of reach. It is not: given the timeline the model reads `provider.status_code=429`, `retry_count=3` and the backing-off line against a contrast journey at 235ms, and names provider rate limiting. It rules the deploy out on two grounds I only had one of — that it postdates onset, *and* that its title describes a different channel's provider seam. It correctly declines to say what ended the incident. |
| **F4** trace break | **yes** | `trace_id` fragments in 8 of 41 journeys and holds in the other 33, so it is not how the transport behaves in general. Reported as an input defect, with the limit it imposes. |
| **F5** no error signal | **yes** | `status` constant at OK across 273 records; `level` never reaches ERROR. Reported *first*, because it constrains everything below it. |
| **F6** fake gauge | **partly**, sideways | The gauge is in the 86% of records that join to nothing, so no journey ever reaches it and no check inspects it. But the unjoined-record defect names the highest-volume shapes, and on the push question the model picked `depth=0` and `received 0 messages` out of that list as *corroboration* for the instrumentation-gap alternative. It never says the gauge is broken. It reaches the record by a different door. |
| **F7** unjoinable logs | **yes** | 86% of records carry no correlation key. Reported as a limit on every claim, not as a log-hygiene complaint. |

Six and a half of seven, with no rule describing any of them.

**I was wrong about two of these, and the live runs are what corrected me.** I
scored F3 as partly and F6 as missed, reasoning that a tool which only knows
journeys cannot see a provider's behaviour or a metric with no correlation ID.
The first half holds — no *check* inspects either. What I had not accounted for
is that a model reading a timeline sees the attributes on every record, so the
429 and the retry count are right there next to a contrast at 235ms.

The limitation is still real and still worth stating: **nothing here reasons
about a record outside a journey.** What changed is my estimate of how much that
costs, and the honest version is that handing a model the raw sequence recovers
more than a rule over the same data would.

---
