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

from datetime import timedelta

from . import (
    bounded, chaser, concierge, digest, events, media, registrar, report,
    sentinel, store,
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
                )
                if doctor.awaiting_relay_id:
                    # doctor_reply clears the flag itself, as it did in S2.
                    await concierge.doctor_reply(doctor, doctor.awaiting_relay_id, text)
                else:
                    await concierge.note_to_patient(
                        doctor, doctor.awaiting_note_loop_id or "", text
                    )
                    await clear_pending(doctor)
                return
            # The window closed; this is an ordinary message again.
            await clear_pending(doctor)
            await out.send(msg.sender_ref, OutboundMessage(text=EXPIRED))

        if text.startswith("/"):
            await events.append_event(
                doctor.id, "doctor_in", text, channel=msg.channel,
                meta={"source": "command"},
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
        )
        return

    if role == "patient":
        patient = await store.get_patient(value)
        if patient is None:
            return
        doctor = await store.doctor_by_id(patient.doctor_id)
        if doctor is None:
            return

        # Gate 1 runs here, on this lane, and it runs twice for a reason.
        #
        # Typed text is checked as it arrives. A voice note has no text until a
        # model has made some, so the transcript is a model output, and the
        # code sentinel is the first thing that reads it: transcribe, then
        # check, then hand the checked verdict to the Concierge. Nothing
        # between those two lines can generate a reply, and the Concierge is
        # given the verdict rather than being trusted to ask for one.
        text, voice = (msg.text or "").strip(), False
        gate = await sentinel.check(text) if text else sentinel.Sentinel()
        if msg.audio_bytes:
            # codex item 11. Bounded, and a failure is an answered turn and not
            # a 500. There is no transcript to check, so there is nothing for
            # the Sentinel to read and nothing for the Concierge to answer: the
            # turn ends here with the patient asked for the message again and
            # the doctor told one arrived that Sanad could not hear.
            try:
                text = await bounded.within(
                    bounded.TRANSCRIBE, media.transcribe_async(msg.audio_bytes),
                    what="the voice transcription")
            except Exception as exc:  # noqa: BLE001 - the card carries the error
                await concierge.voice_unreadable(
                    patient, doctor, " ".join(str(exc).split())[:200] or
                    exc.__class__.__name__, channel=msg.channel)
                return
            voice = True
            text = (text or "").strip()
            gate = await sentinel.check(text) if text else gate

        await concierge.handle_patient_message(
            patient, doctor, text, channel=msg.channel,
            image_bytes=msg.image_bytes, mime=msg.mime or "image/jpeg", voice=voice,
            gate=gate,
        )
        return

    # An unresolvable ref has no feed to write to, which is why the /c routes
    # validate the token and the patient id before they ever call in here.
    await out.send(msg.sender_ref, OutboundMessage(text=UNKNOWN_REPLY))


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
        awaiting_since=None,
    )
