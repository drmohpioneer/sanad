"""Durable, provider-neutral replay claims for :class:`CommandBus`.

The receipt document contains only an opaque deterministic id, a SHA-256
fingerprint, state, a typed result, and timestamps. Raw commands and transport
identifiers never cross the store boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from . import store
from .channel_contracts import Command, CommandResult
from .command_bus import ReplayClaim


_FORBIDDEN_RESULT_KEYS = (
    "attachment_bytes",
    "authorization",
    "chat_id",
    "cookie",
    "endpoint_id",
    "idempotency_key",
    "inline_bytes",
    "provider_secret",
    "raw_payload",
    "thread_id",
    "token",
    "webhook_secret",
)


def _canonical(value: Any) -> Any:
    """JSON-safe canonical content used only as input to SHA-256."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("command fingerprint cannot contain non-finite numbers")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "$type": "bytes_sha256",
            "sha256": hashlib.sha256(bytes(value)).hexdigest(),
        }
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, uuid.UUID):
        return {"$type": "uuid", "value": str(value)}
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise TypeError(
        f"command fingerprint does not accept {type(value).__name__} values"
    )


def command_receipt_id(command: Command) -> str:
    """Opaque UUID5 for one tenant, command kind, and raw idempotency key."""
    digest = command.payload.get("ingress_sha256")
    if (
        command.source in {"telegram", "cloud_tasks"}
        and isinstance(digest, str)
        and len(digest) == 64
    ):
        # Provider delivery identities are global to their provider account.
        # Tenant resolution may legitimately change while handling /start, so
        # it cannot create a second receipt for the same admitted update.
        return store.derived_id(
            "provider-command-receipt",
            command.source,
            command.kind,
            command.idempotency_key,
        )
    return store.derived_id(
        "command-receipt",
        command.tenant_id,
        command.kind,
        command.idempotency_key,
    )


def command_fingerprint(command: Command) -> str:
    """Hash semantic command content without retaining its replay key."""
    body = command.model_dump(
        mode="python",
        exclude={"id", "idempotency_key", "created_at"},
    )
    payload = body.get("payload")
    if isinstance(payload, dict):
        envelope = payload.get("envelope")
        if isinstance(envelope, dict):
            # Receipt time is observation metadata, not provider content. A
            # retry of one update/task must not conflict merely because a new
            # process normalized it a few seconds later.
            envelope.pop("received_at", None)
        digest = payload.get("ingress_sha256")
        if (
            command.source in {"telegram", "cloud_tasks"}
            and isinstance(digest, str)
            and len(digest) == 64
        ):
            # The digest is over the admitted raw provider body. It is the
            # immutable content identity; actor/tenant resolution can change
            # after a successful /start without turning a replay into new work.
            body = {
                "schema_version": body.get("schema_version"),
                "kind": body.get("kind"),
                "source": body.get("source"),
                "expected_version": body.get("expected_version"),
                "payload": {
                    key: value for key, value in payload.items()
                    if key != "envelope"
                },
            }
    encoded = json.dumps(
        _canonical(body),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_persistable_result(value: Any, key: str = "") -> None:
    lowered = key.lower()
    if any(part in lowered for part in _FORBIDDEN_RESULT_KEYS):
        raise ValueError(f"command result contains forbidden transport field: {key}")
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("command result cannot persist attachment bytes")
    if isinstance(value, dict):
        for name, item in value.items():
            _assert_persistable_result(item, str(name))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_persistable_result(item)
        return
    if value is None or isinstance(value, (bool, int, float, str, datetime, Enum)):
        return
    raise TypeError(
        f"command result does not accept {type(value).__name__} values"
    )


def _stored_result(result: CommandResult) -> dict[str, Any]:
    body = result.model_dump(mode="python")
    _assert_persistable_result(body)
    return result.model_dump(mode="json")


class DurableReplayLedger:
    """The :class:`ReplayLedger` protocol backed by ``core.store`` receipts."""

    async def claim(self, command: Command) -> ReplayClaim:
        row = await store.claim_command_receipt(
            command_receipt_id(command),
            command_fingerprint(command),
            datetime.now(timezone.utc),
        )
        state = row.get("state")
        if state == "CLAIMED":
            return ReplayClaim(state="CLAIMED")
        if state == "MISMATCH":
            return ReplayClaim(
                state="IN_FLIGHT",
                result=CommandResult.conflict("idempotency_mismatch"),
            )
        if state == "IN_FLIGHT":
            return ReplayClaim(state="IN_FLIGHT")
        if state == "COMPLETED":
            raw = row.get("result")
            if not isinstance(raw, dict):
                return ReplayClaim(state="COMPLETED")
            return ReplayClaim(
                state="COMPLETED",
                result=CommandResult.model_validate(raw),
            )
        raise RuntimeError(f"unknown command receipt claim state: {state!r}")

    async def complete(self, command: Command, result: CommandResult) -> None:
        await store.complete_command_receipt(
            command_receipt_id(command),
            command_fingerprint(command),
            _stored_result(result),
            datetime.now(timezone.utc),
        )

    async def release(self, command: Command) -> None:
        await store.release_command_receipt(
            command_receipt_id(command),
            command_fingerprint(command),
        )
