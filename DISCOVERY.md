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
| **F3** March 9 | **partly** | `send` shows a 17.5x spread — p50 235ms, max 4120ms — and the timing layer flags the distribution as not worth averaging. Slicing that window puts deploy `c52a0f9` inline five hours after both slow sends, so the rule-out is readable. It does **not** name throttling or reach a cause; that was mine. |
| **F4** trace break | **yes** | `trace_id` fragments in 8 of 41 journeys and holds in the other 33, so it is not how the transport behaves in general. Reported as an input defect, with the limit it imposes. |
| **F5** no error signal | **yes** | `status` constant at OK across 273 records; `level` never reaches ERROR. Reported *first*, because it constrains everything below it. |
| **F6** fake gauge | **no** | The gauge is in the 86% of records that join to nothing, so no journey ever sees it. The tool says that share exists and what it prevents concluding — the honest partial answer, not the finding. |
| **F7** unjoinable logs | **yes** | 86% of records carry no correlation key. Reported as a limit on every claim, not as a log-hygiene complaint. |

Five and a half of seven, with no rule describing any of them.

**What it misses is as interesting as what it finds.** F3's mechanism and F6
entirely. Both come from the same place: the tool reasons about journeys, and
anything outside a journey — a gauge with no correlation ID, a provider's own
behaviour — is invisible to it. That is a real limitation, and it is stated in
the output rather than left for a reader to discover.

---

## What I'd fix first

Bugs and anything customer-facing come first. That's a business call as much as an
engineering one, so I'd set the order with them rather than alone.

**Push loss and duplicate emails, now.** Push being dropped is a bug, but it's an
internal team reporting it. Supporters getting the same email twice is the one
they actually feel — it's annoying, and it chips away at trust in the product.

**Fix the logging on those two paths while you're in there.** We have a good idea
what's broken in each case, but part of it is still hypothesis. Tightening the
logs and traces around those features is cheap when you're already in the code,
and it's how you confirm the fix actually worked.

**Then the proper overhaul** — real error signal and the delivery ledger. Worth
doing, and it's the difference between diagnosing this by hand and being told.
Just not ahead of a defect dropping messages today.

**The noise, investigate before deleting.** I don't know why it's there. It might
be an uptime check, or how ops spots an unhealthy instance getting swapped out.
Worth an afternoon to find out. If it's useful, the answer isn't deleting it —
it's a viewer that hides it by default so devs can find the message they're
chasing.

**SMS trace propagation last.** It costs investigation time, not wrong outcomes,
and the tool already routes around it. Same for March 9 — it resolved itself and
lost nothing.
