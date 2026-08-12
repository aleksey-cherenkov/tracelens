"""Domain model for the comms pipeline.

The pipeline is a fixed seven-stage path. Naming the stages once, here, is what
lets every other module talk about "where did this message stop" without
re-parsing span names.

    ingest.POST /api/v1/messages   ACCEPT
    ingest.publish comms-topic     PUBLISH_TOPIC
      -- topic hop --
    orchestrator.consume           CONSUME_TOPIC
    orchestrator.route <type>      ROUTE
    orchestrator.publish <t>-queue PUBLISH_QUEUE
      -- queue hop --
    sender.consume <t>-queue       CONSUME_QUEUE
    sender.send <type>             SEND_PROVIDER
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def parse_ts(value: str) -> datetime:
    """Parse the export's ISO-8601 'Z' timestamps into aware datetimes."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Stage(str, Enum):
    """Ordered stages of the pipeline. Order matters: it defines "how far did
    this message get" and therefore the delivery funnel."""

    ACCEPT = "accept"
    PUBLISH_TOPIC = "publish_topic"
    CONSUME_TOPIC = "consume_topic"
    ROUTE = "route"
    PUBLISH_QUEUE = "publish_queue"
    CONSUME_QUEUE = "consume_queue"
    SEND_PROVIDER = "send_provider"

    @property
    def index(self) -> int:
        return STAGE_ORDER.index(self)

    @property
    def service(self) -> str:
        return STAGE_SERVICE[self]

    @property
    def label(self) -> str:
        return STAGE_LABEL[self]


STAGE_ORDER: list[Stage] = list(Stage)

STAGE_SERVICE: dict[Stage, str] = {
    Stage.ACCEPT: "comms-ingest",
    Stage.PUBLISH_TOPIC: "comms-ingest",
    Stage.CONSUME_TOPIC: "comms-orchestrator",
    Stage.ROUTE: "comms-orchestrator",
    Stage.PUBLISH_QUEUE: "comms-orchestrator",
    Stage.CONSUME_QUEUE: "comms-sender",
    Stage.SEND_PROVIDER: "comms-sender",
}

STAGE_LABEL: dict[Stage, str] = {
    Stage.ACCEPT: "accept request",
    Stage.PUBLISH_TOPIC: "publish to topic",
    Stage.CONSUME_TOPIC: "consume from topic",
    Stage.ROUTE: "route to channel",
    Stage.PUBLISH_QUEUE: "publish to channel queue",
    Stage.CONSUME_QUEUE: "consume from channel queue",
    Stage.SEND_PROVIDER: "call provider",
}

SERVICES = ["comms-ingest", "comms-orchestrator", "comms-sender"]


@dataclass(frozen=True)
class Hop:
    """A boundary between two stages. Async hops are where traces break and
    where messages disappear, so they get first-class names."""

    name: str
    frm: Stage
    to: Stage
    asynchronous: bool


HOPS: list[Hop] = [
    Hop("ingest:accept->publish", Stage.ACCEPT, Stage.PUBLISH_TOPIC, False),
    Hop("topic", Stage.PUBLISH_TOPIC, Stage.CONSUME_TOPIC, True),
    Hop("orchestrator:consume->route", Stage.CONSUME_TOPIC, Stage.ROUTE, False),
    Hop("orchestrator:route->publish", Stage.ROUTE, Stage.PUBLISH_QUEUE, False),
    Hop("channel-queue", Stage.PUBLISH_QUEUE, Stage.CONSUME_QUEUE, True),
    Hop("sender:consume->send", Stage.CONSUME_QUEUE, Stage.SEND_PROVIDER, False),
]


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    name: str
    kind: str
    start_time: datetime
    duration_ms: int
    status: str
    attributes: dict[str, Any]

    @property
    def end_time(self) -> datetime:
        from datetime import timedelta

        return self.start_time + timedelta(milliseconds=self.duration_ms)

    @property
    def correlation_id(self) -> str | None:
        return self.attributes.get("correlation_id")

    @property
    def message_type(self) -> str | None:
        return self.attributes.get("message_type")

    @property
    def tenant_id(self) -> str | None:
        return self.attributes.get("tenant_id")

    @property
    def retry_count(self) -> int:
        return int(self.attributes.get("retry_count", 0) or 0)

    @property
    def receive_count(self) -> int:
        """SQS redelivery counter. Absent means first delivery."""
        return int(self.attributes.get("sqs.receive_count", 1) or 1)

    @property
    def stage(self) -> Stage | None:
        return classify_stage(self)

    @property
    def short_trace(self) -> str:
        return self.trace_id[-8:]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Span":
        return cls(
            trace_id=raw["trace_id"],
            span_id=raw["span_id"],
            parent_span_id=raw.get("parent_span_id"),
            service=raw["service"],
            name=raw["name"],
            kind=raw["kind"],
            start_time=parse_ts(raw["start_time"]),
            duration_ms=int(raw["duration_ms"]),
            status=raw.get("status", "UNSET"),
            attributes=dict(raw.get("attributes") or {}),
        )


def classify_stage(span: Span) -> Stage | None:
    """Map a span onto a pipeline stage.

    Deliberately keyed on (service, kind, name prefix) rather than an exact name
    match, because the channel is baked into the name ("publish sms-queue",
    "send email"). An unrecognised span returns None instead of raising -- new
    span names should not break the funnel, they should show up as unmapped.
    """
    name, svc, kind = span.name, span.service, span.kind
    if svc == "comms-ingest":
        if kind == "SERVER":
            return Stage.ACCEPT
        if kind == "PRODUCER":
            return Stage.PUBLISH_TOPIC
    elif svc == "comms-orchestrator":
        if kind == "CONSUMER":
            return Stage.CONSUME_TOPIC
        if kind == "INTERNAL" or name.startswith("route "):
            return Stage.ROUTE
        if kind == "PRODUCER":
            return Stage.PUBLISH_QUEUE
    elif svc == "comms-sender":
        if kind == "CONSUMER":
            return Stage.CONSUME_QUEUE
        if kind == "CLIENT" or name.startswith("send "):
            return Stage.SEND_PROVIDER
    return None


@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    service: str
    level: str
    message: str
    trace_id: str | None
    attributes: dict[str, Any]

    @property
    def correlation_id(self) -> str | None:
        return self.attributes.get("correlation_id")

    @property
    def is_message_scoped(self) -> bool:
        """True if this line can be tied back to a specific message.

        Everything else (health checks, queue-depth gauges, poll chatter) is
        operational noise that dominates the volume.
        """
        return self.correlation_id is not None or self.trace_id is not None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "LogRecord":
        return cls(
            timestamp=parse_ts(raw["timestamp"]),
            service=raw["service"],
            level=raw["level"],
            message=raw["message"],
            trace_id=raw.get("trace_id"),
            attributes=dict(raw.get("attributes") or {}),
        )


@dataclass(frozen=True)
class Deploy:
    service: str
    sha: str
    deployed_at: datetime
    pr: int
    title: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Deploy":
        return cls(
            service=raw["service"],
            sha=raw["sha"],
            deployed_at=parse_ts(raw["deployed_at"]),
            pr=int(raw["pr"]),
            title=raw.get("title"),
        )

    def __str__(self) -> str:
        title = f" '{self.title}'" if self.title else ""
        return f"{self.service}@{self.sha} (PR #{self.pr}{title}) at {fmt_ts(self.deployed_at)}"


@dataclass(frozen=True)
class AcceptedMessage:
    """The platform's promise: ingest returned 202, so this must be delivered."""

    correlation_id: str
    message_type: str
    tenant_id: str
    accepted_at: datetime

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "AcceptedMessage":
        return cls(
            correlation_id=raw["correlation_id"],
            message_type=raw["message_type"],
            tenant_id=raw["tenant_id"],
            accepted_at=parse_ts(raw["accepted_at"]),
        )


@dataclass(frozen=True)
class Symptom:
    source: str
    text: str


@dataclass
class Dataset:
    spans: list[Span] = field(default_factory=list)
    logs: list[LogRecord] = field(default_factory=list)
    deploys: list[Deploy] = field(default_factory=list)
    accepted: list[AcceptedMessage] = field(default_factory=list)
    symptoms: list[Symptom] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._spans_by_corr: dict[str, list[Span]] | None = None
        self._logs_by_corr: dict[str, list[LogRecord]] | None = None

    @property
    def spans_by_correlation(self) -> dict[str, list[Span]]:
        if self._spans_by_corr is None:
            index: dict[str, list[Span]] = {}
            for span in self.spans:
                cid = span.correlation_id
                if cid:
                    index.setdefault(cid, []).append(span)
            for group in index.values():
                group.sort(key=lambda s: (s.start_time, s.span_id))
            self._spans_by_corr = index
        return self._spans_by_corr

    @property
    def logs_by_correlation(self) -> dict[str, list[LogRecord]]:
        if self._logs_by_corr is None:
            index: dict[str, list[LogRecord]] = {}
            for log in self.logs:
                cid = log.correlation_id
                if cid:
                    index.setdefault(cid, []).append(log)
            for group in index.values():
                group.sort(key=lambda r: r.timestamp)
            self._logs_by_corr = index
        return self._logs_by_corr

    @property
    def window(self) -> tuple[datetime, datetime]:
        stamps = [s.start_time for s in self.spans] + [r.timestamp for r in self.logs]
        return min(stamps), max(stamps)

    def accepted_by_correlation(self) -> dict[str, AcceptedMessage]:
        return {m.correlation_id: m for m in self.accepted}

    def deploys_for(self, service: str) -> list[Deploy]:
        return sorted(
            (d for d in self.deploys if d.service == service), key=lambda d: d.deployed_at
        )
