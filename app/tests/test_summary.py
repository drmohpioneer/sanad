"""The end-of-day summary, and the claim that nothing is lost.

S6++ item K. The wording is fixed by the spec and asserted here word for word,
because a doctor reading "2 patients could not be reached" has to be able to
take it literally.

The important test is `TheClassifierIsTotal`: it drives every combination of
state, barrier, pause and review flag through `classify` and asserts that the
six buckets always add up to the number of obligations carried. That is what
"lost is zero by construction" means, and it is why the number can be printed on
a card at all.
"""

from __future__ import annotations

import itertools
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

from core import summary

TODAY = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
STATES = ("open", "waiting_patient", "received", "pending_review", "done",
          "unreachable")


def loop(loop_id="l", state="open", patient="p", barrier="", paused=False,
         results=(), readings=(), doctor_reviewed=False):
    return SimpleNamespace(
        id=loop_id, patient_id=patient, state=state, barrier=barrier,
        paused=paused, results=list(results), readings=list(readings),
        doctor_reviewed=doctor_reviewed, title="Lipid panel",
    )


def event(kind="escalation", text="", loop_id=None, concept="", ts=TODAY):
    return SimpleNamespace(
        kind=kind, text=text, loop_id=loop_id, ts=ts,
        meta={"sentinel": {"concept": concept}} if concept else {},
    )


def relay(relay_id="r", patient="p", reason="", loop_id=None):
    return SimpleNamespace(id=relay_id, patient_id=patient, reason=reason,
                           loop_id=loop_id)


class TheClassifierIsTotal(unittest.TestCase):
    def test_every_obligation_lands_in_exactly_one_bucket(self) -> None:
        combinations = itertools.product(
            STATES, ("", "cost", "forgot"), (False, True), (False, True),
            (False, True),
        )
        loops = [
            loop(f"l{i}", state=state, barrier=barrier, paused=paused,
                 results=[{"analyte": "K"}] if evidence else [],
                 doctor_reviewed=reviewed)
            for i, (state, barrier, paused, evidence, reviewed)
            in enumerate(combinations)
        ]
        counts = summary.compute(loops)
        self.assertEqual(counts.carried, len(loops))
        self.assertEqual(sum(counts.buckets.values()), len(loops))
        self.assertEqual(counts.lost, 0)

    def test_the_bucket_names_are_the_six_the_module_declares(self) -> None:
        counts = summary.compute([loop()])
        self.assertEqual(set(counts.buckets), set(summary.BUCKETS))

    def test_a_loop_is_never_counted_twice(self) -> None:
        """Escalated and unreachable at once is one obligation, not two."""
        board = [loop("l1", state="unreachable")]
        counts = summary.compute(board, [event(loop_id="l1", concept="critical lab value")])
        self.assertEqual(counts.carried, 1)
        self.assertEqual(sum(counts.buckets.values()), 1)
        self.assertEqual(counts.buckets["critical"], 1)
        self.assertEqual(counts.buckets["unreachable"], 0)


class WhatEachNumberMeans(unittest.TestCase):
    def board(self):
        return [
            loop("done1", state="done", patient="p1", results=[{"analyte": "LDL"}]),
            loop("open1", state="waiting_patient", patient="p2"),
            loop("cost1", state="waiting_patient", patient="p3", barrier="cost",
                 paused=True),
            loop("gone1", state="unreachable", patient="p4"),
            loop("crit1", state="pending_review", patient="p5",
                 results=[{"analyte": "K"}]),
        ]

    def counts(self):
        history = [event(loop_id="crit1", text="emergency: critical lab value")]
        relays = [SimpleNamespace(id="r1"), SimpleNamespace(id="r2")]
        return summary.compute(self.board(), history, relays)

    def test_the_numbers(self) -> None:
        counts = self.counts()
        self.assertEqual(counts.carried, 5)
        self.assertEqual(counts.buckets["completed_with_evidence"], 1)
        self.assertEqual(counts.buckets["progressing"], 1)
        self.assertEqual(counts.patients_needing_help, 1)
        self.assertEqual(counts.patients_unreachable, 1)
        self.assertEqual(counts.questions, 2)
        self.assertEqual(counts.criticals, 1)
        self.assertEqual(counts.lost, 0)

    def test_doctor_attention_counts_cases_and_does_not_double_count(self) -> None:
        """The critical, the barrier, the unreachable loop and two questions."""
        self.assertEqual(self.counts().attention, 5)

    def test_a_closed_loop_with_no_evidence_is_not_counted_as_evidence(self) -> None:
        counts = summary.compute([loop("v", state="done", patient="p")])
        self.assertEqual(counts.buckets["completed_with_evidence"], 0)
        self.assertEqual(counts.buckets["closed_without_evidence"], 1)
        self.assertEqual(counts.lost, 0)

    def test_a_critical_reading_with_no_loop_behind_it_is_still_counted(self) -> None:
        counts = summary.compute(
            [loop("open1")], [event(concept="hypertensive crisis")]
        )
        self.assertEqual(counts.criticals, 1)
        self.assertEqual(counts.carried, 1)
        self.assertEqual(counts.lost, 0)

    def test_yesterdays_escalation_is_not_todays(self) -> None:
        yesterday = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
        counts = summary.compute(
            [loop("l1")],
            [event(loop_id="l1", text="emergency: critical lab value", ts=yesterday)],
            on=TODAY.date(),
        )
        self.assertEqual(counts.criticals, 0)


class TheDayIsTheDoctorsDay(unittest.TestCase):
    """S11 wave A item 19, from reviews/codex-troubleshoot-1.md line 20:

    "MEDIUM Daily summary uses UTC dates not Cairo (main.py:716;
    summary.py:69,177); barrier relays double-counted as treatment questions
    and attention."

    Cairo runs two or three hours ahead of UTC, so everything between midnight
    and 03:00 Cairo belongs to yesterday in UTC. A critical result escalated at
    01:30 Cairo was dropped from that morning's summary and counted again on the
    day before, which is a doctor being told nothing happened on the night
    something did.
    """

    #  2026-08-28 23:30 UTC is 2026-08-29 01:30 Cairo.
    HALF_ONE_CAIRO = datetime(2026, 8, 28, 23, 30, tzinfo=timezone.utc)

    def test_an_escalation_at_half_past_one_cairo_is_todays(self) -> None:
        counts = summary.compute(
            [loop("l1")],
            [event(loop_id="l1", text="emergency: critical lab value",
                   ts=self.HALF_ONE_CAIRO)],
            on=date(2026, 8, 29),
        )
        self.assertEqual(counts.criticals, 1)
        self.assertEqual(counts.buckets["critical"], 1)

    def test_the_same_escalation_is_not_yesterdays(self) -> None:
        counts = summary.compute(
            [loop("l1")],
            [event(loop_id="l1", text="emergency: critical lab value",
                   ts=self.HALF_ONE_CAIRO)],
            on=date(2026, 8, 28),
        )
        self.assertEqual(counts.criticals, 0)

    def test_a_loose_critical_reading_is_dated_the_same_way(self) -> None:
        counts = summary.compute(
            [loop("l1")],
            [event(concept="hypertensive crisis", ts=self.HALF_ONE_CAIRO)],
            on=date(2026, 8, 29),
        )
        self.assertEqual(counts.criticals, 1)

    def test_the_default_day_is_cairos_day(self) -> None:
        self.assertEqual(
            summary.today(datetime(2026, 8, 28, 23, 30, tzinfo=timezone.utc)),
            date(2026, 8, 29),
        )


class ABarrierIsLogisticsNotAQuestion(unittest.TestCase):
    """The second half of item 19. A patient who cannot afford the test has not
    asked a treatment question, and the doctor should not be told twice about
    one patient because the barrier opened a card as well as a bucket."""

    def board(self):
        return [loop("cost1", state="waiting_patient", patient="p3",
                     barrier="cost", paused=True)]

    def test_a_cost_relay_is_logistical_help_and_not_a_treatment_question(self) -> None:
        counts = summary.compute(
            self.board(), [],
            [relay("r1", patient="p3", reason="barrier: cost", loop_id="cost1")],
        )
        self.assertEqual(counts.questions, 0)
        self.assertEqual(counts.patients_needing_help, 1)

    def test_the_same_patient_and_the_same_reason_is_one_case(self) -> None:
        counts = summary.compute(
            self.board(), [],
            [relay("r1", patient="p3", reason="barrier: cost", loop_id="cost1")],
        )
        self.assertEqual(counts.attention, 1)

    def test_two_barrier_relays_on_one_patient_are_still_one_case(self) -> None:
        counts = summary.compute(
            self.board(), [],
            [relay("r1", patient="p3", reason="barrier: cost", loop_id="cost1"),
             relay("r2", patient="p3", reason="barrier: transport")],
        )
        self.assertEqual(counts.attention, 1)
        self.assertEqual(counts.questions, 0)

    def test_a_clinical_refusal_is_still_a_treatment_question(self) -> None:
        """"I am fine, why should I come back" is the patient arguing with the
        treatment, and only the doctor can answer it."""
        counts = summary.compute(
            [loop("l1", state="open", patient="p9", barrier="asymptomatic")], [],
            [relay("r1", patient="p9", reason="barrier: asymptomatic")],
        )
        self.assertEqual(counts.questions, 1)

    def test_an_ordinary_concierge_relay_is_a_treatment_question(self) -> None:
        counts = summary.compute(
            [loop("l1", patient="p1")], [],
            [relay("r1", patient="p1", reason="the reply named a dose")],
        )
        self.assertEqual(counts.questions, 1)
        self.assertEqual(counts.patients_needing_help, 0)

    def test_two_different_reasons_on_one_patient_are_two_cases(self) -> None:
        counts = summary.compute(
            [loop("cost1", state="waiting_patient", patient="p3", barrier="cost")],
            [],
            [relay("r1", patient="p3", reason="barrier: cost"),
             relay("r2", patient="p3", reason="the reply named a dose")],
        )
        self.assertEqual(counts.attention, 2)

    def test_a_relay_with_no_patient_on_it_is_never_merged_with_another(self) -> None:
        """The dedup key is the patient. A record that does not carry one is
        counted on its own id rather than folded into every other orphan."""
        counts = summary.compute(
            [], [], [SimpleNamespace(id="r1"), SimpleNamespace(id="r2")]
        )
        self.assertEqual(counts.attention, 2)
        self.assertEqual(counts.questions, 2)


class OneCasePerThingTheDoctorMustDo(unittest.TestCase):
    """S11 wave A round 2, kernel review F14.

    Round 1 keyed every attention case on (patient, reason), which is right for
    a barrier and wrong for a result. One patient who cannot afford one test is
    one case however many records carry it. One patient with two different
    results waiting for his review is two things he has to read, and merging
    them told him there was one.
    """

    def test_two_results_awaiting_review_on_one_patient_are_two_cases(self) -> None:
        counts = summary.compute([
            loop("l1", state="pending_review", patient="p1"),
            loop("l2", state="pending_review", patient="p1"),
        ])
        self.assertEqual(counts.attention, 2)

    def test_two_critical_results_on_one_patient_are_two_cases(self) -> None:
        counts = summary.compute(
            [loop("l1", patient="p1"), loop("l2", patient="p1")],
            [event(loop_id="l1", text="emergency: critical lab value"),
             event(loop_id="l2", text="emergency: critical lab value")],
        )
        self.assertEqual(counts.buckets["critical"], 2)
        self.assertEqual(counts.attention, 2)

    def test_two_barriers_on_one_patient_are_still_one_case(self) -> None:
        """A barrier is about the patient, not about the obligation: telling the
        doctor twice that one patient cannot pay is telling him one thing."""
        counts = summary.compute([
            loop("l1", state="waiting_patient", patient="p1", barrier="cost"),
            loop("l2", state="waiting_patient", patient="p1", barrier="transport"),
        ])
        self.assertEqual(counts.buckets["needs_help"], 2)
        self.assertEqual(counts.patients_needing_help, 1)
        self.assertEqual(counts.attention, 1)

    def test_two_unreachable_loops_on_one_patient_are_still_one_case(self) -> None:
        counts = summary.compute([
            loop("l1", state="unreachable", patient="p1"),
            loop("l2", state="unreachable", patient="p1"),
        ])
        self.assertEqual(counts.attention, 1)

    def test_a_barrier_relay_still_merges_with_its_own_loop(self) -> None:
        counts = summary.compute(
            [loop("l1", state="waiting_patient", patient="p1", barrier="cost")],
            [],
            [relay("r1", patient="p1", reason="barrier: cost", loop_id="l1")],
        )
        self.assertEqual(counts.attention, 1)


class TheExactWording(unittest.TestCase):
    def test_the_line_is_the_sentence_the_spec_wrote(self) -> None:
        counts = summary.Counts(carried=12)
        counts.buckets["completed_with_evidence"] = 4
        counts.buckets["progressing"] = 5
        counts.patients_needing_help = 2
        counts.patients_unreachable = 1
        counts.questions = 3
        counts.criticals = 1
        counts.attention = 6
        self.assertEqual(summary.line(counts), (
            "Today Sanad carried 12 care obligations · 4 completed with "
            "evidence · 5 progressing normally · 2 patients needed logistical "
            "help · 1 patients could not be reached · 3 treatment questions "
            "need you · 1 critical results escalated · Doctor attention "
            "required: 6 cases"
        ))

    def test_the_card_says_lost_is_zero_and_why(self) -> None:
        card = summary.card(summary.compute([loop()]), "Dr Mohamed", TODAY)
        self.assertIn("Lost: 0.", card["lines"][1])
        self.assertTrue(any("by construction" in line for line in card["lines"]))
        self.assertTrue(any("No model was asked" in line for line in card["lines"]))

    def test_no_dash_anywhere_on_the_card(self) -> None:
        card = summary.card(summary.compute([loop()]), "Dr Mohamed", TODAY)
        for line in [card["title"], *card["lines"]]:
            with self.subTest(line=line):
                self.assertNotIn("—", line)
                self.assertNotIn("–", line)


if __name__ == "__main__":
    unittest.main()
