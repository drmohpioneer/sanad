"""Owns a photographed picture: what it is, what is printed on it, what happens.

The split is the whole point of this file, and it is a split in code, not in a
prompt:

  the model   classifies the picture (lab slip, blood-pressure monitor screen,
              prescription, other) and transcribes what is printed - analyte,
              value, unit, the slip's own printed reference range, the slip's own
              printed flag - and is told, in the instruction and by the schema,
              that it may not judge, interpret, reassure or advise;
  this file   corrects the orientation, stores the bytes, asks core/photos.py
              which route this is, and hands any lab rows to core/labs.py, which
              does every comparison against the critical-value table, the
              doctor's target and the doctor's baseline in Python.

So a critical potassium is a red card because 6.4 > 6.0 in a table, not because
a model thought it looked bad. `decided_by` on the event says so, and the card
prints it.

Every photo is read. A lab slip is extracted and compared whether or not a TEST
loop is open: with a matching loop it attaches and the loop moves to pending
review; without one the doctor gets a yellow "unexpected result" card carrying
the same values and two buttons - keep it on the patient's record, or open a
loop for it. A monitor screen joins the patient's chart, and its two numbers
are graded by core/vitals.py exactly as a lab value is graded by core/labs.py.
Anything else is stored and relayed unread, because guessing what an unexpected
photo is for is exactly the kind of decision Sanad does not make.

Orientation: the photo is first turned upright from its own EXIF tag, which is
what a phone writes and what makes a portrait photo arrive sideways. If the
model still reports the text as sideways or upside down, the image is rotated
and asked once more - once, never in a loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from datetime import timedelta
from typing import Any, Optional

from google.genai import types

from . import (
    bounded, coordinator, escalate, events, gender, labs, lang, media, photos,
    provenance, sentinel, settings, storage, store, timing, verify, vitals,
)
from .adapters import OutboundMessage, fanout
from .channel_contracts import NotificationClass
from .models import Doctor, Event, Loop, Patient, PhotoReading

log = logging.getLogger("sanad.extractor")

PROMPT = """You are reading a photograph sent to a clinic by a patient.

First say what the photograph is:
  lab_slip      a laboratory report, printed or handwritten;
  bp_monitor    the screen of a blood-pressure machine;
  prescription  a doctor's written or printed prescription;
  other         anything else at all.

Then transcribe what is printed. Do not interpret it.

If it is a lab slip: return the patient name printed on it, exactly as printed
and in the script it is printed in, empty if the slip prints none, and never
taken from anywhere but the paper. Then, for every analyte row you can read,
return the analyte name as printed, the value exactly as printed (digits only,
no unit), the unit as printed, the reference range the slip itself prints for
that row, and the flag the slip itself prints for that row (H, L, HIGH, LOW,
POSITIVE, and so on).
Leave a field empty when the slip does not print it. Never copy a reference
range from one row onto another, and never supply a reference range or a flag
the slip does not show.

If it is a blood-pressure monitor: return the systolic (upper) number, the
diastolic (lower) number, and the pulse if the screen shows one.

If a value is unreadable, return the row with an empty value rather than
guessing the number.

Report the orientation of the printed text in the image as given: upright,
sideways, or upside_down.

You must not say whether any value is normal, high, low, dangerous or fine, you
must not name a diagnosis, and you must not give any advice. Another part of
this system compares these numbers with a fixed clinical table. Your only job is
to say what the picture is and to read it accurately."""

PATIENT_RECEIVED = {
    "ar": "وصلني التحليل، وبعته لدكتورك دلوقتي. هيبص عليه ويرد عليك.",
    "en": "I have your lab result and I have sent it to your doctor. "
          "He will look at it and get back to you.",
}
PATIENT_READING = {
    "ar": "وصلتني القراءة وسجلتها في متابعتك، ودكتورك بيشوفها.",
    "en": "I have your reading and I have recorded it in your follow-up. "
          "Your doctor can see it.",
}
# The same reading with no monitoring loop to file it on. Sanad does not tell a
# patient it recorded something in a chart that does not exist.
PATIENT_READING_UNFILED = {
    "ar": "وصلتني القراءة وبعتها لدكتورك.",
    "en": "I have your reading and I have sent it to your doctor.",
}
PATIENT_CRITICAL = {
    "ar": {
        "m": "🚨 في نتيجة في التحليل ده عند مستوى خطر ومحتاجة دكتور دلوقتي.\n"
             "روح أقرب مستشفى أو قسم طوارئ حالاً، أو اتصل بالإسعاف 123.\n"
             "متستناش رد هنا. دكتورك اتبلغ دلوقتي.",
        "f": "🚨 في نتيجة في التحليل ده عند مستوى خطر ومحتاجة دكتور دلوقتي.\n"
             "روحي أقرب مستشفى أو قسم طوارئ حالاً، أو اتصلي بالإسعاف 123.\n"
             "متستنيش رد هنا. دكتورك اتبلغ دلوقتي.",
        "u": "🚨 في نتيجة في التحليل ده عند مستوى خطر ومحتاجة دكتور دلوقتي.\n"
             "المطلوب دلوقتي أقرب مستشفى أو قسم طوارئ حالاً، أو الاتصال بالإسعاف "
             "123.\nمن غير انتظار رد هنا. دكتورك اتبلغ دلوقتي.",
    },
    "en": "🚨 One of the values on this result is at a dangerous level and needs "
          "a doctor now.\nGo to the nearest emergency room immediately, or call "
          "123 (ambulance).\nDo not wait for a reply here. Your doctor has just "
          "been alerted.",
}
PATIENT_UNEXPECTED = {
    "ar": "وصلتني الصورة وبعتها لدكتورك.",
    "en": "I have received your photo and passed it to your doctor.",
}
PATIENT_DUPLICATE = {
    "ar": "وصلتني الصورة دي قبل كده النهارده. ماعملتش نتيجة أو كارت تاني.",
    "en": "I already received this image today. I did not create another result or card.",
}

# --------------------------------------------------------------------------- #
# Who decided this card (rev 18 item 3)
# --------------------------------------------------------------------------- #
# A model reads the pixels on this path and nothing else. What the photo IS
# (core/photos.py routing table), what the numbers MEAN (core/vitals.py,
# core/labs.py), whether the slip satisfies the contract (core/verify.py) and
# therefore which card the doctor gets are all decided by tables in code over
# the transcription, which is why every label below says code. The
# transcription itself is named on the same events as `route` and `orientation`,
# so nothing here is claiming a model was not involved: it is saying no model
# decided anything.
DECIDED_ROUTE = "code (core/photos.py routing table)"
DECIDED_VITALS = "code (core/vitals.py blood pressure table)"
DECIDED_UNREAD = "code (core/photos.py routing table, nothing was acted on)"
DECIDED_LABS = "code (core/labs.py value tables and core/verify.py slip checks)"
DECIDED_DOCTOR_OPENED = ("code (core/extractor.py, the doctor pressed Open a "
                         "loop)")


def critical_text(speak: str, who: str) -> str:
    """The critical-value block, in the patient's language and gender."""
    if speak != "ar":
        return PATIENT_CRITICAL["en"]
    return PATIENT_CRITICAL["ar"].get(who, PATIENT_CRITICAL["ar"]["u"])


# --------------------------------------------------------------------------- #
# The image
# --------------------------------------------------------------------------- #
def _reencode(raw: bytes, rotate: int = 0) -> bytes:
    """EXIF-upright, optionally rotated, as JPEG. Blocking: PIL is not async."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if rotate:
            img = img.rotate(rotate, expand=True)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=88)
        return out.getvalue()


ROTATION_FOR = {"sideways": 270, "upside_down": 180}


async def upright(raw: bytes) -> bytes:
    """A phone photo, turned the right way up from its own EXIF tag.

    The Registrar reads a prescription photo through this too, so a picture the
    doctor took in portrait does not reach the model on its side.
    """
    return await asyncio.to_thread(_reencode, raw)


async def read_photo(raw: bytes) -> tuple[Optional[PhotoReading], dict]:
    """Photo bytes -> the schema, plus a note on what had to be done to read it."""
    note: dict = {"rotated": 0, "attempts": 1}
    try:
        image = await asyncio.to_thread(_reencode, raw)
    except Exception:  # an unreadable file is not a crash, it is a yellow card
        log.exception("could not decode the uploaded image")
        return None, {"error": "image could not be decoded"}

    reading = await _ask(image)
    if reading is None:
        return None, {"error": "the model returned nothing readable"}

    rotation = ROTATION_FOR.get(reading.text_orientation, 0)
    if rotation:
        # Once. If the second read is no better, the first one still stands.
        note.update(rotated=rotation, attempts=2)
        rotated = await asyncio.to_thread(_reencode, raw, rotation)
        second = await _ask(rotated)
        if second is not None and (
            second.text_orientation == "upright"
            or len(second.analytes) > len(reading.analytes)
        ):
            return second, note
    return reading, note


async def _ask(image: bytes) -> Optional[PhotoReading]:
    """One model call, structured output, no tools, nothing kept afterwards.

    Bounded and caught (codex item 11). A Gemini call that hung here held the
    patient's page open until Cloud Run gave up, and one that threw was an HTTP
    500: either way the photograph he had just taken was gone and nobody had
    been told. None is the file's own "I could not read this", and `read_photo`
    turns it into the yellow "stored and relayed unread" card that already
    exists for a photo Sanad will not act on.
    """
    try:
        response = await bounded.within(
            bounded.PHOTO,
            media.client.aio.models.generate_content(
                model=media.MODEL,
                contents=[
                    types.Part.from_bytes(data=image, mime_type="image/jpeg"),
                    types.Part(text=PROMPT),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=PhotoReading,
                ),
            ),
            what="the photo read")
    except Exception:
        log.warning("the photo could not be read", exc_info=True)
        return None
    try:
        return PhotoReading.model_validate(json.loads(response.text or "{}"))
    except (ValueError, TypeError):
        log.error("photo reading did not validate")
        return None


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def _raw_row(finding: labs.Finding) -> str:
    bits = [finding.analyte, finding.printed, finding.unit or "-",
            f"ref {finding.ref_range or '-'}", f"flag {finding.flag or '-'}"]
    return "    " + " | ".join(bits)


def _source(reading: PhotoReading) -> str:
    return ", ".join(x for x in (reading.lab_name, reading.taken_on) if x)


def _value_lines(
    reading: PhotoReading, findings: list[labs.Finding], image_path: str,
    critical: bool, urgent: bool = False,
) -> list[str]:
    """The body every lab card shares: the judged lines, then the raw table."""
    lines: list[str] = []
    if critical:
        lines.append("decided in code by the critical-value table, no model vote")
    if urgent:
        lines.append(
            "one or more values could not be judged in code (its unit, its flag "
            "or an analyte with no row) - read those yourself"
        )
    lines += [f.line for f in findings]
    lines.append("raw extracted table:")
    lines += [_raw_row(f) for f in findings]
    # An empty path means the bucket did not answer inside its deadline (codex
    # item 11). The values were still read and still judged; the doctor is told
    # the picture behind them was not kept, so he does not go looking for it.
    lines.append(f"image: {image_path}" if image_path
                 else "image NOT stored: the bucket did not answer")
    return lines


def _lab_title(patient: Patient, critical: bool, urgent: bool, ordinary: str) -> str:
    """Which of the three headlines a lab card carries.

    A row the table could not judge is neither the ordinary yellow result nor a
    critical value: it is amber-red. It lands in the same place on the console
    as a red card, because a haemoglobin printed in a unit nobody can convert
    may be a transfusion, and the wording says exactly why code stood down.
    """
    if critical:
        return f"🚨 CRITICAL LAB · {patient.name}"
    if urgent:
        return f"🚨 URGENT REVIEW · {patient.name}"
    return ordinary


def values_card(
    patient: Patient, loop: Loop, reading: PhotoReading,
    findings: list[labs.Finding], image_path: str, critical: bool,
    urgent: bool = False,
) -> dict:
    """A result that matched an open test loop."""
    source = _source(reading)
    return {
        "title": _lab_title(patient, critical, urgent,
                            f"🧪 Lab results · {patient.name}"),
        "severity": "red" if critical or urgent else "yellow",
        "lines": [loop.title + (f" ({source})" if source else "")]
                 + _value_lines(reading, findings, image_path, critical, urgent),
        "actions": [
            {"id": f"reviewed:{loop.id}", "label": "Reviewed"},
            {"id": f"note:{loop.id}", "label": "Send a note", "input": True},
        ],
    }


def unexpected_result_card(
    patient: Patient, reading: PhotoReading, findings: list[labs.Finding],
    image_path: str, critical: bool, event_id: str, urgent: bool = False,
) -> dict:
    """A real lab result with no order behind it. Read, compared, then handed over."""
    source = _source(reading)
    head = "Lab result with no open test" + (f" ({source})" if source else "")
    return {
        "title": _lab_title(patient, critical, urgent,
                            f"🟡 Unexpected result · {patient.name}"),
        "severity": "red" if critical or urgent else "yellow",
        "lines": [head, "Nothing was ordered for this, so nothing was attached yet."]
                 + _value_lines(reading, findings, image_path, critical, urgent),
        "actions": [
            {"id": f"attach:{event_id}", "label": "Attach to record"},
            {"id": f"openloop:{event_id}", "label": "Open a loop"},
        ],
    }


def identity_mismatch_card(
    patient: Patient, reading: PhotoReading, findings: list[labs.Finding],
    image_path: str, critical: bool, event_id: str, why: str,
    urgent: bool = False,
) -> dict:
    """Show the values without pretending no test existed or allowing attach."""
    return {
        "title": _lab_title(patient, critical, urgent,
                            f"🟡 Identity mismatch · {patient.name}"),
        "severity": "red" if critical or urgent else "yellow",
        "lines": [
            "This slip was NOT attached to any obligation.",
            f"Identity check failed: {why or 'the printed name does not match the record'}.",
            "Ordered-test completeness was not applied after identity failed.",
            *_value_lines(reading, findings, image_path, critical, urgent),
        ],
        "actions": [{"id": f"seen:{event_id}", "label": "Seen"}],
    }


def reading_card(
    patient: Patient, loop: Optional[Loop], row: dict, image_path: str
) -> dict:
    """A blood-pressure monitor screen, filed or unfiled."""
    where = (f"Added to {loop.title}." if loop is not None
             else "No monitoring loop is open, so this is not on a chart.")
    return {
        "title": f"🩺 Monitor reading · {patient.name}",
        "severity": "green" if loop is not None else "yellow",
        "lines": [photos.reading_line(row), where]
                 + ([f"image: {image_path}"] if image_path else []),
        "actions": [],
    }


def unexpected_card(patient: Patient, image_path: str, why: str) -> dict:
    return {
        "title": f"🟡 Photo from {patient.name}",
        "severity": "yellow",
        "lines": [why, f"image: {image_path}" if image_path else "image not stored"],
        "actions": [],
    }


# --------------------------------------------------------------------------- #
# The patient's photo, start to finish
# --------------------------------------------------------------------------- #
async def handle_photo(
    patient: Patient, doctor: Doctor, image: bytes, mime: str = "image/jpeg",
    *, caption: str = "", channel: str = "web", synthetic: bool = True,
) -> None:
    """Claim identical bytes once per patient/day, then read and route them."""
    synthetic = provenance.derived(patient.synthetic, synthetic)
    digest = hashlib.sha256(image).hexdigest()
    day_index = timing.day_index(store.now(), timing.REAL_DAY_SECONDS)
    owner = store.new_id()
    if not await store.claim_photo(patient.id, day_index, digest, owner):
        speak = lang.of(caption) if caption else await lang.for_patient(
            patient, doctor.id)
        await events.append_event(
            doctor.id, "system", f"duplicate photo ignored for {patient.name}",
            patient_id=patient.id, channel=channel,
            meta={"duplicate_image": True, "digest": digest[:16],
                  "decided_by": "code (same image bytes, patient, Cairo day)"},
            synthetic=synthetic,
        )
        await fanout().send(f"patient:{patient.id}", OutboundMessage(
            text=PATIENT_DUPLICATE[speak],
            meta={"audit": {"tier": "duplicate", "generated": "code template"}},
        ))
        return
    try:
        await _handle_photo_claimed(
            patient, doctor, image, mime, caption=caption, channel=channel,
            synthetic=synthetic)
    except Exception:
        await store.release_photo(patient.id, day_index, digest, owner)
        raise
    await store.complete_photo(patient.id, day_index, digest, owner)


async def _handle_photo_claimed(
    patient: Patient, doctor: Doctor, image: bytes, mime: str = "image/jpeg",
    *, caption: str = "", channel: str = "web", synthetic: bool = True,
) -> None:
    """A newly claimed photo from a patient, read and routed once."""
    out = fanout()
    to_patient, to_doctor = f"patient:{patient.id}", f"doctor:{doctor.web_token}"
    speak = lang.of(caption) if caption else await lang.for_patient(patient, doctor.id)
    who = gender.of_patient(patient)
    run_id, _ = await settings.current()
    # codex item 11. Keeping the bytes is worth less than reading them: a bucket
    # that is slow or down must not cost the doctor a critical potassium. The
    # path becomes empty, the card says the picture was not kept, and everything
    # downstream carries on with the values.
    image_path = await bounded.or_none(
        bounded.STORAGE,
        storage.put_image(image, run_id=run_id, patient_id=patient.id, mime=mime),
        what="storing the photo") or ""

    reading, note = await read_photo(image)
    if reading is None:
        await _relay_unread(
            patient, doctor, image_path, speak, channel,
            note.get("error") or "This photo could not be read.", note,
            synthetic=synthetic,
        )
        return

    loops = await store.list_loops(patient.id)
    # The slip's own analytes choose between the doctor's open tests, so a
    # potassium result does not land on his lipid panel (core/photos.py).
    test_loop = photos.open_test_loop(loops, [a.analyte for a in reading.analytes])
    monitor_loop = photos.open_monitor_loop(loops)
    route = photos.route(
        reading.kind,
        test_loop=test_loop is not None,
        monitor_loop=monitor_loop is not None,
    )
    log.info("photo kind=%s route=%s patient=%s", reading.kind, route, patient.id)

    if route in ("attach_to_loop", "unexpected_result"):
        if not reading.analytes:
            await _relay_unread(
                patient, doctor, image_path, speak, channel,
                "This reads as a lab report but no values could be read off it.",
                note, synthetic=synthetic,
            )
            return
        await _handle_lab(
            patient, doctor, reading, note, image_path, speak, who, channel,
            test_loop if route == "attach_to_loop" else None, caption=caption,
            synthetic=synthetic,
        )
        return

    if route in ("monitor_reading", "unfiled_reading"):
        row = photos.reading_row(
            reading.systolic, reading.diastolic, reading.pulse, store.now()
        )
        if row is None:
            await _relay_unread(
                patient, doctor, image_path, speak, channel,
                "This reads as a monitor screen but the numbers were not legible.",
                note, synthetic=synthetic,
            )
            return
        row = provenance.evidence(row, synthetic=synthetic)
        loop = monitor_loop if route == "monitor_reading" else None
        if loop is not None:
            # ArrayUnion, not read-append-write (codex item 13, wave B's
            # handoff): two readings that arrive together both survive, and the
            # same photo delivered twice is still one row.
            await store.append_reading(loop.id, row)
            await store.update_loop(
                loop.id, attempts=0, last_reply_at=store.now(),
            )
        # The number is graded in code by core/vitals.py, the same table a typed
        # reading meets, so photographing the machine and typing what it says
        # cannot get two different answers. It is filed either way; the verdict
        # only decides which card the doctor gets.
        verdict = vitals.judge_text(str(row.get("value", "")))
        await events.append_event(
            doctor.id, "system", f"monitor reading from {patient.name}",
            patient_id=patient.id, loop_id=loop.id if loop else None, channel=channel,
            media=[provenance.evidence(
                {"kind": "image", "path": image_path}, synthetic=synthetic
            )],
            meta={"reading": row, "route": route,
                  "decided_by": "code (core/photos.py routing table)",
                  "vitals": verdict.as_meta() if verdict else None},
            synthetic=synthetic,
        )
        if verdict is not None and verdict.red:
            await escalate_bp(patient, doctor, verdict, row, speak=speak,
                              who=who, channel=channel, loop=loop,
                              image_path=image_path, synthetic=synthetic)
            return
        told = PATIENT_READING if loop is not None else PATIENT_READING_UNFILED
        await out.send(to_patient, OutboundMessage(text=told[speak]))
        await out.send(to_doctor, OutboundMessage(
            text=f"{patient.name} sent a monitor reading.", patient_id=patient.id,
            meta={"decided_by": DECIDED_ROUTE},
            card=reading_card(patient, loop, row, image_path)))
        return

    await _relay_unread(
        patient, doctor, image_path, speak, channel,
        f"Classified as {reading.kind}, so it was stored and passed on unread.",
        note, synthetic=synthetic,
    )


# --------------------------------------------------------------------------- #
# A blood pressure the table calls critical
# --------------------------------------------------------------------------- #
async def escalate_bp(
    patient: Patient, doctor: Doctor, verdict: "vitals.Verdict", row: dict,
    *, speak: str, who: str, channel: str, loop: Optional[Loop] = None,
    image_path: str = "", synthetic: bool = True,
) -> None:
    """One red blood pressure, told to whoever needs to hear it.

    Both readings end here: the one the patient photographed off the machine
    (above in this file) and the one he typed into the chat, which the
    Concierge hands over. Keeping it in one function is what stops a
    photographed crisis and a typed one from being answered differently.

    A crisis-range reading gets the same emergency block the Sentinel sends, in
    the patient's language and grammatical gender, so no new wording is
    invented for it. A low reading gets the ordinary acknowledgement: it is the
    doctor who is being woken, not the patient who is being sent to hospital
    (core/vitals.py explains why the two differ).
    """
    out = fanout()
    synthetic = provenance.derived(patient.synthetic, synthetic)
    row = provenance.evidence(row, synthetic=synthetic)
    to_patient, to_doctor = f"patient:{patient.id}", f"doctor:{doctor.web_token}"

    where = (f"Added to {loop.title}." if loop is not None
             else "No monitoring loop is open, so this is not on a chart.")
    extra = [where, f"pulse {row['pulse']}"] if row.get("pulse") else [where]
    if verdict.emergency:
        extra.append("Sanad told the patient to go to the nearest ER and call 123.")
    if image_path:
        extra.append(f"image: {image_path}")

    # codex item 10. The emergency block on a crisis reading ends "your doctor
    # has just been alerted", so the escalation and the red card exist before it
    # is said. The low-reading acknowledgement is not a promise about the
    # doctor, but it takes the same road: the card is what the doctor acts on
    # either way, and one order for both is one thing to keep true.
    async def persist() -> None:
        await events.append_event(
            doctor.id, "escalation", f"{verdict.concept}: {verdict.line}",
            patient_id=patient.id, loop_id=loop.id if loop else None,
            channel=channel,
            meta={"sentinel": verdict.as_meta(), "reading": row,
                  "told_patient": "emergency block" if verdict.emergency
                                  else "reading acknowledged"},
            synthetic=synthetic,
        )
        await out.send(to_doctor, OutboundMessage(
            text=f"Critical blood pressure for {patient.name}.",
            patient_id=patient.id,
            meta={
                "decided_by": DECIDED_VITALS,
                **(
                    {"notification_class": NotificationClass.DANGER.value}
                    if doctor.workspace_facts_enabled else {}
                ),
            },
            card=vitals.red_card(patient.name, verdict, extra)))

    landed = await escalate.told_or_fail_closed(
        persist, doctor_id=doctor.id, patient_id=patient.id,
        what="the blood pressure escalation",
        loop_id=loop.id if loop else None, channel=channel,
        synthetic=synthetic,
    )
    if not landed:
        told = escalate.fail_closed_text(speak, who, emergency=True)
    elif verdict.emergency:
        told = sentinel.emergency_text(speak, who)
    else:
        told = (PATIENT_READING if loop is not None
                else PATIENT_READING_UNFILED)[speak]
    await out.send(to_patient, OutboundMessage(
        text=told,
        meta={"audit": {"tier": "emergency" if verdict.emergency else "reading",
                        "net": "code", "concept": verdict.concept,
                        "decided_by": vitals.DECIDED_BY,
                        **({} if landed else {"error": escalate.FAIL_CLOSED})}}))


async def _relay_unread(
    patient: Patient, doctor: Doctor, image_path: str, speak: str, channel: str,
    why: str, note: dict, *, synthetic: bool = True,
) -> None:
    """The one exit for a photo Sanad will not act on: store it, hand it over."""
    out = fanout()
    await events.append_event(
        doctor.id, "system", "photo stored and relayed unread",
        patient_id=patient.id, channel=channel,
        media=[provenance.evidence(
            {"kind": "image", "path": image_path}, synthetic=synthetic
        )],
        meta={"why": why, "note": note},
        synthetic=synthetic,
    )
    await out.send(f"patient:{patient.id}",
                   OutboundMessage(text=PATIENT_UNEXPECTED[speak]))
    await out.send(f"doctor:{doctor.web_token}", OutboundMessage(
        text=f"Photo from {patient.name}.", patient_id=patient.id,
        meta={"decided_by": DECIDED_UNREAD},
        card=unexpected_card(patient, image_path, why)))


# How far back the patient's own words are read when a slip is judged. The
# same window core/labs.py documents and docs/SAFETY.md names.
CONTEXT_HOURS = 48


async def recent_words(patient: Patient, doctor: Doctor, caption: str) -> list[str]:
    """The caption on this photo plus what this patient said in the last 48 hours.

    Kernel review F1. The pregnancy row in core/labs.py needs two facts and only
    one of them is printed on the slip: the second is the patient saying he has
    abdominal pain. Until this existed the second fact never reached the table,
    so a positive test with pain stopped at urgent review instead of critical,
    which is the safe direction and still one fact short of what docs/SAFETY.md
    says the rule is.

    Nothing here judges anything. It collects strings; core/labs.py does the
    matching, the negation and the proximity in code.

    A list is always returned, never None, and that distinction is load-bearing:
    `labs.context_searched` reads None as "nobody looked" and prints that
    differently on the doctor's card from "looked and the patient had said
    nothing". A patient who has been silent for two days must come back as the
    second, because the search was made.

    A read that throws comes back as the caption alone rather than as a 500 on
    the photo path (codex item 11): losing the history downgrades a finding to
    urgent review, which is the direction this file already fails in.
    """
    words: list[str] = []
    if (caption or "").strip():
        words.append(caption)
    cutoff = store.now() - timedelta(hours=CONTEXT_HOURS)
    try:
        history = await events.last_events(doctor.id, 0)
    except Exception:
        log.warning("could not read the message history for the lab context",
                    exc_info=True)
        return words
    for event in history:
        if event.patient_id != patient.id or event.kind != "patient_in":
            continue
        stamp = getattr(event, "ts", None)
        if stamp is not None and stamp < cutoff:
            continue
        if (event.text or "").strip():
            words.append(event.text)
    return words


async def _handle_lab(
    patient: Patient, doctor: Doctor, reading: PhotoReading, note: dict,
    image_path: str, speak: str, who: str, channel: str, loop: Optional[Loop],
    caption: str = "", synthetic: bool = True,
) -> None:
    """A lab slip, with or without an order behind it. Same reading, same table."""
    out = fanout()
    to_patient, to_doctor = f"patient:{patient.id}", f"doctor:{doctor.web_token}"

    # Every comparison from here down is core/labs.py, in code. The context is
    # the patient's own words, and it is what completes the two-factor
    # pregnancy rule (kernel review F1): the slip carries the positive test and
    # only the patient carries the pain.
    context = await recent_words(patient, doctor, caption)
    findings = labs.assess(
        [a.model_dump() for a in reading.analytes], patient.targets,
        patient.baseline, context=context,
    )
    critical = labs.criticals(findings)
    urgent = labs.urgents(findings)

    # What the model read off the slip is model output, and it is checked by the
    # same code word list the patient's own text meets. An analyte name or a
    # flag that carries an emergency word ("critical", "panic") cannot reach the
    # doctor labelled as an ordinary result.
    slip_words = " ".join(
        f"{f.analyte} {f.flag}" for f in findings if f.analyte or f.flag
    )
    slip_concept = sentinel.code_net(slip_words)

    results = [
        provenance.evidence(
            {"analyte": f.analyte, "value": f.printed, "unit": f.unit,
             "ref_range": f.ref_range, "flag": f.flag, "level": f.level,
             "target": f.target, "baseline": f.baseline, "line": f.line},
            synthetic=synthetic,
        )
        for f in findings
    ]

    # The three verifier checks, in code, before this slip is allowed to satisfy
    # the contract behind the loop (core/verify.py): the printed name is this
    # patient's, the collection date is on or after the order date, and every
    # analyte the doctor asked for is on the slip.
    verdict = None
    identity_lines: list[str] = []
    if loop is not None:
        verdict = verify.check(
            printed_name=reading.patient_name,
            printed_date=reading.taken_on,
            printed_analytes=[f.analyte for f in findings],
            patient_name=patient.name,
            ordered_on=loop.created_at,
            required=verify.required_analytes(loop),
        )
        if verdict.identity_failed:
            # It never attaches. The values are still read, still compared and
            # still put in front of the doctor, because a critical value on a
            # slip nobody can identify is exactly the thing that must not go
            # quiet; it goes to him as an unexpected result with the mismatch
            # named on the card.
            await events.append_event(
                doctor.id, "escalation",
                f"identity check failed on a slip sent by {patient.name}",
                patient_id=patient.id, loop_id=loop.id, channel=channel,
                media=[provenance.evidence(
                    {"kind": "image", "path": image_path}, synthetic=synthetic
                )],
                meta={"verify": provenance.evidence(
                          verdict.as_meta(), synthetic=synthetic),
                      "results": results,
                      "decided_by": "code (core/verify.py identity check)"},
                synthetic=synthetic,
            )
            # Completeness is about this patient's order. Once identity failed,
            # printing "3 of 4 requested analytes" beside an unattached slip is
            # a contradictory story, so those lines are deliberately omitted.
            identity_lines = []
            loop = None
        else:
            identity_lines = verdict.lines()

    if loop is not None and verdict is not None:
        # A slip that satisfies the contract moves the loop to the doctor. A
        # partial one, or one collected before the order, keeps the contract
        # open with the values on it: the evidence is real, it is just not yet
        # the evidence that was asked for.
        #
        # The rows are appended, never written over the list that was there
        # (codex item 13, wave B's handoff): a second slip on the same loop, a
        # partial one followed by the missing half, used to erase the first.
        await store.append_result(loop.id, results)
        fields: dict[str, Any] = {
            "attempts": 0, "last_reply_at": store.now(),
            "verified": provenance.evidence(
                verdict.as_meta(), synthetic=synthetic
            ),
        }
        if verdict.satisfies:
            fields["state"] = "pending_review"
        await store.update_loop(loop.id, **fields)
        # Evidence has arrived on this loop, so every rung of the ladder still
        # sitting on the Cloud Tasks queue is out of date (kernel review F8b).
        # Those rungs were made at commit time and they say "please do the
        # test"; a patient who has already sent the slip receiving one the next
        # day is the worst sentence this system can produce. Nothing is deleted
        # from the queue, because Cloud Tasks cannot be reached into: the
        # version on the loop moves and core/chaser.fire refuses the old ones on
        # arrival. This runs whether or not the slip satisfied the contract,
        # because the ladder's sentence is wrong either way; what happens next
        # to an unsatisfied contract is the Coordinator's, below.
        from . import chaser  # here, not at import time: chaser imports us

        await chaser.supersede_ladder(loop.id, "the evidence arrived")
        loop = await store.get_loop(loop.id) or loop

    event = await events.append_event(
        doctor.id, "system",
        f"lab slip read for {loop.title}" if loop is not None
        else f"lab slip read with no open test for {patient.name}",
        patient_id=patient.id, loop_id=loop.id if loop else None, channel=channel,
        media=[provenance.evidence(
            {"kind": "image", "path": image_path}, synthetic=synthetic
        )],
        meta={
            "lab": reading.lab_name, "taken_on": reading.taken_on,
            "orientation": note, "results": results,
            "image_path": image_path,
            "attached": loop is not None,
            "decided_by": "code (core/labs.py critical-value table)",
            "urgent_review": [f.analyte for f in urgent],
            "slip_text_sentinel": slip_concept or "",
            "verify": (provenance.evidence(
                verdict.as_meta(), synthetic=synthetic
            ) if verdict is not None else None),
        },
        synthetic=synthetic,
    )

    # Identity failure is a yellow verification problem, not a red clinical
    # verdict. Actual critical/urgent values and sentinel words remain red even
    # when the name does not match, so uncertainty never hides a dangerous row.
    identity_failed = bool(verdict is not None and verdict.identity_failed)
    flagged = bool(urgent) or slip_concept is not None
    if identity_failed:
        card = identity_mismatch_card(
            patient, reading, findings, image_path, bool(critical), event.id,
            verdict.identity_why if verdict is not None else "", flagged)
    elif loop is not None:
        card = values_card(patient, loop, reading, findings, image_path,
                           bool(critical), flagged)
    else:
        card = unexpected_result_card(patient, reading, findings, image_path,
                                      bool(critical), event.id, flagged)
    if identity_lines:
        card["lines"] = [*identity_lines, *card["lines"]]

    if critical:
        # codex item 10. The critical block ends "your doctor has just been
        # alerted", so the escalation and the red card are written before the
        # patient is told. The instruction to go is kept either way; only the
        # sentence about the doctor is withdrawn (core/escalate.py).
        async def persist_critical() -> None:
            await events.append_event(
                doctor.id, "escalation",
                "emergency: critical lab value: "
                + "; ".join(f.line for f in critical),
                patient_id=patient.id, loop_id=loop.id if loop else None,
                channel=channel,
                meta={"sentinel": {"fired": True, "net": "code",
                                   "concept": "critical lab value",
                                   "nets_run": ["code"]},
                      "results": results},
                synthetic=synthetic,
            )
            await out.send(to_doctor, OutboundMessage(
                text=f"Critical lab value for {patient.name}.",
                patient_id=patient.id,
                meta={
                    "decided_by": DECIDED_LABS,
                    **(
                        {"notification_class": NotificationClass.DANGER.value}
                        if doctor.workspace_facts_enabled else {}
                    ),
                },
                card=card))

        landed = await escalate.told_or_fail_closed(
            persist_critical, doctor_id=doctor.id, patient_id=patient.id,
            what="the critical lab escalation",
            loop_id=loop.id if loop else None, channel=channel,
            synthetic=synthetic,
        )
        await out.send(to_patient, OutboundMessage(
            text=critical_text(speak, who) if landed
            else escalate.fail_closed_text(speak, who, emergency=True),
            meta={"audit": {"tier": "emergency", "net": "code",
                            "concept": "critical lab value",
                            **({} if landed else {"error": escalate.FAIL_CLOSED})}}))
        return

    # Not critical, but the table could not judge one of the rows, or the slip's
    # own words carried an emergency term. The patient hears the ordinary "sent
    # to your doctor": this is the doctor being asked to look tonight, not the
    # patient being sent to an emergency room. The escalation event is what
    # makes it visible as urgent on the board.
    if flagged:
        await events.append_event(
            doctor.id, "escalation",
            "urgent review: " + "; ".join(
                f.line for f in (urgent or findings) if f.urgent or slip_concept
            ),
            patient_id=patient.id, loop_id=loop.id if loop else None, channel=channel,
            meta={"sentinel": {"fired": True, "net": "code",
                               "concept": slip_concept or "lab value cannot be judged",
                               "nets_run": ["code"]},
                  "results": results,
                  "decided_by": "code (core/labs.py unit and flag rules)"},
            synthetic=synthetic,
        )

    await out.send(to_patient, OutboundMessage(text=PATIENT_RECEIVED[speak]))
    possessive = gender.possessive(who)
    headline = (f"{patient.name} sent {possessive} {loop.title} result."
                if loop is not None
                else f"{patient.name} sent a result nothing was ordered for.")
    if identity_failed and not flagged:
        headline = f"{patient.name} sent a result with an identity mismatch."
    elif flagged:
        headline = f"{patient.name} sent a result that needs your eyes."
    await out.send(to_doctor, OutboundMessage(
        text=headline, card=card, patient_id=patient.id,
        meta={
            "decided_by": DECIDED_LABS,
            **(
                {"notification_class": NotificationClass.URGENT_SLA.value}
                if flagged and doctor.workspace_facts_enabled else {}
            ),
        }))

    # Evidence arrived and did not satisfy the contract: a part is missing, or
    # it was collected before the doctor ordered it. That is the Coordinator's
    # to act on, and its request_missing_evidence names the missing analyte
    # (core/coordinator.py). A slip that DID satisfy the contract does not wake
    # it: the only action left on that path is the one the code above has
    # already taken, and a model call could only add noise.
    if loop is not None and verdict is not None and not verdict.satisfies:
        await coordinator.on_evidence(
            loop, patient, doctor,
            note="a result arrived: " + "; ".join(verdict.reasons),
        )


# --------------------------------------------------------------------------- #
# The two buttons on an unexpected result
# --------------------------------------------------------------------------- #
async def _stored_result(doctor: Doctor, event_id: str) -> Optional[Event]:
    event = await store.get_event(event_id)
    verification = (event.meta.get("verify") or {}) if event is not None else {}
    if (event is None or event.doctor_id != doctor.id
            or not event.meta.get("results")
            or verification.get("identity") in ("mismatch", "cannot_compare")):
        return None
    return event


async def attach_results(doctor: Doctor, event_id: str) -> None:
    """"Attach to record": the values stay on the patient, no loop is invented."""
    out, to_doctor = fanout(), f"doctor:{doctor.web_token}"
    event = await _stored_result(doctor, event_id)
    patient = await store.get_patient(event.patient_id) if event else None
    if event is None or patient is None:
        await out.send(to_doctor, OutboundMessage(text="That result is gone."))
        return
    results = provenance.evidence_rows(event.meta.get("results", []))
    evidence_synthetic = provenance.derived(
        event.synthetic,
        *(row.get("synthetic") for row in results),
    )
    entry = provenance.evidence({
        "at": store.now().isoformat(timespec="minutes"),
        "lab": event.meta.get("lab", ""),
        "taken_on": event.meta.get("taken_on", ""),
        "image": event.meta.get("image_path", ""),
        "results": results,
    }, synthetic=evidence_synthetic)
    await store.update_patient(
        patient.id, results=[*(patient.results or []), entry]
    )
    await events.append_event(
        doctor.id, "system", f"result attached to {patient.name}'s record",
        patient_id=patient.id, meta={"from_event": event_id},
        synthetic=evidence_synthetic,
    )
    await out.send(to_doctor, OutboundMessage(
        text=f"Kept on {patient.name}'s record. No loop was opened."))


async def open_loop_for(doctor: Doctor, event_id: str) -> None:
    """"Open a loop": a TEST loop, already carrying the result, awaiting review."""
    out, to_doctor = fanout(), f"doctor:{doctor.web_token}"
    event = await _stored_result(doctor, event_id)
    patient = await store.get_patient(event.patient_id) if event else None
    if event is None or patient is None:
        await out.send(to_doctor, OutboundMessage(text="That result is gone."))
        return
    results = provenance.evidence_rows(event.meta.get("results", []))
    evidence_synthetic = provenance.derived(
        event.synthetic,
        *(row.get("synthetic") for row in results),
    )
    mission_synthetic = provenance.derived(
        patient.synthetic, evidence_synthetic
    )
    named = ", ".join(str(r.get("analyte", "")) for r in results[:4] if r.get("analyte"))
    made = store.now()
    loop = await store.create_loop(Loop(
        id=store.new_id(), synthetic=mission_synthetic,
        patient_id=patient.id, doctor_id=doctor.id,
        type="TEST", title="Lab result", details={"test_name": named or "Lab result"},
        state="pending_review", results=results, created_at=made, updated_at=made,
    ))
    await events.append_event(
        doctor.id, "system", f"loop opened for {patient.name}'s lab result",
        patient_id=patient.id, loop_id=loop.id, meta={"from_event": event_id},
        synthetic=mission_synthetic,
    )
    await out.send(to_doctor, OutboundMessage(
        text=f"Opened {loop.title} for {patient.name}, waiting for your review.",
        patient_id=patient.id,
        meta={"decided_by": DECIDED_DOCTOR_OPENED},
        card={
            "title": f"🧪 Lab results · {patient.name}",
            "severity": "yellow",
            "lines": [loop.title] + [str(r.get("line", "")) for r in results],
            "actions": [
                {"id": f"reviewed:{loop.id}", "label": "Reviewed"},
                {"id": f"note:{loop.id}", "label": "Send a note", "input": True},
            ],
        }))
