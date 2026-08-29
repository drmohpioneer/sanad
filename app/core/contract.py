"""Owns the Care Contract: the loop and the doctor's policy, said out loud.

Nothing new is stored. A contract is the object the doctor and the judge see:
one open loop, plus the policy the Coordinator is bound by, rendered as the six
things that make it a contract rather than a reminder.

  objective    what, for whom, by when
  evidence     which analytes or readings count, and the three conditions a
               slip has to meet (core/verify.py)
  actions      the permitted tool list, and nothing outside it
  safety       one fixed sentence, the same on every contract
  deadline     the due date and the end of the doctor's window
  escalation   the conditions that put it in front of the doctor

It is used in two places: the per-patient view on the console, and the confirm
card the doctor taps at intake. Both read the same function, so what he confirms
and what he sees later cannot drift apart.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from . import labs, policy as policy_module, verify

# The one sentence every contract carries, word for word, on every screen.
SAFETY_SENTENCE = (
    "Sanad does not diagnose and does not change treatment. The critical-value "
    "table decides what is an emergency. The doctor owns every clinical "
    "decision, and only the doctor closes a result."
)

# What puts a contract in front of the doctor. These are conditions in code,
# every one of them: a table, a gate, a verifier check or a counter.
ESCALATION_CONDITIONS: tuple[str, ...] = (
    "a critical value on the critical-value table (core/labs.py)",
    "a request to start, stop or change treatment (core/validator.py)",
    "an identity mismatch on the evidence (core/verify.py)",
    "the deadline at risk: the end of the window with no evidence",
    "a barrier that persists, and any cost barrier at once",
)

WHAT_FOR_TYPE = {
    "TEST": "a laboratory result",
    "MONITOR": "readings the patient measures",
    "MEDICATION": "a medication the doctor started, stopped or changed",
    "VISIT": "a return visit",
    "TASK": "something the doctor asked the patient to do",
}


# What the obligation is, said as the sentence its type deserves. A medication
# is not "obtained" and a visit is not "collected".
OBJECTIVE_FOR: dict[str, str] = {
    "TEST": "Obtain {what} for {who}{when}.",
    "MONITOR": "Collect {what} from {who}{when}.",
    "MEDICATION": "Carry the medication the doctor changed, {what}, for {who}{when}.",
    "VISIT": "Bring {who} back for {what}{when}.",
    "TASK": "See that {who} does what the doctor asked, {what}{when}.",
}


def objective(loop: Any, patient_name: str) -> str:
    """What, for whom, by when, in one line."""
    details = getattr(loop, "details", None) or {}
    kind = str(getattr(loop, "type", "TASK"))
    what = str(details.get("test_name") or details.get("metric")
               or details.get("drug") or details.get("text")
               or getattr(loop, "title", "") or "the follow-up")
    due = getattr(loop, "due_at", None)
    when = f" by {due:%Y-%m-%d}" if due else ", with no date dictated"
    shape = OBJECTIVE_FOR.get(kind, OBJECTIVE_FOR["TASK"])
    return shape.format(what=what, who=patient_name, when=when)


def evidence_required(loop: Any) -> dict[str, Any]:
    """What counts as evidence for this contract, and what is checked on it."""
    kind = getattr(loop, "type", "TASK")
    required = verify.required_analytes(loop) if kind == "TEST" else ()
    details = getattr(loop, "details", None) or {}
    if kind == "MONITOR":
        wanted = ", ".join(
            str(details.get(key)) for key in ("metric", "schedule", "days")
            if details.get(key)
        )
        return {
            "kind": "readings",
            "wanted": wanted or "readings the patient sends",
            "analytes": [],
            "checks": ["each reading is graded by the blood-pressure table in code"],
        }
    return {
        "kind": WHAT_FOR_TYPE.get(kind, "evidence"),
        "wanted": str(details.get("test_name") or getattr(loop, "title", "")),
        "analytes": [labs.display(a) for a in required],
        "checks": [
            "the name printed on the slip matches the record",
            "the collection date is on or after the order date",
            "every analyte the doctor asked for is present",
        ],
    }


def deadline(loop: Any, pol: policy_module.Policy) -> dict[str, Any]:
    """The due date, and the last day the Coordinator may still contact anyone."""
    due = getattr(loop, "due_at", None)
    latest = due + timedelta(days=pol.grace_days) if due else None
    return {
        "due_at": due.isoformat() if due else "",
        "window_ends": latest.isoformat() if latest else "",
        "in_words": (f"due {due:%Y-%m-%d}, the window closes {latest:%Y-%m-%d}"
                     if due else "no due date was dictated"),
    }


def state_of(loop: Any) -> dict[str, Any]:
    """Where this contract stands, in the numbers the guards actually read."""
    return {
        "state": getattr(loop, "state", "open"),
        "contacts": int(getattr(loop, "contacts", 0) or 0),
        "evidence_requests": int(getattr(loop, "evidence_requests", 0) or 0),
        "barrier": getattr(loop, "barrier", "") or "",
        "paused": bool(getattr(loop, "paused", False)),
        "doctor_reviewed": bool(getattr(loop, "doctor_reviewed", False)),
        "verified": dict(getattr(loop, "verified", None) or {}),
    }


def render(loop: Any, pol: policy_module.Policy, doctor_name: str,
           patient_name: str) -> dict[str, Any]:
    """One open loop plus one policy -> the contract shape, as plain data."""
    return {
        "loop_id": getattr(loop, "id", ""),
        "type": getattr(loop, "type", ""),
        "title": getattr(loop, "title", ""),
        "objective": objective(loop, patient_name),
        "for_patient": patient_name,
        "doctor": doctor_name,
        "evidence": evidence_required(loop),
        "permitted_actions": list(policy_module.TOOLS),
        "safety": SAFETY_SENTENCE,
        "deadline": deadline(loop, pol),
        "escalation_conditions": list(ESCALATION_CONDITIONS),
        "policy": pol.as_meta(),
        "state": state_of(loop),
    }


def lines(contract: dict[str, Any]) -> list[str]:
    """The contract as the lines a card prints. No dashes, one idea a line."""
    evidence = contract["evidence"]
    wanted = ", ".join(evidence["analytes"]) or evidence["wanted"] or "evidence"
    out = [
        contract["objective"],
        f"Evidence required: {wanted}.",
        "Checked in code: " + "; ".join(evidence["checks"]) + ".",
        "Permitted actions: " + ", ".join(contract["permitted_actions"]) + ".",
        f"Deadline: {contract['deadline']['in_words']}.",
        "Escalates on: " + "; ".join(contract["escalation_conditions"]) + ".",
        contract["safety"],
    ]
    return out


def for_confirm(loop: Any, pol: policy_module.Policy, doctor_name: str,
                patient_name: str) -> list[str]:
    """The three lines a confirm card carries per proposed loop."""
    contract = render(loop, pol, doctor_name, patient_name)
    evidence = contract["evidence"]
    wanted = ", ".join(evidence["analytes"]) or evidence["wanted"] or "evidence"
    return [
        contract["objective"],
        f"Evidence: {wanted}. Deadline: {contract['deadline']['in_words']}.",
        SAFETY_SENTENCE,
    ]
