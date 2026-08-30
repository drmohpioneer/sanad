"""Shadow-only durable observation of legacy outbound messages.

Gate 2 never dispatches these records. It records a provider-neutral intent for
comparison while the existing Fanout remains the only sender.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from . import store
from .channel_contracts import (
    DeliveryOutcome,
    NotificationClass,
    OutboundIntent,
    OutboxRecord,
    OutboxState,
    outbound_content_hash,
)


_OUTBOX_NAMESPACE = uuid.UUID("694305c4-004d-5a65-b64f-b5814e874679")
_SENSITIVE_KEYS = ("token", "secret", "password", "chat_id", "raw_payload", "bytes")
_START_TOKEN = re.compile(r"([?&]start=)[^&\s]+", re.IGNORECASE)
_PATH_TOKEN = re.compile(r"(/(?:c|p|qr)/)[A-Za-z0-9_-]+(?:\.png)?", re.IGNORECASE)


def _redact_text(value: str) -> str:
    value = _START_TOKEN.sub(r"\1[REDACTED]", value)
    return _PATH_TOKEN.sub(r"\1[REDACTED]", value)


def _safe(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_KEYS):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            str(name): cleaned
            for name, item in value.items()
            if (cleaned := _safe(item, str(name))) is not None
        }
    if isinstance(value, (list, tuple)):
        return [cleaned for item in value if (cleaned := _safe(item)) is not None]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return _safe(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    # Arbitrary object reprs can contain credentials or raw binary. The shadow
    # ledger keeps only the JSON-like values the legacy message intentionally
    # exposed; unknown objects fail closed instead of being stringified.
    return None


def legacy_intent(
    doctor_id: str,
    recipient_type: str,
    recipient_id: str,
    message: Any,
    *,
    contextual_patient_id: Optional[str] = None,
    synthetic: bool = True,
    now: Optional[datetime] = None,
) -> OutboundIntent:
    """Convert one already-decided legacy message without retaining credentials."""
    if recipient_type not in {"doctor", "patient"}:
        raise ValueError("legacy recipient_type must be doctor or patient")
    receipt = str(getattr(message, "receipt", "") or "").strip()
    stable = bool(receipt)
    if stable:
        ident = uuid.uuid5(
            _OUTBOX_NAMESPACE,
            ":".join((doctor_id, recipient_type, recipient_id, receipt)),
        ).hex
        key = receipt
    else:
        ident = uuid.uuid4().hex
        key = f"unstable:{ident}"
    at = now or datetime.now(timezone.utc)
    return OutboundIntent(
        id=ident,
        synthetic=synthetic,
        doctor_id=doctor_id,
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        patient_id=contextual_patient_id,
        notification_class=NotificationClass.LEGACY_UNCLASSIFIED,
        text=_redact_text(str(getattr(message, "text", "") or "")),
        card=_safe(getattr(message, "card", None)),
        meta=_safe(getattr(message, "meta", {}) or {}),
        idempotency_key=key,
        stable_idempotency=stable,
        created_at=at,
    )


def content_hash(intent: OutboundIntent) -> str:
    return outbound_content_hash(intent)


async def record_shadow(intent: OutboundIntent) -> OutboxRecord:
    """Persist one non-dispatchable record; this function never sends."""
    record = OutboxRecord(
        id=intent.id,
        doctor_id=intent.doctor_id,
        intent=intent,
        content_hash=content_hash(intent),
        state=OutboxState.SHADOW,
        delivery=DeliveryOutcome.UNKNOWN,
        created_at=intent.created_at,
        updated_at=intent.created_at,
    )
    await store.create_outbound_intent(record)
    return record
