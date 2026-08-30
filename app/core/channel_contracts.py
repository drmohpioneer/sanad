"""Provider-neutral transport contracts for the additive S23 seams.

This is a leaf module: it knows no provider, database, route, or specialist.
Legacy messages are bridged into these shapes at the adapter boundary while the
legacy runtime remains authoritative.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, StrictBool, model_validator


SCHEMA_VERSION = "2.0"


class SignatureVerdict(str, Enum):
    VERIFIED = "VERIFIED"
    SYNTHETIC = "SYNTHETIC"
    REJECTED = "REJECTED"


class CommandStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CONFLICT = "CONFLICT"
    RETRYABLE = "RETRYABLE"


class DeliveryOutcome(str, Enum):
    DELIVERED = "DELIVERED"
    ACCEPTED_BY_PROVIDER = "ACCEPTED_BY_PROVIDER"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    UNKNOWN = "UNKNOWN"


class NotificationClass(str, Enum):
    DANGER = "DANGER"
    URGENT_SLA = "URGENT_SLA"
    REVIEW_READY = "REVIEW_READY"
    DEADLINE_OUTCOME = "DEADLINE_OUTCOME"
    SILENT_WORK = "SILENT_WORK"
    SOLICITED_RESPONSE = "SOLICITED_RESPONSE"
    LEGACY_UNCLASSIFIED = "LEGACY_UNCLASSIFIED"


class OutboxState(str, Enum):
    SHADOW = "SHADOW"
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    TERMINAL = "TERMINAL"


class ActorRef(BaseModel):
    kind: Literal["doctor", "patient", "system", "task", "unknown"]
    id: str = Field(min_length=1)


class InboundAttachment(BaseModel):
    kind: Literal["audio", "image", "document", "other"]
    mime: str = "application/octet-stream"
    size: int = Field(default=0, ge=0)
    sha256: str = ""
    storage_ref: str = ""
    # A bridge may carry bytes for the existing handler, but durable dumps and
    # model prompts can never contain them.
    inline_bytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)


class OutboundAttachment(BaseModel):
    kind: Literal["image", "document", "other"]
    mime: str = "application/octet-stream"
    storage_ref: str = Field(min_length=1)
    name: str = ""


class InboundEnvelope(BaseModel):
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    provider: str = Field(min_length=1)
    provider_account: str = Field(min_length=1)
    provider_message_id: str = Field(min_length=1)
    received_at: datetime
    signature_verdict: SignatureVerdict
    tenant_id: str = Field(min_length=1)
    actor: ActorRef
    principal: ActorRef
    endpoint_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    reply_to_action: Optional[str] = None
    text: Optional[str] = None
    attachments: tuple[InboundAttachment, ...] = ()
    ordering_key: Optional[str] = None
    consent: dict[str, Any] = Field(default_factory=dict)
    identity: dict[str, Any] = Field(default_factory=dict)
    raw_payload_ref: str = Field(min_length=1)
    synthetic: StrictBool
    # Provider adapters may bridge their already-authenticated request object
    # to a legacy handler.  It is deliberately absent from every durable dump,
    # canonical replay value, prompt, and repr.
    transient_payload: Any = Field(default=None, exclude=True, repr=False)

    def canonical_content(self) -> dict[str, Any]:
        """The complete normalized envelope, with inline bytes excluded."""
        return self.model_dump(mode="python")


class Command(BaseModel):
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    actor: ActorRef
    principal: ActorRef
    source: str = Field(min_length=1)
    endpoint_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    expected_version: Optional[int] = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    synthetic: StrictBool
    # The live envelope lets a claimed handler reach transient provider data
    # and excluded attachment bytes.  Replay/persistence use model dumps, where
    # this field is unconditionally absent.
    envelope: Optional[InboundEnvelope] = Field(
        default=None,
        exclude=True,
        repr=False,
    )


class CommandResult(BaseModel):
    status: CommandStatus
    code: str = Field(min_length=1)
    detail: str = ""
    value: dict[str, Any] = Field(default_factory=dict)
    # A live edge may need to preserve a byte-neutral legacy response while
    # the durable replay projection replaces provider-owned identifiers with
    # opaque references.  This copy never enters repr, model dumps, receipts,
    # outbox observations, or any reconstructed completed replay.
    transient_legacy_body: Optional[dict[str, Any]] = Field(
        default=None,
        exclude=True,
        repr=False,
    )

    @classmethod
    def accepted(cls, code: str = "accepted", **value: Any) -> "CommandResult":
        return cls(status=CommandStatus.ACCEPTED, code=code, value=value)

    @classmethod
    def rejected(cls, code: str, detail: str = "") -> "CommandResult":
        return cls(status=CommandStatus.REJECTED, code=code, detail=detail)

    @classmethod
    def conflict(cls, code: str = "in_flight", detail: str = "") -> "CommandResult":
        return cls(status=CommandStatus.CONFLICT, code=code, detail=detail)

    @classmethod
    def retryable(cls, code: str, detail: str = "") -> "CommandResult":
        return cls(status=CommandStatus.RETRYABLE, code=code, detail=detail)

    def as_http(self) -> dict[str, Any]:
        return {
            **self.value,
            "ok": self.status == CommandStatus.ACCEPTED,
            "status": self.status.value,
            "code": self.code,
            "detail": self.detail,
        }


class OutboundIntent(BaseModel):
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    id: str = Field(min_length=1)
    synthetic: StrictBool
    doctor_id: str = Field(min_length=1)
    recipient_type: Literal["doctor", "patient"]
    recipient_id: str = Field(min_length=1)
    patient_id: Optional[str] = None
    notification_class: NotificationClass
    text: str
    card: Optional[dict[str, Any]] = None
    meta: dict[str, Any] = Field(default_factory=dict)
    attachments: tuple[OutboundAttachment, ...] = ()
    idempotency_key: str = Field(min_length=1)
    stable_idempotency: StrictBool
    created_at: datetime


def outbound_content_hash(intent: OutboundIntent) -> str:
    """Canonical content identity; transport timestamps and row ids are not content."""
    body = intent.model_dump(mode="json", exclude={"id", "created_at"})
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class OutboxRecord(BaseModel):
    id: str = Field(min_length=1)
    doctor_id: str = Field(min_length=1)
    intent: OutboundIntent
    content_hash: str = Field(min_length=64, max_length=64)
    state: OutboxState = OutboxState.SHADOW
    delivery: DeliveryOutcome = DeliveryOutcome.UNKNOWN
    provider: str = ""
    provider_receipt_ref: str = ""
    attempts: int = Field(default=0, ge=0)
    error: str = ""
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def consistent_identity(self) -> "OutboxRecord":
        """Reject cross-tenant or corrupt rows before they reach the store."""
        if self.id != self.intent.id:
            raise ValueError("outbox record id must equal intent id")
        if self.doctor_id != self.intent.doctor_id:
            raise ValueError("outbox record doctor_id must equal intent doctor_id")
        if self.content_hash != outbound_content_hash(self.intent):
            raise ValueError("outbox content_hash does not match intent content")
        return self


class DeliveryReceipt(BaseModel):
    provider: str = Field(min_length=1)
    outcome: DeliveryOutcome
    provider_receipt_ref: str = ""
    detail: str = ""
