"""Owns the doctor's policy and every guard the Care Coordinator has to pass.

The Coordinator is a model that chooses one tool. This file is the code that
decides whether that choice is allowed to happen, and it is the whole reason an
agent can be given tools at all: the model picks, `check()` rules, and only then
does anything leave the process.

The rules, all of them from the doctor's own policy record (defaults below):

  schedule_next_contact    not before tomorrow, not after the loop's due date
                           plus the grace window, one contact per day, quiet
                           hours moved rather than sent into, and never more
                           than six contacts on one loop: the seventh is
                           refused. A wake-up is the one exception to "not
                           before tomorrow": the reminder that is due now is
                           the ladder step S3 already owns, and refusing it
                           would mean a scheduled wake-up sent nothing.
  request_missing_evidence twice per loop, then it stops asking, and exactly
                           one analyte per request (core/labs.named_analytes):
                           the sentence it produces is about one missing part.
  classify_barrier         one of BARRIERS, never a free-text label.
  escalate_barrier         always allowed, and it always produces a doctor card.
  mark_evidence_received   only when an extractor result or a typed reading is
                           actually on the loop.
  close_verified_loop      only after the doctor's own review flag is set. The
                           two-state gate is untouched: an agent can never be
                           the second state.
  pause_loop               only with a barrier recorded.

There is no tool for cancelling an escalation, changing a dose or editing the
plan text, so those are not refused here: they do not exist. TOOLS below is the
whole surface, and a test asserts it.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import labs, timing

# The fixed tool list. Nothing outside it is a tool, and the Coordinator is
# built from this tuple, so the two can never drift apart.
TOOLS: tuple[str, ...] = (
    "schedule_next_contact",
    "request_missing_evidence",
    "classify_barrier",
    "escalate_barrier",
    "mark_evidence_received",
    "close_verified_loop",
    "pause_loop",
)

# The barrier classes, and there are no others. "unclear" is what an unreadable
# reason becomes; it is a class, not a failure, and it still reaches the doctor.
BARRIERS: tuple[str, ...] = (
    "cost", "availability", "transport", "forgot", "refuses", "unclear",
    "in_hospital", "asymptomatic",
)
# A barrier Sanad is not allowed to discuss with the patient. It is recorded,
# the reminders stop, the doctor gets a card, and the patient is told only that
# the doctor has been told. Cost is on this list by default because what to do
# about the price of a test is the doctor's call, not an assistant's.
ESCALATE_ONLY: tuple[str, ...] = ("cost",)

# The pre-approved reason line, item I. An empty policy field means "use the
# gendered template in the patient's own language" (core/templates.py); a
# doctor who dictates his own line gets his words sent as his.
DEFAULT_FOLLOWUP_REASON = ""


@dataclass(frozen=True)
class Policy:
    """One doctor's rules. Defaults are what the demo runs on."""

    earliest_days: int = 1        # not before tomorrow
    grace_days: int = 7           # not after the due date plus this
    max_contacts: int = 6         # per loop, ever; the seventh is refused
    max_per_day: int = 1          # contacts per loop per day
    quiet_from: int = timing.QUIET_FROM_HOUR
    quiet_until: int = timing.QUIET_UNTIL_HOUR
    max_evidence_requests: int = 2
    cost_escalate_only: bool = True
    followup_reason: str = DEFAULT_FOLLOWUP_REASON

    def escalate_only(self) -> tuple[str, ...]:
        return ESCALATE_ONLY if self.cost_escalate_only else ()

    def as_meta(self) -> dict[str, Any]:
        return {
            "earliest_days": self.earliest_days, "grace_days": self.grace_days,
            "max_contacts": self.max_contacts, "max_per_day": self.max_per_day,
            "quiet_hours": f"{self.quiet_from:02d}:00 to {self.quiet_until:02d}:00 Cairo",
            "max_evidence_requests": self.max_evidence_requests,
            "cost_escalate_only": self.cost_escalate_only,
        }


DEFAULT = Policy()

_INTS = ("earliest_days", "grace_days", "max_contacts", "max_per_day",
         "quiet_from", "quiet_until", "max_evidence_requests")


def parse(raw: Optional[dict[str, Any]]) -> Policy:
    """A stored policy record -> a Policy. Anything unreadable takes the default.

    A doctor's settings row is data that arrived from outside, so every field is
    read defensively: a missing key, a string where a number belongs, a negative
    contact cap. None of those may become a guard that does not guard.
    """
    policy = DEFAULT
    if not isinstance(raw, dict):
        return policy
    changes: dict[str, Any] = {}
    for name in _INTS:
        if name not in raw:
            continue
        try:
            value = int(raw[name])
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        changes[name] = value
    if "cost_escalate_only" in raw:
        changes["cost_escalate_only"] = bool(raw["cost_escalate_only"])
    if isinstance(raw.get("followup_reason"), str):
        changes["followup_reason"] = raw["followup_reason"].strip()
    policy = replace(policy, **changes)
    # A cap of zero contacts would be a loop nobody may ever be reminded about,
    # and a quiet window outside the clock is not a window. Both fall back.
    if policy.max_contacts < 1 or policy.max_per_day < 1:
        policy = replace(policy, max_contacts=DEFAULT.max_contacts,
                         max_per_day=DEFAULT.max_per_day)
    if not (0 <= policy.quiet_from <= 23 and 0 <= policy.quiet_until <= 23):
        policy = replace(policy, quiet_from=DEFAULT.quiet_from,
                         quiet_until=DEFAULT.quiet_until)
    return policy


def for_doctor(doctor: object) -> Policy:
    """The policy on a doctor record, or the defaults when it carries none."""
    return parse(getattr(doctor, "policy", None))


# --------------------------------------------------------------------------- #
# What a guard is allowed to know
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoopFacts:
    """Everything `check()` reads. Facts only: no records, no I/O, no model.

    Built by core/coordinator.py from the loop, the clock and the settings, so
    every guard below can be tested with nothing installed.
    """

    now: datetime
    time_scale: int = timing.REAL_DAY_SECONDS
    wake: bool = False            # this is a scheduled wake-up, not a reply
    state: str = "open"
    due_at: Optional[datetime] = None
    contacts: int = 0
    contact_days: tuple[int, ...] = ()
    evidence_requests: int = 0
    has_evidence: bool = False
    # What core/verify.py said about the slip on this loop, if it saw one.
    # True: all three checks passed. False: the values attached but the
    # obligation was not closed (an unprinted name, an unreadable date, a
    # missing analyte). None: the verifier never saw this loop at all, which is
    # every typed reading and every monitoring loop, and the guards below then
    # behave exactly as they did before S11 because there is nothing for them
    # to contradict.
    verified_satisfies: Optional[bool] = None
    doctor_reviewed: bool = False
    barrier: str = ""
    reluctance: int = 0
    paused: bool = False


@dataclass
class Decision:
    """What code decided about one tool call, and why, in words a card prints."""

    tool: str
    allowed: bool
    why: str = ""                  # the refusal, when there is one
    reason: str = ""               # the model's own stated reason
    args: dict[str, Any] = field(default_factory=dict)
    when: Optional[datetime] = None  # schedule_next_contact only
    notes: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        return not self.allowed

    def audit(self) -> str:
        """The audit line the event carries and the card prints."""
        head = f"coordinator: {self.tool} {'accepted' if self.allowed else 'refused'}"
        bits = [head]
        if self.reason:
            bits.append(f"reason: {self.reason}")
        if self.why:
            bits.append(f"guard: {self.why}")
        for note in self.notes:
            bits.append(note)
        bits.append("decided_by: model choice, guards in code (core/policy.py)")
        return " · ".join(bits)

    def as_meta(self) -> dict[str, Any]:
        return {
            "tool": self.tool, "allowed": self.allowed, "guard": self.why,
            "reason": self.reason, "args": self.args,
            "when": self.when.isoformat() if self.when else None,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Quiet hours, this file's own window
# --------------------------------------------------------------------------- #
# core/timing.py owns the shared default window (22:00 to 09:00 Cairo). A
# doctor's stored policy may narrow it further; the Chaser still applies the
# shared window at send time, so a stored value can never widen that floor.
def in_quiet_hours(when: datetime, policy: Policy = DEFAULT,
                   time_scale: int = timing.REAL_DAY_SECONDS) -> bool:
    """True when a contact at this moment would land inside the quiet window.

    Always False on a compressed clock, for the reason core/timing.py gives: a
    day that lasts three seconds has no wall clock to be quiet in.
    """
    if time_scale != timing.REAL_DAY_SECONDS:
        return False
    hour = when.astimezone(timing.CAIRO).hour
    if policy.quiet_from == policy.quiet_until:
        return False
    if policy.quiet_from > policy.quiet_until:      # the window crosses midnight
        return hour >= policy.quiet_from or hour < policy.quiet_until
    return policy.quiet_from <= hour < policy.quiet_until


def out_of_quiet_hours(when: datetime, policy: Policy = DEFAULT,
                       time_scale: int = timing.REAL_DAY_SECONDS) -> datetime:
    """The first moment at or after `when` that is not inside the quiet window."""
    if not in_quiet_hours(when, policy, time_scale):
        return when
    local = when.astimezone(timing.CAIRO)
    target = local.replace(hour=policy.quiet_until, minute=0, second=0, microsecond=0)
    if local.hour >= policy.quiet_until:
        target += timedelta(days=1)
    return target.astimezone(when.tzinfo or timezone.utc)


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #
def one_sentence(reason: str) -> str:
    """The model's stated reason, tidied once, here, where it enters the system.

    rev 18 item a. Every place that prints a reason puts a full stop after it
    ("Coordinator's reason: {reason}.", and the board's own row), and the model
    writes reasons that already end in one, so the board read "refuses to do the
    lipid panel.." live. Trimming it in the page would have fixed the page and
    left the card, the event and the audit line saying it twice, so it is
    trimmed at the source: a Decision's reason never ends in a full stop, and
    every printer adds exactly one.

    Whitespace is collapsed and the length is capped at 200 characters, which is
    what this line has always done: a reason is one sentence for a doctor to
    read, not a paragraph a model wrote to itself.
    """
    return " ".join((reason or "").split())[:200].rstrip(" .")


UNKNOWN_TOOL = "no such tool"
ONE_ACTION = "one action per wake-up"
ONE_ANALYTE = "one analyte per request"


# The verifier decides whether a slip closes an obligation, and no tool call
# talks it out of that. Wave A made `verify.satisfies` strict; this is the guard
# that stops the Coordinator reaching the same end state by a model vote
# (kernel review F8a). The wording is what the doctor's card prints, so it says
# what the agent should have done instead.
UNVERIFIED = (
    "the verifier did not accept this slip (identity or date not printed, or "
    "an analyte missing), so this cannot be marked received: escalate_barrier "
    "and let the doctor decide"
)


def check(tool: str, args: dict[str, Any], facts: LoopFacts,
          policy: Policy = DEFAULT, *, reason: str = "") -> Decision:
    """One proposed tool call -> allowed or refused, with the reason in words."""
    args = dict(args or {})
    reason = one_sentence(reason)
    make = lambda allowed, why="", **extra: Decision(  # noqa: E731 - one shape
        tool=tool, allowed=allowed, why=why, reason=reason, args=args, **extra
    )

    if tool not in TOOLS:
        return make(False, UNKNOWN_TOOL)

    if tool == "schedule_next_contact":
        return _schedule(args, facts, policy, make)

    if tool == "request_missing_evidence":
        analyte = str(args.get("analyte") or "").strip()
        if not analyte:
            return make(False, "name the missing analyte")
        # Exactly one. The template that carries this field is one sentence
        # about one missing part, in three genders and two languages, and a
        # list in that slot does not agree in either of them.
        named = labs.named_analytes(analyte)
        if len(named) != 1:
            return make(False, ONE_ANALYTE)
        args["analyte"] = named[0]
        if facts.evidence_requests >= policy.max_evidence_requests:
            return make(
                False,
                f"already asked for the missing evidence "
                f"{policy.max_evidence_requests} times on this loop",
            )
        return make(True)

    if tool == "classify_barrier":
        barrier = str(args.get("barrier") or "").strip().lower()
        if barrier not in BARRIERS:
            return make(False, f"{barrier or 'empty'} is not a barrier class")
        notes = ()
        if barrier in policy.escalate_only():
            notes = (f"{barrier} is escalate-only: the doctor is told, the "
                     "patient is not advised",)
        return make(True, notes=notes)

    if tool == "escalate_barrier":
        # Always allowed, by design. An unreadable class becomes "unclear"
        # rather than a refusal: the doctor still has to see it.
        barrier = str(args.get("barrier") or "").strip().lower()
        if barrier not in BARRIERS:
            args["barrier"] = "unclear"
        return make(True)

    if tool == "mark_evidence_received":
        if not facts.has_evidence:
            return make(False, "no extractor result and no typed reading on this loop")
        if facts.verified_satisfies is False:
            return make(False, UNVERIFIED)
        return make(True)

    if tool == "close_verified_loop":
        if not facts.doctor_reviewed:
            return make(False, "the doctor has not reviewed this result yet")
        if not facts.has_evidence:
            return make(False, "there is no evidence on this loop to close it on")
        if facts.verified_satisfies is False:
            return make(False, UNVERIFIED)
        return make(True)

    if tool == "pause_loop":
        if not facts.barrier:
            return make(False, "no barrier is recorded, so there is nothing to pause on")
        return make(True)

    return make(False, UNKNOWN_TOOL)  # unreachable, kept as the closed default


def _schedule(args: dict[str, Any], facts: LoopFacts, policy: Policy,
              make) -> Decision:
    """The schedule window, in the order a refusal is worth reading in."""
    try:
        days = int(args.get("days_from_now"))
    except (TypeError, ValueError):
        return make(False, "days_from_now must be a whole number of days")

    if facts.contacts >= policy.max_contacts:
        return make(
            False,
            f"{facts.contacts} contacts already on this loop and the policy "
            f"limit is {policy.max_contacts}",
        )
    if facts.paused:
        return make(False, "this loop is paused on a barrier the doctor has")

    # A scheduled wake-up is the reminder that is due now; refusing it would
    # mean a task fired and nothing happened. Every other trigger has to wait
    # until tomorrow, which is the doctor's window.
    if days == 0 and not facts.wake:
        return make(False, "not before tomorrow")
    if days < 0 or (days < policy.earliest_days and not facts.wake):
        return make(False, "not before tomorrow")

    when = facts.now + timedelta(seconds=timing.seconds(days, facts.time_scale))
    notes: list[str] = []
    moved = out_of_quiet_hours(when, policy, facts.time_scale)
    if moved != when:
        notes.append(
            f"moved out of quiet hours ({policy.quiet_from:02d}:00 to "
            f"{policy.quiet_until:02d}:00 Cairo)"
        )
        when = moved

    if facts.due_at is not None:
        latest = facts.due_at + timedelta(
            seconds=timing.seconds(policy.grace_days, facts.time_scale)
        )
        if when > latest:
            return make(
                False,
                f"past the due date plus {policy.grace_days} days, which is the "
                "end of the doctor's window",
            )

    day = timing.day_index(when, facts.time_scale)
    if facts.contact_days.count(day) >= policy.max_per_day:
        return make(False, "this patient already hears from Sanad that day")

    return make(True, when=when, notes=tuple(notes))
