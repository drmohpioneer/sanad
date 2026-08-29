"""The Care Contract: the loop and the policy, said out loud, always the same.

S6+ item A. Nothing new is stored, so what this suite protects is the wording
and the shape: the same six parts on the console, on the confirm card and in the
Coordinator's own instruction, so a doctor cannot be shown one contract and have
another one carried out.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core import contract, policy

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def loop(kind="TEST", title="Lipid panel", due_days=14, **details):
    return SimpleNamespace(
        id="l1", type=kind, title=title,
        details=details or ({"test_name": title} if kind == "TEST" else {}),
        state="open",
        due_at=NOW + timedelta(days=due_days) if due_days is not None else None,
        contacts=1, evidence_requests=0, barrier="", paused=False,
        doctor_reviewed=False, verified={}, results=[], readings=[],
    )


class TheSixParts(unittest.TestCase):
    def rendered(self, **kw):
        return contract.render(loop(**kw), policy.DEFAULT, "Dr Mohamed",
                               "Ahmed Ali")

    def test_it_carries_all_six(self) -> None:
        c = self.rendered()
        for key in ("objective", "evidence", "permitted_actions", "safety",
                    "deadline", "escalation_conditions"):
            with self.subTest(key=key):
                self.assertTrue(c[key])

    def test_the_objective_is_what_for_whom_by_when(self) -> None:
        c = self.rendered()
        self.assertIn("Lipid panel", c["objective"])
        self.assertIn("Ahmed Ali", c["objective"])
        self.assertIn("2026-09-12", c["objective"])

    def test_each_type_gets_the_verb_it_deserves(self) -> None:
        visit = contract.objective(loop("VISIT", "Follow-up visit"), "Ahmed Ali")
        drug = contract.objective(loop("MEDICATION", "Atorvastatin",
                                       drug="atorvastatin"), "Ahmed Ali")
        self.assertIn("Bring Ahmed Ali back", visit)
        self.assertIn("medication", drug)

    def test_the_evidence_is_the_analytes_and_the_three_checks(self) -> None:
        evidence = self.rendered()["evidence"]
        self.assertEqual(evidence["analytes"],
                         ["Total cholesterol", "Triglycerides", "HDL", "LDL"])
        self.assertEqual(len(evidence["checks"]), 3)
        self.assertTrue(any("name printed" in c for c in evidence["checks"]))
        self.assertTrue(any("collection date" in c for c in evidence["checks"]))

    def test_a_monitor_contract_asks_for_readings_not_analytes(self) -> None:
        evidence = contract.evidence_required(
            loop("MONITOR", "Blood pressure", metric="BP", schedule="twice a day",
                 days=7)
        )
        self.assertEqual(evidence["kind"], "readings")
        self.assertIn("BP", evidence["wanted"])
        self.assertEqual(evidence["analytes"], [])

    def test_the_permitted_actions_are_exactly_the_tool_list(self) -> None:
        self.assertEqual(self.rendered()["permitted_actions"], list(policy.TOOLS))

    def test_the_safety_sentence_is_fixed_and_says_who_owns_the_decision(self) -> None:
        safety = self.rendered()["safety"]
        self.assertEqual(safety, contract.SAFETY_SENTENCE)
        self.assertIn("does not diagnose", safety)
        self.assertIn("critical-value table", safety)
        self.assertIn("doctor owns every clinical decision", safety)

    def test_the_deadline_carries_the_end_of_the_doctors_window(self) -> None:
        deadline = self.rendered()["deadline"]
        self.assertIn("2026-09-12", deadline["due_at"])
        self.assertIn("2026-09-19", deadline["window_ends"])

    def test_a_loop_with_no_date_says_so_rather_than_inventing_one(self) -> None:
        deadline = self.rendered(due_days=None)["deadline"]
        self.assertEqual(deadline["due_at"], "")
        self.assertIn("no due date", deadline["in_words"])

    def test_the_escalation_conditions_name_the_code_that_fires_them(self) -> None:
        conditions = " ".join(self.rendered()["escalation_conditions"])
        for named in ("core/labs.py", "core/validator.py", "core/verify.py",
                      "deadline", "barrier"):
            with self.subTest(named=named):
                self.assertIn(named, conditions)

    def test_the_policy_that_binds_it_is_shown_with_it(self) -> None:
        shown = self.rendered()["policy"]
        self.assertEqual(shown["max_contacts"], 6)
        self.assertIn("22:00 to 08:00 Cairo", shown["quiet_hours"])

    def test_where_it_stands_is_read_from_the_loop(self) -> None:
        state = self.rendered()["state"]
        self.assertEqual(state["contacts"], 1)
        self.assertFalse(state["paused"])
        self.assertFalse(state["doctor_reviewed"])


class TheLinesACardPrints(unittest.TestCase):
    def lines(self):
        return contract.lines(contract.render(
            loop(), policy.DEFAULT, "Dr Mohamed", "Ahmed Ali"))

    def test_the_confirm_card_and_the_console_read_the_same_function(self) -> None:
        confirm = contract.for_confirm(loop(), policy.DEFAULT, "Dr Mohamed",
                                       "Ahmed Ali")
        self.assertEqual(confirm[0], self.lines()[0])
        self.assertEqual(confirm[-1], contract.SAFETY_SENTENCE)

    def test_no_dash_reads_as_a_machine_wrote_it(self) -> None:
        for line in self.lines():
            with self.subTest(line=line):
                self.assertNotIn("—", line)
                self.assertNotIn("–", line)


if __name__ == "__main__":
    unittest.main()
