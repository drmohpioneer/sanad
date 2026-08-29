"""The order of the gates, asserted against the source that implements it.

The Concierge's docstring says the gates run Sentinel, then the blood-pressure
table, then the change-request gate, then generation, then the validator. A
docstring is a claim; these are the rail. They read the module source the way
the Codex red team did, because the statement order *is* the guarantee: a photo
branch sitting one line too early is exactly how a caption reached the
extractor's model with nothing in front of it (S5 red team).

The card builders below are asserted for real, so the urgent-review path is
tested as behaviour and not only as text. They need the cloud SDK, which the
image has and a laptop may not, so that half skips when it is missing.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
CONCIERGE = (APP_ROOT / "core" / "concierge.py").read_text(encoding="utf-8")
DISPATCH = (APP_ROOT / "core" / "dispatch.py").read_text(encoding="utf-8")
EXTRACTOR = (APP_ROOT / "core" / "extractor.py").read_text(encoding="utf-8")
STORE = (APP_ROOT / "core" / "store.py").read_text(encoding="utf-8")

PATIENT_TURN = CONCIERGE.split("async def handle_patient_message", 1)[1].split(
    "async def open_relay", 1
)[0]


class TheOrderOfTheGates(unittest.TestCase):
    def positions(self, *needles: str) -> list[int]:
        return [PATIENT_TURN.index(n) for n in needles]

    def test_every_gate_runs_in_the_order_the_docstring_claims(self) -> None:
        order = self.positions(
            "vitals.judge_text",               # 1a blood pressure, in code
            "gate.fired",                      # 1  Sentinel
            "validator.wants_treatment_change",  # 2b change request, in code
            "validator.model_change_vote",     # 2b change request, model vote
            "if image_bytes",                  # the photo branch
            "intents.handle",                  # 2c the administrative tier
            "coordinator.on_patient_reply",    # 2c the Coordinator's own turn
            "answer(patient, doctor, text, history)",  # 2 the one generation
            "validator.validate",              # 3  the output validator
            "validator.model_reassurance_vote",  # 3b the reassurance vote
        )
        self.assertEqual(order, sorted(order))

    def test_an_administrative_intent_never_gets_ahead_of_a_gate(self) -> None:
        """S6++ item G. A sentinel hit still wins over an intent.

        "I did the test but I have a terrible pain in the middle of my chest"
        is both. The Sentinel returns from this function before the tier is
        reached at all, so the order below is the whole guarantee.
        """
        emergency, change, admin = self.positions(
            "gate.fired", "validator.model_change_vote", "intents.handle",
        )
        self.assertLess(emergency, admin)
        self.assertLess(change, admin)
        # And the emergency branch leaves: nothing after it in the function runs.
        head = PATIENT_TURN[emergency:admin]
        self.assertIn("return", head)

    def test_the_tier_is_only_asked_about_a_message_that_is_not_a_change(
            self) -> None:
        self.assertIn("if not change_reason and not is_reading(text):",
                      PATIENT_TURN)
        self.assertLess(
            PATIENT_TURN.index("if not change_reason and not is_reading(text):"),
            PATIENT_TURN.index("intents.handle"),
        )

    def test_the_photo_branch_cannot_get_ahead_of_the_change_gate(self) -> None:
        """A caption is the patient's words before it is a caption."""
        code, model, photo = self.positions(
            "validator.wants_treatment_change", "validator.model_change_vote",
            "if image_bytes",
        )
        self.assertLess(code, photo)
        self.assertLess(model, photo)

    def test_the_reassurance_vote_can_only_add_a_relay(self) -> None:
        """It is asked only when the code rules already said pass."""
        self.assertIn('verdict.action == "pass"', PATIENT_TURN)
        self.assertLess(
            PATIENT_TURN.index('verdict.action == "pass"'),
            PATIENT_TURN.index("validator.model_reassurance_vote"),
        )

    def test_a_failed_triage_relays_instead_of_answering(self) -> None:
        self.assertIn("gate.unavailable", PATIENT_TURN)
        self.assertLess(
            PATIENT_TURN.index("gate.unavailable"),
            PATIENT_TURN.index("validator.wants_treatment_change"),
        )

    def test_a_reading_is_graded_in_code_before_any_triage_vote(self) -> None:
        """Found live: a real 185/125 was escalated by the model vote first.

        The reading never reached the table, so it never reached the chart and
        the card named a vote instead of three numbers. The table now runs
        first, and only on a red reading, so a normal one still falls through
        to the Sentinel and the model vote can still add an escalation.
        """
        table, gate = self.positions("vitals.judge_text", "gate.fired")
        self.assertLess(table, gate)
        self.assertIn("bp.red", PATIENT_TURN)


class TheVoiceLane(unittest.TestCase):
    def test_the_transcript_is_checked_where_it_is_made(self) -> None:
        lane = DISPATCH.split('if role == "patient":', 1)[1].split(
            "# An unresolvable ref", 1
        )[0]
        self.assertLess(lane.index("sentinel.check"),
                        lane.index("media.transcribe_async"))
        self.assertIn("gate=gate", lane)


class TheSlipIsModelOutputToo(unittest.TestCase):
    def test_the_extracted_text_meets_the_code_word_list(self) -> None:
        lab = EXTRACTOR.split("async def _handle_lab", 1)[1]
        self.assertIn("sentinel.code_net", lab)
        self.assertIn("labs.urgents", lab)


class ResetStaysInsideOneDoctor(unittest.TestCase):
    def test_pending_starts_are_not_cleared_globally(self) -> None:
        """A test board's reset must not clear a /start Mohamed is binding."""
        wipe = STORE.split("async def wipe_doctor", 1)[1]
        self.assertIn("chat_id", wipe)
        self.assertIn('row.get("chat_id") == chat_id', wipe)


# core.extractor imports the cloud SDK. The image always has it; a laptop may
# not, and the source-order tests above must still run there, so only the class
# that needs the import is skipped.
try:
    from core import extractor, labs
    from core.models import Patient, PhotoReading
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheUrgentReviewCard(unittest.TestCase):
    """S5 item D11. A value the table could not judge is not the ordinary yellow."""

    def patient(self):
        return Patient(id="p", doctor_id="d", name="Hend Ismail", created_at=NOW)

    def findings(self) -> list:
        return labs.assess([
            {"analyte": "Haemoglobin", "value": "9.0", "unit": "pints",
             "ref_range": "11.5-16.5", "flag": ""},
        ])

    def test_an_unjudgeable_value_is_amber_red_and_says_why(self) -> None:
        findings = self.findings()
        card = extractor.unexpected_result_card(
            self.patient(), PhotoReading(kind="lab_slip", text_orientation="upright"), findings, "", False, "e1",
            urgent=True,
        )
        self.assertEqual(card["severity"], "red")
        self.assertIn("URGENT REVIEW", card["title"])
        self.assertTrue(any("could not be judged" in line for line in card["lines"]))

    def test_an_ordinary_result_is_still_yellow(self) -> None:
        card = extractor.unexpected_result_card(
            self.patient(), PhotoReading(kind="lab_slip", text_orientation="upright"), [], "", False, "e1",
        )
        self.assertEqual(card["severity"], "yellow")
        self.assertNotIn("URGENT", card["title"])


if __name__ == "__main__":
    unittest.main()
