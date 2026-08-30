"""Owns the channel boundary: how a message reaches Sanad and how a reply leaves.

Two implementations, one interface. The web console is the doctor's board and
always gets a copy of everything, because the event log is what the console
polls and what a judge reads afterwards. Telegram is the phone, and it only
delivers when that person has actually bound a chat.

`fanout()` returns both, so nothing above this file has to know which channels a
patient or doctor happens to have.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional, Protocol, TypeVar

from pydantic import BaseModel, Field, StrictBool

from . import events, outbox, runtime, store, telegram
from .channel_contracts import DeliveryOutcome, DeliveryReceipt


log = logging.getLogger("sanad.adapters")


# Shadow observation is bounded per process. These tasks never dispatch work;
# the set exists only so a slow ledger cannot grow without limit while the
# legacy sender remains authoritative.
_SHADOW_TASKS: set[asyncio.Task[None]] = set()
_ProviderResponse = TypeVar("_ProviderResponse")


@dataclass(frozen=True)
class ResolvedTarget:
    doctor_id: str
    patient_id: Optional[str]
    synthetic: bool


class InboundMessage(BaseModel):
    channel: Literal["web", "telegram"]
    # False is privileged: only main.telegram_webhook may originate it after
    # the provider webhook secret has verified. Missing/internal input is
    # synthetic by default.
    synthetic: StrictBool = True
    sender_ref: str  # "doctor:<web_token>" or "patient:<patient_id>"
    text: Optional[str] = None
    audio_bytes: Optional[bytes] = None
    image_bytes: Optional[bytes] = None
    mime: Optional[str] = None


class OutboundMessage(BaseModel):
    text: str
    card: Optional[dict[str, Any]] = None  # {"title","severity","lines","actions"}
    meta: dict[str, Any] = Field(default_factory=dict)  # audit trail, stored as-is
    # Which patient this is about, when the target is the doctor (rev 17 item
    # 15). A doctor is addressed by his own token, so `resolve_ref` can only
    # answer (doctor_id, None) and every card a doctor ever received was
    # written with no patient on it: live, that is why the Inbox could not
    # offer "Open patient" on a card that named one. Sends to a patient do not
    # need it, because the ref already carries the patient's id.
    patient_id: Optional[str] = None
    # The send record this message belongs to, when it has one (codex re-audit
    # 5). Only core/chaser.py sets it, because only the Chaser's messages have
    # a receipt in Firestore to hang a per-channel result on. With it, a retry
    # of a half-finished fan-out re-delivers only on the channel that failed;
    # without it, the fan-out still remembers within its own instance.
    receipt: str = ""


class ChannelAdapter(Protocol):
    name: str

    async def send(self, target_ref: str, msg: OutboundMessage
                   ) -> Optional[str]: ...


class WebAdapter:
    """Delivery for the web console: append an event, let the poller find it.

    The console polls /c/{token}/feed every 2s, so "sending" is just writing to
    the event log. No websockets, no push, nothing to keep alive between requests.

    It returns the id of the event it wrote, which is the receipt for that one
    message (rev 18 item 2). A caller that has just answered a patient can then
    write that id onto its own event instead of the board having to guess later
    which message went with which decision, which it got wrong: the answer is
    always written a few hundred milliseconds BEFORE the event that explains it,
    so a search forward in time from the explanation finds the next message and
    never the right one.
    """

    name = "web"

    async def send(self, target_ref: str, msg: OutboundMessage
                   ) -> Optional[str]:
        doctor_id, patient_id = await resolve_ref(target_ref)
        if doctor_id is None:
            return None
        # A doctor-bound message says who it is about; a patient-bound one is
        # already about the patient in its own ref, which always wins.
        patient_id = patient_id or msg.patient_id
        meta = dict(msg.meta)
        if msg.card:
            meta["card"] = msg.card
        event = await events.append_event(
            doctor_id,
            "card" if msg.card else "agent_out",
            msg.text,
            patient_id=patient_id,
            channel="web",
            meta=meta,
        )
        return event.id


class TelegramAdapter:
    """Delivery to a bound phone. A ref with no chat id is a silent no-op."""

    name = "telegram"

    async def send(self, target_ref: str, msg: OutboundMessage
                   ) -> Optional[str]:
        chat_id = await telegram.chat_id_for(target_ref)
        if chat_id is None or not telegram.enabled():
            return None
        response = await telegram.send_card(chat_id, msg.text, msg.card)
        if not isinstance(response, dict) or response.get("ok") is not True:
            # A 200 response with {ok:false} is a rejected provider operation,
            # not proof of delivery. Raising preserves the legacy retry path
            # and, critically, prevents Fanout from marking this channel done.
            raise RuntimeError("Telegram provider rejected send")
        return None


class Fanout:
    """Every reply goes to the console feed, and to Telegram when it is bound.

    codex re-audit 5. The channels were delivered in order and a channel that
    threw took the whole fan-out down with it, so the one retry the Chaser
    allows re-ran every channel, including the ones that had already delivered.
    Live that meant a Telegram outage put a second copy of one reminder in the
    doctor's console feed, and the console feed is the record a judge reads.

    A delivery is remembered per channel now, in two places for two different
    lifetimes. On this object, so that calling `send` twice on one fan-out is
    one delivery per channel whatever the caller is doing. And on the send
    record in Firestore when the message carries a receipt, which is what makes
    it true across a retry in another process a minute later.
    """

    def __init__(self) -> None:
        self.channels: tuple[ChannelAdapter, ...] = (WebAdapter(), TelegramAdapter())
        self._delivered: set[tuple[str, str, str]] = set()
        # Receiptless legacy messages have no durable idempotency key. Keep a
        # process-local observation key so calling this same Fanout twice does
        # not invent two shadow intents for one suppressed legacy delivery.
        self._shadowed_unstable: set[tuple[str, str, str, str]] = set()

    @staticmethod
    def _name(channel: Any) -> str:
        """What this channel is called in a receipt. Never raises on a double."""
        return str(getattr(channel, "name", "") or type(channel).__name__)

    async def _record_shadow(self, target_ref: str, msg: OutboundMessage) -> None:
        """Observe a legacy send before delivery, without becoming a sender.

        Shadow persistence is diagnostic and deliberately best-effort: a store
        outage must not suppress or duplicate the legacy channel call. Stable
        receipts are de-duplicated durably by the store; receiptless messages
        can only be de-duplicated within this Fanout instance.
        """
        try:
            target = await resolve_target(target_ref)
            if target is None:
                return
            kind, _, _ = target_ref.partition(":")
            if kind == "doctor":
                recipient_type = "doctor"
                recipient_id = target.doctor_id
            elif kind == "patient" and target.patient_id is not None:
                recipient_type = "patient"
                recipient_id = target.patient_id
            else:
                return

            ticket = (msg.receipt or "").strip()
            unstable_key = (
                target.doctor_id,
                recipient_type,
                recipient_id,
                msg.text,
            )
            if not ticket and unstable_key in self._shadowed_unstable:
                return

            patient_id = target.patient_id
            synthetic = target.synthetic
            if kind == "doctor" and msg.patient_id:
                context = await store.get_patient(msg.patient_id)
                if context is None or context.doctor_id != target.doctor_id:
                    # An unresolved or cross-tenant context fails closed. It is
                    # never allowed to make an observed record look real.
                    patient_id = None
                    synthetic = True
                else:
                    patient_id = context.id
                    synthetic = synthetic or context.synthetic
            intent = outbox.legacy_intent(
                target.doctor_id,
                recipient_type,
                recipient_id,
                msg,
                contextual_patient_id=patient_id,
                synthetic=synthetic,
            )
            await outbox.record_shadow(intent)
            if not ticket:
                self._shadowed_unstable.add(unstable_key)
        except Exception:  # noqa: BLE001 - observation cannot stop legacy send
            log.exception("shadow outbox observation failed; legacy delivery continues")

    def _start_shadow(
        self, target_ref: str, msg: OutboundMessage
    ) -> Optional[tuple[asyncio.Task[None], float]]:
        """Start one bounded observation without changing legacy behavior."""
        try:
            if runtime.outbox_mode() != "shadow":
                return None
            timeout = runtime.shadow_timeout_seconds()
            capacity = runtime.shadow_max_in_flight()
        except Exception:  # startup normally rejects this; request path fails open
            log.exception("shadow outbox configuration invalid; observation skipped")
            return None

        # Done callbacks normally prune immediately. The comprehension also
        # protects tests and unusual loop shutdown paths where callbacks have
        # not yet had another event-loop turn.
        _SHADOW_TASKS.difference_update(
            task for task in tuple(_SHADOW_TASKS) if task.done()
        )
        if len(_SHADOW_TASKS) >= capacity:
            log.warning("shadow outbox capacity_drop; legacy delivery continues")
            return None

        loop = asyncio.get_running_loop()
        task = loop.create_task(self._record_shadow(target_ref, msg))
        _SHADOW_TASKS.add(task)
        task.add_done_callback(_SHADOW_TASKS.discard)
        return task, loop.time() + timeout

    @staticmethod
    async def _finish_shadow(
        observation: Optional[tuple[asyncio.Task[None], float]],
    ) -> None:
        """Consume, bound, and clean up one best-effort observation task."""
        if observation is None:
            return
        task, deadline = observation
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            if task.done():
                await task
            elif remaining > 0:
                await asyncio.wait_for(task, timeout=remaining)
            else:
                task.cancel()
                await task
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            log.warning("shadow outbox timeout; legacy delivery unchanged")
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                task.cancel()
                raise
            # Cancellation after our explicit deadline is cleanup. If the
            # observation was independently cancelled, it is still never a
            # reason to relabel the authoritative delivery.
            pass
        except Exception:  # defensive: _record_shadow already contains errors
            log.exception("shadow outbox task failed; legacy delivery unchanged")

    async def send(self, target_ref: str, msg: OutboundMessage
                   ) -> Optional[str]:
        """The receipt of the console event, when there was one.

        Only the web channel writes an event, so only it can answer with an id.
        Telegram is a delivery and not a record, and a phone that is not bound
        is a silent no-op, so neither ever changes what comes back from here.
        """
        if not runtime.legacy_runtime():
            raise RuntimeError(
                "the replacement sender is not active at Gate 2; "
                "set LEGACY_RUNTIME=true"
            )
        observation = self._start_shadow(target_ref, msg)
        if observation is not None:
            # Let observation begin, but never wait for recipient lookup or
            # persistence before starting the unchanged legacy path.
            await asyncio.sleep(0)
        try:
            ticket = getattr(msg, "receipt", "") or ""
            # Two identical messages to one person on one fan-out are one message.
            # With a receipt the key is the receipt; without one it is the text,
            # which is the only thing that identifies "the same message" for the
            # paths that have no send record at all.
            key = ticket or msg.text
            done = await store.channels_done(ticket) if ticket else frozenset()

            receipt: Optional[str] = None
            for channel in self.channels:
                name = self._name(channel)
                if name in done or (target_ref, key, name) in self._delivered:
                    continue
                written = await channel.send(target_ref, msg)
                self._delivered.add((target_ref, key, name))
                if ticket:
                    await store.mark_channel_done(ticket, name)
                receipt = receipt or written
            return receipt
        finally:
            await self._finish_shadow(observation)


def fanout() -> Fanout:
    return Fanout()


async def patient_deep_link(token_id: str) -> Optional[str]:
    """Provider-neutral edge used by onboarding domain code."""
    return await telegram.deep_link(token_id)


async def _observed_provider_call(
    target_ref: Optional[str],
    message: OutboundMessage,
    call: Callable[[], Awaitable[_ProviderResponse]],
) -> _ProviderResponse:
    """Observe one direct edge send while invoking its provider exactly once."""
    if not runtime.legacy_runtime():
        raise RuntimeError(
            "the replacement sender is not active at Gate 2; "
            "set LEGACY_RUNTIME=true"
        )
    fan = Fanout()
    observation = fan._start_shadow(target_ref, message) if target_ref else None
    if observation is not None:
        await asyncio.sleep(0)
    try:
        return await call()
    finally:
        await fan._finish_shadow(observation)


def _telegram_receipt(response: object) -> DeliveryReceipt:
    if isinstance(response, dict) and response.get("ok") is True:
        result = response.get("result") or {}
        receipt = str(result.get("message_id") or "") if isinstance(result, dict) else ""
        return DeliveryReceipt(
            provider="telegram",
            outcome=DeliveryOutcome.ACCEPTED_BY_PROVIDER,
            provider_receipt_ref=receipt,
        )
    return DeliveryReceipt(
        provider="telegram",
        outcome=DeliveryOutcome.RETRYABLE_FAILURE,
        detail="provider rejected the operation",
    )


async def send_card(
    endpoint_id: Optional[int],
    text: str,
    card: Optional[dict[str, Any]] = None,
    *,
    target_ref: Optional[str] = None,
    patient_id: Optional[str] = None,
) -> DeliveryReceipt:
    """Typed direct Telegram edge send, optionally shadowed by internal target."""
    if endpoint_id is None:
        return DeliveryReceipt(
            provider="telegram",
            outcome=DeliveryOutcome.UNKNOWN,
            detail="endpoint unavailable",
        )
    response = await _observed_provider_call(
        target_ref,
        OutboundMessage(text=text, card=card, patient_id=patient_id),
        lambda: telegram.send_card(endpoint_id, text, card),
    )
    return _telegram_receipt(response)


async def send_photo(
    endpoint_id: Optional[int],
    image_bytes: bytes,
    *,
    caption: str = "",
    target_ref: Optional[str] = None,
    patient_id: Optional[str] = None,
) -> DeliveryReceipt:
    """Typed QR-image edge send; raw image bytes never enter the shadow intent."""
    if endpoint_id is None or not telegram.enabled():
        return DeliveryReceipt(
            provider="telegram",
            outcome=DeliveryOutcome.UNKNOWN,
            detail="endpoint unavailable",
        )
    response = await _observed_provider_call(
        target_ref,
        OutboundMessage(
            text=caption,
            patient_id=patient_id,
            meta={"artifact": "patient_qr"},
        ),
        lambda: telegram.send_photo(endpoint_id, image_bytes, caption=caption),
    )
    return _telegram_receipt(response)


async def resolve_ref(ref: str) -> tuple[Optional[str], Optional[str]]:
    """'doctor:<token>' | 'patient:<id>' -> (doctor_id, patient_id)."""
    target = await resolve_target(ref)
    if target is None:
        return None, None
    return target.doctor_id, target.patient_id


async def resolve_target(ref: str) -> Optional[ResolvedTarget]:
    """Resolve internal recipient identity and fail-closed provenance."""
    kind, _, value = ref.partition(":")
    if kind == "doctor":
        doctor = await store.doctor_by_token(value)
        if doctor is None:
            return None
        return ResolvedTarget(
            doctor_id=doctor.id,
            patient_id=None,
            synthetic=doctor.synthetic,
        )
    if kind == "patient":
        patient = await store.get_patient(value)
        if patient is None:
            return None
        doctor = await store.doctor_by_id(patient.doctor_id)
        return ResolvedTarget(
            doctor_id=patient.doctor_id,
            patient_id=patient.id,
            synthetic=patient.synthetic or doctor is None or doctor.synthetic,
        )
    return None
