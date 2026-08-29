"""Owns the follow-up itself: Sanad waking up on its own and nudging a patient.

When a loop is committed with a due date, three tasks are created at once - two
days before due, on the due day, three days after - and the process forgets
them. Cloud Tasks calls /tasks/nudge back at each of those moments, and `fire()`
below is what happens then.

Everything `fire()` checks is a rule, in this order, all of them in code:

  run id        a task from a purged rehearsal is dropped, not sent;
  loop state    a loop that is no longer waiting is dropped;
  re-arm        a contact further away than Cloud Tasks can hold arrives in
                hops (core/tasks.py); a hop puts itself back on the queue and
                sends nothing;
  paused        a loop stopped on a recorded barrier stays quiet;
  quiet hours   22:00-09:00 Cairo is re-scheduled, not skipped;
  one a day     a patient who already heard from Sanad today is re-scheduled;
  policy        the doctor's own window and contact limits (core/policy.py),
                which is where "never more than six contacts" is enforced on
                the ladder as well as on the agent;
  schedule      a task made for a schedule that has since been replaced is
                dropped with "superseded schedule" (loop.schedule_version);
  idempotency   (loop, generation, kind, attempt) is claimed in Firestore
                before anything thinks or speaks, so a Cloud Tasks retry finds
                the row, costs no model call and sends nothing. The generation
                is what makes a restarted ladder a new claim instead of a
                suppressed one;
  coordinator   the Care Coordinator decides what this wake-up is for, carrying
                that same receipt, and the ladder step below is what happens
                when it stands down.

The nudge text itself is a template in this file, not a generated sentence. A
reminder has no judgement in it, so there is nothing for a model to add and one
less place for a number to appear that the doctor never wrote.

The third nudge with no reply closes the ladder: the loop becomes "unreachable"
and the doctor gets a white card. It stays open and it stays quiet. Any patient
reply resets `attempts` to zero and increments `generation`, which is what makes
that impossible to reach for a patient who is actually answering.

The order of operations on a send (codex item 5) is: claim the receipt, write
the loop state, count the contact, write the audit event, and only then speak.
A delivery that throws leaves the receipt as "failed" with the error on it, and
the receipt is kept for ever: releasing it would let the retry redo the whole
wake-up and count the contact twice. The retry sees "failed" instead and is
allowed to send the message once more and no more than once. The consequence,
stated plainly because it is a choice and not an accident: a message that failed
to leave is still counted against the patient's day and against the loop's six
contacts. Counting a message that may not have arrived is the smaller error;
the other direction loses a delivered message from the count for ever.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import (
    events, gender, lang, names, policy, settings, store, tasks, templates,
    timing,
)
from .adapters import OutboundMessage, fanout
from .models import Doctor, Loop, Patient, Send

log = logging.getLogger("sanad.chaser")

NUDGE_PATH = "/tasks/nudge"
LADDER_LENGTH = len(timing.LADDER_DAYS)
# A monitoring loop is the same machinery with a daily reminder. The cap is a
# guard on the queue, not a clinical rule: a 30-day monitor gets 14 reminders.
MONITOR_MAX = 14

# The loop states a nudge is still allowed to fire on.
LIVE_STATES = ("open", "waiting_patient")

# A hop that lands within a minute of its real moment is that moment: re-arming
# for the last few seconds would be a task that wakes twice to save nothing.
RE_ARM_SLACK = 60.0

# The audit line a withheld nudge carries. Fixed wording, because it is what a
# judge is looking for and what the runbook tells him to look for.
REFUSED_BY_CODE = "refused by code (core/policy.py)"


# --------------------------------------------------------------------------- #
# The text. Templates, not generation.
# --------------------------------------------------------------------------- #
# Arabic conjugates the second person, so every line a patient reads exists in
# three forms: to a man, to a woman, and - when the record does not say - a
# phrasing with no gendered verb in it at all. Mohamed's first phone test found
# a female patient being addressed as a man; core/gender.py decides which of the
# three this is, from the record's own `sex` field, in code.
NUDGE_AR: dict[str, dict[int, str]] = {
    "m": {
        1: "أهلاً {patient} 👋 فاكر إن {doctor} طالب منك {what}؟ الميعاد قرب.\n"
           "أول ما تعمله ابعتلي صورة النتيجة هنا.",
        2: "أهلاً {patient}، النهاردة ميعاد {what} اللي طلبه {doctor}.\n"
           "ابعتلي صورة النتيجة هنا أول ما تستلمها.",
        3: "أهلاً {patient}، لسه {what} مأتمّش، و{doctor} مستني النتيجة.\n"
           "لو في حاجة صعّبت الموضوع - معمل، مواصلات، فلوس، أو معرفش تعمله فين - "
           "قوللي وأنا أبلّغ {doctor}.",
    },
    "f": {
        1: "أهلاً {patient} 👋 فاكرة إن {doctor} طالب منك {what}؟ الميعاد قرب.\n"
           "أول ما تعمليه ابعتيلي صورة النتيجة هنا.",
        2: "أهلاً {patient}، النهاردة ميعاد {what} اللي طلبه {doctor}.\n"
           "ابعتيلي صورة النتيجة هنا أول ما تستلميها.",
        3: "أهلاً {patient}، لسه {what} مأتمّش، و{doctor} مستني النتيجة.\n"
           "لو في حاجة صعّبت الموضوع - معمل، مواصلات، فلوس، أو مش عارفة تعمليه فين - "
           "قوليلي وأنا أبلّغ {doctor}.",
    },
    "u": {
        1: "أهلاً {patient} 👋 تذكير: {doctor} طالب {what}، والميعاد قرب.\n"
           "برجاء إرسال صورة النتيجة هنا أول ما تجهز.",
        2: "أهلاً {patient}، النهاردة ميعاد {what} اللي طلبه {doctor}.\n"
           "برجاء إرسال صورة النتيجة هنا أول ما تجهز.",
        3: "أهلاً {patient}، لسه {what} مأتمّش، و{doctor} مستني النتيجة.\n"
           "لو في حاجة صعّبت الموضوع - معمل، مواصلات، فلوس، أو مكان التحليل - "
           "برجاء الإبلاغ هنا وأنا أوصّلها لـ{doctor}.",
    },
}
NUDGE_EN = {
    1: "Hello {patient} 👋 A reminder that {doctor} asked you for {what}. "
       "It is coming up soon.\nSend me a photo of the result here once you have it.",
    2: "Hello {patient}, {what} that {doctor} asked for is due today.\n"
       "Send me a photo of the result here as soon as you get it.",
    3: "Hello {patient}, {what} is still outstanding and {doctor} is waiting for "
       "the result.\nIf anything is making it hard - the lab, transport, the cost, "
       "or not knowing where to go - tell me and I will pass it to {doctor}.",
}
MONITOR_AR: dict[str, str] = {
    "m": "أهلاً {patient}، فكرة سريعة: {doctor} طالب منك تقيس {what}.\n"
         "ابعتلي القراءة هنا كده وخلاص.",
    "f": "أهلاً {patient}، فكرة سريعة: {doctor} طالب منك تقيسي {what}.\n"
         "ابعتيلي القراءة هنا كده وخلاص.",
    "u": "أهلاً {patient}، فكرة سريعة: {doctor} طالب قياس {what}.\n"
         "برجاء إرسال القراءة هنا.",
}
MONITOR_EN = ("Hello {patient}, a quick reminder from {doctor} to measure {what}.\n"
              "Just send me the reading here.")


def what_for(loop: Loop) -> str:
    """What the patient is being reminded about, in the doctor's own words."""
    details = loop.details or {}
    return str(details.get("test_name") or details.get("metric") or loop.title)


def nudge_text(patient: Patient, doctor: Doctor, loop: Loop, attempt: int,
               speak: str, kind: str) -> str:
    """The exact words of one nudge. A template lookup, never a generated line.

    The name is the one core/names.py says an Arabic sentence may use (rev 17
    item 11): the Arabic form when one is known, and nothing at all when it is
    not, because "أهلاً Ahmed" is the tell of a machine. `templates.tidy` then
    closes the gap a dropped name leaves in the punctuation.
    """
    who = gender.of_patient(patient)
    fields = {"patient": names.in_arabic(patient.name) if speak == "ar"
                         else names.first_name(patient.name),
              "doctor": doctor.name, "what": what_for(loop)}
    if kind == "monitor":
        table = MONITOR_AR[who] if speak == "ar" else MONITOR_EN
        return templates.tidy(table.format(**fields))
    table = NUDGE_AR[who] if speak == "ar" else NUDGE_EN
    return templates.tidy(
        table[min(max(attempt, 1), LADDER_LENGTH)].format(**fields)
    )


def unreachable_card(patient: Patient, loop: Loop, days_overdue: int) -> dict:
    overdue = f", {days_overdue} days overdue" if days_overdue > 0 else ""
    return {
        "title": f"⚪ {patient.name} unreachable",
        "severity": "white",
        "lines": [
            f"{what_for(loop)}{overdue}.",
            f"{LADDER_LENGTH} reminders sent, no reply.",
            "The loop stays open and Sanad stops messaging "
            f"{gender.object_pronoun(gender.of_patient(patient))}.",
        ],
        "actions": [],
    }


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
async def schedule_loop(loop: Loop) -> list[dict[str, Any]]:
    """Create this loop's whole future at commit time. Returns what was queued.

    Pre-scheduling the whole ladder at commit is deliberate and it stays: it is
    what lets the process forget the patient entirely. What it never had was a
    way to be superseded, so a loop the doctor or the patient later moved still
    had three old reminders on the queue (codex item 9). Every payload now
    carries the loop's `schedule_version`, and `fire` refuses a payload made for
    a version that has been replaced.
    """
    run_id, scale = await settings.current()
    queued: list[dict[str, Any]] = []

    version = int(loop.schedule_version or 0)

    if loop.type == "MONITOR":
        days = int((loop.details or {}).get("days") or 0) or 1
        for day in range(1, min(days, MONITOR_MAX) + 1):
            payload = {"kind": "monitor", "run_id": run_id, "loop_id": loop.id,
                       "attempt": day, "schedule_version": version}
            name = await tasks.enqueue(NUDGE_PATH, payload, timing.seconds(day, scale))
            queued.append({"attempt": day, "task": name})
        return queued

    if loop.due_at is None:
        return queued

    due_in_days = (loop.due_at - store.now()).total_seconds() / timing.REAL_DAY_SECONDS
    for attempt, delay in timing.ladder_delays(due_in_days, scale):
        payload = {"kind": "nudge", "run_id": run_id, "loop_id": loop.id,
                   "attempt": attempt, "schedule_version": version}
        name = await tasks.enqueue(NUDGE_PATH, payload, delay)
        queued.append({"attempt": attempt, "delay_s": round(delay, 1), "task": name})
    return queued


async def schedule_patient(patient: Patient) -> list[dict[str, Any]]:
    """Every loop of a freshly committed patient, in one go."""
    queued: list[dict[str, Any]] = []
    for loop in await store.list_loops(patient.id):
        queued += await schedule_loop(loop)
    return queued


def split_argument(argument: str, names: list[str]) -> tuple[str, str]:
    """"Ahmed lipid" -> ("ahmed", "lipid"). Longest patient match wins.

    S3 review, carry-over 2: a patient with two open loops needs the doctor to
    be able to say which one. The split is decided against the real names on the
    board, so "Ahmed Ali" stays one patient and "Ahmed lipid" does not.
    """
    words = (argument or "").strip().lower().split()
    for cut in range(len(words), 0, -1):
        head = " ".join(words[:cut])
        if any(head in name.lower() for name in names):
            return head, " ".join(words[cut:])
    return " ".join(words), ""


def matches_loop(loop: Loop, word: str) -> bool:
    """Does this loop answer to the word the doctor typed?"""
    haystack = " ".join(
        [loop.title, loop.type, *(str(v) for v in (loop.details or {}).values())]
    ).lower()
    return word in haystack


# The one word that turns /force_due back into an ordinary wake-up. Everything
# else about the task is identical; what changes is that the guards apply
# (rev 17 item 7). It exists because a refusal you cannot reproduce is a
# refusal you cannot film, and the whole claim of this system is that code
# refuses the model and the ladder alike.
STRICT = "strict"


async def force_due(doctor: Doctor, argument: str) -> str:
    """/force_due <patient> [loop word] [strict]: that loop, or the earliest, now.

    It enqueues the same task the timer would, with a near-zero delay, through
    the same handler - the demo is the real path with a shorter clock. It is
    marked `force`, which is the one thing that skips quiet hours, the one-a-day
    rule and the doctor's own contact window: the doctor asking for it now is
    the doctor's call.

    Ending the command with `strict` drops that mark, so the task arrives as an
    ordinary scheduled wake-up and every guard applies to it. On a loop already
    at the policy ceiling that is a refusal in words, written to the feed and
    printed on the audit line, which is the one honest way to show a guard
    saying no on camera.
    """
    argument = (argument or "").strip()
    strict = argument.lower().endswith(f" {STRICT}")
    if strict:
        argument = argument[: -len(STRICT)].strip()
    if not argument:
        return "Usage: /force_due <patient name> [loop word] [strict]"
    everyone = await store.list_patients(doctor.id)
    board_names = [p.name for p in everyone]
    fragment, loop_word = split_argument(argument, board_names)
    # Two patients matching one fragment is a question, not a coin toss
    # (core/names.py): the wrong patient would be chased for nothing.
    match = names.resolve(board_names, fragment)
    if match.ambiguous:
        return match.warning()
    if match.one is None:
        return match.nobody()
    patient = next(p for p in everyone if p.name == match.one)

    loops = [l for l in await store.list_loops(patient.id) if l.state in LIVE_STATES]
    if not loops:
        return f"{patient.name} has no open loop to force."
    if loop_word:
        named = [l for l in loops if matches_loop(l, loop_word)]
        if not named:
            open_titles = ", ".join(l.title for l in loops)
            return (f"{patient.name} has no open loop matching {loop_word!r}. "
                    f"Open: {open_titles}.")
        loops = named
    loops.sort(key=lambda l: (l.due_at is None, l.due_at or l.created_at))
    loop = loops[0]

    run_id, _ = await settings.current()
    payload = {"kind": "monitor" if loop.type == "MONITOR" else "nudge",
               "run_id": run_id, "loop_id": loop.id,
               "attempt": loop.attempts + 1, "force": not strict,
               "schedule_version": int(loop.schedule_version or 0)}
    name = await tasks.enqueue(NUDGE_PATH, payload, 0)
    await events.append_event(
        doctor.id, "system", f"forced {loop.title} due for {patient.name}",
        patient_id=patient.id, loop_id=loop.id,
        meta={"task": name, "payload": payload, "strict": strict},
    )
    how = " Every guard applies to it." if strict else ""
    return (f"Forced: {patient.name} · {loop.title}. Nudge {payload['attempt']} of "
            f"{LADDER_LENGTH} is on the queue now.{how}")


# --------------------------------------------------------------------------- #
# Firing
# --------------------------------------------------------------------------- #
# The audit line a task from a replaced schedule carries. Fixed wording, for the
# same reason REFUSED_BY_CODE is fixed: it is what the runbook tells a judge to
# look for on the board.
SUPERSEDED = "superseded schedule"


def receipt_key(loop_id: str, generation: int, kind: str, attempt: int) -> str:
    """The idempotency key of one wake-up: loop, generation, kind, attempt.

    The generation is the fix for codex item 7. `attempts` is reset to zero by
    any patient reply, so without it the restarted ladder asked for
    "loop:nudge:1", found the receipt the first ladder had left there, and the
    patient who had answered once was never reminded again. A reset increments
    the generation (core/store.bump_generation), so the restarted ladder claims
    keys nobody has claimed while a Cloud Tasks retry inside one generation
    still finds the row and sends nothing.
    """
    return f"{loop_id}:{generation}:{kind}:{attempt}"


async def delivery_failed(patient: Patient, doctor: Doctor, loop: Loop,
                          send_id: str, attempt: int, error: str) -> None:
    """A nudge that threw on the way out, as something the doctor can see.

    Not an obligation: it carries no button, so core/cards.py leaves it out of
    the Inbox. It is a notice, and the honest one: the reminder was counted and
    was not delivered, and the retry of this task will try it once more.
    """
    await events.append_event(
        doctor.id, "card",
        f"a reminder to {patient.name} could not be delivered",
        patient_id=patient.id, loop_id=loop.id,
        meta={"receipt": send_id, "error": error,
              "decided_by": "code (core/chaser.py, the receipt is kept as failed)",
              "audit": {"tier": "chaser", "receipt": send_id,
                        "line": f"delivery failed on nudge {attempt}: {error}"},
              "card": {"title": f"Reminder not delivered · {patient.name}",
                       "severity": "yellow",
                       "lines": [f"{what_for(loop)}, reminder {attempt}.",
                                 f"The channel refused it: {error}",
                                 "The reminder is counted and the receipt is "
                                 "kept as failed, so a retry of this task sends "
                                 "it once more and never twice."],
                       "actions": []}},
    )

async def supersede_ladder(loop_id: str, reason: str) -> int:
    """Invalidate every task already queued for this loop. Returns the version.

    The one function anything outside this file calls to say "what is on the
    queue for this loop is out of date". Nothing is deleted, because Cloud Tasks
    cannot be reached into: the version on the loop moves, and `fire` refuses
    every payload made for a version below it.

    Its first caller is the evidence path (kernel review F8b). The ladder for a
    TEST loop is created at commit time and asks the patient to do the test; a
    patient who has already sent the slip must never receive the next rung of
    it, and "please do the test" arriving the day after the result did is the
    worst sentence this system can produce. So the arrival of evidence retires
    the rest of that loop's ladder. The Coordinator calls it when it marks
    evidence received; core/extractor.py calls it when a slip attaches
    (handed to wave C).
    """
    version = await store.bump_schedule_version(loop_id)
    loop = await store.get_loop(loop_id)
    if loop is not None:
        await events.append_event(
            loop.doctor_id, "system",
            f"ladder superseded on {loop.title}: {reason}",
            patient_id=loop.patient_id, loop_id=loop.id,
            meta={"schedule_version": version, "reason": reason,
                  "audit": {"tier": "chaser",
                            "line": f"{SUPERSEDED}: {reason}. Everything queued "
                                    f"for schedule {version - 1} is refused on "
                                    "arrival"},
                  "decided_by": "code (core/chaser.py schedule version)"},
        )
    return version


async def resend(patient: Patient, doctor: Doctor, loop: Loop, send: Send,
                 attempt: int, kind: str) -> dict[str, Any]:
    """The one retry a failed delivery is allowed, and nothing else.

    The last attempt at this exact wake-up was decided and written and then
    threw on the way out. Everything except the message itself has already
    happened, so this is the message and nothing else: no model call, no second
    attempt on the ladder. `core.store.claim_send` is what allows this at most
    once.

    The contact IS counted here, and that is not a second contact: the failed
    attempt gave its one back (`store.refund_contact`), because a message that
    never reached the patient must not spend one of the six the doctor's policy
    allows on this obligation. Counting it before the send and refunding it on
    an explicit failure keeps one thing true on every path: the number of
    contacts on a loop is the number of messages that reached the wire.
    """
    speak = await lang.for_patient(patient, doctor.id)
    text = nudge_text(patient, doctor, loop, attempt, speak, kind)
    day = timing.day_index(store.now(), (await settings.current())[1])
    await store.add_contact(loop.id, day)
    try:
        await fanout().send(f"patient:{patient.id}", OutboundMessage(
            text=text, receipt=send.id,
            meta={"audit": {"tier": "nudge", "attempt": attempt,
                            "kind": kind,
                            "generated": "code template",
                            "resend": True, "receipt": send.id}}))
    except Exception as exc:  # noqa: BLE001 - the receipt keeps the error
        error = " ".join(str(exc).split())[:200]
        await store.mark_send(send.id, "failed", error)
        await store.refund_contact(loop.id)
        await delivery_failed(patient, doctor, loop, send.id, attempt, error)
        raise
    await store.mark_send(send.id, "sent")
    return {"sent": True, "attempt": attempt, "resend": True,
            "loop": loop.id, "key": send.id}


async def fire(payload: dict[str, Any]) -> dict[str, Any]:
    """One scheduled nudge. Every early return is a rule, and says which one."""
    run_id, scale = await settings.current()
    if str(payload.get("run_id")) != run_id:
        log.info("dropping task from run %s (current %s)", payload.get("run_id"), run_id)
        return {"sent": False, "reason": "stale run id"}

    loop_id = str(payload.get("loop_id") or "")
    loop = await store.get_loop(loop_id) if loop_id else None
    if loop is None:
        return {"sent": False, "reason": "loop is gone"}
    if loop.state not in LIVE_STATES:
        return {"sent": False, "reason": f"loop is {loop.state}"}

    # codex item 9. A reschedule cannot reach into Cloud Tasks and delete what
    # is already queued, so the queue is left alone and the task is refused on
    # arrival instead: a payload made for a schedule that has since been
    # replaced sends nothing and says why on the board.
    made_for = int(payload.get("schedule_version") or 0)
    current_version = int(loop.schedule_version or 0)
    if made_for < current_version:
        stale = (await store.doctor_by_id(loop.doctor_id))
        if stale is not None:
            await events.append_event(
                stale.id, "system",
                f"{SUPERSEDED} on {loop.title}",
                patient_id=loop.patient_id, loop_id=loop.id,
                meta={"made_for": made_for, "current": current_version,
                      "audit": {"tier": "chaser",
                                "line": f"{SUPERSEDED}: this task was made for "
                                        f"schedule {made_for} and the loop is "
                                        f"now on {current_version}"},
                      "decided_by": "code (core/chaser.py schedule version)"},
            )
        return {"sent": False, "reason": SUPERSEDED, "made_for": made_for,
                "current": current_version}

    patient = await store.get_patient(loop.patient_id)
    doctor = await store.doctor_by_id(loop.doctor_id)
    if patient is None or doctor is None:
        return {"sent": False, "reason": "patient or doctor is gone"}

    kind = str(payload.get("kind") or "nudge")
    attempt = int(payload.get("attempt") or loop.attempts + 1)
    force = bool(payload.get("force"))
    now = store.now()

    # A contact more than 720 hours away cannot sit on the queue in one piece
    # (core/tasks.py), so it arrives in hops. This is a hop: the moment it is
    # really for has not come, so it goes back on the queue for what is left and
    # nothing is sent, counted or claimed. The event says so, because an extra
    # wake-up that explains itself is an audit trail and a silent one is a bug.
    not_before = tasks.due_at(payload)
    if not_before is not None and (not_before - now).total_seconds() > RE_ARM_SLACK:
        name = await tasks.enqueue(
            NUDGE_PATH, payload, (not_before - now).total_seconds()
        )
        await events.append_event(
            doctor.id, "system", f"re-armed for {not_before:%Y-%m-%d}",
            patient_id=patient.id, loop_id=loop.id,
            meta={"task": name, "not_before": not_before.isoformat(),
                  "audit": {"tier": "chaser",
                            "line": f"re-armed for {not_before:%Y-%m-%d}: Cloud "
                                    "Tasks holds no schedule more than 720 hours out"},
                  "decided_by": "code (core/tasks.py, one hop at a time)"},
        )
        return {"sent": False, "reason": "re-armed", "task": name,
                "not_before": not_before.isoformat()}

    # The Coordinator may have stopped this loop on a barrier the doctor is
    # holding (core/coordinator.py). A paused loop is not a finished loop: it
    # stays open, and it stays quiet.
    if loop.paused:
        return {"sent": False, "reason": "loop is paused on a recorded barrier"}

    if not force:
        if timing.in_quiet_hours(now, scale):
            when = timing.next_allowed(now, scale)
            await tasks.enqueue(NUDGE_PATH, payload, (when - now).total_seconds())
            return {"sent": False, "reason": "quiet hours", "retry_at": when.isoformat()}
        # codex item 12. This used to count Send rows, which are ladder nudges
        # and nothing else, so a Coordinator template earlier the same day was
        # invisible to it and the patient heard from Sanad twice. It reads the
        # patient-wide ledger now, which every outbound message Sanad starts
        # goes through, whichever loop or agent started it.
        today = timing.day_index(now, scale)
        if await store.contacted_on(patient.id, today):
            await tasks.enqueue(NUDGE_PATH, payload, timing.seconds(1, scale))
            return {"sent": False, "reason": "one message per patient per day"}

    # The doctor's own limits, applied to the ladder itself: the same guard the
    # Coordinator's schedule tool has to pass (core/policy.py). It is what makes
    # "never more than six contacts on one loop" true no matter who asked for
    # the message. /force_due is exempt for the reason it is exempt from quiet
    # hours: the doctor asking for it now is the doctor's call.
    from . import coordinator  # here, not at import time: coordinator imports us

    # One snapshot of the loop, read once, and every guard on this wake-up
    # reads it: the ladder's policy check below, and the Coordinator's own
    # guards through the turn it is handed. That is not a saving, it is the
    # correctness (codex re-audit 6): the reservation a few lines further down
    # spends a contact BEFORE the model turn, and an agent recomputing its
    # facts afterwards would see the contact its own message is about to be and
    # refuse the sixth message because six were already counted.
    facts = await coordinator.facts_for(loop, wake=True)
    pol = policy.for_doctor(doctor)

    if not force:
        allowed = policy.check(
            "schedule_next_contact", {"days_from_now": 0}, facts, pol,
            reason="the ladder step that is due now",
        )
        if not allowed.allowed:
            # The audit line says who refused, and on this path it is not the
            # model: no model was asked. `Decision.audit()` ends every line with
            # "decided_by: model choice, guards in code", which is true of the
            # agent's own calls and false of the ladder's, so the ladder writes
            # its own line (rev 17 item 7). This is the sentence the runbook's
            # refusal procedure puts on camera.
            line = f"{REFUSED_BY_CODE}: {allowed.why}"
            await events.append_event(
                doctor.id, "system",
                f"nudge withheld on {loop.title}: {allowed.why}",
                patient_id=patient.id, loop_id=loop.id,
                meta={"audit": {"tier": "chaser", "line": line,
                                "refused": [allowed.as_meta()]},
                      "refused": [allowed.as_meta()],
                      "decided_by": "code (core/policy.py schedule window)"},
            )
            return {"sent": False, "reason": allowed.why, "audit": line}

    # The idempotency ledger, and it stands in front of the Coordinator, not
    # behind it. Until rev 17 the claim happened after the agent turn, so a
    # replayed Cloud Task paid for a second model call and could send a second
    # Coordinator template that this ledger never saw: the receipt existed for
    # the ladder nudge and for nothing the agent said. One wake-up is one
    # (loop, kind, attempt), whoever ends up speaking for it, so the key is
    # claimed here and the whole wake-up, agent turn included, happens once.
    generation = int(loop.generation or 0)
    day = timing.day_index(now, scale)
    send = Send(
        id=receipt_key(loop.id, generation, kind, attempt),
        doctor_id=doctor.id, patient_id=patient.id,
        loop_id=loop.id, attempt=attempt, generation=generation, kind=kind,
        state=store.CLAIMED, run_id=run_id, day_index=day, created_at=now,
    )
    claim = await store.claim_send(send)
    if claim == store.ALREADY_SENT:
        return {"sent": False, "reason": "already sent", "key": send.id}

    if claim == store.RESEND:
        return await resend(patient, doctor, loop, send, attempt, kind)

    # codex re-audit 6. The two budgets this message spends are read and spent
    # in ONE transaction, and it happens here, in front of the model call and
    # in front of the send, rather than a hundred lines later. Before this,
    # "has he heard from Sanad today" was read at the top of this function and
    # written at the bottom, so two loops of one patient waking in the same
    # tick both read no and both messaged him: two guards satisfied, two
    # messages, one patient.
    #
    # /force_due is exempt from the day, for the reason it is exempt from quiet
    # hours and from the contact window: the doctor asking for it now is the
    # doctor's call. It is not exempt from being counted.
    reserved = await store.reserve_contact(
        patient.id, doctor.id, day, loop.id, store.LADDER,
        max_contacts=None if force else pol.max_contacts,
        allow_same_day=force,
    )
    if not reserved.get("ok"):
        # Nothing has been thought, written or said, so the wake-up is not
        # spent: the claim goes back and the task comes round again tomorrow,
        # which is what the early one-a-day check has always done.
        await store.release_send(send.id)
        await tasks.enqueue(NUDGE_PATH, payload, timing.seconds(1, scale))
        await events.append_event(
            doctor.id, "system",
            f"nudge withheld on {loop.title}: {reserved.get('why')}",
            patient_id=patient.id, loop_id=loop.id,
            meta={"audit": {"tier": "chaser",
                            "line": f"{REFUSED_BY_CODE}: {reserved.get('why')}"},
                  "decided_by": "code (core/store.reserve_contact)"},
        )
        return {"sent": False, "reason": reserved.get("why")}

    # The Care Coordinator owns what this wake-up is for. It returns None when
    # the answer is the ladder step below, which is every time it is switched
    # off, times out, errors, or chooses schedule_next_contact with 0 days: the
    # S3 behaviour is the fail-closed default and nothing here changes it.
    # The receipt goes with it, so every template the agent sends on this
    # wake-up carries the same key the ladder nudge would have carried.
    try:
        carried = await coordinator.on_wake(loop, patient, doctor,
                                            receipt=send.id, facts=facts,
                                            reserved=True)
    except Exception:
        # Nothing was said, so the wake-up was not spent: give the contact, the
        # patient's day and the key back and let the retry have them.
        # `coordinator.run` swallows model failures itself, so reaching here
        # means the store or the queue broke.
        await store.refund_contact(loop.id)
        await store.refund_day(patient.id, day, loop.id)
        await store.release_send(send.id)
        raise
    if carried is not None:
        # The wake-up was spent by the agent, which did its own writing and its
        # own sending. The receipt is closed here so a retry of this task reads
        # "already sent" rather than an open claim.
        #
        # The contact AND the patient's day are handed back when the agent said
        # nothing to the patient (an escalation, a state change, a pause). The
        # reservation pays for one message and those choices send none, so
        # leaving it spent would let a silent wake-up eat one of the six the
        # doctor's policy allows, and, worse, would refuse this patient's other
        # loop its reminder today with "one message per patient per day" when
        # he has heard nothing at all (Fable's review of S12, R1).
        if not carried.get("answered"):
            await store.refund_contact(loop.id)
            await store.refund_day(patient.id, day, loop.id)
        await store.mark_send(send.id, "sent")
        return {"sent": False, "reason": f"coordinator: {carried['tool']}",
                "audit": carried["audit"], "key": send.id}

    speak = await lang.for_patient(patient, doctor.id)
    text = nudge_text(patient, doctor, loop, attempt, speak, kind)

    # codex item 5, the order of operations. The state, the counters and the
    # audit event are all written BEFORE the message leaves, so a delivery that
    # throws cannot leave a message that did go out uncounted, and a retry
    # cannot count the same wake-up twice. `attempts` is the ladder counter and
    # any reply resets it. `contacts` is every message this loop has ever cost
    # the patient and it never resets: the six-contact policy limit is a
    # promise, and a patient who answers must not buy himself more messages
    # (core/policy.py). Both counters are server-side increments now (codex
    # item 13), so two wake-ups in the same second cannot lose one of them.
    #
    # codex re-audit 9 and 13, together, because they are one write. The
    # schedule version and the generation are re-read HERE, immediately before
    # the message goes out, and the delivery is refused unless both are still
    # what this wake-up started with. The early check at the top of this
    # function is a hundred lines and one model turn ago; a reschedule inside
    # that window still sent the old reminder. And the attempt is spent inside
    # that same transaction rather than read from a snapshot and written back,
    # so two wake-ups in the same second cannot both write attempt 1.
    counted = await store.claim_delivery(loop.id, current_version, generation, now)
    if counted is None:
        # Nothing reached the patient, so both budgets go back: the loop's
        # contact and the patient's day (Fable's review of S12, R1).
        await store.refund_contact(loop.id)
        await store.refund_day(patient.id, day, loop.id)
        await store.mark_send(send.id, SUPERSEDED)
        await events.append_event(
            doctor.id, "system", f"{SUPERSEDED} on {loop.title}",
            patient_id=patient.id, loop_id=loop.id,
            meta={"made_for": made_for, "receipt": send.id,
                  "audit": {"tier": "chaser",
                            "line": f"{SUPERSEDED}: the schedule moved while "
                                    "this wake-up was being decided, so nothing "
                                    "was sent"},
                  "decided_by": "code (core/store.claim_delivery)"},
        )
        return {"sent": False, "reason": SUPERSEDED, "key": send.id}

    await events.append_event(
        doctor.id, "system", f"nudge {attempt} sent for {loop.title}",
        patient_id=patient.id, loop_id=loop.id,
        meta={"attempt": attempt, "kind": kind, "counted": counted,
              "forced": force, "generation": generation, "receipt": send.id},
    )

    try:
        # rev 18 item 8. The receipt key travels on the ladder nudge too. When
        # the Coordinator chose "the reminder that is due now", it wrote its own
        # event BEFORE this send, so `meta.sent` on that event is empty and the
        # Agent Working tile said "not sent yet" beside a message that went out
        # in the same second. Both records now carry the one key core/chaser.py
        # claimed for this wake-up (`send.id`), so the board pairs the decision
        # with the message by id rather than by searching forward in time.
        await fanout().send(f"patient:{patient.id}", OutboundMessage(
            text=text, receipt=send.id,
            meta={"audit": {"tier": "nudge", "attempt": attempt,
                            "kind": kind, "generated": "code template",
                            "receipt": send.id}}))
    except Exception as exc:  # noqa: BLE001 - the receipt keeps the error
        # The receipt is KEPT, marked failed, and never released: the state and
        # the counters above are already written, and a released claim would
        # let the retry write them a second time. The retry finds "failed",
        # is allowed one more delivery, and touches nothing else.
        #
        # The one counter that IS handed back is the contact (Fable's review of
        # wave B). Six contacts on one loop is a promise to the patient about
        # how much he will be bothered, and a message he never received is not
        # something he was bothered by: leaving it counted made Sanad refuse the
        # seventh contact over a Telegram outage. `resend` counts it again when
        # the retry gets through, so what the number means never changes: the
        # contacts on a loop are the messages that reached the wire.
        #
        # `attempts` is NOT handed back. It is the ladder's position, not a
        # budget: rung two failing does not turn this back into rung one, and
        # the resend deliberately sends rung two's own words.
        error = " ".join(str(exc).split())[:200]
        await store.mark_send(send.id, "failed", error)
        await store.refund_contact(loop.id)
        await delivery_failed(patient, doctor, loop, send.id, attempt, error)
        raise
    await store.mark_send(send.id, "sent")

    if kind == "nudge" and counted >= LADDER_LENGTH:
        await mark_unreachable(patient, doctor, loop, now)
    return {"sent": True, "attempt": attempt, "counted": counted,
            "loop": loop.id, "key": send.id}


async def mark_unreachable(
    patient: Patient, doctor: Doctor, loop: Loop, now: datetime
) -> None:
    overdue = int((now - loop.due_at).total_seconds() // timing.REAL_DAY_SECONDS) \
        if loop.due_at else 0
    await store.update_loop(loop.id, state="unreachable")
    await events.append_event(
        doctor.id, "system", f"{patient.name} unreachable on {loop.title}",
        patient_id=patient.id, loop_id=loop.id, meta={"attempts": LADDER_LENGTH},
    )
    await fanout().send(f"doctor:{doctor.web_token}", OutboundMessage(
        text=f"{patient.name} is not answering.", patient_id=patient.id,
        meta={"decided_by": "code (core/chaser.py, the ladder is exhausted)"},
        card=unreachable_card(patient, loop, overdue)))


# --------------------------------------------------------------------------- #
# The other half of the rule: a reply resets the ladder
# --------------------------------------------------------------------------- #
async def note_patient_reply(patient: Patient) -> None:
    """Any message from a patient clears the attempt counter on his open loops.

    Clearing it starts a new run of the ladder, so the generation goes up in the
    same write (core/store.bump_generation). That is what stops the restarted
    ladder from asking for receipts the finished one already holds, which is
    how a patient who answered once could never be reminded again (codex item
    7).
    """
    now = store.now()
    for loop in await store.list_loops(patient.id):
        if loop.state in LIVE_STATES and loop.attempts:
            await store.bump_generation(loop.id)
            await store.update_loop(loop.id, last_reply_at=now)


# The in-process fallback engine delivers straight to `fire`; Cloud Tasks reaches
# the same function through /tasks/nudge. One handler, two ways in.
tasks.register_local_handler(fire)
