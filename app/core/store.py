"""Owns all Firestore access. Nothing else in Sanad touches the database.

Every request is stateless: load what it needs, write, discard. The only global
is the Firestore client itself.

Query note: reads use equality filters only (no order_by next to a `where`).
Firestore serves equality-only queries from its automatic single-field indexes,
so the demo needs zero composite-index creation at deploy time. Ordering and
`since` filtering happen in Python, which is correct at demo volumes and would
be the first thing to change if a doctor's history grew large.
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .models import (
    Doctor,
    Event,
    LinkToken,
    Loop,
    Patient,
    PendingConfirm,
    PendingStart,
    Relay,
    Report,
    Send,
)

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "sanad-506914")

_db: Optional[firestore.AsyncClient] = None


def db() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient(project=PROJECT)
    return _db


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


# The namespace every derived id is minted under. A fixed uuid5 namespace means
# the same name always produces the same id, on any instance, at any time.
SANAD_NS = uuid.UUID("8f6d1b7e-3c22-5a41-9e0d-7a3f2b6c4d15")


def derived_id(*parts: str) -> str:
    """A deterministic document id for `parts`. Same name, same id, for ever.

    This is what makes a retried commit idempotent (codex item 6): the patient
    a confirmation creates is uuid5(confirmation id, "patient"), so a Confirm
    that half finished and ran again writes the same document over itself
    instead of putting a second Ahmed on the board.
    """
    return uuid.uuid5(SANAD_NS, ":".join(parts)).hex


def _write(record: Any) -> dict[str, Any]:
    """Pydantic record -> Firestore document body (id lives in the doc name)."""
    body = record.model_dump()
    body.pop("id", None)
    return body


# --------------------------------------------------------------------------- #
# Doctors
# --------------------------------------------------------------------------- #
async def create_doctor(
    name: str, lang: str = "en", specialty: str = "general practice"
) -> Doctor:
    doc = Doctor(
        id=new_id(),
        name=name,
        specialty=specialty,
        lang=lang,
        web_token=new_web_token(),
        created_at=now(),
    )
    await db().collection("doctors").document(doc.id).set(_write(doc))
    return doc


def new_web_token() -> str:
    """A console token. The same mint `create_doctor` uses, in one place.

    It is a bearer credential: whoever has it can dictate, confirm and answer
    cards on that doctor's board. `POST /admin/rotate-token` calls this after a
    recording, because the token is legible in the browser address bar of the
    submission video and the video stays public for weeks.
    """
    return secrets.token_hex(16)


async def doctor_by_name(name: str) -> Optional[Doctor]:
    async for snap in (
        db().collection("doctors").where(filter=FieldFilter("name", "==", name)).stream()
    ):
        return Doctor(id=snap.id, **snap.to_dict())
    return None


async def doctor_by_id(doctor_id: str) -> Optional[Doctor]:
    snap = await db().collection("doctors").document(doctor_id).get()
    return Doctor(id=snap.id, **snap.to_dict()) if snap.exists else None


async def doctor_by_telegram(chat_id: int) -> Optional[Doctor]:
    async for snap in (
        db()
        .collection("doctors")
        .where(filter=FieldFilter("telegram_chat_id", "==", chat_id))
        .stream()
    ):
        return Doctor(id=snap.id, **snap.to_dict())
    return None


async def update_doctor(doctor_id: str, **fields: Any) -> None:
    await db().collection("doctors").document(doctor_id).update(fields)


async def doctor_by_token(token: str) -> Optional[Doctor]:
    async for snap in (
        db()
        .collection("doctors")
        .where(filter=FieldFilter("web_token", "==", token))
        .stream()
    ):
        return Doctor(id=snap.id, **snap.to_dict())
    return None


# --------------------------------------------------------------------------- #
# Patients
# --------------------------------------------------------------------------- #
async def create_patient(patient: Patient) -> Patient:
    await db().collection("patients").document(patient.id).set(_write(patient))
    return patient


async def get_patient(patient_id: str) -> Optional[Patient]:
    snap = await db().collection("patients").document(patient_id).get()
    return Patient(id=snap.id, **snap.to_dict()) if snap.exists else None


async def list_patients(doctor_id: str) -> list[Patient]:
    out = [
        Patient(id=s.id, **s.to_dict())
        async for s in db()
        .collection("patients")
        .where(filter=FieldFilter("doctor_id", "==", doctor_id))
        .stream()
    ]
    return sorted(out, key=lambda p: p.created_at)


async def patient_by_telegram(chat_id: int) -> Optional[Patient]:
    async for snap in (
        db()
        .collection("patients")
        .where(filter=FieldFilter("channels.telegram_chat_id", "==", chat_id))
        .stream()
    ):
        return Patient(id=snap.id, **snap.to_dict())
    return None


async def patients_by_telegram(chat_id: int) -> list[Patient]:
    """Every patient record bound to this chat. Normally none or one."""
    return [
        Patient(id=snap.id, **snap.to_dict())
        async for snap in db()
        .collection("patients")
        .where(filter=FieldFilter("channels.telegram_chat_id", "==", chat_id))
        .stream()
    ]


async def update_patient(patient_id: str, **fields: Any) -> None:
    await db().collection("patients").document(patient_id).update(fields)


# --------------------------------------------------------------------------- #
# A doctor's chat bound as a patient (S12 item 2)
# --------------------------------------------------------------------------- #
# Mohamed tapped one of his own patients' deep links on his own phone and the
# bot bound his chat as that patient. core/tg_router.py refuses that now, but a
# board that already carries one is a live wrong binding: the doctor's typed
# messages arrive as the patient's and every card meant for the patient goes to
# him. It is quiet, so it is checked rather than hoped about.
async def doctor_chat_bindings() -> list[dict[str, Any]]:
    """Patients whose telegram chat is really a doctor's chat. Empty is healthy."""
    out: list[dict[str, Any]] = []
    async for snap in db().collection("doctors").stream():
        doctor = Doctor(id=snap.id, **snap.to_dict())
        if doctor.telegram_chat_id is None:
            continue
        for patient in await patients_by_telegram(doctor.telegram_chat_id):
            out.append({"patient_id": patient.id, "patient_name": patient.name,
                        "doctor_id": doctor.id, "doctor_name": doctor.name,
                        "chat_id": doctor.telegram_chat_id})
    return out


# --------------------------------------------------------------------------- #
# Loops
# --------------------------------------------------------------------------- #
async def create_loop(loop: Loop) -> Loop:
    await db().collection("loops").document(loop.id).set(_write(loop))
    return loop


async def get_loop(loop_id: str) -> Optional[Loop]:
    snap = await db().collection("loops").document(loop_id).get()
    return Loop(id=snap.id, **snap.to_dict()) if snap.exists else None


async def update_loop(loop_id: str, **fields: Any) -> None:
    fields.setdefault("updated_at", now())
    await db().collection("loops").document(loop_id).update(fields)


async def list_loops(patient_id: str) -> list[Loop]:
    out = [
        Loop(id=s.id, **s.to_dict())
        async for s in db()
        .collection("loops")
        .where(filter=FieldFilter("patient_id", "==", patient_id))
        .stream()
    ]
    return sorted(out, key=lambda l: l.created_at)


# --------------------------------------------------------------------------- #
# Sends - the Chaser's idempotency ledger, one row per nudge actually sent
# --------------------------------------------------------------------------- #
# What a claim can answer. Three words, because "may I send this" has three
# honest answers and a boolean only had two.
CLAIMED = "claimed"          # this wake-up is mine, nobody has had it
ALREADY_SENT = "already sent"  # somebody has it or has finished with it
RESEND = "resend"            # the last attempt failed on delivery; try once more
# How many times a failed receipt may be sent again. One: a delivery that fails
# twice is a channel that is down, and a queue of retries against it would be a
# patient receiving five copies when it comes back.
MAX_RESENDS = 1


# --------------------------------------------------------------------------- #
# Claim leases - codex re-audit 4
# --------------------------------------------------------------------------- #
# Every claim in this file was permanent. A send claimed by an instance that
# then died, a confirm claimed by a request that was cut off mid-commit, a card
# claimed by a press whose process went away: all three left a row that said
# "somebody is doing this" for ever, and nothing ever did it. The patient was
# never nudged again on that rung and the doctor's Confirm answered "already
# being made" until somebody went into Firestore by hand.
#
# So a claim carries the moment it was taken and who took it, and it is only
# good for five minutes. Five is chosen against the two clocks that matter: a
# Cloud Run request is capped well below it, and the Coordinator's own turn is
# capped at twenty five seconds, so a claim older than five minutes belongs to
# nobody. The owner id is not a lock, it is what the audit line names.
CLAIM_LEASE = timedelta(minutes=5)


def _claimed_at(row: dict[str, Any]) -> Optional[datetime]:
    """When this row was claimed, however the claim recorded it.

    Firestore hands a timestamp back as a datetime; the card claim stores an
    ISO string inside an event's meta map, because that map is the doctor's
    audit trail and is read as text. Both are read here so the lease is one
    rule rather than one rule per collection.
    """
    for key in ("claimed_at", "created_at"):
        value = row.get(key)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def claim_expired(row: dict[str, Any], at: Optional[datetime] = None) -> bool:
    """Is the claim on this row old enough that its owner is presumed gone?

    A row that records no moment at all is NOT expired. That is deliberate and
    it is the fail-closed direction: a claim of unknown age might still be in
    flight, and taking it over would be the duplicate message this whole file
    exists to prevent. Every claim written from now on carries a moment, so the
    unknown case is only ever a row written before this shipped.
    """
    taken = _claimed_at(row)
    if taken is None:
        return False
    return ((at or now()) - taken) > CLAIM_LEASE


async def claim_send(send: Send, owner: str = "") -> str:
    """Reserve one wake-up before anything is written or spoken.

    `create` fails when the document exists, which is the whole point: two tasks
    for the same attempt - a Cloud Tasks retry, or a force_due racing the timer -
    cannot both come back CLAIMED, so the patient is never messaged twice.

    The second answer is the one codex item 5 added. A receipt whose delivery
    failed is kept as "failed" and never deleted, so the wake-up cannot be
    replayed from the beginning and counted twice; a retry of the same task
    finds it, flips it back to "claimed" inside a transaction, and is allowed
    to send the message once more and no more than once.

    The third is the lease (codex re-audit 4). A row that is still "claimed"
    five minutes later belongs to an instance that is not coming back, so the
    retry takes it over instead of reading it as a message that was sent.
    """
    owner = owner or new_id()
    body = {**_write(send), "claimed_at": now(), "claimed_by": owner}
    try:
        await db().collection("sends").document(send.id).create(body)
        return CLAIMED
    except gexc.AlreadyExists:
        pass

    ref = db().collection("sends").document(send.id)

    @firestore.async_transactional
    async def claim(transaction: Any) -> str:
        snap = await ref.get(transaction=transaction)
        row = snap.to_dict() or {}
        if row.get("state") == CLAIMED and claim_expired(row):
            transaction.update(ref, {"claimed_at": now(), "claimed_by": owner,
                                     "reclaimed": True})
            return CLAIMED
        if row.get("state") != "failed" or int(row.get("resends") or 0) >= MAX_RESENDS:
            return ALREADY_SENT
        transaction.update(ref, {"state": CLAIMED, "error": "",
                                 "claimed_at": now(), "claimed_by": owner,
                                 "resends": int(row.get("resends") or 0) + 1})
        return RESEND

    return await claim(db().transaction())


# --------------------------------------------------------------------------- #
# One receipt per channel on a send - codex re-audit 5
# --------------------------------------------------------------------------- #
async def channels_done(send_id: str) -> frozenset[str]:
    """Which channels have already delivered this message. Empty is normal.

    core/adapters.py reads this before a fan-out and writes it after each
    channel, so the one retry a failed delivery is allowed re-delivers only on
    the channel that failed. Without it a Telegram outage put a second copy of
    the same reminder in the doctor's console feed every time.
    """
    if not send_id:
        return frozenset()
    snap = await db().collection("sends").document(send_id).get()
    row = snap.to_dict() or {} if snap.exists else {}
    return frozenset(name for name in ("web", "telegram")
                     if row.get(f"{name}_done"))


async def mark_channel_done(send_id: str, channel: str) -> None:
    """This channel has delivered this message. Never unset."""
    if not send_id or channel not in ("web", "telegram"):
        return
    await db().collection("sends").document(send_id).update(
        {f"{channel}_done": True})


async def mark_send(send_id: str, state: str, error: str = "") -> None:
    """The receipt's own outcome: "sent", or "failed" with the error on it."""
    await db().collection("sends").document(send_id).update(
        {"state": state, "error": error}
    )


async def sends_for_patient(patient_id: str) -> list[Send]:
    return [
        Send(id=s.id, **s.to_dict())
        async for s in db()
        .collection("sends")
        .where(filter=FieldFilter("patient_id", "==", patient_id))
        .stream()
    ]


async def release_send(send_id: str) -> None:
    """Undo a claim when nothing at all happened on this wake-up.

    This is not what a failed delivery does. A message that was attempted and
    threw leaves a "failed" receipt (`mark_send`) that is kept for ever, because
    the loop state and the audit event were already written for it and a
    released claim would let the retry write them a second time. Releasing is
    for the case where the turn died before anything was said or counted, so
    the retry really is the first attempt.
    """
    await db().collection("sends").document(send_id).delete()


# --------------------------------------------------------------------------- #
# The contact ledger - one row per patient per Cairo day, whoever spoke
# --------------------------------------------------------------------------- #
# codex item 12. "One message per patient per day" was three different counts
# before this: the Chaser counted Send rows (ladder nudges only), the
# Coordinator counted `contact_days` on one loop, and the pre-approved
# reluctance line was counted nowhere at all. A patient with two open loops
# could therefore hear from Sanad three times in one day and every guard was
# still satisfied.
#
# There is one row now, and every outbound patient message that Sanad itself
# started goes through it: the ladder nudge, every Coordinator template, the
# doctor's pre-approved reluctance line, and the administrative intent replies.
# The doctor's own relay answers are not contacts, because they are his words
# to a patient who asked him a question, and neither is the onboarding hello,
# which is the patient opening his own link.
LADDER, COORDINATOR, RELUCTANCE, INTENT = (
    "ladder", "coordinator", "reluctance", "intent")


def contact_id(patient_id: str, day_index: int) -> str:
    """The ledger row for one patient on one day. Deterministic, so the guard
    reads it with a single get and never a query."""
    return f"{patient_id}:{day_index}"


async def note_contact(patient_id: str, doctor_id: str, day_index: int,
                       kind: str, loop_id: str = "") -> int:
    """Record one outbound contact. Returns how many that patient had that day.

    A transaction, not a read-then-write: two loops of the same patient waking
    in the same second must not both read zero.
    """
    ref = db().collection("contacts").document(contact_id(patient_id, day_index))

    @firestore.async_transactional
    async def bump(transaction: Any) -> int:
        snap = await ref.get(transaction=transaction)
        row = snap.to_dict() or {}
        count = int(row.get("count") or 0) + 1
        kinds = [k for k in (row.get("kinds") or []) if isinstance(k, str)]
        if kind not in kinds:
            kinds.append(kind)
        loops = [l for l in (row.get("loops") or []) if isinstance(l, str)]
        if loop_id and loop_id not in loops:
            loops.append(loop_id)
        body = {"patient_id": patient_id, "doctor_id": doctor_id,
                "day_index": day_index, "count": count, "kinds": kinds,
                "loops": loops, "last_at": now()}
        if snap.exists:
            transaction.update(ref, body)
        else:
            transaction.set(ref, body)
        return count

    return await bump(db().transaction())


# --------------------------------------------------------------------------- #
# Reserving a contact before anything is thought or spoken - codex re-audit 6
# --------------------------------------------------------------------------- #
# "One message a day" and "six contacts on this loop" were both checked at the
# top of core/chaser.fire and written a hundred lines later, with a model call
# in between. Two loops of the same patient waking in the same tick therefore
# both read "nobody has spoken to him today", both paid for a model turn, and
# both messaged him. The check and the write are one step now, and they happen
# before the model call rather than after it, so what the guard allowed is what
# was spent.
NO_DAY_LEFT = "one message per patient per day"
NO_CONTACTS_LEFT = "the contact limit on this loop is spent"


async def reserve_contact(patient_id: str, doctor_id: str, day_index: int,
                          loop_id: str, kind: str, *,
                          max_contacts: Optional[int] = None,
                          allow_same_day: bool = False) -> dict[str, Any]:
    """Read both guards and spend both budgets in one transaction.

    Returns {"ok": True, "count", "contacts"} when the message may be sent, and
    {"ok": False, "why"} when it may not. A refusal has written nothing.

    `allow_same_day` is what /force_due passes: a doctor asking for a reminder
    now is the doctor's call, exactly as it is for quiet hours. The contact is
    still counted, because he did hear from Sanad.
    """
    contact_ref = db().collection("contacts").document(
        contact_id(patient_id, day_index))
    loop_ref = db().collection("loops").document(loop_id)

    @firestore.async_transactional
    async def reserve(transaction: Any) -> dict[str, Any]:
        # Every read before every write: a Firestore transaction requires it,
        # and it is also the order the guard reads in.
        contact_snap = await contact_ref.get(transaction=transaction)
        loop_snap = await loop_ref.get(transaction=transaction)
        contact_row = contact_snap.to_dict() or {}
        loop_row = loop_snap.to_dict() or {}

        if contact_snap.exists and not allow_same_day:
            return {"ok": False, "why": NO_DAY_LEFT}
        contacts = int(loop_row.get("contacts") or 0)
        if max_contacts is not None and contacts >= max_contacts:
            return {"ok": False, "why": NO_CONTACTS_LEFT,
                    "contacts": contacts, "limit": max_contacts}

        count = int(contact_row.get("count") or 0) + 1
        kinds = [k for k in (contact_row.get("kinds") or []) if isinstance(k, str)]
        if kind not in kinds:
            kinds.append(kind)
        loops = [l for l in (contact_row.get("loops") or []) if isinstance(l, str)]
        if loop_id and loop_id not in loops:
            loops.append(loop_id)
        body = {"patient_id": patient_id, "doctor_id": doctor_id,
                "day_index": day_index, "count": count, "kinds": kinds,
                "loops": loops, "last_at": now()}
        if contact_snap.exists:
            transaction.update(contact_ref, body)
        else:
            transaction.set(contact_ref, body)
        transaction.update(loop_ref, {
            "contacts": contacts + 1,
            "contact_days": firestore.ArrayUnion([day_index]),
            "updated_at": now(),
        })
        return {"ok": True, "count": count, "contacts": contacts + 1}

    return await reserve(db().transaction())


async def refund_day(patient_id: str, day_index: int, loop_id: str = "") -> int:
    """Give back a reserved contact the patient never received. Returns the count.

    Fable's review of S12, R1. The reservation is written before the model turn,
    which is what makes "one message a day" true against two wake-ups in the
    same tick. The price of writing it that early is that a wake-up which ends
    in silence has spent the patient's day on nothing: the Coordinator
    escalating to the doctor, changing a state or pausing a loop says nothing to
    the patient, and neither does a delivery refused because the schedule moved
    underneath it. Before this slice the row was written only when a message
    actually went out, so a patient with two open loops was refused his second
    loop's reminder that day having heard nothing at all.

    So the three silent paths hand the day back, in a transaction, and the row
    is deleted outright when its count reaches zero, because `contacted_on`
    asks whether the row exists. The loop is taken off the row's `loops` list
    with it, so what the row says about who spoke to this patient today stays
    true.

    The one path that does NOT hand it back is an explicit delivery failure. A
    message that was decided, counted and attempted may well have arrived, and
    this codebase has already chosen which of the two errors is smaller
    (core/chaser.py, the order of operations): at worst the patient hears
    nothing more from Sanad today, which is what happened anyway.
    """
    ref = db().collection("contacts").document(contact_id(patient_id, day_index))

    @firestore.async_transactional
    async def give_back(transaction: Any) -> int:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return 0
        row = snap.to_dict() or {}
        count = max(0, int(row.get("count") or 0) - 1)
        if count == 0:
            transaction.delete(ref)
            return 0
        loops = [l for l in (row.get("loops") or [])
                 if isinstance(l, str) and l != loop_id]
        transaction.update(ref, {"count": count, "loops": loops,
                                 "last_at": now()})
        return count

    return await give_back(db().transaction())


async def add_contact_kind(patient_id: str, day_index: int, kind: str) -> None:
    """Say what an already-reserved contact was spent on. Never counts one.

    A wake-up reserves its contact as the ladder's, because the ladder is what
    happens unless the Coordinator takes the wake-up over. When it does take it
    over, the message that goes out is its own, and the ledger row should say
    so: the count is the same either way, and the label is the thing a doctor
    reads when he asks what his patient's one message of the day was spent on.
    A union, so the row ends up naming both the wake-up and what answered it,
    and so a repeat writes nothing new.
    """
    if not kind:
        return
    await db().collection("contacts").document(
        contact_id(patient_id, day_index)).update(
            {"kinds": firestore.ArrayUnion([kind])})


async def contacted_on(patient_id: str, day_index: int) -> bool:
    """Has this patient heard from Sanad on this day, from any loop at all?"""
    snap = await db().collection("contacts").document(
        contact_id(patient_id, day_index)).get()
    return snap.exists


async def contact_days_for_patient(patient_id: str) -> tuple[int, ...]:
    """Every day this patient has heard from Sanad, as the guard reads them."""
    days = [
        int((s.to_dict() or {}).get("day_index") or 0)
        async for s in db()
        .collection("contacts")
        .where(filter=FieldFilter("patient_id", "==", patient_id))
        .stream()
    ]
    return tuple(sorted(days))


# --------------------------------------------------------------------------- #
# Atomic loop counters - codex item 13
# --------------------------------------------------------------------------- #
# Every one of these was a read-modify-write across an await before this, which
# on two wake-ups in the same second loses one of them. `Increment` and
# `ArrayUnion` are applied by the server, so the count is right without anyone
# holding a lock, and the two that have to answer with their new value take a
# transaction instead.
async def add_contact(loop_id: str, day_index: int) -> None:
    """One more contact on this loop, on this day. Server-side, not read-modify."""
    await db().collection("loops").document(loop_id).update({
        "contacts": firestore.Increment(1),
        "contact_days": firestore.ArrayUnion([day_index]),
        "updated_at": now(),
    })


async def refund_contact(loop_id: str) -> int:
    """Give back one contact on this loop. Returns what the count became.

    The other half of `add_contact`, and it exists because of the order the
    Chaser writes in. The counters go down before the message goes out, so that
    a message that DID leave can never end up uncounted (codex item 5); the
    price of that order is that a delivery which throws has already spent one of
    the six contacts the doctor's policy allows on this obligation. Sanad then
    refuses the seventh contact over a message the patient never received.

    So an explicit delivery failure hands it back, inside a transaction and
    floored at zero, and `core/chaser.resend` counts again when it retries. The
    invariant that survives both is the one that matters: the number of contacts
    on a loop is the number of messages that reached the wire.

    The DAY is not handed back HERE, and that is still right for the caller
    this was written for: an explicit delivery failure is a message that was
    decided, counted and attempted, and may well have arrived. Leaving the day
    spent is the quiet direction there: at worst the patient hears nothing more
    from Sanad today, which is what happened anyway.

    A wake-up that ended in silence is the other case, and it has its own
    function: `refund_day` below, called beside this one on the three paths
    where nothing was said to the patient at all (Fable's review of S12, R1).
    """
    ref = db().collection("loops").document(loop_id)

    @firestore.async_transactional
    async def give_back(transaction: Any) -> int:
        snap = await ref.get(transaction=transaction)
        row = snap.to_dict() or {}
        value = max(0, int(row.get("contacts") or 0) - 1)
        transaction.update(ref, {"contacts": value, "updated_at": now()})
        return value

    return await give_back(db().transaction())


async def append_reading(loop_id: str, row: dict[str, Any]) -> None:
    """One patient reading onto a MONITOR loop, without losing a concurrent one.

    ArrayUnion also makes a replayed message harmless: the same (day, slot,
    value) row added twice is one row.
    """
    await db().collection("loops").document(loop_id).update({
        "readings": firestore.ArrayUnion([row]),
        "updated_at": now(),
    })


async def append_result(
    loop_id: str, rows: Union[dict[str, Any], list[dict[str, Any]]]
) -> None:
    """Extracted lab results onto a TEST loop. Same rule as the readings.

    One row or a whole slip's worth: a slip is read as a set of analytes and
    they belong on the loop together, so the list form is one ArrayUnion and one
    round trip rather than five. Either way the union is what makes a replayed
    photo harmless, and what stops a second slip on the same loop erasing the
    first, which a whole-list write did (codex item 13).
    """
    batch = [rows] if isinstance(rows, dict) else list(rows)
    if not batch:
        return
    await db().collection("loops").document(loop_id).update({
        "results": firestore.ArrayUnion(batch),
        "updated_at": now(),
    })


async def _bump_field(loop_id: str, field: str, also: Optional[dict] = None) -> int:
    """Increment one integer on a loop and answer with what it became."""
    ref = db().collection("loops").document(loop_id)

    @firestore.async_transactional
    async def bump(transaction: Any) -> int:
        snap = await ref.get(transaction=transaction)
        row = snap.to_dict() or {}
        value = int(row.get(field) or 0) + 1
        transaction.update(ref, {field: value, "updated_at": now(),
                                 **(also or {})})
        return value

    return await bump(db().transaction())


async def add_evidence_request(loop_id: str) -> int:
    """One more "please send the missing part" on this loop. Returns the count.

    codex re-audit 13. The Coordinator read `loop.evidence_requests` off a
    snapshot taken at the top of its turn and wrote back that number plus one,
    across a model call, so two turns on one loop in the same second both wrote
    1 and the guard that allows exactly two requests could be walked past for
    ever. It is a transaction now, like every other counter a guard reads.
    """
    return await _bump_field(loop_id, "evidence_requests")


async def claim_delivery(loop_id: str, schedule_version: int, generation: int,
                         at: datetime) -> Optional[int]:
    """The last gate in front of a nudge. None means the schedule moved on.

    codex re-audit 9. `core/chaser.fire` checked the loop's schedule version at
    the top and delivered a hundred lines later, and everything in between is
    slow: the doctor's policy, the idempotency claim, and a model turn that may
    take twenty five seconds. A patient who rescheduled inside that window, or
    a doctor who moved the date from his phone, still got the old reminder.

    So the version and the generation are read again here, inside the same
    transaction that spends the attempt, and the delivery is refused unless
    both are still what the wake-up started with. That makes the check and the
    send one step: nothing can move in between, because there is no in between.

    The attempt counter is spent in the same transaction rather than read and
    written across an await (codex re-audit 13), so the number this returns is
    the number the record holds.
    """
    ref = db().collection("loops").document(loop_id)

    @firestore.async_transactional
    async def claim(transaction: Any) -> Optional[int]:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return None
        row = snap.to_dict() or {}
        if int(row.get("schedule_version") or 0) != int(schedule_version):
            return None
        if int(row.get("generation") or 0) != int(generation):
            return None
        attempts = int(row.get("attempts") or 0) + 1
        transaction.update(ref, {"attempts": attempts,
                                 "state": "waiting_patient",
                                 "last_attempt_at": at, "updated_at": now()})
        return attempts

    return await claim(db().transaction())


async def add_reluctance(loop_id: str) -> int:
    """"I am fine, why should I come back?", counted. Returns the count.

    The Coordinator prints this number to the doctor ("this is refusal number
    2") and escalates on the second one, so it must be the number the write
    actually produced and not one a stale read guessed at.
    """
    return await _bump_field(loop_id, "reluctance")


async def bump_generation(loop_id: str) -> int:
    """A reply resets the ladder: attempts to zero, generation up by one.

    The two move together and in one write, because a generation without the
    reset would suppress nothing and a reset without the generation is the
    defect (codex item 7).
    """
    return await _bump_field(loop_id, "generation", also={"attempts": 0})


async def bump_schedule_version(loop_id: str) -> int:
    """A reschedule happened. Everything queued for the old version is stale."""
    return await _bump_field(loop_id, "schedule_version")


async def claim_resume(loop_id: str, note: str) -> bool:
    """Take a loop off its barrier, once, whoever answers the card first.

    codex item 13. Two answers on the same barrier card used to read "paused"
    both times and enqueue two contacts. The state change is the claim now: the
    first answer flips paused to False inside a transaction and the second
    finds it already false and does nothing.
    """
    ref = db().collection("loops").document(loop_id)

    @firestore.async_transactional
    async def claim(transaction: Any) -> bool:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return False
        row = snap.to_dict() or {}
        if not (row.get("paused") or row.get("barrier")):
            return False
        transaction.update(ref, {"paused": False, "barrier": "",
                                 "barrier_note": note, "updated_at": now()})
        return True

    return await claim(db().transaction())


# --------------------------------------------------------------------------- #
# Demo settings - the run id and the time scale, changeable without a redeploy
# --------------------------------------------------------------------------- #
SETTINGS_DOC = "demo"


async def get_settings() -> dict[str, Any]:
    snap = await db().collection("settings").document(SETTINGS_DOC).get()
    return snap.to_dict() or {} if snap.exists else {}


async def set_settings(**fields: Any) -> dict[str, Any]:
    ref = db().collection("settings").document(SETTINGS_DOC)
    await ref.set({k: v for k, v in fields.items() if v is not None}, merge=True)
    return await get_settings()


# --------------------------------------------------------------------------- #
# Events (append-only history)
# --------------------------------------------------------------------------- #
async def add_event(event: Event) -> Event:
    await db().collection("events").document(event.id).set(_write(event))
    return event


async def list_events(doctor_id: str) -> list[Event]:
    out = [
        Event(id=s.id, **s.to_dict())
        async for s in db()
        .collection("events")
        .where(filter=FieldFilter("doctor_id", "==", doctor_id))
        .stream()
    ]
    return sorted(out, key=lambda e: e.ts)


async def get_event(event_id: str) -> Optional[Event]:
    """One event by id. The unexpected-result buttons read their values back
    out of the event that produced the card, so nothing is held in between."""
    snap = await db().collection("events").document(event_id).get()
    return Event(id=snap.id, **snap.to_dict()) if snap.exists else None


async def update_event(event_id: str, **fields: Any) -> None:
    """The one exception to "nothing here mutates an event".

    A card's `meta.card.resolved` flag is written back onto the event that
    produced the card, so a reload cannot resurrect a card the doctor already
    finished (core/cards.py). Nothing else updates an event: the text, the
    kind, the timestamp and the media of a stored event are still write-once,
    so the history a judge reads is unchanged by this.
    """
    await db().collection("events").document(event_id).update(fields)


async def claim_card_action(event_id: str, action_id: str, at: datetime) -> bool:
    """Flag one card as being acted on, before the work starts. False: it is taken.

    codex item 17. The action route did the domain work and only afterwards
    wrote the resolved flag, so a doctor who tapped Confirm twice (a slow
    network, a phone that re-sent the callback) ran the work twice and the
    second run was a second patient. The claim is a transaction on the card
    event itself, so the second press is refused before anything happens.
    """
    ref = db().collection("events").document(event_id)

    @firestore.async_transactional
    async def claim(transaction: Any) -> bool:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return False
        meta = dict((snap.to_dict() or {}).get("meta") or {})
        card = dict(meta.get("card") or {})
        if card.get("resolved"):
            return False
        # codex re-audit 4, the card's half of the lease. A press whose request
        # died left `claimed_by` on the card and the button was dead for ever:
        # the doctor could see the card, press it, and be told "already done"
        # about work that never ran. The claim is good for five minutes.
        if card.get("claimed_by") and not claim_expired(card, at):
            return False
        card["claimed_by"] = action_id
        card["claimed_at"] = at.isoformat()
        meta["card"] = card
        transaction.update(ref, {"meta": meta})
        return True

    return await claim(db().transaction())


# --------------------------------------------------------------------------- #
# One card action, once - codex re-audit 17
# --------------------------------------------------------------------------- #
# The card claim above is a fact on the CARD, and it is given back when the work
# behind the button fails, which is what lets a doctor press again after a real
# failure. What it could not answer is the other half: the work succeeded and
# the write that retires the card threw, so the claim went back and the next
# press did the work a second time. Two Confirms are two patients.
#
# So the domain work carries its own key, and the key is the action id the
# doctor pressed. It is a create-if-absent row, which is a transaction: the
# second press finds it and does nothing at all. It is released only when the
# work itself failed, never when the bookkeeping after it did.
async def claim_action(doctor_id: str, action_id: str) -> bool:
    """Take one card action for this doctor. False: it has already been done."""
    ident = f"{doctor_id}:{action_id}"
    try:
        await db().collection("card_actions").document(ident).create(
            {"doctor_id": doctor_id, "action_id": action_id, "at": now()})
        return True
    except gexc.AlreadyExists:
        return False


async def release_action(doctor_id: str, action_id: str) -> None:
    """Give one back, because the work behind the button threw."""
    await db().collection("card_actions").document(
        f"{doctor_id}:{action_id}").delete()


# --------------------------------------------------------------------------- #
# Reclaiming what a dead instance was holding - codex re-audit 4
# --------------------------------------------------------------------------- #
async def reclaim_stale(at: Optional[datetime] = None) -> dict[str, int]:
    """Free every claim whose lease has run out. Returns how many, per kind.

    Called at the top of the Cloud Tasks wake handler, because a wake-up is the
    moment Sanad is already awake and looking at this data, and an admin may
    call it by hand. The per-claim lease checks in `claim_send`,
    `claim_confirm` and `claim_card_action` are what actually make a stranded
    claim takeable; this is the sweep that clears them without waiting for
    somebody to press the same button again.

    Card claims are not swept here. They live inside an event's meta map and
    Firestore cannot query into one without an index this demo deliberately
    does not create, so they are freed by the lease on the next press, which is
    the only moment a stranded card claim costs anybody anything.
    """
    at = at or now()
    freed = {"sends": 0, "pending_confirms": 0}

    async for snap in (
        db().collection("sends")
        .where(filter=FieldFilter("state", "==", CLAIMED)).stream()
    ):
        if claim_expired(snap.to_dict() or {}, at):
            # Deleted, not marked failed, and the difference matters. "Failed"
            # means the message was decided, written and counted and only the
            # delivery threw, so the retry may send it and touch nothing else.
            # A stranded claim is the other case: the instance died with the
            # claim in hand and no way to know how far it got, so the honest
            # answer is to let the retry be a first attempt again. It may count
            # one contact twice; the alternative is a patient who is never
            # reminded, and this codebase already chose which of those two is
            # the smaller error (core/chaser.py, the order of operations).
            await snap.reference.delete()
            freed["sends"] += 1

    async for snap in (
        db().collection("pending_confirms")
        .where(filter=FieldFilter("state", "==", COMMITTING)).stream()
    ):
        if claim_expired(snap.to_dict() or {}, at):
            await snap.reference.update({"state": PENDING, "reclaimed": True})
            freed["pending_confirms"] += 1

    return freed


async def release_card_action(event_id: str) -> None:
    """Give a card back when the work behind the button failed."""
    ref = db().collection("events").document(event_id)
    snap = await ref.get()
    if not snap.exists:
        return
    meta = dict((snap.to_dict() or {}).get("meta") or {})
    card = dict(meta.get("card") or {})
    card.pop("claimed_by", None)
    card.pop("claimed_at", None)
    meta["card"] = card
    await ref.update({"meta": meta})


# --------------------------------------------------------------------------- #
# Reports - completion reports and digests, as records rather than as text
# --------------------------------------------------------------------------- #
async def save_report(report: Report) -> Report:
    await db().collection("reports").document(report.id).set(_write(report))
    return report


async def list_reports(doctor_id: str) -> list[Report]:
    """This doctor's reports, newest first, which is how they are read."""
    rows = [
        Report(id=s.id, **s.to_dict())
        async for s in db()
        .collection("reports")
        .where(filter=FieldFilter("doctor_id", "==", doctor_id))
        .stream()
    ]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


# --------------------------------------------------------------------------- #
# Pending confirms
# --------------------------------------------------------------------------- #
async def save_confirm(confirm: PendingConfirm) -> PendingConfirm:
    await db().collection("pending_confirms").document(confirm.id).set(_write(confirm))
    return confirm


async def get_confirm(confirm_id: str) -> Optional[PendingConfirm]:
    snap = await db().collection("pending_confirms").document(confirm_id).get()
    return PendingConfirm(id=snap.id, **snap.to_dict()) if snap.exists else None


async def delete_confirm(confirm_id: str) -> None:
    await db().collection("pending_confirms").document(confirm_id).delete()


# The two states a proposal has while it is being committed. Nothing else is a
# state: a proposal that has been committed is deleted, which is what it always
# was, so the board and the six-hour expiry are unchanged.
PENDING, COMMITTING = "pending", "committing"


async def claim_confirm(confirm_id: str, owner: str = "") -> bool:
    """Claim a proposal before a single record is written. False: somebody has it.

    codex item 6. Confirm used to create the patient, then the loops, then the
    link, then the tasks, with nothing in front of it: two taps on the same card
    (a double click, a Telegram callback the phone re-sent) made two patients
    with two ladders on the same person. The claim is the transaction that makes
    the second tap a message instead of a record.
    """
    ref = db().collection("pending_confirms").document(confirm_id)

    @firestore.async_transactional
    async def claim(transaction: Any) -> bool:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return False
        row = snap.to_dict() or {}
        state = row.get("state", PENDING)
        # codex re-audit 4. A commit whose instance died left "committing" for
        # ever, and the doctor's Confirm answered "already being made" about a
        # patient nobody was making. Five minutes later the claim is nobody's
        # and the next tap takes it: the records this commit writes carry
        # deterministic ids, so a second run is a rewrite and never a second
        # Ahmed.
        if state != PENDING and not claim_expired(row):
            return False
        transaction.update(ref, {"state": COMMITTING, "claimed_at": now(),
                                 "claimed_by": owner or new_id()})
        return True

    return await claim(db().transaction())


async def release_confirm(confirm_id: str) -> None:
    """Give a claimed proposal back after a commit threw, so a retry may run.

    The records the failed attempt did write carry deterministic ids
    (`derived_id`), so the retry writes the same documents over themselves.
    """
    try:
        await db().collection("pending_confirms").document(confirm_id).update(
            {"state": PENDING}
        )
    except gexc.NotFound:  # the commit got far enough to delete it: nothing to do
        pass


# --------------------------------------------------------------------------- #
# Link tokens, relays, pending Telegram starts
# --------------------------------------------------------------------------- #
async def save_link_token(token: LinkToken) -> LinkToken:
    await db().collection("link_tokens").document(token.id).set(_write(token))
    return token


async def get_link_token(token_id: str) -> Optional[LinkToken]:
    snap = await db().collection("link_tokens").document(token_id).get()
    return LinkToken(id=snap.id, **snap.to_dict()) if snap.exists else None


async def list_link_tokens(doctor_id: str) -> list[LinkToken]:
    """Every patient link this doctor has ever minted, oldest first.

    The board needs one link per patient, not the single latest one, so the QR
    and the `/p/<link>` page can be offered on every row instead of on whichever
    patient happened to be registered last.
    """
    rows = [
        LinkToken(id=s.id, **s.to_dict())
        async for s in db()
        .collection("link_tokens")
        .where(filter=FieldFilter("doctor_id", "==", doctor_id))
        .stream()
    ]
    return sorted(rows, key=lambda t: t.created_at)


async def latest_link_token(doctor_id: str) -> Optional[LinkToken]:
    """The most recently minted patient link, for the QR the console shows."""
    rows = await list_link_tokens(doctor_id)
    return rows[-1] if rows else None


async def burn_link_token(token_id: str) -> None:
    """One-time by design: a used deep link cannot bind a second phone."""
    await db().collection("link_tokens").document(token_id).update({"used": True})


async def consume_link_token(token_id: str) -> Optional[LinkToken]:
    """Read and burn a link token in one transaction, or answer None.

    codex item 14. The check and the burn were two calls with an await between
    them, so two /start messages from two phones inside the same second both
    read `used: False` and both bound themselves to the same patient. Now the
    read and the write are one operation and exactly one of them wins.

    A revoked token is never consumed. Expiry is not decided here, because it
    is a function of the clock rather than of the record: core/links.py owns it.
    """
    ref = db().collection("link_tokens").document(token_id)

    @firestore.async_transactional
    async def consume(transaction: Any) -> Optional[LinkToken]:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return None
        row = snap.to_dict() or {}
        if row.get("used") or row.get("revoked"):
            return None
        transaction.update(ref, {"used": True})
        return LinkToken(id=token_id, **row)

    return await consume(db().transaction())


async def revoke_link_tokens(doctor_id: str) -> int:
    """Kill every patient link this doctor has minted. Returns how many.

    The doctor-side revoke of codex item 14, and it hangs off the gesture that
    already exists: rotating the console token after a recording. A revoked
    link opens nothing, and the patient gets a new one the next time the doctor
    confirms anything about him.
    """
    count = 0
    async for snap in (
        db()
        .collection("link_tokens")
        .where(filter=FieldFilter("doctor_id", "==", doctor_id))
        .stream()
    ):
        if (snap.to_dict() or {}).get("revoked"):
            continue
        await snap.reference.update({"revoked": True})
        count += 1
    return count


async def save_relay(relay: Relay) -> Relay:
    await db().collection("relays").document(relay.id).set(_write(relay))
    return relay


async def get_relay(relay_id: str) -> Optional[Relay]:
    snap = await db().collection("relays").document(relay_id).get()
    return Relay(id=snap.id, **snap.to_dict()) if snap.exists else None


async def open_relays(doctor_id: str) -> list[Relay]:
    """Every question of this doctor's that is still waiting on him.

    The end-of-day summary counts these as the treatment questions that need
    him (core/summary.py), so they are read from the records like everything
    else on that screen.
    """
    rows = [
        Relay(id=s.id, **s.to_dict())
        async for s in db()
        .collection("relays")
        .where(filter=FieldFilter("doctor_id", "==", doctor_id))
        .stream()
    ]
    return sorted([r for r in rows if r.state == "open"], key=lambda r: r.created_at)


async def close_relay(relay_id: str) -> None:
    await db().collection("relays").document(relay_id).update({"state": "answered"})


async def save_pending_start(start: PendingStart) -> PendingStart:
    await db().collection("tg_pending_starts").document(start.id).set(_write(start))
    return start


async def list_pending_starts() -> list[PendingStart]:
    """Every chat that has said /start, newest first.

    Security audit M3. `latest_pending_start` used to be the whole of the bind
    flow, and "the newest one" is whoever spoke last, which on a public bot is
    not necessarily the doctor. The bind route names a chat id now, and this is
    where he reads one.
    """
    rows = [
        PendingStart(id=s.id, **s.to_dict())
        async for s in db().collection("tg_pending_starts").stream()
    ]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


async def latest_pending_start() -> Optional[PendingStart]:
    """The newest /start. Kept for the runbook's own "did it arrive?" check.

    Nothing binds from this any more (security audit M3): it answers whether a
    /start reached the service at all, which is the question the runbook asks
    when the bot looks dead.
    """
    rows = await list_pending_starts()
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# Reset (admin only) - clears a doctor's board so a rehearsal starts clean
# --------------------------------------------------------------------------- #
RESET_COLLECTIONS = ("patients", "loops", "events", "pending_confirms",
                     "link_tokens", "relays", "sends", "reports", "contacts",
                     "card_actions")


async def wipe_doctor(doctor_id: str) -> dict[str, int]:
    """Delete everything belonging to one doctor. The doctor record survives.

    `tg_pending_starts` is the one collection here that carries no doctor_id: a
    /start arrives from a phone nobody has claimed yet. Until S5 this function
    cleared all of them, so resetting a test board wiped the pending /start
    Mohamed was in the middle of binding on his own phone. It now clears only
    the row whose chat is already this doctor's, and otherwise clears none.
    """
    deleted: dict[str, int] = {}
    for name in RESET_COLLECTIONS:
        count = 0
        async for snap in (
            db()
            .collection(name)
            .where(filter=FieldFilter("doctor_id", "==", doctor_id))
            .stream()
        ):
            await snap.reference.delete()
            count += 1
        deleted[name] = count

    doctor = await doctor_by_id(doctor_id)
    chat_id = doctor.telegram_chat_id if doctor is not None else None
    deleted["tg_pending_starts"] = 0
    if chat_id is not None:
        async for snap in db().collection("tg_pending_starts").stream():
            row = snap.to_dict() or {}
            if row.get("chat_id") == chat_id:
                await snap.reference.delete()
                deleted["tg_pending_starts"] += 1
    return deleted
