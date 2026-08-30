"""Owns the channel boundary: how a message reaches Sanad and how a reply leaves.

Two implementations, one interface. The web console is the doctor's board and
always gets a copy of everything, because the event log is what the console
polls and what a judge reads afterwards. Telegram is the phone, and it only
delivers when that person has actually bound a chat.

`fanout()` returns both, so nothing above this file has to know which channels a
patient or doctor happens to have.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, Field, StrictBool

from . import events, store, telegram


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
        await telegram.send_card(chat_id, msg.text, msg.card)
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

    @staticmethod
    def _name(channel: Any) -> str:
        """What this channel is called in a receipt. Never raises on a double."""
        return str(getattr(channel, "name", "") or type(channel).__name__)

    async def send(self, target_ref: str, msg: OutboundMessage
                   ) -> Optional[str]:
        """The receipt of the console event, when there was one.

        Only the web channel writes an event, so only it can answer with an id.
        Telegram is a delivery and not a record, and a phone that is not bound
        is a silent no-op, so neither ever changes what comes back from here.
        """
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


def fanout() -> Fanout:
    return Fanout()


async def resolve_ref(ref: str) -> tuple[Optional[str], Optional[str]]:
    """'doctor:<token>' | 'patient:<id>' -> (doctor_id, patient_id)."""
    kind, _, value = ref.partition(":")
    if kind == "doctor":
        doctor = await store.doctor_by_token(value)
        return (doctor.id, None) if doctor else (None, None)
    if kind == "patient":
        patient = await store.get_patient(value)
        return (patient.doctor_id, patient.id) if patient else (None, None)
    return (None, None)
