"""Owns intake: a doctor's dictation becomes a proposed record, then a real one.

The flow is deliberately two-step. The agent only ever *proposes*; nothing
reaches `patients` or `loops` until the doctor taps Confirm. Between the two
steps the proposal sits in `pending_confirms` and the process keeps no state.

Stateless pattern: a fresh Agent + InMemorySessionService + Runner are built for
each request and discarded when it returns. No session is ever reused, so any
instance can serve any request and a restart loses nothing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from . import (
    bounded, chaser, contract, duedates, events, extractor, gender, identify,
    links, media, policy, provenance, sentinel, settings, storage, store, telegram,
)
from .adapters import OutboundMessage, fanout
from .models import (
    DETAIL_FIELDS,
    Doctor,
    Loop,
    Patient,
    PendingConfirm,
    OtherPatientMention,
    ProposedLoop,
    ProposedRecord,
    loop_details,
)

log = logging.getLogger("sanad.registrar")

MODEL = "gemini-3.5-flash"
APP_NAME = "sanad-registrar"
CONFIRM_TTL = timedelta(hours=6)

# Telegram refuses a callback_data longer than 64 bytes, and it refuses the
# whole message rather than the one button, so an over-long action id would not
# be a broken button: it would be a card the doctor never receives at all.
#
# S9's "existing:<patient id>:<proposal id>" carries two ids in one action, and
# two full uuid hex strings are 74 bytes with the verb on the front. The patient
# id stays whole, because that is the thing the route validates against the
# doctor's own board. The proposal id is shortened here instead: a pending
# confirm lives for six hours and exists only to be tapped, so 48 bits of it is
# an id, and the guard below is the test that keeps the pair inside the limit.
CONFIRM_ID_LENGTH = 12
TELEGRAM_CALLBACK_LIMIT = 64


def new_confirm_id() -> str:
    return store.new_id()[:CONFIRM_ID_LENGTH]

REGISTRAR_PROMPT = """You are the Registrar of a clinical follow-up system.

A doctor dictates a new patient in one go. The dictation may be English, Arabic,
Egyptian Arabic, or mixed. Your structured output is always English.

Extract:
- the patient's identity and diagnosis, exactly as dictated, nothing invented;
- baseline metrics the doctor stated, and any targets they set;
- plan_text: the doctor's plan rewritten for the patient in plain, warm, everyday
  words. This is the only text the system will ever quote back to the patient, so
  it must contain nothing the doctor did not say;
- loops: one per follow-up item the doctor asked for. Use TEST for a lab or
  imaging order, MONITOR for something the patient measures repeatedly,
  MEDICATION for starting/stopping/changing a drug, VISIT for a return
  appointment, TASK for anything else.
- other_patients: if the doctor mentions more than one patient, choose only one
  primary patient for `patient` and `loops`, and list every other named patient
  here with that person's instruction exactly as dictated. Never merge two
  patients' instructions and never silently omit the other person.

Fill only the fields that belong to a loop's type. TEST needs test_name.
MEDICATION needs drug and action. MONITOR needs metric, and schedule and days
when given. Put relative timing in due_in_days ("in two weeks" -> 14, "in a
month" -> 30); never write a calendar date. If the doctor gave no timing, leave
due_in_days empty.

Do not add follow-ups the doctor did not ask for."""

PRESCRIPTION_PROMPT = REGISTRAR_PROMPT + """

This dictation arrived as a photograph of the doctor's own prescription or
notes. Read what is written on the paper - the patient, the diagnosis, the drugs
and doses, the tests ordered, the follow-up - and fill the same structure from
it. Read only what is written. Do not add an order the paper does not carry, and
leave a field empty rather than guessing at handwriting you cannot read."""


# --------------------------------------------------------------------------- #
# Agent turn
# --------------------------------------------------------------------------- #
async def propose(text: str) -> ProposedRecord:
    """One agent turn, structured output, everything thrown away afterwards."""
    agent = Agent(
        model=MODEL,
        name="registrar",
        instruction=REGISTRAR_PROMPT,
        output_schema=ProposedRecord,
    )
    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=session_service)

    user_id, session_id = "registrar", store.new_id()
    await session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )

    raw = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            raw = "".join(p.text or "" for p in event.content.parts)

    return ProposedRecord.model_validate(json.loads(raw))


async def propose_from_image(image: bytes, mime: str = "image/jpeg") -> ProposedRecord:
    """A photographed prescription -> the same proposal a dictation produces.

    Voice, text and photo are one path: they differ only in how the doctor's
    words reach this function. Everything after it - the code validation, the
    confirm card, the commit - is identical, so a photo can no more create a
    patient without a tap than a voice note can.

    This one is NOT an ADK turn, and the docs say so out loud rather than
    letting "three ADK agents" cover it (rev 17 item 5). `propose` above builds
    an `Agent` with `output_schema=ProposedRecord`; this calls the same model
    with the same schema through `google.genai` directly, because the input is
    image bytes plus a prompt and an ADK agent with an output schema takes a
    text turn. Same model, same schema, same `ProposedRecord`, and the code
    validation below is the thing that decides whether either of them is
    allowed to become a patient, so nothing about the guarantee depends on
    which of the two produced the record. It is a second code path with one
    output type, and pretending otherwise in the architecture document would be
    the kind of small dishonesty a judge is right to punish.
    """
    response = await media.client.aio.models.generate_content(
        model=media.MODEL,
        contents=[
            types.Part.from_bytes(data=await extractor.upright(image),
                                  mime_type="image/jpeg"),
            types.Part(text=PRESCRIPTION_PROMPT),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=ProposedRecord
        ),
    )
    return ProposedRecord.model_validate(json.loads(response.text or "{}"))


# --------------------------------------------------------------------------- #
# Validation (in code, never trusted to the model)
# --------------------------------------------------------------------------- #
# A dictation with no name makes the model invent a placeholder rather than
# leave the field empty, and S1 duly created a patient called "Unknown". These
# are treated as missing: the doctor is asked for the name instead.
PLACEHOLDER_NAMES: frozenset[str] = frozenset(
    sentinel.normalize(n).strip()
    for n in (
        "unknown", "unknown patient", "unnamed", "no name", "not given", "n a",
        "na", "none", "anonymous", "patient", "the patient", "new patient",
        "unspecified", "not specified",
        "غير معروف", "مش معروف", "مجهول", "بدون اسم", "مريض", "المريض",
        "غير محدد",
    )
)


def is_placeholder_name(name: str) -> bool:
    """True when the model filled the name field with a stand-in, not a name."""
    return sentinel.normalize(name).strip() in PLACEHOLDER_NAMES


def checked_other_patients(
    record: ProposedRecord, dictation: str, rows: list[Any] = ()
) -> list[OtherPatientMention]:
    """Second names that code can point to in the doctor's original words.

    `other_patients` is model output, so its instruction is never displayed or
    stored. A name is retained only when the normalized full name occurs in the
    dictation and it is not the primary patient's name. Existing board names
    provide an independent code backstop when the extraction omits the second
    person entirely, which is the deployed two-patient failure Fable found.
    """
    said = sentinel.normalize(dictation).strip()
    primary = sentinel.normalize(record.patient.name).strip()
    kept: list[OtherPatientMention] = []
    seen: set[str] = set()

    padded_said = f" {said} "

    def keep(name: str, instruction: str = "") -> None:
        clean = " ".join(str(name or "").split())
        normalized = sentinel.normalize(clean).strip()
        if (not clean or is_placeholder_name(clean) or normalized == primary
                or normalized in seen or f" {normalized} " not in padded_said):
            return
        seen.add(normalized)
        exact = " ".join(str(instruction or "").split())
        normalized_exact = sentinel.normalize(exact).strip()
        # The instruction is printed only when the model returned a contiguous
        # phrase the doctor actually said. A paraphrase or invention is blanked.
        if not normalized_exact or f" {normalized_exact} " not in padded_said:
            exact = ""
        kept.append(OtherPatientMention(name=clean, instruction=exact))

    for mention in record.other_patients:
        keep(mention.name, mention.instruction)
    for row in rows:
        keep(getattr(row, "name", ""))
    return kept


def _other_patient_warning_lines(record: ProposedRecord) -> list[str]:
    if not record.other_patients:
        return []
    lines = ["🔴 SAFETY WARNING: this dictation named more than one patient."]
    for mention in record.other_patients:
        detail = f" - {mention.instruction}" if mention.instruction else ""
        lines.append(
            f"    Not registered from this dictation: {mention.name}{detail}. "
            "Dictate that patient's instructions separately."
        )
    return lines


# --------------------------------------------------------------------------- #
# The missing-contract rail (S18 item 2, S17 live defect 1)
# --------------------------------------------------------------------------- #
# Measured live: one beat-1 run in five came back with two contracts instead of
# four. The blood-pressure monitoring and the follow-up visit were in the plan
# sentence the patient reads and in no obligation Sanad carries, and the card
# gave the doctor nothing to notice that with. `core/duedates.py` cannot help
# here: it fills a date on a loop the model proposed, and this is a loop the
# model never proposed at all.
#
# So the card says so, in the same shape as the two-patient warning above: a red
# line naming the doctor's own words and the type that is missing. Nothing is
# created, nothing is corrected, and no loop is ever added in code. The only
# thing this can do is make the doctor read his card before he taps Confirm, and
# the remedy it offers is the one the runbook already gives him: cancel and
# dictate again.
#
# The words are matched the way the Coordinator matches a patient's reply
# (core/coordinator.LOOP_WORDS): an English word between spaces, so "test"
# inside "latest" is not the word test, and an Arabic word as a substring,
# because Arabic writes "the" onto the front of it and "التحليل" is "تحليل".
# Round 2, measured on the finished rail rather than guessed. Three of the
# words the first list carried are also how a doctor says a DOSE FREQUENCY or a
# BASELINE, so the rail printed a red line on ordinary medication dictations:
# "Start atorvastatin 40 mg daily", "take bisoprolol twice a day" and
# "Weight 95 kg, start metformin" each produced a MONITOR warning about a loop
# nobody had ordered. A rail that fires when nothing is wrong is a rail the
# doctor learns to tap past, so bare "daily" and bare "weight" are gone from
# the list below and the two frequency phrases moved to the table after it.
MISSING_CONTRACT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MONITOR", (
        "blood pressure", "pressure", "sugar", "glucose", "measure",
        "readings", "ضغط", "سكر", "وزن", "قيس", "قياس")),
    ("VISIT", (
        "come back", "follow up", "see you", "visit", "appointment",
        "تعالى", "تعالي", "ارجع", "موعد", "كشف")),
    ("TEST", (
        "test", "panel", "lab", "analysis", "تحليل", "تحاليل", "اشعة")),
)

# How often, which is a monitoring order only when it is said about something
# measurable. "twice a day" next to a metric is the doctor's own phrasing of
# the blood-pressure order and belongs on the card; "twice a day" next to a
# drug name is a dose. So these count only when one of the words above occurs
# in the SAME clause, and they can never make a type fire on their own: a
# metric already does that, and all these add is the doctor's second phrase to
# a line that was going to print anyway.
MISSING_CONTRACT_FREQUENCY: dict[str, tuple[str, ...]] = {
    "MONITOR": ("twice a day", "daily"),
}

# A clause ends where he pauses. The split happens before folding, because
# `sentinel.normalize` turns every one of these into a space and the whole
# point is to know that "40 mg daily" and "blood pressure" were two different
# breaths.
CLAUSE_BREAK = re.compile(r"[.,;:!?\n\u060c\u061b]+")


def _said_in(dictation: str, word: str) -> bool:
    """Is this word in the doctor's own sentence, folded as the Sentinel folds."""
    wanted = sentinel.normalize(word).strip()
    if not wanted:
        return False
    if wanted.isascii():
        return f" {wanted} " in dictation
    return wanted in dictation


def _clauses(dictation: str) -> list[str]:
    """His sentence, cut at the pauses, each part folded and space-padded."""
    return [sentinel.normalize(part)
            for part in CLAUSE_BREAK.split(dictation or "") if part.strip()]


# At most three phrases are printed, because the line is a prompt to re-read the
# card and not a transcript. A shorter phrase already contained in a longer one
# is dropped, so "blood pressure" does not print next to "pressure".
MISSING_CONTRACT_PHRASES = 3


def missing_contracts(record: ProposedRecord,
                      dictation: str) -> list[tuple[str, list[str]]]:
    """Loop types the dictation asks for and the proposal does not carry.

    Returns (type, the doctor's own matched phrases). An empty list is the
    ordinary case: everything he said became a contract.
    """
    said = sentinel.normalize(dictation)
    clauses = _clauses(dictation)
    have = {loop.type for loop in record.loops}
    out: list[tuple[str, list[str]]] = []
    for kind, words in MISSING_CONTRACT_WORDS:
        if kind in have:
            continue
        hit = [word for word in words if _said_in(said, word)]
        if hit:
            hit += [
                often for often in MISSING_CONTRACT_FREQUENCY.get(kind, ())
                if any(_said_in(clause, often)
                       and any(_said_in(clause, word) for word in words)
                       for clause in clauses)
            ]
        kept = [word for word in hit
                if not any(other != word and word in other for other in hit)]
        if kept:
            out.append((kind, kept[:MISSING_CONTRACT_PHRASES]))
    return out


def _missing_contract_lines(record: ProposedRecord, dictation: str) -> list[str]:
    return [
        f"🔴 Possible missing contract: the dictation mentions {', '.join(kept)} "
        f"but no {kind} was proposed. Cancel and dictate again if it should "
        "be there."
        for kind, kept in missing_contracts(record, dictation)
    ]

# codex item 16. The model's output was trusted for everything the two checks
# below did not name, so a negative due date, an empty plan, an age of 400 and a
# monitoring loop with nothing to measure all became records the doctor was
# asked to confirm. These are the outer bounds of a real dictation, not a
# clinical judgement: a follow-up further out than a year is a mis-heard number,
# not a plan, and nothing here decides anything about care.
MAX_DUE_DAYS = 365
MAX_AGE = 120


def validate(record: ProposedRecord, *,
             existing: Optional[bool] = None) -> list[str]:
    """Return a list of problems; empty means the proposal is safe to store.

    A problem here is refused: the doctor is told what was wrong and asked to
    restate it, and nothing at all is written. That is the right answer for
    anything the model could only have got wrong, and the wrong answer for
    something the doctor simply did not say, which is what `flags` is for.

    `existing` is what the identification decided this dictation is about, and
    it changes exactly one check, the plan:

      False  a new patient. He must arrive with a plan, because `plan_text` is
             the only text this system ever quotes back to a patient and a
             record without one opens his own page blank under his doctor's
             name.
      True   an addition to a record that already has a plan (S9). "Follow up
             with Ahmed about his potassium in a week" is a whole dictation and
             there is no new plan in it: Ahmed's plan is already on his record.
             An empty `plan_text` appends nothing; a present one is appended as
             the dated addendum, which is what `record_updates` already does.
      None   the identification has not run yet. This is the only check that
             depends on it, so it is the only one that waits, and the caller
             asks again once the outcome is known.
    """
    problems: list[str] = []
    if not record.patient.name.strip() or is_placeholder_name(record.patient.name):
        problems.append("the patient's name is missing")
    age = record.patient.age
    if age is not None and not (0 <= age <= MAX_AGE):
        problems.append(f"the age reads as {age}, which is not a person's age")
    if existing is False and not (record.plan_text or "").strip():
        problems.append("there is no plan for the patient to read")
    for i, loop in enumerate(record.loops, 1):
        if loop.type not in DETAIL_FIELDS:
            problems.append(f"loop {i} has an unknown type {loop.type!r}")
            continue
        if not (loop.title or "").strip():
            problems.append(f"loop {i} has no title")
        due = loop.due_in_days
        if due is not None and due < 0:
            problems.append(f"loop {i} is due {due} days from today, in the past")
        elif due is not None and due > MAX_DUE_DAYS:
            problems.append(
                f"loop {i} is due in {due} days, more than {MAX_DUE_DAYS}")
        if loop.type == "MEDICATION" and not (loop.drug and loop.action):
            problems.append(f"loop {i} is a medication with no drug or no action")
        if loop.type == "TEST" and not loop.test_name:
            problems.append(f"loop {i} is a test with no test name")
        if loop.type == "MONITOR" and not (loop.metric or "").strip():
            problems.append(f"loop {i} is a monitoring loop with nothing to measure")
        if loop.type == "TASK" and not (loop.text or "").strip():
            problems.append(f"loop {i} is a task with nothing in it")
    return problems


# What a flag says on the card. The wording matters: it names what is missing
# and never guesses at it.
DOSE_MISSING = "dose missing: not dictated, and nothing was filled in"
SCHEDULE_MISSING = "how often was not dictated"
DURATION_MISSING = "for how long was not dictated"
# S17. A deadline nobody set is the one absence the card used to hide: the loop
# was simply printed without one and the doctor had to notice the missing words.
# It is said out loud now, in the same block and the same voice as the others,
# and it is only ever printed after core/duedates.py has failed to read a date
# out of the doctor's own sentence.
DUE_MISSING = "Due date: not dictated, not filled in by Sanad"


def flags(record: ProposedRecord) -> list[str]:
    """Things the doctor did not say, named on the card instead of invented.

    codex item 16. A medication start with no dose is the one that matters: the
    dose is the most dangerous field in this system, the model will happily
    supply a plausible one, and core/validator.py's whole plan tier is built on
    the plan containing only numbers the doctor gave. So it is never filled in,
    the proposal is not refused either, and the card says the word "missing"
    where the dose would be. The doctor is looking at the card anyway; he is the
    one who knows the dose.

    Refusing it instead would be worse: he dictated a real drug for a real
    patient, and throwing that away over a field he can see is blank turns one
    tap into a whole second dictation.
    """
    said: list[str] = []
    for i, loop in enumerate(record.loops, 1):
        where = f"loop {i} ({loop.title or loop.type})"
        if (loop.type == "MEDICATION" and loop.action in ("start", "change")
                and not (loop.dose or "").strip()):
            said.append(f"{where}: {DOSE_MISSING}")
        if loop.type == "MONITOR":
            if not (loop.schedule or "").strip():
                said.append(f"{where}: {SCHEDULE_MISSING}")
            if loop.days is None:
                said.append(f"{where}: {DURATION_MISSING}")
        if loop.type in duedates.FALLBACK_TYPES and loop.due_in_days is None:
            said.append(f"{where}: {DUE_MISSING}")
    return said


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def _loop_line(loop: ProposedLoop) -> str:
    parts = [f"{loop.type} - {loop.title}"]
    detail = ", ".join(f"{k}: {v}" for k, v in loop_details(loop).items())
    if detail:
        parts.append(f"({detail})")
    if loop.due_in_days is not None:
        parts.append(f"due in {loop.due_in_days}d")
    return " ".join(parts)


def _as_loop(proposal: ProposedLoop, now: datetime) -> SimpleNamespace:
    """A proposal, shaped like the loop it will become, for the contract text.

    Nothing is stored. The confirm card has to show the doctor the contract he
    is about to accept, and the contract is rendered from a loop, so the
    proposal is read through the same fields core/contract.py reads.
    """
    return SimpleNamespace(
        id="", synthetic=True, type=proposal.type, title=proposal.title,
        details=loop_details(proposal), state="open",
        due_at=(now + timedelta(days=proposal.due_in_days)
                if proposal.due_in_days is not None else None),
        contacts=0, evidence_requests=0, barrier="", paused=False,
        doctor_reviewed=False, verified={}, results=[], readings=[],
    )


def describe(patient: Any) -> str:
    """"Ahmed Ali, 58, heart failure", off a record or off a board row."""
    bits: list[str] = [str(getattr(patient, "name", "") or "")]
    age = getattr(patient, "age", None)
    if age is not None:
        bits.append(str(age))
    diagnosis = str(getattr(patient, "diagnosis", "") or "").strip()
    if diagnosis:
        bits.append(diagnosis)
    return ", ".join(b for b in bits if b)


def _blank(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return text or "(blank)"


def record_updates(record: ProposedRecord, patient: Any,
                   now: Optional[datetime] = None) -> tuple[dict, list[str]]:
    """What this dictation changes on a record that already exists.

    Two things come back: the fields to write, and the lines the confirm card
    prints so the doctor reads every one of them before he taps.

    The rule is that a field the dictation did not mention is not touched. The
    extraction fills only what the doctor said, so an empty age or an empty
    diagnosis on the proposal is silence and not an instruction to blank the
    record. Baseline and target metrics are merged by name for the same reason:
    a dictation that states today's weight must not delete the LDL that was
    already on the record.

    The plan is the one field that is never replaced at all. New instructions go
    on as a dated addendum, the same shape and the same function the doctor's
    own relay answers use (core/concierge.with_addendum), so a plan reads as one
    history rather than as whatever the last dictation happened to say.

    The diagnosis is the second field that is never replaced, added by the S9
    review. It is filled when the record has none, and a different one becomes a
    dated note instead of an overwrite: a dictation about one afternoon's
    potassium is not a doctor changing his mind about heart failure.
    """
    from . import concierge  # imported here: concierge does not import this file

    now = now or store.now()
    fields: dict[str, Any] = {}
    lines: list[str] = []
    notes: list[str] = []

    for name, label in (("phone", "Phone"), ("age", "Age"), ("sex", "Sex")):
        said = getattr(record.patient, name, None)
        if said is None or (isinstance(said, str) and not said.strip()):
            continue
        held = getattr(patient, name, None)
        if said == held:
            continue
        fields[name] = said
        lines.append(f"{label}: {_blank(held)} becomes {_blank(said)}")

    # The diagnosis is not one of those, and this is the S9 review's amendment.
    # A follow-up dictation is about one visit, and the extraction fills the
    # diagnosis field from whatever the doctor happened to mention in it, so
    # "his potassium is low" could replace "heart failure" on the record of a
    # man who still has heart failure. A blank field is silence and is filled;
    # a different one is a second opinion and becomes a dated note, which is
    # the same shape everything else the doctor says about a person is kept in.
    said = (getattr(record.patient, "diagnosis", "") or "").strip()
    held = (getattr(patient, "diagnosis", "") or "").strip()
    if said and not held:
        fields["diagnosis"] = said
        lines.append(f"Diagnosis: (blank) becomes {said}")
    elif said and said != held:
        notes.append(f"diagnosis dictated: {said}")
        lines.append(f"Diagnosis stays {held}. Noted on the record: {said}")

    for name, metrics in (("baseline", record.baseline), ("targets", record.targets)):
        said = {m.name: m.value for m in metrics}
        if not said:
            continue
        held = dict(getattr(patient, name, None) or {})
        changed = {k: v for k, v in said.items() if held.get(k) != v}
        if not changed:
            continue
        held.update(said)
        fields[name] = held
        for key, value in changed.items():
            lines.append(f"{name.capitalize()} {key}: {_blank(said.get(key))}")

    plan = (record.plan_text or "").strip()
    if plan:
        fields["plan_text"] = concierge.with_addendum(
            getattr(patient, "plan_text", "") or "", plan, now)
        lines.append("Plan: " + concierge.addendum(plan, now))
    if notes:
        fields["notes"] = [*(getattr(patient, "notes", None) or []),
                           *(note_entries(text, now)[0] for text in notes)]
    return fields, lines


def confirm_card(record: ProposedRecord, confirm_id: str,
                 doctor_name: str = "your doctor",
                 pol: Optional[policy.Policy] = None,
                 existing: Any = None, why: str = "", note: str = "",
                 said: str = "") -> dict:
    """What the doctor taps. Every loop is shown as the contract it becomes.

    The contract wording here and the contract on the patient's page are the
    same function (core/contract.py), so what he confirms and what he is shown
    afterwards cannot drift apart.

    S9 gave this card a second shape. When the dictation is about a patient who
    is already on the board the title names that patient rather than the
    dictation, the lines separate what is being ADDED from what is being
    CHANGED, and a third button says "This is a new patient" so one tap undoes
    an identification the doctor disagrees with. Everything else, the loops, the
    contracts, the safety sentence, is the card he already knows.
    """
    now = store.now()
    lines: list[str] = []
    pol = pol or policy.DEFAULT
    # On an addition the contract is about the record, so it is written with the
    # name on the record and not with whatever half of it the doctor dictated:
    # "Obtain Potassium for Ahmed Ali", never "for Ahmed".
    called = (getattr(existing, "name", "") or record.patient.name
              if existing is not None else record.patient.name)
    if existing is not None:
        lines.append("Adding to the record:")
    for proposal in record.loops:
        lines.append(_loop_line(proposal))
        shaped = _as_loop(proposal, now)
        lines += [
            "    " + line
            for line in contract.for_confirm(shaped, pol, doctor_name,
                                             called)[:2]
        ]
    # codex item 16. What the doctor did not say is printed where it is
    # missing, never filled in. It sits above the safety sentence on both
    # shapes of this card, so it is the last thing read before the tap.
    missing = flags(record)
    if missing:
        lines.append("Not dictated, and NOT filled in by Sanad:")
        lines += [f"    {one}" for one in missing]

    # Fable red-team 2 item 8. ProposedRecord can hold only one primary record,
    # so a second patient must be impossible to lose silently. These names were
    # checked against the original dictation by `checked_other_patients`; no
    # model-written instruction is trusted or stored.
    lines += _other_patient_warning_lines(record)

    # S18 item 2. The other way one dictation can lose half of itself: the
    # model proposes fewer obligations than the doctor asked for, and the
    # missing one lives in the plan sentence where nothing chases it. Measured
    # live on one beat-1 run in five. No loop is added here; the doctor is told
    # what his own words mentioned and what the card does not carry.
    lines += _missing_contract_lines(record, said)

    # codex re-audit 2. The identification note is stored on the record as a
    # dated note the moment he taps Confirm, and until now he never saw it: it
    # was written by a model and checked by nothing he could read. It is
    # printed here, above the safety sentence, on both shapes of this card.
    # `core/identify.clean_note` has already refused anything with a number in
    # it, anything longer than twelve words, and anything the doctor did not
    # say, so what is printed is his own words said back to him.
    said = (note or "").strip()
    if said:
        lines.append(f"note: {said}")

    if existing is None:
        # rev 18 item 9. "Plan: " with nothing after it is a blank the eye
        # slides over, and this card is reached with an empty plan by one real
        # gesture: a dictation about a patient already on the board, which
        # needs no plan of its own, followed by "This is a new patient". The
        # absence is printed as an absence, in the same voice as the "Not
        # dictated" block above it.
        plan_text = (record.plan_text or "").strip()
        lines.append(f"Plan: {plan_text}" if plan_text else "Plan: none dictated")
        lines.append(contract.SAFETY_SENTENCE)
        return {
            "title": f"New patient: {record.patient.name}",
            "lines": lines,
            "actions": [
                {"id": f"confirm:{confirm_id}", "label": "Confirm"},
                {"id": f"cancel:{confirm_id}", "label": "Cancel"},
            ],
        }

    _, changes = record_updates(record, existing, now)
    lines.append("Changing on the record:" if changes
                 else "Nothing on the record changes.")
    lines += changes
    lines.append("Nothing else on the record is touched, and the plan is "
                 "added to, never replaced.")
    if why:
        lines.append(f"Matched because: {why}")
    lines.append(contract.SAFETY_SENTENCE)
    return {
        "title": f"Existing patient: {describe(existing)}",
        "lines": lines,
        "actions": [
            {"id": f"confirm:{confirm_id}", "label": "Confirm"},
            {"id": f"cancel:{confirm_id}", "label": "Cancel"},
            {"id": f"newpatient:{confirm_id}", "label": "This is a new patient"},
        ],
    }


def ask_card(record: ProposedRecord, confirm_id: str,
             rows: list, outcome: identify.Outcome) -> dict:
    """More than one reading of who this is, so the doctor picks and code waits.

    Nothing is written by this card. The proposal is already in
    `pending_confirms` with no patient on it, every button rewrites that one
    field, and the ordinary confirm card is what commits anything at all. Two
    taps, and the first of them only chooses a record.
    """
    by_id = {row.id: row for row in rows}
    lines = [f"The dictation reads as: {describe(record.patient)}."]
    lines += _other_patient_warning_lines(record)
    actions: list[dict] = []
    for patient_id, reason in outcome.candidates:
        row = by_id.get(patient_id)
        if row is None:
            continue
        lines.append(f"{row.label()}: {reason}")
        actions.append({"id": f"existing:{patient_id}:{confirm_id}",
                        "label": row.label()})
    if outcome.needs_name:
        lines.append("Sanad could not tell who this is about. Say the name and "
                     "dictate again, or register it as a new patient.")
    lines.append("Nothing is written until you tap.")
    actions.append({"id": f"newpatient:{confirm_id}", "label": "New patient"})
    actions.append({"id": f"cancel:{confirm_id}", "label": "Cancel"})
    return {"title": "Which patient is this?", "severity": "yellow",
            "lines": lines, "actions": actions}


def lookup_card(fragment: str, rows: list, outcome: identify.Outcome) -> dict:
    """The doctor asked to find somebody. A list, and nothing is created.

    The buttons open a record. There is no confirm on this card because there is
    no proposal behind it: a lookup never reaches `pending_confirms` at all.
    """
    by_id = {row.id: row for row in rows}
    lines: list[str] = []
    actions: list[dict] = []
    for patient_id, reason in outcome.candidates:
        row = by_id.get(patient_id)
        if row is None:
            continue
        lines.append(f"{row.label()}: {reason}")
        actions.append({"id": f"openpatient:{patient_id}", "label": row.label()})
    if not lines:
        lines.append(f"No patient of yours matches {fragment!r}.")
    lines.append("Nothing was created. This was a lookup.")
    return {"title": f"Lookup: {fragment}" if fragment else "Lookup",
            "lines": lines, "actions": actions}


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
async def handle_doctor(
    doctor: Doctor,
    text: Optional[str] = None,
    audio_bytes: Optional[bytes] = None,
    image_bytes: Optional[bytes] = None,
    mime: str = "image/jpeg",
    channel: str = "web",
    synthetic: bool = True,
) -> None:
    """A dictation - typed, spoken or photographed -> a confirm card in the feed."""
    adapter, target = fanout(), f"doctor:{doctor.web_token}"
    evidence_synthetic = provenance.derived(doctor.synthetic, synthetic)

    if image_bytes:
        run_id, _ = await settings.current()
        path = await storage.put_image(
            image_bytes, run_id=run_id, patient_id="intake", mime=mime
        )
        await events.append_event(
            doctor.id,
            "doctor_in",
            f"prescription photo: {text.strip()}" if (text or "").strip()
            else "prescription photo",
            channel=channel,
            media=[provenance.evidence(
                {"kind": "image", "path": path},
                synthetic=evidence_synthetic,
            )],
            meta={"source": "photo"},
            synthetic=synthetic,
        )
        record = await propose_from_image(image_bytes, mime)
    elif audio_bytes:
        # codex item 11, the doctor's side of it. A transcription that hangs
        # used to hold the console's Dictate sheet open until Cloud Run gave up.
        # The dictation is his to repeat, so the honest answer is to say so.
        try:
            text = await bounded.within(
                bounded.TRANSCRIBE, media.transcribe_async(audio_bytes),
                what="the dictation transcription")
        except Exception:
            log.warning("the dictation could not be transcribed", exc_info=True)
            await adapter.send(target, OutboundMessage(
                text="I could not hear that dictation. Nothing was created. "
                     "Say it again, or type it."))
            return
        await events.append_event(
            doctor.id,
            "doctor_in",
            text,
            channel=channel,
            media=[provenance.evidence(
                {"kind": "audio", "inline_note": "voice note, transcribed"},
                synthetic=evidence_synthetic,
            )],
            meta={"source": "voice"},
            synthetic=synthetic,
        )
        record = await propose(text) if text.strip() else None
    else:
        text = (text or "").strip()
        await events.append_event(
            doctor.id, "doctor_in", text, channel=channel,
            meta={"source": "text"}, synthetic=synthetic,
        )
        record = await propose(text) if text else None

    if record is None:
        await adapter.send(target, OutboundMessage(text="I got an empty dictation."))
        return

    # S9. Which patient is this about? Two answers are taken and code decides
    # between them: the same name matcher the commands use, and one Gemini read
    # of the doctor's words against his own board. Neither of them writes
    # anything; all they can do is choose which card the doctor is shown.
    said = (text or "").strip()
    rows = await board_for(doctor)
    record = record.model_copy(
        update={"other_patients": checked_other_patients(record, said, rows)}
    )
    if identify.is_bare_name(said, record.patient.name):
        matches = identify.code_matches(rows, record.patient.name)
        outcome = identify.Outcome(
            kind=identify.LIST,
            candidates=tuple((row.id, "the name matches this record")
                             for row in matches),
            why="a bare name is a lookup, never a clinical proposal",
        )
        await events.append_event(
            doctor.id, "system", f"lookup: {record.patient.name}",
            meta={"identification": identified(outcome, None)},
        )
        await adapter.send(target, OutboundMessage(
            text=f"Looking up {record.patient.name}.",
            meta={"decided_by": "code (bare names are lookup-only)"},
            card=lookup_card(record.patient.name, rows, outcome),
        ))
        return
    # S17. The due date the model dropped, read back out of the doctor's own
    # sentence in code. This runs before the first validation step and therefore
    # before the card is built, so what the doctor confirms already carries the
    # deadline he dictated, the ladder is queued for it, and the two checks
    # below (a date in the past, a date past a year) apply to a derived date
    # exactly as they apply to the model's own. Nothing is invented: a loop
    # whose deadline cannot be read stays without one and the card says so.
    record, _ = duedates.fill(record, said, cap=MAX_DUE_DAYS)
    problems = validate(record)
    if problems and not identify.asks_lookup(said):
        await adapter.send(
            target,
            OutboundMessage(
                text="I could not register this safely: "
                + "; ".join(problems)
                + ". Could you restate it?"
            ),
        )
        return

    verdict = await identify.identify(said, rows, record.patient.name)
    outcome = identify.decide(said, record.patient.name, rows, verdict)

    if outcome.kind == identify.LIST:
        # A lookup creates nothing at all: no patient, no loop, and not even a
        # pending proposal. It is a list of records with a button that opens
        # one, which is why it is answered here and not through a confirm.
        await events.append_event(
            doctor.id, "system", f"lookup: {record.patient.name}",
            meta={"identification": identified(outcome, verdict)},
        )
        await adapter.send(
            target,
            OutboundMessage(
                text=f"Looking up {record.patient.name}.",
                meta={"decided_by": "model identification (Registrar), the "
                                    "code created nothing"},
                card=lookup_card(record.patient.name, rows, outcome),
            ),
        )
        return

    # The identification has answered, so the one check that was waiting for it
    # can run: a NEW patient needs a plan of his own, an addition to a record
    # that already has one does not. `identify.NEW` is the only outcome that
    # creates a person; EXISTING attaches to one, and ASK hands the choice to
    # the doctor between records that already exist.
    problems = validate(record, existing=outcome.kind != identify.NEW)
    if problems:
        await adapter.send(
            target,
            OutboundMessage(
                text="I could not register this safely: "
                + "; ".join(problems)
                + ". Could you restate it?"
            ),
        )
        return

    # codex re-audit 2. The note the identification wrote is checked in code
    # before it is stored anywhere, and a note that fails is dropped with the
    # reason on the board. The prompt asks for twelve words of the doctor's own
    # description; this is what makes that true of the record.
    note, dropped = identify.clean_note(
        outcome.note, said, identify.clinical_words(record))
    if dropped:
        await events.append_event(
            doctor.id, "system",
            f"identification note dropped: {dropped}",
            meta={"note": (outcome.note or "").strip(), "why": dropped,
                  "decided_by": "code (core/identify.clean_note)"},
        )
    outcome = replace(outcome, note=note)

    confirm = PendingConfirm(
        id=new_confirm_id(),
        synthetic=evidence_synthetic,
        doctor_id=doctor.id,
        proposed=record.model_dump(),
        patient_id=outcome.patient_id or None,
        note=note,
        said=said,
        expires_at=store.now() + CONFIRM_TTL,
    )
    await store.save_confirm(confirm)

    if outcome.kind == identify.ASK:
        await adapter.send(
            target,
            OutboundMessage(
                text=f"Which patient is {record.patient.name}?",
                meta={"decided_by": "model identification (Registrar), code "
                                    "refused to choose between the readings, "
                                    "the doctor picks",
                      "identification": identified(outcome, verdict)},
                card=ask_card(record, confirm.id, rows, outcome),
            ),
        )
        return

    await send_confirm(doctor, confirm, record, outcome, verdict)


async def board_for(doctor: Doctor) -> list:
    """The doctor's own board, most recent first, as the identification reads it.

    Recency is the last event on each patient, which is one read of the event
    log the console reads anyway. A doctor with no patients gets an empty list
    and the model is still asked, because "the board is empty" is exactly the
    context that makes a new patient the obvious answer.
    """
    patients = await store.list_patients(doctor.id)
    seen: dict[str, Any] = {}
    for event in await events.last_events(doctor.id, 0):
        if event.patient_id:
            seen[event.patient_id] = event.ts
    return identify.board(patients, seen)


# The two intake labels, as module constants so the `decided_by` rail can read
# them without running anything (tests/test_decided_by.py). Both of them name a
# model and code, because both are true: a model drafted the record, code
# validated every field of it, and the doctor is the one who commits it.
DECIDED_NEW = ("model draft (Registrar), code validated every field, the "
               "doctor confirms")
DECIDED_EXISTING = ("model draft (Registrar), the code name matcher and the "
                    "identification agree on the record, the doctor confirms")


def decided_by_intake(existing: Any) -> str:
    return DECIDED_EXISTING if existing is not None else DECIDED_NEW


def identified(outcome: Optional[identify.Outcome],
               verdict: Optional[identify.Verdict]) -> dict:
    """The identification, as the audit line on the card. Read by nothing else."""
    if outcome is None:
        return {}
    return {
        "kind": outcome.kind,
        "why": outcome.why,
        "intent": verdict.intent if verdict is not None else "model unavailable",
        "patient_id": outcome.patient_id,
        "note": outcome.note,
        "candidates": [{"patient_id": pid, "reason": reason}
                       for pid, reason in outcome.candidates],
    }


async def send_confirm(doctor: Doctor, confirm: PendingConfirm,
                       record: ProposedRecord,
                       outcome: Optional[identify.Outcome] = None,
                       verdict: Optional[identify.Verdict] = None) -> None:
    """The confirm card for whatever `confirm.patient_id` says right now.

    One function for all three ways of arriving at it: the identification chose
    a record, the doctor tapped a name on the ask card, or the doctor tapped
    "This is a new patient". The card is built from the stored proposal, so the
    three paths cannot show three different things.
    """
    adapter, target = fanout(), f"doctor:{doctor.web_token}"
    existing = None
    if confirm.patient_id:
        existing = await store.get_patient(confirm.patient_id)
        if existing is not None and existing.doctor_id != doctor.id:
            existing = None
        if existing is None:
            # The record went away between the dictation and the tap. Fall back
            # to a new patient rather than to a Confirm that would fail.
            confirm = confirm.model_copy(update={"patient_id": None})
            await store.save_confirm(confirm)
    await adapter.send(
        target,
        OutboundMessage(
            text=(f"Ready to add to {existing.name}'s record."
                  if existing is not None
                  else f"Ready to register {record.patient.name}."),
            patient_id=existing.id if existing is not None else None,
            # rev 17 item 12: every card says who decided it. This one is the
            # honest description of the intake path: a model drafted the
            # record, code checked every loop type and required field before
            # the doctor saw it, and the doctor is the one who commits it.
            meta={"decided_by": decided_by_intake(existing),
                  "identification": identified(outcome, verdict)},
            card=confirm_card(record, confirm.id, doctor.name,
                              policy.for_doctor(doctor), existing=existing,
                              why=outcome.why if outcome is not None else "",
                              note=confirm.note, said=confirm.said),
        ),
    )


async def choose_existing(doctor: Doctor, patient_id: str, confirm_id: str) -> None:
    """"existing:<patient id>:<proposal id>": the doctor picked a record.

    This writes one field on the pending proposal and nothing else. The patient
    and the loops are still created by Confirm, which is the tap that has always
    committed anything in Sanad.
    """
    adapter, target = fanout(), f"doctor:{doctor.web_token}"
    confirm = await store.get_confirm(confirm_id)
    if confirm is None or confirm.doctor_id != doctor.id:
        await adapter.send(target, OutboundMessage(text="That proposal is gone."))
        return
    patient = await store.get_patient(patient_id)
    if patient is None or patient.doctor_id != doctor.id:
        await adapter.send(target, OutboundMessage(text="That patient is gone."))
        return
    confirm = confirm.model_copy(update={"patient_id": patient.id})
    await store.save_confirm(confirm)
    await send_confirm(doctor, confirm, ProposedRecord.model_validate(confirm.proposed))


async def choose_new(doctor: Doctor, confirm_id: str) -> None:
    """"newpatient:<proposal id>": the doctor says this is somebody new."""
    adapter, target = fanout(), f"doctor:{doctor.web_token}"
    confirm = await store.get_confirm(confirm_id)
    if confirm is None or confirm.doctor_id != doctor.id:
        await adapter.send(target, OutboundMessage(text="That proposal is gone."))
        return
    record = ProposedRecord.model_validate(confirm.proposed)
    if is_placeholder_name(record.patient.name):
        await adapter.send(target, OutboundMessage(
            text="I need the patient's name before I can register anyone. "
                 "Please dictate it again with the name."))
        return
    confirm = confirm.model_copy(update={"patient_id": None})
    await store.save_confirm(confirm)
    await send_confirm(doctor, confirm, ProposedRecord.model_validate(confirm.proposed))


ALREADY_CONFIRMED = ("That record is already being made: already confirmed. "
                     "Nothing was created twice.")


async def commit(doctor: Doctor, confirm_id: str, base_url: str = "") -> None:
    """Turn a confirmed proposal into a patient, their loops and their link.

    Two things stand in front of the writing (codex item 6). The proposal is
    claimed in a Firestore transaction, so a second tap on the same card, which
    is what a double click or a re-sent Telegram callback is, is answered in
    words instead of making a second Ahmed. And every document this writes has
    an id derived from the confirmation rather than a fresh uuid, so a commit
    that fell over halfway writes the same patient and the same loops over
    themselves when it runs again. The claim is given back on any failure,
    which is what lets it run again at all.
    """
    adapter, target = fanout(), f"doctor:{doctor.web_token}"
    confirm = await store.get_confirm(confirm_id)
    if confirm is None or confirm.doctor_id != doctor.id:
        await adapter.send(target, OutboundMessage(text="That proposal is gone."))
        return

    if not await store.claim_confirm(confirm_id):
        await adapter.send(target, OutboundMessage(text=ALREADY_CONFIRMED))
        return

    try:
        await _commit(doctor, confirm, base_url)
    except Exception:
        # The claim goes back so a retry may run, and the retry writes the same
        # deterministic ids over whatever the failed attempt managed to write.
        await store.release_confirm(confirm_id)
        raise


async def _commit(doctor: Doctor, confirm: PendingConfirm,
                  base_url: str = "") -> None:
    """The commit itself, with the proposal already claimed."""
    adapter, target = fanout(), f"doctor:{doctor.web_token}"
    confirm_id = confirm.id
    record = ProposedRecord.model_validate(confirm.proposed)

    # S9. The proposal knows whether it is a new record or an addition to one
    # the doctor already keeps, because the card he tapped said so and the
    # buttons on it rewrote that one field. Nothing here re-decides it.
    if confirm.patient_id:
        await attach(doctor, confirm, record)
        return

    ts = store.now()
    patient = await store.create_patient(
        Patient(
            # Derived, not fresh: the same confirmation always makes the same
            # patient, so a retry is a rewrite and never a second person.
            id=store.derived_id(confirm_id, "patient"),
            synthetic=provenance.derived(doctor.synthetic, confirm.synthetic),
            doctor_id=doctor.id,
            name=record.patient.name,
            phone=record.patient.phone,
            age=record.patient.age,
            sex=record.patient.sex,
            diagnosis=record.patient.diagnosis,
            baseline={m.name: m.value for m in record.baseline},
            targets={m.name: m.value for m in record.targets},
            plan_text=record.plan_text,
            # Web demo: the console is the patient's channel, so they are active
            # immediately. A Telegram-only patient would sit at "pending_link".
            channels={"web": True, "telegram_chat_id": None},
            status="active",
            notes=note_entries(confirm.note, ts),
            created_at=ts,
        )
    )

    for index, proposal in enumerate(record.loops):
        # created_at is read per loop, not reused from `ts`: it is what the board
        # sorts on, and identical timestamps would leave the order to Firestore.
        made = store.now()
        await store.create_loop(
            Loop(
                id=store.derived_id(confirm_id, "loop", str(index)),
                synthetic=provenance.derived(
                    patient.synthetic, confirm.synthetic
                ),
                patient_id=patient.id,
                doctor_id=doctor.id,
                type=proposal.type,
                title=proposal.title,
                details=loop_details(proposal),
                state="open",
                due_at=(
                    ts + timedelta(days=proposal.due_in_days)
                    if proposal.due_in_days is not None
                    else None
                ),
                attempts=0,
                created_at=made,
                updated_at=made,
            )
        )

    await store.delete_confirm(confirm_id)
    await events.append_event(
        doctor.id, "system", "record committed", patient_id=patient.id,
        synthetic=patient.synthetic,
    )

    # The patient onboards himself: one-time deep link now, QR of the same link.
    #
    # This is minted BEFORE the queue is touched, and that order is the fix for
    # a defect proved live on rev sanad-00015-p6x: a loop dated more than 30
    # days out made the enqueue throw, Confirm returned 500, and the record was
    # committed with no link, so the patient could never be bound at all. The
    # link is the one thing the doctor cannot recreate from the console, so it
    # is created first and nothing after it may take it away.
    token = await links.mint(doctor, patient,
                             token_id=store.derived_id(confirm_id, "link"))
    link_lines = await links.card_lines(token, base_url)

    # The whole future of this patient is created here, in one go: three nudges
    # per dated loop, and a daily reminder on days two through N per monitoring
    # loop, day one being this confirmation itself. Nothing in Sanad
    # stays alive in between (core/chaser.py). A queue that refuses is a card
    # the doctor can act on, never a failed Confirm: the record is already real
    # and the reminders can be put back with /force_due.
    queued: list = []
    queue_error = ""
    try:
        queued = await chaser.schedule_patient(patient)
    except Exception as exc:  # noqa: BLE001 - the record must survive the queue
        queue_error = " ".join(str(exc).split())[:200]
        log.exception("scheduling failed for patient=%s; the record stands",
                      patient.id)
    await events.append_event(
        doctor.id, "system", f"{len(queued)} follow-up tasks scheduled",
        patient_id=patient.id,
        meta={"queued": queued, "queue_error": queue_error},
        synthetic=patient.synthetic,
    )

    await adapter.send(
        target,
        OutboundMessage(
            text=f"{patient.name} is registered.",
            # rev 18 item 4. This card names the patient in its text and in its
            # first line and carried no `patient_id`, so the Inbox could not
            # offer "Open the patient" on the one card where the doctor has
            # just met that patient. The record exists by now: `patient` was
            # created a few lines above, which is what the confirm card could
            # not say.
            patient_id=patient.id,
            meta={"decided_by": "model draft (Registrar), code validated every "
                                "field, the doctor confirmed it"},
            card={
                "title": "Committed.",
                "severity": "green",
                "lines": [f"{patient.name}, {len(record.loops)} care loops open.",
                          f"{len(queued)} reminders scheduled.",
                          *link_lines],
                "actions": [],
            },
        ),
    )
    if queue_error:
        await adapter.send(
            target,
            OutboundMessage(
                text=f"{patient.name} is registered, but the reminders are not "
                     "on the queue.",
                patient_id=patient.id,
                meta={"decided_by": "code (core/registrar.py, the record "
                                    "outlives the queue)"},
                card={
                    "title": f"Reminders not scheduled · {patient.name}",
                    "severity": "yellow",
                    "lines": [
                        f"{patient.name} is committed and his link is above.",
                        "The follow-up tasks could not be put on the queue.",
                        f"Reason: {queue_error}",
                        "Nothing was lost: /force_due <name> puts a reminder "
                        "back on the queue now.",
                        "decided_by: code (core/registrar.py, the record "
                        "outlives the queue)",
                    ],
                    "actions": [],
                },
            ),
        )
    if doctor.telegram_chat_id and telegram.enabled():
        link = await telegram.deep_link(token.id)
        if link:
            await telegram.send_photo(
                doctor.telegram_chat_id, links.qr_png(link),
                caption=f"{patient.name}: forward this to "
                        f"{gender.object_pronoun(gender.of_patient(patient))}, "
                        f"or send {link}",
            )


# --------------------------------------------------------------------------- #
# The other commit: a dictation about a patient who is already on the board
# --------------------------------------------------------------------------- #
def note_entries(note: str, at: datetime) -> list[dict]:
    """A relationship or description, as the dated note it is stored as.

    Doctor text only. Nothing a patient ever sends reaches this list, and
    nothing generated for a patient does either: the only writer is the moment
    the doctor taps Confirm on his own dictation.
    """
    text = (note or "").strip()
    if not text:
        return []
    return [{"text": text, "at": at.strftime("%Y-%m-%d"),
             "source": "doctor dictation"}]


async def attach(doctor: Doctor, confirm: PendingConfirm,
                 record: ProposedRecord) -> None:
    """A confirmed proposal, added to a record that already exists.

    What this does NOT do is as much of the point as what it does. It does not
    create a patient, it does not mint a second link (the patient already has
    one, and his chat is already bound), and it does not call
    `chaser.schedule_patient`, which would put a fresh ladder on every loop the
    patient already had. Only the loops this dictation asked for are created,
    and only those are scheduled.

    The plan is added to and never replaced, the demographic fields the
    dictation did not mention are left alone, and the doctor read every line of
    that on the confirm card before he tapped.
    """
    adapter, target = fanout(), f"doctor:{doctor.web_token}"
    patient = await store.get_patient(confirm.patient_id or "")
    if patient is None or patient.doctor_id != doctor.id:
        await store.delete_confirm(confirm.id)
        await adapter.send(target, OutboundMessage(text="That patient is gone."))
        return

    ts = store.now()
    fields, changes = record_updates(record, patient, ts)
    if (confirm.note or "").strip():
        fields["notes"] = [*(patient.notes or []),
                           *note_entries(confirm.note, ts)]
    if fields:
        await store.update_patient(patient.id, **fields)

    made: list[Loop] = []
    for index, proposal in enumerate(record.loops):
        at = store.now()
        made.append(await store.create_loop(
            Loop(
                # Derived from the confirmation, exactly as a new record's loops
                # are: a retried addition adds the same loops and not a second
                # copy of them (codex item 6).
                id=store.derived_id(confirm.id, "loop", str(index)),
                synthetic=provenance.derived(
                    patient.synthetic, confirm.synthetic
                ),
                patient_id=patient.id,
                doctor_id=doctor.id,
                type=proposal.type,
                title=proposal.title,
                details=loop_details(proposal),
                state="open",
                due_at=(
                    ts + timedelta(days=proposal.due_in_days)
                    if proposal.due_in_days is not None
                    else None
                ),
                attempts=0,
                created_at=at,
                updated_at=at,
            )
        ))

    await store.delete_confirm(confirm.id)
    await events.append_event(
        doctor.id, "system", f"record updated for {patient.name}",
        patient_id=patient.id,
        meta={"added_loops": [loop.id for loop in made],
              "changed": sorted(fields), "changes": changes,
              "note": (confirm.note or "").strip()},
        synthetic=provenance.derived(patient.synthetic, confirm.synthetic),
    )

    # Same rule as a new record: the queue may refuse and the record still
    # stands. /force_due puts a reminder back.
    queued: list = []
    queue_error = ""
    try:
        for loop in made:
            queued += await chaser.schedule_loop(loop)
    except Exception as exc:  # noqa: BLE001 - the record must survive the queue
        queue_error = " ".join(str(exc).split())[:200]
        log.exception("scheduling failed for patient=%s; the record stands",
                      patient.id)
    await events.append_event(
        doctor.id, "system", f"{len(queued)} follow-up tasks scheduled",
        patient_id=patient.id,
        meta={"queued": queued, "queue_error": queue_error},
        synthetic=provenance.derived(patient.synthetic, confirm.synthetic),
    )

    lines = [f"{len(made)} new care loops on {describe(patient)}."]
    lines += changes or ["Nothing else on the record changed."]
    lines.append(f"{len(queued)} reminders scheduled.")
    lines.append("The link and the chat this patient already had are unchanged.")
    if queue_error:
        lines.append(f"The queue refused: {queue_error}. Nothing was lost: "
                     "/force_due <name> puts a reminder back on the queue now.")
    await adapter.send(
        target,
        OutboundMessage(
            text=f"{patient.name}'s record is updated.",
            patient_id=patient.id,
            meta={"decided_by": "model draft (Registrar), code validated every "
                                "field, the doctor confirmed the record it "
                                "attaches to"},
            card={"title": f"Added to {patient.name}.",
                  "severity": "yellow" if queue_error else "green",
                  "lines": lines, "actions": []},
        ),
    )


async def cancel(doctor: Doctor, confirm_id: str) -> None:
    """Throw a proposal away. Security audit L1: only your own.

    This deleted `pending_confirms/<id>` for any id at all, which every other
    verb on the action route already refused to do: `commit`, `doctor_reply`,
    `mark_reviewed`, `note_to_patient`, `attach_results` and `open_loop_for`
    all compare `doctor_id` first. Ids are uuid4 hex, so the gap only ever
    opened for somebody who already knew another doctor's confirm id, and it is
    the kind of gap that closes in one line.

    A confirmation that is not this doctor's, and one that is already gone, get
    the same answer: there is nothing here to cancel.
    """
    confirm = await store.get_confirm(confirm_id)
    if confirm is None or confirm.doctor_id != doctor.id:
        await fanout().send(
            f"doctor:{doctor.web_token}",
            OutboundMessage(text="There is nothing to cancel."))
        return
    await store.delete_confirm(confirm_id)
    await fanout().send(
        f"doctor:{doctor.web_token}", OutboundMessage(text="Proposal cancelled.")
    )
