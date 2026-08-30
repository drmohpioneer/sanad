"""Production composition helpers for Gate 2's canonical ingress path.

The registered adapters normalize authenticated, already-admitted edge input.
They may resolve identity with read-only store calls, but they never invoke a
specialist or mutate domain state. Raw uploads, Telegram updates, task payloads,
and provider credentials are attached only to excluded transient fields; the
canonical command contains internal identities and safe references.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import store
from .adapters import InboundMessage
from .channel_contracts import (
    ActorRef,
    Command,
    CommandResult,
    InboundAttachment,
    InboundEnvelope,
    SignatureVerdict,
)
from .command_bus import CommandBus, Handler, ReplayClaim
from .command_replay import DurableReplayLedger
from .transport_runtime import InjectedChannelAdapter, TransportRuntime


MESSAGE = "LEGACY_MESSAGE"
ACTION = "LEGACY_ACTION"
TELEGRAM_UPDATE = "LEGACY_TELEGRAM_UPDATE"
NUDGE = "LEGACY_NUDGE"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def opaque(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def ingress_digest(raw: Mapping[str, Any]) -> str:
    """Hash the admitted provider body without retaining any of its values."""
    encoded = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class WebContext:
    provider_message_id: str
    tenant_id: str
    actor: ActorRef
    principal: ActorRef
    endpoint_id: str
    thread_id: str
    received_at: datetime = field(default_factory=utc_now)
    identity_method: str = "authenticated_web"
    consent: Mapping[str, Any] = field(default_factory=dict)
    transient_payload: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class TelegramContext:
    base_url: str
    secret_token: str = field(repr=False)
    received_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class TelegramInvocation:
    update: Mapping[str, Any] = field(repr=False)
    base_url: str
    secret_token: str = field(repr=False)


@dataclass(frozen=True)
class TaskContext:
    task_name: str = field(repr=False)
    received_at: datetime = field(default_factory=utc_now)
    verified_provider: bool = True


def _attachment(kind: str, mime: str, raw: bytes) -> InboundAttachment:
    digest = hashlib.sha256(raw).hexdigest()
    return InboundAttachment(
        kind=kind,
        mime=mime or "application/octet-stream",
        size=len(raw),
        sha256=digest,
        storage_ref=f"inline:{digest}",
        inline_bytes=raw,
    )


async def normalize_web(raw: object, context: Any) -> InboundEnvelope:
    if not isinstance(raw, InboundMessage) or not isinstance(context, WebContext):
        raise TypeError("web ingress requires InboundMessage and WebContext")
    attachments: list[InboundAttachment] = []
    if raw.audio_bytes is not None:
        attachments.append(_attachment("audio", raw.mime or "audio/mpeg", raw.audio_bytes))
    if raw.image_bytes is not None:
        attachments.append(_attachment("image", raw.mime or "image/jpeg", raw.image_bytes))
    return InboundEnvelope(
        provider="web",
        provider_account="sanad-web",
        provider_message_id=context.provider_message_id,
        received_at=context.received_at,
        signature_verdict=SignatureVerdict.SYNTHETIC,
        tenant_id=context.tenant_id,
        actor=context.actor,
        principal=context.principal,
        endpoint_id=context.endpoint_id,
        thread_id=context.thread_id,
        text=raw.text,
        attachments=tuple(attachments),
        consent=dict(context.consent),
        identity={"method": context.identity_method, "verified": True},
        raw_payload_ref=f"web:request:{context.provider_message_id}",
        synthetic=raw.synthetic,
        transient_payload=(
            raw if context.transient_payload is None else context.transient_payload
        ),
    )


def _telegram_message(update: Mapping[str, Any]) -> Mapping[str, Any]:
    held = update.get("message") or update.get("edited_message") or {}
    return held if isinstance(held, Mapping) else {}


def _telegram_chat(update: Mapping[str, Any]) -> Optional[int]:
    callback = update.get("callback_query")
    if isinstance(callback, Mapping):
        message = callback.get("message") or {}
    else:
        message = _telegram_message(update)
    chat = message.get("chat") if isinstance(message, Mapping) else {}
    value = chat.get("id") if isinstance(chat, Mapping) else None
    return value if isinstance(value, int) else None


def _telegram_attachments(message: Mapping[str, Any]) -> tuple[InboundAttachment, ...]:
    part: Optional[Mapping[str, Any]] = None
    kind = "other"
    mime = "application/octet-stream"
    voice = message.get("voice") or message.get("audio")
    photos = message.get("photo") or []
    if isinstance(voice, Mapping):
        part, kind = voice, "audio"
        mime = str(voice.get("mime_type") or "audio/ogg")
    elif isinstance(photos, (list, tuple)) and photos and isinstance(photos[-1], Mapping):
        part, kind, mime = photos[-1], "image", "image/jpeg"
    if part is None:
        return ()
    file_id = str(part.get("file_id") or "missing")
    return (
        InboundAttachment(
            kind=kind,
            mime=mime,
            size=max(0, int(part.get("file_size") or 0)),
            storage_ref=opaque("telegram:file", file_id),
        ),
    )


async def normalize_telegram(raw: object, context: Any) -> InboundEnvelope:
    if not isinstance(raw, Mapping) or not isinstance(context, TelegramContext):
        raise TypeError("Telegram ingress requires an update and TelegramContext")
    update_id = str(raw.get("update_id") or "").strip()
    if not update_id:
        raise ValueError("Telegram update_id is required for replay protection")
    chat_id = _telegram_chat(raw)
    endpoint = opaque("telegram:endpoint", chat_id if chat_id is not None else "unknown")

    doctor = await store.doctor_by_telegram(chat_id) if chat_id is not None else None
    patient = None
    if doctor is None and chat_id is not None:
        patient = await store.patient_by_telegram(chat_id)
    if doctor is not None:
        tenant_id = doctor.id
        actor = ActorRef(kind="doctor", id=doctor.id)
        identity_method = "telegram_doctor_binding"
    elif patient is not None:
        tenant_id = patient.doctor_id
        actor = ActorRef(kind="patient", id=patient.id)
        identity_method = "telegram_patient_binding"
    else:
        tenant_id = opaque("telegram:unbound", endpoint)
        actor = ActorRef(kind="unknown", id=endpoint)
        identity_method = "unbound_telegram_chat"

    callback = raw.get("callback_query")
    reply_to_action = ""
    if isinstance(callback, Mapping):
        reply_to_action = str(callback.get("data") or "")
    message = _telegram_message(raw)
    text = str(message.get("text") or message.get("caption") or "").strip()
    if text.startswith("/start"):
        # The one-time link token remains available only in transient_payload.
        text = "/start"
    verdict = SignatureVerdict.VERIFIED
    return InboundEnvelope(
        provider="telegram",
        provider_account="sanad-bot",
        provider_message_id=update_id,
        received_at=context.received_at,
        signature_verdict=verdict,
        tenant_id=tenant_id,
        actor=actor,
        principal=actor,
        endpoint_id=endpoint,
        thread_id=endpoint,
        reply_to_action=reply_to_action or None,
        text=text or None,
        attachments=_telegram_attachments(message),
        ordering_key=update_id,
        identity={"method": identity_method, "verified": True},
        raw_payload_ref=f"telegram:update:{update_id}",
        synthetic=verdict != SignatureVerdict.VERIFIED,
        transient_payload=TelegramInvocation(
            update=raw,
            base_url=context.base_url,
            secret_token=context.secret_token,
        ),
    )


def task_identity(raw: Mapping[str, Any], supplied: str) -> str:
    if supplied.strip():
        return opaque("cloud_tasks:name", supplied.strip())
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return opaque("cloud_tasks:payload", encoded)


def durable_task_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Replace provider-owned task names in a persisted result with references.

    The live route still projects the exact legacy result through
    ``CommandResult.transient_legacy_body``.  A reconstructed completed replay
    intentionally has only this provider-neutral projection.
    """
    def sanitize(value: Any, key: str = "") -> Any:
        if (
            key.strip().lower() in {"task", "task_name"}
            and isinstance(value, str)
            and value.strip()
        ):
            return opaque("cloud_tasks:task", value.strip())
        if isinstance(value, Mapping):
            return {
                str(name): sanitize(item, str(name))
                for name, item in value.items()
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(sanitize(item) for item in value)
        return value

    return sanitize(dict(raw))


async def normalize_cloud_task(raw: object, context: Any) -> InboundEnvelope:
    if not isinstance(raw, Mapping) or not isinstance(context, TaskContext):
        raise TypeError("Cloud Tasks ingress requires a payload and TaskContext")
    task_key = task_identity(raw, context.task_name)
    loop_id = str(raw.get("loop_id") or "")
    loop = await store.get_loop(loop_id) if loop_id else None
    tenant_id = loop.doctor_id if loop is not None else "unresolved-cloud-task"
    actor_id = opaque("cloud_tasks:task", task_key)
    verdict = (
        SignatureVerdict.VERIFIED
        if context.verified_provider
        else SignatureVerdict.SYNTHETIC
    )
    return InboundEnvelope(
        provider="cloud_tasks",
        provider_account=(
            "sanad-chase" if context.verified_provider else "sanad-inprocess"
        ),
        provider_message_id=task_key,
        received_at=context.received_at,
        signature_verdict=verdict,
        tenant_id=tenant_id,
        actor=ActorRef(kind="task", id=actor_id),
        principal=ActorRef(kind="system", id="cloud-tasks"),
        endpoint_id="cloud_tasks:sanad-chase",
        thread_id=f"loop:{loop_id}" if loop_id else actor_id,
        ordering_key=task_key,
        identity={
            "method": (
                "google_oidc"
                if context.verified_provider
                else "trusted_inprocess_scheduler"
            ),
            "verified": context.verified_provider,
        },
        raw_payload_ref=opaque("cloud_tasks:request", task_key),
        synthetic=True if loop is None else loop.synthetic,
        transient_payload=dict(raw),
    )


async def authorize(command: Command) -> Optional[CommandResult]:
    """Validate normalization invariants again before replay or domain work."""
    envelope = command.envelope
    if envelope is None:
        return CommandResult.rejected("missing_envelope")
    if (
        command.source != envelope.provider
        or command.tenant_id != envelope.tenant_id
        or command.actor != envelope.actor
        or command.principal != envelope.principal
        or command.endpoint_id != envelope.endpoint_id
        or command.thread_id != envelope.thread_id
        or command.synthetic is not envelope.synthetic
        or command.payload.get("envelope") != envelope.canonical_content()
    ):
        return CommandResult.rejected("normalization_mismatch")
    if envelope.signature_verdict == SignatureVerdict.REJECTED:
        return CommandResult.rejected("unverified_ingress")
    if command.source == "web":
        if envelope.signature_verdict != SignatureVerdict.SYNTHETIC or not command.synthetic:
            return CommandResult.rejected("invalid_web_provenance")
    elif command.source == "telegram":
        if envelope.signature_verdict != SignatureVerdict.VERIFIED:
            return CommandResult.rejected("unverified_provider")
    elif command.source == "cloud_tasks":
        if envelope.signature_verdict not in {
            SignatureVerdict.VERIFIED,
            SignatureVerdict.SYNTHETIC,
        }:
            return CommandResult.rejected("unverified_provider")
    else:
        return CommandResult.rejected("unknown_provider")
    return None


class Gate2ReplayLedger:
    """Durable replay where a provider supplies a stable delivery identity.

    Browser form submissions receive a new server command id and retain their
    existing domain claims (notably action ids). Telegram update ids and Cloud
    Task names are provider retry identities, so those claims are durable.
    """

    DURABLE_SOURCES = frozenset({"telegram", "cloud_tasks"})

    def __init__(self, durable: Optional[DurableReplayLedger] = None) -> None:
        self._durable = durable or DurableReplayLedger()

    def _uses_store(self, command: Command) -> bool:
        return command.source in self.DURABLE_SOURCES

    async def claim(self, command: Command) -> ReplayClaim:
        if not self._uses_store(command):
            return ReplayClaim(state="CLAIMED")
        return await self._durable.claim(command)

    async def complete(self, command: Command, result: CommandResult) -> None:
        if self._uses_store(command):
            await self._durable.complete(command, result)

    async def release(self, command: Command) -> None:
        if self._uses_store(command):
            await self._durable.release(command)


def build(handlers: Mapping[str, Handler]) -> TransportRuntime:
    required = {MESSAGE, ACTION, TELEGRAM_UPDATE, NUDGE}
    missing = required - set(handlers)
    if missing:
        raise ValueError("live transport is missing handlers: " + ", ".join(sorted(missing)))
    bus = CommandBus(
        handlers={kind: handlers[kind] for kind in sorted(required)},
        authorizer=authorize,
        replay=Gate2ReplayLedger(),
    )
    return TransportRuntime(
        bus=bus,
        adapters=(
            InjectedChannelAdapter("web", normalize_web),
            InjectedChannelAdapter("telegram", normalize_telegram),
            InjectedChannelAdapter("cloud_tasks", normalize_cloud_task),
        ),
    )
