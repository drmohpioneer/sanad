"""Owns the twenty background patients: a board with real load on it.

S6++ item J. The video follows one patient, and behind him the board has to look
like a clinic rather than a demo: twenty more people across specialties, one to
three care obligations each, in every state the system has, so that the
end-of-day summary (S6++ item K) counts something a judge cannot do in his head
and the board colours are not all the same.

**Every one of them is invented.** The names are made up, the phone numbers are
all in one impossible block (0100 000 00NN), the diagnoses are textbook lines
and not anybody's history, and there is not one photograph anywhere in this
file. Nothing here came from a real patient, a real record or a real result.

The data is a table and the seeder is eight lines, which is the shape that
matters: `records()` builds the whole board in memory as ordinary records, with
no I/O at all, so `expected()` can count the summary these patients produce
without a database, and the runbook can print numbers a test asserts rather than
numbers somebody typed. `seed()` is the only function here that writes, and it
writes patients, loops, events and relays and nothing else: no Cloud Task, no
message, no card. Seeding twenty patients must never send twenty phones
anything.

The document ids are derived from the doctor, so seeding twice replaces the same
twenty patients instead of making forty of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from . import monitoring
from .models import Event, Loop, Patient, Relay

# The phone block. Not a real range, on purpose.
PHONE = "0100 000 00{:02d}"


@dataclass(frozen=True)
class Contract:
    """One care obligation, as the fixture writes it."""

    type: str
    title: str
    details: dict[str, Any] = field(default_factory=dict)
    state: str = "waiting_patient"
    due_in_days: Optional[int] = None
    barrier: str = ""
    paused: bool = False
    reviewed: bool = False
    contacts: int = 1
    results: tuple[dict[str, Any], ...] = ()
    readings: tuple[dict[str, Any], ...] = ()
    critical: str = ""   # the concept of a critical escalation, if one fired
    relay: str = ""      # a question of this patient's still waiting on him


@dataclass(frozen=True)
class Person:
    """One invented patient and everything Sanad is carrying for him."""

    name: str
    sex: str
    age: int
    specialty: str
    diagnosis: str
    speak: str
    plan: str
    said: str
    contracts: tuple[Contract, ...]


def _lipids(ldl: str = "150") -> tuple[dict[str, Any], ...]:
    return (
        {"analyte": "LDL", "value": ldl, "unit": "mg/dL", "level": "above_target",
         "line": f"LDL {ldl} mg/dL, above target"},
        {"analyte": "Total cholesterol", "value": "230", "unit": "mg/dL",
         "level": "normal",
         "line": "Total cholesterol 230 mg/dL, no printed range to compare"},
    )


def _bp(days: int, per_day: int = 2, start: int = 145,
        skip: tuple[tuple[int, int], ...] = ()) -> tuple[dict[str, Any], ...]:
    """Readings that fall a little, with the named (day, slot) gaps left out."""
    rows: list[dict[str, Any]] = []
    for day in range(1, days + 1):
        for slot in range(per_day):
            if (day, slot) in skip:
                continue
            systolic = start - day
            rows.append({
                "day": day, "slot": slot, "value": f"{systolic}/{systolic - 55}",
                "number": float(systolic),
            })
    return tuple(rows)


# --------------------------------------------------------------------------- #
# The twenty. All invented, across specialties, both genders, both languages.
# --------------------------------------------------------------------------- #
PEOPLE: tuple[Person, ...] = (
    Person(
        name="Nabila Sorour", sex="female", age=61, specialty="cardiology",
        diagnosis="hypertension, on two agents", speak="ar",
        plan="Take the medicine every morning and measure blood pressure morning and night.",
        said="I measured my blood pressure this morning",
        contracts=(
            Contract(type="MONITOR", title="Blood pressure monitoring",
                     details={"metric": "BP", "schedule": "twice a day",
                              "days": 7},
                     readings=_bp(7, skip=((3, 1), (5, 1), (6, 1))), contacts=3),
            Contract(type="VISIT", title="Follow-up visit", due_in_days=9,
                     state="open"),
            Contract(type="TEST", title="Lipid panel",
                     details={"test_name": "Lipid panel"}, due_in_days=14),
        ),
    ),
    Person(
        name="Wagdy Kamel", sex="male", age=54, specialty="cardiology",
        diagnosis="heart failure, reduced ejection fraction", speak="en",
        plan="Weigh yourself every morning and send me the number.",
        said="I weighed 84 today",
        contracts=(
            Contract(type="MONITOR", title="Daily weight",
                     details={"metric": "weight", "schedule": "once a day",
                              "days": 14},
                     readings=({"day": 1, "slot": 0, "value": "86", "number": 86.0},
                               {"day": 2, "slot": 0, "value": "85", "number": 85.0},
                               {"day": 3, "slot": 0, "value": "84", "number": 84.0}),
                     contacts=2),
            Contract(type="TEST", title="Kidney function tests",
                     details={"test_name": "Kidney function tests"},
                     due_in_days=4, state="waiting_patient", contacts=2),
            Contract(type="MEDICATION", title="Continue the diuretic",
                     details={"drug": "furosemide", "dose": "as dictated",
                              "action": "start"}, state="open"),
        ),
    ),
    Person(
        name="Salwa Abdelhamid", sex="female", age=47, specialty="endocrinology",
        diagnosis="type 2 diabetes", speak="ar",
        plan="Do the HbA1c test within two weeks and send me the result.",
        said="The lab in my area is closed",
        contracts=(
            Contract(type="TEST", title="HbA1c",
                     details={"test_name": "glycated haemoglobin"},
                     due_in_days=6, barrier="availability", contacts=2),
        ),
    ),
    Person(
        name="Refaat Zaghloul", sex="male", age=66, specialty="endocrinology",
        diagnosis="type 2 diabetes with neuropathy", speak="ar",
        plan="The test is important so we can confirm the treatment is working.",
        said="I will not do the test because it is too expensive",
        contracts=(
            Contract(type="TEST", title="Lipid panel",
                     details={"test_name": "Lipid panel"}, due_in_days=3,
                     barrier="cost", paused=True, contacts=2,
                     relay="I will not do the test because it is too expensive"),
            Contract(type="MEDICATION", title="Start metformin",
                     details={"drug": "metformin", "dose": "500 twice a day",
                              "action": "start"}, state="open"),
        ),
    ),
    Person(
        name="Hoda Serageldin", sex="female", age=38, specialty="nephrology",
        diagnosis="chronic kidney disease stage three", speak="en",
        plan="Repeat the kidney function tests in one week.",
        said="I did the test yesterday",
        contracts=(
            Contract(type="TEST", title="Kidney function tests",
                     details={"test_name": "Kidney function tests"},
                     due_in_days=-1, state="pending_review", contacts=3,
                     results=({"analyte": "Creatinine", "value": "1.9",
                               "unit": "mg/dL", "level": "above_target",
                               "line": "Creatinine 1.9 mg/dL, above target"},)),
        ),
    ),
    Person(
        name="Mostafa Ghoneim", sex="male", age=71, specialty="nephrology",
        diagnosis="chronic kidney disease, on a potassium binder", speak="ar",
        plan="This test is important and must be done on time.",
        said="Did you receive the result?",
        contracts=(
            Contract(type="TEST", title="Electrolytes",
                     details={"test_name": "electrolytes"}, due_in_days=-2,
                     state="pending_review", contacts=3,
                     critical="critical lab value",
                     results=({"analyte": "K", "value": "6.4", "unit": "mmol/L",
                               "level": "critical",
                               "line": "Potassium 6.4 mmol/L, CRITICAL"},)),
        ),
    ),
    Person(
        name="Amany Roushdy", sex="female", age=29, specialty="obstetrics",
        diagnosis="first pregnancy, twenty six weeks", speak="ar",
        plan="Do the pregnancy glucose test next week.",
        said="Can I come Monday instead of Wednesday?",
        contracts=(
            Contract(type="VISIT", title="Antenatal visit", due_in_days=5,
                     state="open", contacts=1),
            # rev 17 items 7 and 14: one loop on the board is at the policy
            # ceiling, so a wake-up on it is REFUSED by core/policy.py rather
            # than sent, deterministically and with no staging. It is what the
            # runbook films: `/force_due Amany glucose strict`. Nothing else
            # about this patient is special, which is the point: the six
            # contacts are a number on a record, and the refusal is code
            # reading that number.
            Contract(type="TEST", title="Glucose tolerance test",
                     details={"test_name": "glucose"}, due_in_days=7,
                     contacts=6),
        ),
    ),
    Person(
        name="Gehan Mounir", sex="female", age=33, specialty="obstetrics",
        diagnosis="second pregnancy, anaemia", speak="ar",
        plan="Take iron every day after food and repeat the blood count in one month.",
        said="The medicine is not available at the pharmacy",
        contracts=(
            Contract(type="MEDICATION", title="Start oral iron",
                     details={"drug": "ferrous sulfate", "dose": "one a day",
                              "action": "start"}, state="open",
                     barrier="availability", contacts=2,
                     relay="The medicine is not available at the pharmacy"),
            Contract(type="TEST", title="Complete blood count",
                     details={"test_name": "cbc"}, due_in_days=21),
        ),
    ),
    Person(
        name="Youssef Abu Zeid", sex="male", age=7, specialty="paediatrics",
        diagnosis="asthma, on an inhaler", speak="ar",
        plan="Tell me if the cough gets worse at night.",
        said="Thank you, doctor",
        contracts=(
            Contract(type="VISIT", title="Asthma review", due_in_days=12,
                     state="open", contacts=1),
        ),
    ),
    Person(
        name="Malak Sabry", sex="female", age=11, specialty="paediatrics",
        diagnosis="recurrent tonsillitis", speak="ar",
        plan="Finish the full treatment even if the fever settles.",
        said="We finished the treatment",
        contracts=(
            Contract(type="MEDICATION", title="Finish the antibiotic course",
                     details={"drug": "amoxicillin", "dose": "three times a day",
                              "action": "start"}, state="done", reviewed=True,
                     contacts=2,
                     results=({"analyte": "course", "value": "completed",
                               "line": "course reported completed"},)),
        ),
    ),
    Person(
        name="Ibrahim Sallam", sex="male", age=59, specialty="general medicine",
        diagnosis="high cholesterol", speak="en",
        plan="Repeat the lipid panel in two weeks.",
        said="where do I send it?",
        contracts=(
            Contract(type="TEST", title="Lipid panel",
                     details={"test_name": "Lipid panel"}, due_in_days=-3,
                     state="done", reviewed=True, contacts=3,
                     results=_lipids("128")),
        ),
    ),
    Person(
        name="Fatma El Deeb", sex="female", age=44, specialty="general medicine",
        diagnosis="vitamin D deficiency", speak="ar",
        plan="Take the vitamin once a week for two months.",
        said="I forgot last week's dose",
        contracts=(
            Contract(type="MEDICATION", title="Weekly vitamin D",
                     details={"drug": "vitamin D", "dose": "once a week",
                              "action": "start"}, state="open",
                     barrier="forgot", contacts=2),
        ),
    ),
    Person(
        name="Kamal Wahba", sex="male", age=68, specialty="pulmonology",
        diagnosis="chronic obstructive pulmonary disease", speak="ar",
        plan="Tell me at once if your breathing becomes worse than usual.",
        said="",
        contracts=(
            Contract(type="VISIT", title="Chest clinic review", due_in_days=-6,
                     state="unreachable", contacts=3),
            Contract(type="TEST", title="Chest imaging",
                     details={"test_name": "chest x ray"}, due_in_days=-4,
                     state="unreachable", contacts=3),
        ),
    ),
    Person(
        name="Noura Bahgat", sex="female", age=26, specialty="pulmonology",
        diagnosis="asthma in pregnancy", speak="en",
        plan="Keep the inhaler with you and tell me if you need it more often.",
        said="I used it three times this week",
        contracts=(
            Contract(type="MONITOR", title="Inhaler use",
                     details={"metric": "inhaler puffs", "schedule": "once a day",
                              "days": 7},
                     readings=({"day": 1, "slot": 0, "value": "2", "number": 2.0},
                               {"day": 2, "slot": 0, "value": "1", "number": 1.0}),
                     contacts=2),
        ),
    ),
    Person(
        name="Tarek Shalaby", sex="male", age=50, specialty="gastroenterology",
        diagnosis="fatty liver disease", speak="en",
        plan="Repeat the liver function tests in three weeks.",
        said="I lost my prescription",
        contracts=(
            Contract(type="TEST", title="Liver function tests",
                     details={"test_name": "liver"}, due_in_days=17),
        ),
    ),
    Person(
        name="Reem Fahmy", sex="female", age=35, specialty="gastroenterology",
        diagnosis="irritable bowel syndrome", speak="ar",
        plan="Try the diet we agreed and record your symptoms.",
        said="I feel well, why should I come back?",
        contracts=(
            Contract(type="VISIT", title="Follow-up visit", due_in_days=10,
                     state="open", barrier="asymptomatic", contacts=2,
                     relay="I feel well, why should I come back?"),
        ),
    ),
    Person(
        name="Sherif Nassar", sex="male", age=62, specialty="rheumatology",
        diagnosis="gout", speak="ar",
        plan="Do a uric acid test in two weeks.",
        said="I did the test yesterday",
        contracts=(
            Contract(type="TEST", title="Uric acid",
                     details={"test_name": "uric acid"}, due_in_days=2,
                     contacts=2),
            Contract(type="MEDICATION", title="Start allopurinol",
                     details={"drug": "allopurinol", "dose": "100 daily",
                              "action": "start"}, state="open"),
        ),
    ),
    Person(
        name="Laila Ezzat", sex="female", age=57, specialty="rheumatology",
        diagnosis="rheumatoid arthritis, on a weekly agent", speak="en",
        plan="Blood tests every month while you are on this treatment.",
        said="the transport is difficult for me",
        contracts=(
            Contract(type="TEST", title="Complete blood count",
                     details={"test_name": "cbc"}, due_in_days=1,
                     barrier="transport", contacts=3),
            Contract(type="TEST", title="Liver function tests",
                     details={"test_name": "liver"}, due_in_days=1, contacts=2),
        ),
    ),
    Person(
        name="Adel Mansi", sex="male", age=73, specialty="haematology",
        diagnosis="on long term anticoagulation", speak="ar",
        plan="Do the clotting test every two weeks without delay.",
        said="I have the result and will send it",
        contracts=(
            Contract(type="TEST", title="Coagulation profile",
                     details={"test_name": "coagulation"}, due_in_days=-1,
                     state="pending_review", contacts=3,
                     critical="critical lab value",
                     results=({"analyte": "INR", "value": "6.1", "unit": "",
                               "level": "critical",
                               "line": "INR 6.1, CRITICAL"},)),
        ),
    ),
    Person(
        name="Mervat Halim", sex="female", age=41, specialty="haematology",
        diagnosis="iron deficiency anaemia", speak="ar",
        plan="Continue the iron and repeat the blood count in one month.",
        said="I finished the first strip",
        contracts=(
            Contract(type="TEST", title="Complete blood count",
                     details={"test_name": "cbc"}, due_in_days=-5,
                     state="done", reviewed=True, contacts=3,
                     results=({"analyte": "Hb", "value": "11.8", "unit": "g/dL",
                               "level": "normal",
                               "line": "Haemoglobin 11.8 g/dL, no printed range to compare"},)),
            Contract(type="MEDICATION", title="Continue oral iron",
                     details={"drug": "ferrous sulfate", "dose": "one a day",
                              "action": "start"}, state="done", reviewed=True,
                     contacts=1),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# The board, in memory
# --------------------------------------------------------------------------- #
def _id(doctor_id: str, *parts: Any) -> str:
    """A deterministic document id, so seeding twice replaces rather than doubles."""
    return "bg" + doctor_id[:8] + "".join(f"-{part}" for part in parts)


def _reading_row(base: datetime, row: dict[str, Any], per_day: int
                 ) -> dict[str, Any]:
    """A fixture reading (day, slot) -> the row a MONITOR loop stores.

    Day one is the confirmation day, when the plan and welcome first ask for
    the reading (core/chaser.schedule_loop, core/monitoring.FIRST_REMINDER_DAY).
    Reminders begin on contract day two, so the fixture uses that same origin.
    """
    hour = (8, 20, 13, 17)[min(int(row.get("slot", 0)), 3)] if per_day > 1 else 9
    when = base + timedelta(days=int(row.get("day", 1)) + monitoring.FIRST_REMINDER_DAY - 1)
    when = when.replace(hour=hour, minute=0, second=0, microsecond=0)
    return {"at": when.isoformat(timespec="minutes"),
            "value": row["value"], "number": row["number"]}


def records(doctor_id: str, now: datetime
            ) -> tuple[list[Patient], list[Loop], list[Event], list[Relay]]:
    """The whole background board as records. No I/O, no ids from anywhere else."""
    patients: list[Patient] = []
    loops: list[Loop] = []
    events: list[Event] = []
    relays: list[Relay] = []

    for index, person in enumerate(PEOPLE, start=1):
        patient_id = _id(doctor_id, index)
        made = now - timedelta(minutes=len(PEOPLE) - index)
        patients.append(Patient(
            id=patient_id, doctor_id=doctor_id, name=person.name,
            phone=PHONE.format(index), age=person.age, sex=person.sex,
            diagnosis=person.diagnosis, plan_text=person.plan,
            channels={"web": True, "telegram_chat_id": None},
            status="active", created_at=made,
        ))
        if person.said:
            events.append(Event(
                id=_id(doctor_id, index, "said"), doctor_id=doctor_id,
                patient_id=patient_id, kind="patient_in", channel="web",
                text=person.said,
                meta={"source": "text", "synthetic": True}, ts=made,
            ))

        for number, contract in enumerate(person.contracts, start=1):
            loop_id = _id(doctor_id, index, number)
            started = made + timedelta(seconds=number)
            per_day = 2 if "twice" in str(contract.details.get("schedule") or "") \
                else 1
            loops.append(Loop(
                id=loop_id, patient_id=patient_id, doctor_id=doctor_id,
                type=contract.type, title=contract.title,
                details=dict(contract.details), state=contract.state,
                due_at=(now + timedelta(days=contract.due_in_days)
                        if contract.due_in_days is not None else None),
                attempts=min(contract.contacts, 3),
                results=[dict(row) for row in contract.results],
                readings=[_reading_row(started, dict(row), per_day)
                          for row in contract.readings],
                contacts=contract.contacts,
                barrier=contract.barrier, paused=contract.paused,
                doctor_reviewed=contract.reviewed,
                created_at=started, updated_at=started,
            ))
            if contract.critical:
                events.append(Event(
                    id=_id(doctor_id, index, number, "crit"),
                    doctor_id=doctor_id, patient_id=patient_id, loop_id=loop_id,
                    kind="escalation", channel="web",
                    text=f"critical result escalated: {contract.title}",
                    meta={"sentinel": {"fired": True, "net": "code",
                                       "concept": contract.critical,
                                       "nets_run": ["code"]},
                          "synthetic": True,
                          "decided_by": "code (core/labs.py critical table)"},
                    # Exactly `now`, not `started`: the end-of-day summary counts
                    # a critical result for the day it fired, and a seed run a
                    # few minutes after midnight UTC would otherwise put some of
                    # these on yesterday and quietly change the runbook numbers.
                    ts=now,
                ))
            if contract.relay:
                relays.append(Relay(
                    id=_id(doctor_id, index, number, "relay"),
                    doctor_id=doctor_id, patient_id=patient_id, loop_id=loop_id,
                    question=contract.relay,
                    reason=f"barrier: {contract.barrier or 'unclear'}",
                    state="open", created_at=started,
                ))

    return patients, loops, events, relays


def expected(doctor_id: str = "background", now: Optional[datetime] = None):
    """The end-of-day counts these twenty produce, on a board with nobody else.

    Computed by core/summary.py from the same records `seed()` writes, so the
    numbers in docs/RUNBOOK.md are asserted rather than typed (a test reads both
    and compares them).
    """
    from . import summary

    when = now or datetime(2026, 8, 29, 10, 0)
    _, loops, events, relays = records(doctor_id, when)
    return summary.compute(loops, events, relays)


def line(doctor_id: str = "background", now: Optional[datetime] = None) -> str:
    from . import summary

    return summary.line(expected(doctor_id, now))


# --------------------------------------------------------------------------- #
# Writing them
# --------------------------------------------------------------------------- #
async def seed(doctor: Any) -> dict[str, Any]:
    """Put the twenty on this doctor's board. Writes records and nothing else.

    No Cloud Task is created and no message is sent, which is the whole point:
    twenty background patients are load on the board, not twenty phones being
    written to. `core/store.py` is imported here rather than at module scope so
    that the table above and everything that counts it run with nothing
    installed.
    """
    from . import store

    patients, loops, events, relays = records(doctor.id, store.now())
    for patient in patients:
        await store.create_patient(patient)
    for loop in loops:
        await store.create_loop(loop)
    for event in events:
        await store.add_event(event)
    for relay in relays:
        await store.save_relay(relay)
    counts = expected(doctor.id, store.now())
    return {
        "synthetic": True,
        "patients": len(patients), "loops": len(loops),
        "events": len(events), "relays": len(relays),
        "expected_line": _line_of(counts),
        "expected_counts": counts.as_dict(),
    }


def _line_of(counts: Any) -> str:
    from . import summary

    return summary.line(counts)
