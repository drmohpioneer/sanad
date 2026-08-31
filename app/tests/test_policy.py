"""Every guard the Care Coordinator has to pass, one test each.

The Coordinator is a model with tools. What makes that safe is not the prompt:
it is core/policy.check, which reads the doctor's policy and the loop's own
numbers and answers allowed or refused before anything is written or sent. If
one of these breaks, the image does not build (see the Dockerfile), which is the
point of putting them here rather than in a document.

No model, no database, no network: these run anywhere.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from core import policy, timing

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(days=5)


def facts(**changes) -> policy.LoopFacts:
    base = {"now": NOW, "due_at": DUE, "state": "waiting_patient"}
    base.update(changes)
    return policy.LoopFacts(**base)


class TheToolList(unittest.TestCase):
    def test_there_are_exactly_seven_tools_and_these_are_they(self) -> None:
        self.assertEqual(policy.TOOLS, (
            "schedule_next_contact", "request_missing_evidence",
            "classify_barrier", "escalate_barrier", "mark_evidence_received",
            "close_verified_loop", "pause_loop",
        ))

    def test_the_agent_has_no_tool_for_the_things_it_may_not_do(self) -> None:
        """It cannot cancel an escalation, change a dose or edit the plan.

        Those are not refusals, they are absences: there is nothing to call.
        """
        forbidden = ("cancel_escalation", "change_dose", "edit_plan",
                     "send_message", "close_loop", "write_plan")
        for name in forbidden:
            with self.subTest(name=name):
                self.assertNotIn(name, policy.TOOLS)
                self.assertFalse(policy.check(name, {}, facts()).allowed)

    def test_an_unknown_tool_is_refused_and_says_so(self) -> None:
        decision = policy.check("do_whatever", {}, facts())
        self.assertTrue(decision.refused)
        self.assertEqual(decision.why, policy.UNKNOWN_TOOL)


class TheScheduleWindow(unittest.TestCase):
    def test_a_reply_may_not_schedule_anything_before_tomorrow(self) -> None:
        for days in (0, -1, -30):
            with self.subTest(days=days):
                decision = policy.check(
                    "schedule_next_contact", {"days_from_now": days}, facts()
                )
                self.assertTrue(decision.refused)
                self.assertEqual(decision.why, "not before tomorrow")

    def test_a_wake_up_may_send_the_reminder_that_is_due_now(self) -> None:
        """Otherwise a scheduled task would fire and nothing would happen.

        Zero days is the ladder step the chaser already owns, and it is allowed
        only on a wake-up. This test is the public acceptance evidence.
        """
        decision = policy.check(
            "schedule_next_contact", {"days_from_now": 0}, facts(wake=True)
        )
        self.assertTrue(decision.allowed)

    def test_tomorrow_is_allowed(self) -> None:
        self.assertTrue(policy.check(
            "schedule_next_contact", {"days_from_now": 1}, facts()).allowed)

    def test_nothing_may_be_scheduled_past_the_due_date_plus_seven(self) -> None:
        ok = policy.check("schedule_next_contact", {"days_from_now": 12}, facts())
        late = policy.check("schedule_next_contact", {"days_from_now": 13}, facts())
        self.assertTrue(ok.allowed)
        self.assertTrue(late.refused)
        self.assertIn("due date plus 7 days", late.why)

    def test_a_loop_with_no_due_date_has_no_upper_edge(self) -> None:
        decision = policy.check(
            "schedule_next_contact", {"days_from_now": 40}, facts(due_at=None)
        )
        self.assertTrue(decision.allowed)

    def test_one_contact_a_day(self) -> None:
        tomorrow = timing.day_index(NOW + timedelta(days=1))
        decision = policy.check(
            "schedule_next_contact", {"days_from_now": 1},
            facts(contact_days=(tomorrow,)),
        )
        self.assertTrue(decision.refused)
        self.assertIn("already hears from Sanad that day", decision.why)

    def test_the_seventh_contact_is_rejected(self) -> None:
        """Six per loop, ever. `contacts` never resets, which is why it is not
        the Chaser's `attempts` counter."""
        for used in range(0, 6):
            with self.subTest(used=used):
                self.assertTrue(policy.check(
                    "schedule_next_contact", {"days_from_now": 1},
                    facts(contacts=used)).allowed)
        seventh = policy.check(
            "schedule_next_contact", {"days_from_now": 1}, facts(contacts=6))
        self.assertTrue(seventh.refused)
        self.assertIn("policy limit is 6", seventh.why)

    def test_a_paused_loop_schedules_nothing(self) -> None:
        decision = policy.check(
            "schedule_next_contact", {"days_from_now": 1},
            facts(paused=True, barrier="cost"),
        )
        self.assertTrue(decision.refused)

    def test_days_from_now_has_to_be_a_number(self) -> None:
        for bad in ("soon", None, "", 3.7):
            with self.subTest(bad=bad):
                decision = policy.check(
                    "schedule_next_contact", {"days_from_now": bad}, facts())
                if bad == 3.7:  # a float is read as the whole days it names
                    self.assertTrue(decision.allowed)
                else:
                    self.assertTrue(decision.refused)


class QuietHours(unittest.TestCase):
    """22:00 to 09:00 Cairo, the single patient quiet window.

    core/timing.py owns the Chaser's own send-time window (22:00 to 09:00) and
    is untouched. This one is inside it, so nothing here can widen what the
    Chaser already refuses at send time.
    """

    def evening(self, hour: int) -> datetime:
        return datetime(2026, 8, 29, hour, 0, tzinfo=timing.CAIRO)

    def test_the_window_is_the_policy_window(self) -> None:
        self.assertTrue(policy.in_quiet_hours(self.evening(23)))
        self.assertTrue(policy.in_quiet_hours(self.evening(3)))
        self.assertTrue(policy.in_quiet_hours(self.evening(22)))
        self.assertTrue(policy.in_quiet_hours(self.evening(8)))
        self.assertFalse(policy.in_quiet_hours(self.evening(9)))
        self.assertFalse(policy.in_quiet_hours(self.evening(21)))

    def test_a_contact_inside_it_is_moved_to_the_morning_not_sent(self) -> None:
        moved = policy.out_of_quiet_hours(self.evening(23))
        self.assertEqual(moved.astimezone(timing.CAIRO).hour, 9)
        self.assertEqual(moved.astimezone(timing.CAIRO).day, 30)
        self.assertFalse(policy.in_quiet_hours(moved))

    def test_a_scheduled_contact_never_lands_inside_the_window(self) -> None:
        late = policy.LoopFacts(now=self.evening(23), due_at=None)
        decision = policy.check(
            "schedule_next_contact", {"days_from_now": 1}, late)
        self.assertTrue(decision.allowed)
        self.assertFalse(policy.in_quiet_hours(decision.when))
        self.assertTrue(any("quiet hours" in note for note in decision.notes))

    def test_a_compressed_clock_has_no_quiet_hours(self) -> None:
        """The same rule core/timing.py states: a three-second day has no
        wall clock to be quiet in, and the rehearsal is honest about it."""
        self.assertFalse(policy.in_quiet_hours(self.evening(23), time_scale=3))


class TheOtherSixGuards(unittest.TestCase):
    def test_missing_evidence_is_asked_for_twice_and_no_more(self) -> None:
        for used in (0, 1):
            with self.subTest(used=used):
                self.assertTrue(policy.check(
                    "request_missing_evidence", {"analyte": "Potassium"},
                    facts(evidence_requests=used)).allowed)
        third = policy.check("request_missing_evidence", {"analyte": "Potassium"},
                             facts(evidence_requests=2))
        self.assertTrue(third.refused)
        self.assertIn("2 times", third.why)

    def test_missing_evidence_has_to_name_the_analyte(self) -> None:
        self.assertTrue(policy.check(
            "request_missing_evidence", {"analyte": " "}, facts()).refused)

    def test_a_barrier_class_outside_the_list_is_refused(self) -> None:
        for good in policy.BARRIERS:
            with self.subTest(barrier=good):
                self.assertTrue(policy.check(
                    "classify_barrier", {"barrier": good}, facts()).allowed)
        for bad in ("money", "لا يريد", "", "other", "busy"):
            with self.subTest(barrier=bad):
                self.assertTrue(policy.check(
                    "classify_barrier", {"barrier": bad}, facts()).refused)

    def test_the_eight_classes_are_the_ones_the_spec_names(self) -> None:
        self.assertEqual(set(policy.BARRIERS), {
            "cost", "availability", "transport", "forgot", "refuses",
            "unclear", "in_hospital", "asymptomatic"})

    def test_a_cost_barrier_is_escalate_only_by_default(self) -> None:
        decision = policy.check("classify_barrier", {"barrier": "cost"}, facts())
        self.assertTrue(decision.allowed)
        self.assertTrue(any("escalate-only" in n for n in decision.notes))
        self.assertIn("cost", policy.DEFAULT.escalate_only())

    def test_a_doctor_may_turn_that_off_in_his_own_policy(self) -> None:
        pol = policy.parse({"cost_escalate_only": False})
        self.assertEqual(pol.escalate_only(), ())

    def test_escalation_is_always_allowed(self) -> None:
        for barrier in ("cost", "nonsense", "", "unclear"):
            with self.subTest(barrier=barrier):
                self.assertTrue(policy.check(
                    "escalate_barrier", {"barrier": barrier},
                    facts(paused=True, contacts=99)).allowed)

    def test_an_unreadable_escalation_class_becomes_unclear(self) -> None:
        decision = policy.check("escalate_barrier", {"barrier": "nonsense"}, facts())
        self.assertEqual(decision.args["barrier"], "unclear")

    def test_evidence_is_marked_only_when_there_is_some(self) -> None:
        self.assertTrue(policy.check(
            "mark_evidence_received", {}, facts(has_evidence=True)).allowed)
        empty = policy.check("mark_evidence_received", {}, facts())
        self.assertTrue(empty.refused)
        self.assertIn("no extractor result", empty.why)

    def test_a_loop_cannot_be_closed_before_the_doctor_reviewed_it(self) -> None:
        """The two-state gate. An agent can never be the second state."""
        refused = policy.check(
            "close_verified_loop", {}, facts(has_evidence=True))
        self.assertTrue(refused.refused)
        self.assertIn("has not reviewed", refused.why)
        allowed = policy.check(
            "close_verified_loop", {},
            facts(has_evidence=True, doctor_reviewed=True))
        self.assertTrue(allowed.allowed)

    def test_a_reviewed_loop_with_no_evidence_is_still_not_closed(self) -> None:
        self.assertTrue(policy.check(
            "close_verified_loop", {}, facts(doctor_reviewed=True)).refused)

    def test_pausing_needs_a_barrier_on_the_record(self) -> None:
        self.assertTrue(policy.check("pause_loop", {}, facts()).refused)
        self.assertTrue(policy.check(
            "pause_loop", {}, facts(barrier="cost")).allowed)


class TheDoctorsPolicyRecord(unittest.TestCase):
    def test_the_defaults_are_the_ones_the_spec_names(self) -> None:
        pol = policy.DEFAULT
        self.assertEqual(pol.earliest_days, 1)
        self.assertEqual(pol.grace_days, 7)
        self.assertEqual(pol.max_contacts, 6)
        self.assertEqual(pol.max_per_day, 1)
        self.assertEqual((pol.quiet_from, pol.quiet_until), (22, 9))
        self.assertEqual(pol.max_evidence_requests, 2)
        self.assertTrue(pol.cost_escalate_only)

    def test_a_doctor_with_no_policy_gets_the_defaults(self) -> None:
        class Doctor:
            pass

        self.assertEqual(policy.for_doctor(Doctor()), policy.DEFAULT)
        self.assertEqual(policy.parse(None), policy.DEFAULT)
        self.assertEqual(policy.parse({}), policy.DEFAULT)

    def test_his_own_numbers_are_used(self) -> None:
        pol = policy.parse({"max_contacts": 3, "grace_days": 2})
        self.assertEqual((pol.max_contacts, pol.grace_days), (3, 2))
        self.assertTrue(policy.check(
            "schedule_next_contact", {"days_from_now": 1},
            facts(contacts=3), pol).refused)

    def test_a_policy_that_cannot_be_read_does_not_become_a_guard_that_does_not_guard(
            self) -> None:
        pol = policy.parse({"max_contacts": "lots", "grace_days": -3,
                            "quiet_from": 99, "max_per_day": 0})
        self.assertEqual(pol.max_contacts, policy.DEFAULT.max_contacts)
        self.assertEqual(pol.grace_days, policy.DEFAULT.grace_days)
        self.assertEqual(pol.quiet_from, policy.DEFAULT.quiet_from)
        self.assertEqual(pol.max_per_day, policy.DEFAULT.max_per_day)

    def test_the_pre_approved_reason_line_lives_on_the_policy(self) -> None:
        pol = policy.parse({"followup_reason": "  I want this before I change it.  "})
        self.assertEqual(pol.followup_reason, "I want this before I change it.")
        self.assertEqual(policy.DEFAULT.followup_reason, "")


class TheAuditLine(unittest.TestCase):
    def test_every_decision_carries_its_reason_and_who_decided(self) -> None:
        decision = policy.check(
            "classify_barrier", {"barrier": "availability"}, facts(),
            reason="the lab is closed until Sunday",
        )
        line = decision.audit()
        self.assertIn("classify_barrier accepted", line)
        self.assertIn("the lab is closed until Sunday", line)
        self.assertIn("guards in code", line)
        self.assertNotIn("—", line)

    def test_a_refusal_prints_the_guard_that_refused_it(self) -> None:
        line = policy.check(
            "schedule_next_contact", {"days_from_now": 1}, facts(contacts=6),
            reason="he asked me to check tomorrow",
        ).audit()
        self.assertIn("refused", line)
        self.assertIn("policy limit is 6", line)


class OneAnalytePerRequest(unittest.TestCase):
    """Block 3, item 0c. Proved live on rev sanad-00015-p6x.

    The model called request_missing_evidence with `analyte: "Triglycerides,
    HDL"`, the guard allowed it because something was named, and the patient
    got "I have your result but Triglycerides, HDL is missing", which agrees in
    neither language. One analyte, or the call is refused.
    """

    def test_a_list_is_refused_however_it_is_written(self) -> None:
        for value in ("Triglycerides, HDL", "Triglycerides and HDL",
                      "HDL/LDL", "Triglycerides; HDL",
                      "Triglycerides، HDL", "HDL, LDL, Triglycerides"):
            with self.subTest(value=value):
                decision = policy.check(
                    "request_missing_evidence", {"analyte": value}, facts())
                self.assertTrue(decision.refused)
                self.assertEqual(decision.why, policy.ONE_ANALYTE)

    def test_one_analyte_is_allowed_and_comes_back_trimmed(self) -> None:
        for value in ("Triglycerides", " HDL ", "Total cholesterol"):
            with self.subTest(value=value):
                decision = policy.check(
                    "request_missing_evidence", {"analyte": value}, facts())
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.args["analyte"], value.strip())

    def test_a_plus_in_a_name_is_not_two_analytes(self) -> None:
        """A slip prints potassium as "K+". Splitting on that would be a bug."""
        for value in ("K+", "Na+", "Serum Potassium (K+)"):
            with self.subTest(value=value):
                self.assertTrue(policy.check(
                    "request_missing_evidence", {"analyte": value},
                    facts()).allowed)

    def test_the_empty_refusal_still_comes_first(self) -> None:
        decision = policy.check("request_missing_evidence", {"analyte": " , "},
                                facts())
        self.assertTrue(decision.refused)

    def test_the_cap_is_still_the_cap(self) -> None:
        decision = policy.check("request_missing_evidence",
                                {"analyte": "HDL"}, facts(evidence_requests=2))
        self.assertTrue(decision.refused)
        self.assertIn("2 times", decision.why)


class TheVerifierHasTheLastWordOnEvidence(unittest.TestCase):
    """S11 wave A round 2, kernel review F8a.

    Wave A made `verify.satisfies` strict: an unnamed or undated slip attaches
    its values and leaves the obligation open. The reviewer traced the way
    around it. `extractor` wakes the Coordinator on every unsatisfied verdict,
    the instruction says "a complete result arrived: mark_evidence_received",
    and the guard for that tool asked only whether any values were on the loop,
    which they are. So a model vote could move the loop to `pending_review`, the
    state the verifier had just refused, and the end state was the pre-S11 one
    reached by a model instead of by code. That is the opposite of the rule
    docs/SAFETY.md leads with.

    The guard now reads the verifier's own verdict. Where there is no verdict
    (a typed reading, a monitoring loop, anything the verifier never saw) it
    behaves exactly as before, because there is nothing for it to contradict.
    """

    def test_a_verified_slip_still_marks_evidence(self) -> None:
        decision = policy.check(
            "mark_evidence_received", {},
            facts(has_evidence=True, verified_satisfies=True))
        self.assertTrue(decision.allowed)

    def test_an_unverified_slip_cannot_be_marked_by_the_agent(self) -> None:
        decision = policy.check(
            "mark_evidence_received", {},
            facts(has_evidence=True, verified_satisfies=False))
        self.assertTrue(decision.refused)
        self.assertIn("verifier", decision.why)

    def test_a_loop_the_verifier_never_saw_is_unchanged(self) -> None:
        """A typed blood pressure reading has no slip and no verdict."""
        decision = policy.check(
            "mark_evidence_received", {},
            facts(has_evidence=True, verified_satisfies=None))
        self.assertTrue(decision.allowed)

    def test_the_same_gate_guards_closing_the_loop(self) -> None:
        decision = policy.check(
            "close_verified_loop", {},
            facts(has_evidence=True, doctor_reviewed=True,
                  verified_satisfies=False))
        self.assertTrue(decision.refused)
        self.assertIn("verifier", decision.why)

    def test_the_refusal_names_what_the_doctor_should_do(self) -> None:
        decision = policy.check(
            "mark_evidence_received", {},
            facts(has_evidence=True, verified_satisfies=False))
        self.assertIn("escalate_barrier", decision.why)


if __name__ == "__main__":
    unittest.main()
