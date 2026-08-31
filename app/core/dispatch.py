"""Owns routing: who sent this, and which brain answers it.

Plain Python, no LLM. Choosing the handler is a lookup, not a judgement call,
and keeping it that way means a routing bug can never be a prompt bug.

Three senders, three lanes:
  doctor  -> the relay answer he owes, or a command, or the Registrar (which
             takes a dictation typed, spoken, or photographed as a prescription)
  patient -> the Concierge (Sentinel first, always)
  unknown -> a single flat line

The doctor's commands are matched here, in code, before anything is generated:
/digest, /force_due <patient> [loop word], /report <patient>, /cancel.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from . import (
    bounded, chaser, concierge, digest, doctor_actions, events, lang, media,
    registrar, report, sentinel, store,
)
from .adapters import InboundMessage, OutboundMessage, fanout
from .models import Doctor

UNKNOWN_REPLY = "I do not recognise this sender."
DIGEST_COMMAND = "/digest"
FORCE_DUE_COMMAND = "/force_due"
REPORT_COMMAND = "/report"
CANCEL_COMMAND = "/cancel"

# S2 review, carry-over 2: after the doctor taps "Answer" on a card, his next
# message is consumed as that answer. It now expires, and he can call it off.
ANSWER_WINDOW = timedelta(minutes=10)
CANCELLED = "Cancelled. That question is still waiting; tap Answer again."
EXPIRED = ("That answer window closed, so I treated this as a new message. "
           "Tap Answer on the card again to reply to the patient.")
MAX_PATIENT_TEXT = 1000
PATIENT_TEXT_TOO_LONG = {
    "en": ("That message is longer than 1,000 characters. Shorten it and send it "
           "again. If this may be an emergency, call 123 or go to the nearest ER."),
    "ar": ("الرسالة أطول من ١٠٠٠ حرف. اختصرها وابعتها تاني. لو ممكن تكون حالة "
           "طوارئ، اتصل بالإسعاف 123 أو روح أقرب طوارئ."),
}
PATIENT_TURN_BUSY = {
    "en": ("I am still handling your previous message. Wait for that reply, then "
           "send the next one. If this may be an emergency, call 123 or go to the "
           "nearest ER."),
    "ar": ("لسه بتعامل مع رسالتك اللي قبلها. استنى الرد وبعدها ابعت الرسالة "
           "التالية. لو ممكن تكون حالة طوارئ، اتصل بالإسعاف 123 أو روح أقرب طوارئ."),
}

log = logging.getLogger("sanad.dispatch")


def patient_limit_text(text: str) -> str:
    return PATIENT_TEXT_TOO_LONG[lang.of(text)]


def patient_busy_text(text: str) -> str:
    return PATIENT_TURN_BUSY[lang.of(text)]


async def handle_inbound(msg: InboundMessage) -> None:
    role, _, value = msg.sender_ref.partition(":")
    out = fanout()

    if role == "doctor":
        doctor = await store.doctor_by_token(value)
        if doctor is None:
            return
        text = (msg.text or "").strip()

        # He tapped a button that consumes his next message: "Answer" on a
        # relay card, or "Send a note" on a lab-values card. It lasts ten
        # minutes and /cancel calls it off (S2 review, carry-over 2).
        #
        # S24-C. The window is not scoped to the door it was opened from. For
        # one release it was: a window opened on the phone refused the answer
        # he typed into the console, and /cancel only worked on the surface
        # that had opened it. That is a lock on the doctor rather than on the
        # record, and it made one question two questions. The lock that
        # matters is the action claim below, which is on the record and is the
        # same claim a second press of the button meets.
        if (doctor.awaiting_relay_id or doctor.awaiting_note_loop_id) \
                and text and not msg.audio_bytes and not msg.image_bytes:
            if text.lower().startswith(CANCEL_COMMAND):
                await clear_pending(doctor)
                await out.send(msg.sender_ref, OutboundMessage(text=CANCELLED))
                return
            if await answer_window_open(doctor):
                await events.append_event(
                    doctor.id, "doctor_in", text, channel=msg.channel,
                    meta={"source": "card answer"},
                    synthetic=msg.synthetic,
                )
                await _land_answer(doctor, msg, text)
                return
            # The window closed; this is an ordinary message again.
            await clear_pending(doctor)
            await out.send(msg.sender_ref, OutboundMessage(text=EXPIRED))

        if text.startswith("/"):
            await events.append_event(
                doctor.id, "doctor_in", text, channel=msg.channel,
                meta={"source": "command"},
                synthetic=msg.synthetic,
            )
            await out.send(
                msg.sender_ref, OutboundMessage(text=await command(doctor, text))
            )
            return

        # Voice, text and photo are one path into the Registrar: a photographed
        # prescription is a dictation the doctor happened to write down first.
        await registrar.handle_doctor(
            doctor, text, msg.audio_bytes, msg.image_bytes,
            mime=msg.mime or "image/jpeg", channel=msg.channel,
            synthetic=msg.synthetic,
        )
        return

    if role == "patient":
        patient = await store.get_patient(value)
        if patient is None:
            return
        doctor = await store.doctor_by_id(patient.doctor_id)
        if doctor is None:
            return
        await _handle_patient(patient, doctor, msg)
        return

    # An unresolvable ref has no feed to write to, which is why the /c routes
    # validate the token and the patient id before they ever call in here.
    await out.send(msg.sender_ref, OutboundMessage(text=UNKNOWN_REPLY))


async def _land_answer(doctor: Doctor, msg: InboundMessage, text: str) -> None:
    """The second half of a two-step card action: the doctor's own words.

    S24-C. This used to call the Concierge straight, which closed the relay
    and sent the patient the answer while leaving the card that asked for it
    open for ever. Nothing on the board said the question was finished, and a
    doctor looking at his Inbox saw a question he had already answered.

    So the answer runs the same unit the button runs (core/doctor_actions.py)
    with the same action id the card carries: the card is claimed, the work is
    done, and the card is retired, in that order. An answer that arrives twice,
    or on two surfaces at once, meets the claim and is refused rather than sent
    to the patient a second time.
    """
    out = fanout()
    if doctor.awaiting_relay_id:
        answered = await doctor_actions.perform(
            doctor, f"reply:{doctor.awaiting_relay_id}", text
        )
        if answered.get("already"):
            await out.send(msg.sender_ref, OutboundMessage(
                text=concierge.ALREADY_ANSWERED,
                meta={"decided_by": "code (core/doctor_actions.py, the action "
                                    "is already claimed)"}))
    else:
        # "Send a note" is a side action: it sends the line and deliberately
        # leaves the lab-values card open, because the review that closes the
        # loop has not happened yet (core/cards.SIDE_ACTIONS). Running it
        # through the same unit is what makes that a property of the button
        # rather than of the surface.
        await doctor_actions.perform(
            doctor, f"note:{doctor.awaiting_note_loop_id or ''}", text
        )

    # S24-C review. The window is closed here and on every outcome, not by the
    # Concierge on the one path that reached the end of its work. Its early
    # returns ("that patient is gone", "that loop is gone") left the window
    # standing, so the doctor's NEXT message was eaten as an answer to a
    # question that had just told him it could not be answered. Consuming his
    # message is what the window is for, and it may be spent exactly once.
    # `concierge.doctor_reply` still clears it on its own path; clearing an
    # already clear window writes the same three nulls again.
    await clear_pending(doctor)


async def _handle_patient(patient, doctor, msg: InboundMessage) -> None:
    """One patient turn behind a durable lease, with emergencies never queued."""
    raw_text = msg.text or ""
    text = raw_text.strip()
    code_concept = sentinel.code_net(text) if text else None

    # A large payload does not buy a model call. Explicit code-net emergencies
    # still take the emergency path; every other long message is returned with
    # a fixed instruction that includes the emergency fallback.
    if len(raw_text) > MAX_PATIENT_TEXT and code_concept is None:
        await fanout().send(msg.sender_ref, OutboundMessage(
            text=patient_limit_text(raw_text),
            meta={"audit": {"tier": "input_limit", "generated": "code template"},
                  "decided_by": "code (1,000-character patient limit)"},
        ))
        return

    # Code-net emergencies bypass an ordinary in-flight turn. Waiting behind a
    # model answer is the unsafe direction; this path costs no competing model
    # call and reaches the same emergency persistence order as any other hit.
    owner = ""
    if code_concept is None:
        owner = store.new_id()
        try:
            claimed = await store.claim_patient_turn(patient.id, owner)
        except Exception:  # noqa: BLE001 - fail closed without paying a model
            log.exception("patient turn claim failed patient_id=%s", patient.id)
            claimed = False
        if not claimed:
            await fanout().send(msg.sender_ref, OutboundMessage(
                text=patient_busy_text(raw_text),
                meta={"audit": {"tier": "in_flight", "generated": "code template"},
                      "decided_by": "code (one in-flight patient turn)"},
            ))
            return

    try:
        # Gate 1 runs here, on this lane, and it runs twice for a reason.
        # Typed text is checked as it arrives. A voice note has no text until a
        # model has made some, so the transcript is checked immediately after
        # transcription and before the Concierge sees it.
        voice = False
        gate = (sentinel.Sentinel(fired=True, net="code", concept=code_concept,
                                  checked=["code"])
                if code_concept else
                await sentinel.check(text) if text else sentinel.Sentinel())
        if msg.audio_bytes:
            try:
                transcript = await bounded.within(
                    bounded.TRANSCRIBE, media.transcribe_async(msg.audio_bytes),
                    what="the voice transcription")
            except Exception as exc:  # noqa: BLE001 - the card carries the error
                await concierge.voice_unreadable(
                    patient, doctor, " ".join(str(exc).split())[:200] or
                    exc.__class__.__name__, channel=msg.channel,
                    synthetic=msg.synthetic)
                return
            voice = True
            transcript = transcript or ""
            text = transcript.strip()
            if len(transcript) > MAX_PATIENT_TEXT:
                await fanout().send(msg.sender_ref, OutboundMessage(
                    text=patient_limit_text(transcript),
                    meta={"audit": {"tier": "input_limit",
                                    "generated": "code template"}},
                ))
                return
            gate = await sentinel.check(text) if text else gate

        await concierge.handle_patient_message(
            patient, doctor, text, channel=msg.channel,
            image_bytes=msg.image_bytes, mime=msg.mime or "image/jpeg", voice=voice,
            gate=gate, synthetic=msg.synthetic,
        )
    finally:
        if owner:
            try:
                await store.release_patient_turn(patient.id, owner)
            except Exception:  # noqa: BLE001 - the lease expires on its own
                log.exception("patient turn release failed patient_id=%s", patient.id)


# --------------------------------------------------------------------------- #
# The doctor's commands. A lookup, never a model call.
# --------------------------------------------------------------------------- #
COMMAND_HELP = (
    "Commands: /digest, /force_due <patient> [loop word], /report <patient>, "
    "/cancel."
)


async def command(doctor: Doctor, text: str) -> str:
    verb, _, argument = text.strip().partition(" ")
    verb, argument = verb.lower(), argument.strip()

    if verb == DIGEST_COMMAND:
        # Stored as a record on the way out, so the Reports screen reads a
        # report rather than guessing one out of the text (core/report.py).
        text = await digest.build(doctor)
        await report.record(doctor, "digest", digest.title(doctor), text)
        return text
    if verb == FORCE_DUE_COMMAND:
        return await chaser.force_due(doctor, argument)
    if verb == REPORT_COMMAND:
        patient, why = await report.find_patient(doctor, argument)
        if patient is None:
            return why
        text = await report.build(doctor, patient)
        await report.record(doctor, "completion", report.completion_title(patient),
                            text, patient_id=patient.id)
        return text
    if verb == CANCEL_COMMAND:
        return "Nothing to cancel."
    return f"I do not know {verb}. " + COMMAND_HELP


async def answer_window_open(doctor: Doctor) -> bool:
    """True while the doctor's next message still counts as his card answer.

    Measured from the moment he tapped the button, not from when the patient
    asked: a card he opens a day later still gets its full ten minutes.
    """
    if doctor.awaiting_relay_id:
        relay = await store.get_relay(doctor.awaiting_relay_id)
        if relay is None or relay.state != "open":
            return False
    since = doctor.awaiting_since
    return since is None or store.now() - since <= ANSWER_WINDOW


async def clear_pending(doctor: Doctor) -> None:
    await store.update_doctor(
        doctor.id, awaiting_relay_id=None, awaiting_note_loop_id=None,
        awaiting_since=None, awaiting_channel=None,
    )
