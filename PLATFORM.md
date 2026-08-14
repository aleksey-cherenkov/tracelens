# Communications platform — architecture and product context

Maintained by Platform Engineering. Loaded into the triage prompt so the model
knows what the system *is*, not only what its telemetry says.

Everything here is stable and human-verified. It is deliberately kept separate
from the per-incident evidence, which is computed from telemetry and protected by
the citation gate — this file describes the system, it never describes an
incident.

## What it is

The communications platform sends supporter-facing messages on behalf of every
product at Bloomerang: donation receipts, volunteer confirmations, event
reminders and similar. Bloomerang is a nonprofit CRM, so the recipients are
donors, volunteers and supporters of the organisations using it.

Message content is decided by the calling product. The platform's job is delivery.

## Interfaces

**In:** one endpoint, `POST /api/v1/messages`. Product teams call it, it validates
and returns `202`, and that `202` is a promise to deliver. Five teams use it.

**Out:** three providers, and nothing else.

| Channel | Provider | Used for |
|---|---|---|
| `email` | SendGrid | receipts, confirmations, most volume |
| `sms` | AWS Pinpoint | reminders, time-sensitive notices |
| `push` | AWS Pinpoint | mobile app notifications |

That is the complete integration surface. One inbound endpoint, three outbound
providers. The platform does not expose an API for reading message state, does
not call back into product services, and has no scheduled or batch entry point.

## Internal shape

```
POST /api/v1/messages
        │
   comms-ingest          validates, publishes, returns 202
        │  SNS topic (comms-topic)
   comms-orchestrator    consumes the topic, picks the channel,
        │                publishes to a per-channel SQS queue
        │  SQS ({channel}-queue)
   comms-sender          consumes the channel queue, calls the provider
        │
      provider
```

Every message carries a `correlation_id` from the moment it is accepted.
`tenant_id` identifies the **recipient organisation**, not the sending team —
there is currently no attribute identifying which product team produced a message.

## What the platform promises

A `202` means the message will be delivered exactly once to its provider.
Anything else is a defect: silent loss, duplicate delivery, or delivery to the
wrong recipient.

Delivery *to the provider* is the platform's boundary. Whether the provider then
reaches the recipient's inbox or handset is outside it.

## What matters when something breaks

Donation receipts and volunteer confirmations carry donor-trust weight beyond the
engineering impact. A supporter who receives a receipt twice, or never receives
one, loses confidence in the organisation rather than in the platform. That makes
duplicate and missing supporter-facing messages higher priority than their raw
count suggests.
