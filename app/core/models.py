"""Owns every shape Sanad stores or passes around.

Two families:
  - persisted records (Doctor, Patient, Loop, Event, PendingConfirm) -> Firestore
  - LLM proposal shapes (ProposedRecord & friends) -> the Registrar's output_schema

The proposal shapes deliberately avoid free-form `dict` fields: Vertex structured
output rejects an object schema with no declared properties, so metrics arrive as
a list of name/value pairs and loop details as flat optional fields. Code folds
them into the dicts the persisted records use.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

LoopType = Literal["TEST", "MONITOR", "MEDICATION", "VISIT", "TASK"]
LoopState = Literal[
    "open", "waiting_patient", "received", "pending_review", "done", "unreachable"
]
EventKind = Literal[
    "doctor_in", "patient_in", "agent_out", "card", "system", "escalation"
]
Channel = Literal["web", "telegram"]
# What a photographed picture is. The model picks one of these and stops;
# core/photos.py turns the answer into a route.
PhotoKind = Literal["lab_slip", "bp_monitor", "prescription", "other"]


# --------------------------------------------------------------------------- #
# Persisted records
# --------------------------------------------------------------------------- #
class Doctor(BaseModel):
    id: str
    name: str
    # Sanad serves any clinic specialty; the Concierge is told which one so its
    # general answers stay inside the doctor's field.
    specialty: str = "general practice"
    lang: str = "en"
    web_token: str
    telegram_chat_id: Optional[int] = None
    # Set when the doctor taps "Answer" on a relay card in Telegram, cleared on
    # his next message, on /cancel, or when it expires. Firestore holds it
    # because the process holds nothing.
    awaiting_relay_id: Optional[str] = None
    # Same idea for the "Send a note" button on a lab-values card.
    awaiting_note_loop_id: Optional[str] = None
    awaiting_since: Optional[datetime] = None
    # The doctor's own rules for the Care Coordinator: how long the reschedule
    # window is, how many contacts a loop may cost, when the quiet hours are,
    # whether a cost barrier may be discussed with the patient at all, and the
    # one line he pre-approved as the reason a follow-up is worth doing. Absent
    # means the defaults in core/policy.py, which is what the demo runs on.
    policy: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Patient(BaseModel):
    id: str
    doctor_id: str
    name: str
    phone: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    diagnosis: str = ""
    baseline: dict[str, str] = Field(default_factory=dict)
    targets: dict[str, str] = Field(default_factory=dict)
    plan_text: str = ""
    # Lab values that arrived with no matching open TEST loop and that the
    # doctor chose to keep on the record rather than open a loop for.
    results: list[dict[str, Any]] = Field(default_factory=list)
    # Who this person is, in the doctor's own words: "father of Dr Tarek",
    # "lives in Zagazig", "the one with the swollen legs" (S9). Appended and
    # dated, never replaced, and written only at the moment the doctor confirms
    # a dictation, so it is doctor text and a patient can never reach it. It is
    # what lets a later description find the right record: core/identify.py puts
    # these lines in front of the identification read.
    notes: list[dict[str, Any]] = Field(default_factory=list)
    channels: dict[str, Any] = Field(
        default_factory=lambda: {"web": True, "telegram_chat_id": None}
    )
    status: Literal["pending_link", "active"] = "active"
    # When Sanad first said hello to this patient, on whichever channel he
    # opened first. It is the idempotency flag behind core/links.welcome: a
    # reloaded page, a second scan of the QR and a Telegram bind after a web
    # open must not send the plan three times.
    welcomed_at: Optional[datetime] = None
    welcome_lang: str = ""
    # Patient consent controls proactive reminders. It never blocks an inbound
    # message, emergency handling, evidence, or a doctor's direct reply.
    proactive_paused: bool = False
    opt_out_at: Optional[datetime] = None
    created_at: datetime


class Loop(BaseModel):
    id: str
    patient_id: str
    doctor_id: str
    type: LoopType
    title: str
    details: dict[str, Any] = Field(default_factory=dict)
    state: LoopState = "open"
    due_at: Optional[datetime] = None
    # Chaser bookkeeping. `attempts` is how many nudges have actually been sent;
    # any patient reply resets it to zero, which is what keeps a patient who
    # answers from ever being called unreachable.
    attempts: int = 0
    # Which run of the ladder `attempts` is counting. Every reset of attempts
    # increments it, and the Chaser's receipt key carries it
    # (loop:generation:kind:attempt), so a restarted ladder asks for a key the
    # ladder before it never claimed. Without it, any patient reply set
    # attempts back to zero, the restarted ladder asked for loop:nudge:1 again,
    # found the first ladder's receipt and went quiet for good.
    generation: int = 0
    # Which schedule the tasks on the queue were made for. Every reschedule
    # increments it and every task payload carries the version it was made for,
    # so a task from a schedule that has been replaced is dropped on arrival
    # with "superseded schedule" rather than sending a reminder for a date the
    # doctor or the patient has already moved.
    schedule_version: int = 0
    last_attempt_at: Optional[datetime] = None
    last_reply_at: Optional[datetime] = None
    # What the Lab-Extractor read off the slip, judged in code (core/labs.py).
    results: list[dict[str, Any]] = Field(default_factory=list)
    # MONITOR loops only: what the patient has sent back so far.
    readings: list[dict[str, Any]] = Field(default_factory=list)

    # ----------------------------------------------------------------- #
    # Care Coordinator bookkeeping (S6). Every one of these is a fact a
    # guard in core/policy.py reads before a tool is allowed to run.
    # ----------------------------------------------------------------- #
    # Every contact this loop has ever cost the patient. Unlike `attempts`
    # it never resets, because the six-contacts cap is a promise to the
    # patient and a patient who answers must not buy himself more messages.
    contacts: int = 0
    # The day indices (core/timing.day_index) a contact went out on, which
    # is what the one-contact-a-day guard counts.
    contact_days: list[int] = Field(default_factory=list)
    # How many times Sanad has asked for a missing part of the evidence.
    evidence_requests: int = 0
    # The barrier class the Coordinator recorded, from the fixed list in
    # core/policy.BARRIERS. Empty means none is recorded.
    barrier: str = ""
    barrier_note: str = ""
    # "I am fine, why should I come back?", counted. The second refusal
    # goes to the doctor instead of being answered again.
    reluctance: int = 0
    # Reminders stopped on purpose, with a barrier recorded and the doctor
    # told. Not a state, so the board colour and every existing filter are
    # untouched; the Chaser reads it and drops the nudge.
    paused: bool = False
    # The doctor's two-state review gate, as a flag the Coordinator can
    # read. Set by the "Reviewed" button, never by an agent.
    doctor_reviewed: bool = False
    # What the verifier said about the evidence that arrived (core/verify.py).
    verified: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class Send(BaseModel):
    """One wake-up that was claimed. The idempotency key of the Chaser.

    The document id is "<loop id>:<generation>:<kind>:<attempt>", so a task that
    is retried - Cloud Tasks retries on any non-2xx - finds the row already
    there and sends nothing, while a ladder restarted by a patient reply asks
    for a key carrying the new generation and is not suppressed.

    `state` is what makes the order of operations safe (codex item 5). The row
    is created as "claimed" before anything is written or spoken, becomes
    "sent" when the message is actually delivered, and becomes "failed" with
    the error when delivery threw. A failed receipt is kept and never released,
    because releasing it would let a retry redo the whole wake-up, loop state
    and all; instead the retry sees "failed" and may send the message once
    more, which `resends` counts so it can only happen once.
    """

    id: str
    doctor_id: str
    patient_id: str
    loop_id: str
    attempt: int
    generation: int = 0
    kind: str = "nudge"
    state: str = "claimed"
    error: str = ""
    resends: int = 0
    # One receipt per channel (codex re-audit 5). The fan-out writes Web and
    # then Telegram, and a Telegram outage used to make the one allowed retry
    # re-deliver BOTH, so the doctor's console carried a second copy of a
    # reminder the patient never received. core/adapters.py reads these before
    # a fan-out and sets each one as its channel lands, so a retry re-delivers
    # only on the channel that failed.
    web_done: bool = False
    telegram_done: bool = False
    # Who is holding this claim and since when (codex re-audit 4). A claim
    # older than core/store.CLAIM_LEASE belongs to an instance that is not
    # coming back and the next task takes it over.
    claimed_at: Optional[datetime] = None
    claimed_by: str = ""
    run_id: str
    day_index: int
    created_at: datetime


class Event(BaseModel):
    id: str
    doctor_id: str
    patient_id: Optional[str] = None
    loop_id: Optional[str] = None
    kind: EventKind
    channel: Channel = "web"
    text: str = ""
    media: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    ts: datetime


class Report(BaseModel):
    """One completion report or one digest, stored the moment it is written.

    Before S6 block 2 the dashboard found these by matching the first line of an
    `agent_out` event against "Digest for" or "Completion report", which is a
    string match on generated-looking text: reword the heading and the Reports
    screen silently empties. A report is a record now, so the read route asks
    the database what a report is instead of asking the text.
    """

    id: str
    doctor_id: str
    kind: Literal["completion", "digest"]
    patient_id: Optional[str] = None
    title: str
    body: str
    created_at: datetime


class PendingConfirm(BaseModel):
    id: str
    doctor_id: str
    proposed: dict[str, Any]
    expires_at: datetime
    # The commit claim (codex item 6). "pending" until a Confirm claims it in a
    # Firestore transaction, "committing" while the records are being written.
    # A second tap on the same card finds "committing" and is told the record
    # is already being made; a commit that throws puts it back to "pending", so
    # a retry runs again and writes the same deterministic ids over itself
    # rather than making a second patient.
    state: str = "pending"
    # The record this proposal attaches to, when the dictation was about a
    # patient the doctor already follows (S9). Empty means a new patient, which
    # is what every proposal before S9 was. It is set on the way in when the
    # code name matcher and the identification agree, and it is rewritten by
    # the "existing:" and "This is a new patient" buttons, so the doctor's tap
    # is what decides it and nothing is written until Confirm.
    patient_id: Optional[str] = None
    # A relationship or description the identification read out of the
    # dictation, stored on the patient as a dated note at confirm time.
    note: str = ""


class LinkToken(BaseModel):
    """One-time token behind a patient's t.me deep link and its QR image."""

    id: str  # the token itself, carried in ?start=<id>
    doctor_id: str
    patient_id: str
    used: bool = False
    # A patient link is a bearer credential for one person's whole record, and
    # before this it lived for ever (codex item 14). The web page refuses a
    # token older than links.LINK_TTL_DAYS, and the doctor can kill every one
    # of his links at once through POST /admin/rotate-token?revoke_links=true,
    # which is the same gesture that kills the console token on camera.
    revoked: bool = False
    created_at: datetime


class Relay(BaseModel):
    """A question waiting on the doctor: one the Concierge refused to answer, or
    a barrier the Care Coordinator escalated.

    `loop_id` is what makes a barrier card a two-way door rather than a notice.
    A relay carrying one belongs to an obligation, so when the doctor answers it
    the Coordinator knows which loop to unpause and resume (core/coordinator.py:
    resume_after_answer). A relay from the Concierge carries none.
    """

    id: str
    doctor_id: str
    patient_id: str
    loop_id: Optional[str] = None
    question: str
    proposed_reply: str = ""
    reason: str = ""
    state: Literal["open", "answered"] = "open"
    created_at: datetime


class PendingStart(BaseModel):
    """A /start from an unknown Telegram chat, waiting for the admin bind call."""

    id: str  # chat id as a string; one row per chat, re-sending /start overwrites
    chat_id: int
    display_name: str = ""
    created_at: datetime


# --------------------------------------------------------------------------- #
# Registrar LLM output schema
# --------------------------------------------------------------------------- #
class Metric(BaseModel):
    name: str = Field(description="Metric name, e.g. LDL, BP, weight.")
    value: str = Field(description="Value as written by the doctor, e.g. 160.")


class ProposedPatient(BaseModel):
    name: str
    phone: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    diagnosis: str = Field(description="Free-text diagnosis, in English.")


class ProposedLoop(BaseModel):
    """One care loop. Only the fields relevant to `type` need to be filled."""

    type: LoopType
    title: str = Field(description="Short English title, e.g. 'Lipid panel'.")
    due_in_days: Optional[int] = Field(
        default=None, description="Days from today, if the doctor gave a time."
    )
    test_name: Optional[str] = Field(default=None, description="TEST only.")
    metric: Optional[str] = Field(default=None, description="MONITOR only, e.g. BP.")
    schedule: Optional[str] = Field(
        default=None, description="MONITOR only, e.g. 'twice a day'."
    )
    days: Optional[int] = Field(default=None, description="MONITOR only, duration.")
    drug: Optional[str] = Field(default=None, description="MEDICATION only.")
    dose: Optional[str] = Field(default=None, description="MEDICATION only.")
    action: Optional[Literal["start", "stop", "change"]] = Field(
        default=None, description="MEDICATION only."
    )
    text: Optional[str] = Field(default=None, description="TASK only.")


class OtherPatientMention(BaseModel):
    """A second person named in one dictation, never a record to be committed.

    The Registrar may only use this to warn the doctor that one dictation
    contained more than one patient. Code checks the name against the original
    dictation before it is shown; the instruction is deliberately not trusted
    or stored.
    """

    name: str = Field(description="Another patient's name, exactly as dictated.")
    instruction: str = Field(
        default="", description="That patient's instruction, exactly as dictated."
    )


class ProposedRecord(BaseModel):
    patient: ProposedPatient
    baseline: list[Metric] = Field(default_factory=list)
    targets: list[Metric] = Field(default_factory=list)
    plan_text: str = Field(
        description="The doctor's plan rewritten for the patient in plain words."
    )
    loops: list[ProposedLoop] = Field(default_factory=list)
    other_patients: list[OtherPatientMention] = Field(
        default_factory=list,
        description="People named in the dictation other than the primary patient.",
    )


# --------------------------------------------------------------------------- #
# Concierge LLM output schema
# --------------------------------------------------------------------------- #
class ConciergeAnswer(BaseModel):
    """What the patient-facing agent is allowed to return. Nothing else.

    `tier` is the agent's report of which lane it used; the code decides what
    happens next (see core/concierge.py). The agent has no tools, so this schema
    is the only channel out of the model.
    """

    tier: Literal["plan", "general", "relay"] = Field(
        description="plan = answered from the doctor's plan; general = education; "
        "relay = must be passed to the doctor."
    )
    reply: str = Field(
        description="The message to the patient, in the patient's own language."
    )
    relay_reason: str = Field(
        default="", description="If tier is relay, why, in English, for the doctor."
    )


# --------------------------------------------------------------------------- #
# Lab-Extractor LLM output schema
# --------------------------------------------------------------------------- #
class SlipAnalyte(BaseModel):
    """One row of a lab slip, exactly as printed. The model never judges it."""

    analyte: str = Field(description="Analyte name as printed, e.g. LDL, Potassium.")
    value: str = Field(
        default="", description="Value exactly as printed, digits only, no unit. "
        "Empty if it is not readable."
    )
    unit: str = Field(default="", description="Unit as printed, e.g. mg/dL.")
    ref_range: str = Field(
        default="", description="The slip's own printed reference range for this "
        "row, e.g. '3.5 - 5.1'. Empty if the slip prints none."
    )
    flag: str = Field(
        default="", description="The slip's own printed flag for this row, e.g. "
        "H, L, HIGH, POSITIVE. Empty if the slip prints none."
    )


class PhotoReading(BaseModel):
    """What the model is allowed to return about any photograph a patient sends.

    It classifies the picture and reports what it sees, and nothing else: no
    interpretation, no normal/abnormal opinion of its own, no advice. `kind` is
    the one judgement call the model makes on this path; every routing decision
    that follows it is code (core/photos.py). `text_orientation` is what lets
    code rotate the photo and ask once more instead of guessing at sideways text.
    """

    kind: PhotoKind = Field(
        description="lab_slip for a laboratory report, bp_monitor for the screen "
        "of a blood-pressure machine, prescription for a doctor's written "
        "prescription, other for anything else."
    )
    text_orientation: Literal["upright", "sideways", "upside_down"] = Field(
        description="How the printed text sits in the image as given."
    )
    lab_name: str = Field(default="", description="Laboratory name, if visible.")
    patient_name: str = Field(
        default="", description="The patient name printed on the slip, exactly as "
        "printed, in the script it is printed in. Empty if the slip shows none."
    )
    taken_on: str = Field(default="", description="Sample or report date, if visible.")
    analytes: list[SlipAnalyte] = Field(default_factory=list)
    systolic: str = Field(
        default="", description="bp_monitor only: the large upper number, as shown."
    )
    diastolic: str = Field(
        default="", description="bp_monitor only: the lower number, as shown."
    )
    pulse: str = Field(
        default="", description="bp_monitor only: the pulse reading, if shown."
    )


DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "TEST": ("test_name",),
    "MONITOR": ("metric", "schedule", "days"),
    "MEDICATION": ("drug", "dose", "action"),
    "VISIT": (),
    "TASK": ("text",),
}


def loop_details(p: ProposedLoop) -> dict[str, Any]:
    """Fold a proposal's flat fields into the `details` dict for that loop type."""
    return {f: getattr(p, f) for f in DETAIL_FIELDS[p.type] if getattr(p, f) is not None}
