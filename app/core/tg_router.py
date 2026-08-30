"""Owns one job: turning a Telegram update into a Sanad inbound message.

Nothing here decides what to say. A chat is looked up in Firestore, the update
is reshaped into the same `InboundMessage` the web console produces, and
core/dispatch.py takes it from there. Buttons carry the same action ids as the
console's buttons, so both surfaces drive identical code.

Unknown chats can do exactly two things: introduce themselves (/start with no
token) or bind a patient (/start with a valid one-time token).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import (
    concierge, dispatch, events, gender, links, registrar, store, telegram,
    uploads,
)
from .adapters import InboundMessage, OutboundMessage, fanout
from .models import PendingStart

log = logging.getLogger("sanad.tg_router")

# What a doctor gets when he taps one of his own patients' deep links. Mohamed
# did exactly that on 2026-08-29: the bot bound his own chat as that patient,
# sent him the patient welcome, and every message he typed afterwards was read
# as the patient's. One line back, no token spent, nothing bound.
DOCTOR_TAPPED_LINK = (
    "That link is for your patient, not for you. Forward it to them: it is "
    "still valid and has not been used."
)

INTRO = (
    "I am Sanad, a doctor's AI assistant. If your doctor sent you a link, open "
    "it so I can recognise you. You can write to me in Arabic."
)


def _too_big(part: dict[str, Any]) -> bool:
    """Telegram's own stated size against core/uploads.MAX_BYTES."""
    return int(part.get("file_size") or 0) > uploads.MAX_BYTES


async def _too_big_reply(chat_id: int, part: dict[str, Any]) -> None:
    """One line back to the chat. Nothing is downloaded and nothing is stored.

    The language is chosen the way it is chosen everywhere on this lane before
    a patient record is in hand: both, one after the other, because a chat that
    has sent nothing but an oversized file has told us nothing else.
    """
    await telegram.send_card(
        chat_id,
        uploads.refusal_text("ar", "u", too_large=True) + "\n"
        + uploads.refusal_text("en", "u", too_large=True))


async def handle_update(update: dict[str, Any], base_url: str) -> None:
    if "callback_query" in update:
        await _callback(update["callback_query"], base_url)
        return
    message = update.get("message") or update.get("edited_message")
    if message:
        await _message(message, base_url)


async def _callback(query: dict[str, Any], base_url: str) -> None:
    chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
    data = query.get("data") or ""
    doctor = await store.doctor_by_telegram(chat_id) if chat_id else None
    note = "not your board"
    if doctor is not None:
        verb, _, ident = data.partition(":")
        if verb == "confirm":
            await registrar.commit(doctor, ident, base_url)
            note = "confirmed"
        elif verb == "cancel":
            await registrar.cancel(doctor, ident)
            note = "cancelled"
        elif verb == "existing":
            # S9: the same three ids the console sends, so a doctor who picks
            # the record on his phone gets the same confirm card he would have
            # got in the browser.
            patient_id, _, confirm_id = ident.partition(":")
            await registrar.choose_existing(doctor, patient_id, confirm_id)
            note = "which record"
        elif verb == "newpatient":
            await registrar.choose_new(doctor, ident)
            note = "new patient"
        elif verb == "openpatient":
            note = "open it on the board"
        elif verb in ("reviewed", "note"):
            # The lab-values card carries the same action ids on both surfaces.
            if verb == "reviewed":
                await concierge.mark_reviewed(doctor, ident)
                note = "closed"
            else:
                await store.update_doctor(
                    doctor.id, awaiting_relay_id=None,
                    awaiting_note_loop_id=ident, awaiting_since=store.now(),
                )
                await telegram.send_card(
                    chat_id, "Send your note as the next message. You have ten "
                    "minutes, and /cancel stops it."
                )
                note = "send your note"
        elif verb == "reply":
            # Firestore holds which card he is answering; the process holds nothing.
            await store.update_doctor(
                doctor.id, awaiting_relay_id=ident, awaiting_since=store.now()
            )
            await telegram.send_card(
                chat_id, "Answering the patient: send your answer as the next "
                "message and it goes to the patient and into the plan. You "
                "have ten "
                "minutes, and /cancel stops it."
            )
            note = "waiting for your answer"
        else:
            note = "unknown button"
    await telegram.answer_callback(query.get("id", ""), note)


async def _message(message: dict[str, Any], base_url: str) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return
    text = (message.get("text") or message.get("caption") or "").strip()

    if text.startswith("/start"):
        await _start(chat_id, text.partition(" ")[2].strip(), message)
        return

    audio_bytes: Optional[bytes] = None
    image_bytes: Optional[bytes] = None
    voice = message.get("voice") or message.get("audio")
    photo = (message.get("photo") or [None])[-1]
    # Security audit M2, on this lane too. Telegram states the size in the
    # update, so the cap is applied before anything is downloaded at all: an
    # oversized file costs one API call and no memory. Telegram's own bot API
    # refuses a download over 20 MB, which is above our own limit, so this is
    # the only ceiling that ever bites.
    if voice and _too_big(voice):
        await _too_big_reply(chat_id, voice)
        return
    if photo and _too_big(photo):
        await _too_big_reply(chat_id, photo)
        return
    if voice:
        audio_bytes = await telegram.download(voice["file_id"])
    elif photo:
        image_bytes = await telegram.download(photo["file_id"])

    doctor = await store.doctor_by_telegram(chat_id)
    if doctor is not None:
        await dispatch.handle_inbound(InboundMessage(
            channel="telegram", sender_ref=f"doctor:{doctor.web_token}",
            text=text, audio_bytes=audio_bytes, image_bytes=image_bytes,
        ))
        return

    patient = await store.patient_by_telegram(chat_id)
    if patient is not None:
        await dispatch.handle_inbound(InboundMessage(
            channel="telegram", sender_ref=f"patient:{patient.id}",
            text=text, audio_bytes=audio_bytes, image_bytes=image_bytes,
        ))
        return

    await _remember_start(chat_id, message)
    await telegram.send_card(chat_id, INTRO)


async def _start(chat_id: int, token_id: str, message: dict[str, Any]) -> None:
    """A deep link binds this chat to a patient, once. Anything else gets INTRO.

    The first thing asked is whose chat this is, and it is asked BEFORE the
    token is consumed. A doctor forwarding a link to a patient taps it himself
    to see what the patient will see, and until now that bound his own chat as
    the patient: the token was burned, the welcome went to him, and every
    message he typed afterwards was read as that patient's words. So a chat
    that belongs to a doctor record spends nothing here. The token stays
    unused, the patient stays unbound, the doctor gets one line telling him to
    forward it, and his own board records that the link was tapped and is
    still valid.
    """
    owner = await store.doctor_by_telegram(chat_id) if token_id else None
    if owner is not None:
        await telegram.send_card(chat_id, DOCTOR_TAPPED_LINK)
        await events.append_event(
            owner.id, "system",
            "a patient link was tapped in your own Telegram chat, so nothing "
            "was bound and the link is still valid",
            meta={"token": token_id, "chat_id": chat_id, "consumed": False,
                  "decided_by": "code (core/tg_router.py doctor chat check)"},
        )
        return

    token = await links.consume(token_id) if token_id else None
    if token is None:
        await _remember_start(chat_id, message)
        await telegram.send_card(chat_id, INTRO)
        return

    patient = await store.get_patient(token.patient_id)
    doctor = await store.doctor_by_id(token.doctor_id)
    if patient is None or doctor is None:
        await telegram.send_card(chat_id, INTRO)
        return

    channels = dict(patient.channels or {})
    channels["telegram_chat_id"] = chat_id
    await store.update_patient(patient.id, channels=channels, status="active")
    # The same three bubbles the web page opens with (rev 17 item 9), through
    # the fanout rather than straight to Telegram, so they are events and both
    # channels show the same first conversation. A patient who opened the web
    # link first has already had them and gets nothing twice.
    patient = await store.get_patient(patient.id) or patient
    await links.welcome(patient, doctor)
    await fanout().send(  # the doctor's board sees the link land too
        f"doctor:{doctor.web_token}",
        OutboundMessage(
            text=f"{patient.name} linked "
                 f"{gender.possessive(gender.of_patient(patient))} phone."),
    )


async def wrong_bindings() -> list[dict[str, Any]]:
    """Every patient record holding a doctor's own chat, with an event each.

    S12 item 2, and it is a check and not a repair. The tap is refused in
    `_start` now, but a board bound before this shipped carries the wrong
    binding silently: the doctor's typed messages arrive as that patient's, and
    everything meant for the patient reaches the doctor's phone. Nothing on the
    board said so, which is why it survived an evening of testing.

    The repair is the one that already exists: `POST /admin/reset` wipes that
    board's patients and the binding goes with them. So this reports, on the
    doctor's own board and in the log, and lets the gesture that fixes it stay
    the gesture that already fixes it.

    Called from the startup hook and from `POST /admin/reset`. It is best
    effort by definition: a check that took the service down would be a worse
    failure than the one it looks for.
    """
    rows = await store.doctor_chat_bindings()
    for row in rows:
        log.warning("doctor chat %s is bound to patient %s on board %s",
                    row["chat_id"], row["patient_id"], row["doctor_id"])
        await events.append_event(
            row["doctor_id"], "system",
            f"your own Telegram chat is bound to the patient record "
            f"{row['patient_name']}, so messages for that patient reach you: "
            f"reset the board to clear it",
            patient_id=row["patient_id"],
            meta={"chat_id": row["chat_id"], "patient_id": row["patient_id"],
                  "decided_by": "code (core/tg_router.wrong_bindings)"},
        )
    return rows


async def _remember_start(chat_id: int, message: dict[str, Any]) -> None:
    """Park an unknown chat so POST /admin/bind-doctor can claim it."""
    sender = message.get("from") or {}
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x)
    await store.save_pending_start(PendingStart(
        id=str(chat_id), chat_id=chat_id,
        display_name=name or sender.get("username", ""), created_at=store.now(),
    ))
