"""An omitted loop is a refusal, not a way past the identity check.

S24 finding 4, reproduced live on rev 31.  ``guard`` replaced a loop the model
named wrongly and never replaced one it left out: the ``elif loop_id:`` branch
had no ``else``, so an empty ``loop_id`` survived, the route was recomputed as
``unexpected_result``, ``_handle_lab`` ran with ``loop=None``, and with no loop
there is no ``verify.check`` -- no printed name, no collection date, no
completeness.  ``identity_mismatch_card`` was therefore unreachable, and a slip
printing somebody else's name was filed under "Nothing was ordered for this"
while the patient's own lipid order stood open nine minutes old.

Two things this file also pins, because they are what the fix must not cost:
the code-graded critical value is still red and still escalates whatever the
model says about the loop, and a result that genuinely answers no open order is
still routed as an unexpected result.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from core.models import Loop

from tests.test_wave_c import EXTRACTOR_MISSING, NOW, slip
from tests.test_s24_evidence import _PhotoPath, _proposal


def potassium(printed_name: str, taken_on: str = "30/08/2026"):
    """A slip whose one row is graded critical by the table in code."""
    return slip(
        patient_name=printed_name, taken_on=taken_on, lab_name="Nile Lab",
        analytes=[{"analyte": "Potassium", "value": "6.4",
                   "unit": "mmol/L", "ref_range": "3.5-5.1", "flag": "H"}])


# --------------------------------------------------------------------------- #
# The guard, as a pure function
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class AnOmittedLoopIsGuardedLikeAWrongOne(unittest.TestCase):
    """core/evidence.guard. No model, no store, no network."""

    def setUp(self) -> None:
        from core import evidence

        self.evidence = evidence
        self.loops = [
            Loop(id="test-open", patient_id="p1", doctor_id="d", type="TEST",
                 title="Lipid panel", state="open",
                 details={"test_name": "Lipid panel"},
                 created_at=NOW - timedelta(days=2), updated_at=NOW),
            Loop(id="monitor-open", patient_id="p1", doctor_id="d",
                 type="MONITOR", title="Blood pressure", state="open",
                 details={"metric": "BP", "schedule": "twice a day", "days": 7},
                 created_at=NOW - timedelta(days=1), updated_at=NOW),
        ]

    def _facts(self, reading=None, **over):
        reading = reading if reading is not None else slip(
            patient_name="Mona Said", taken_on="30/08/2026",
            analytes=[{"analyte": "LDL Cholesterol", "value": "160"}])
        fields = {"code_kind": "lab_slip", "code_loop_id": "test-open",
                  "code_route": "attach_to_loop", "legible_reading": False}
        fields.update(over)
        return self.evidence.facts_for("Mona Said", reading, self.loops, **fields)

    def test_an_empty_loop_keeps_the_lane_code_chose(self) -> None:
        decided = self.evidence.guard(self._facts(), _proposal("lab_slip", ""))
        self.assertEqual("test-open", decided.loop_id)
        self.assertEqual("attach_to_loop", decided.route)

    def test_the_refusal_is_printed_on_the_record(self) -> None:
        """A proposal that was refused says so; it never reads as nothing said."""
        decided = self.evidence.guard(self._facts(), _proposal("lab_slip", ""))
        self.assertEqual(1, len(decided.refusals))
        self.assertIn("empty loop", decided.refusals[0])
        self.assertIn("test-open", decided.refusals[0])

    def test_the_reason_the_model_gave_is_still_carried(self) -> None:
        decided = self.evidence.guard(
            self._facts(),
            _proposal("lab_slip", "", "the name on the slip is somebody else"))
        self.assertEqual("the name on the slip is somebody else", decided.reason)

    def test_a_result_answering_no_open_order_stays_unattached(self) -> None:
        """With no code-side candidate there is nothing to keep, so nothing is."""
        decided = self.evidence.guard(
            self._facts(code_loop_id="", code_route="unexpected_result"),
            _proposal("lab_slip", ""))
        self.assertEqual("", decided.loop_id)
        self.assertEqual("unexpected_result", decided.route)
        self.assertEqual((), decided.refusals)

    def test_a_relayed_kind_is_still_never_given_a_loop(self) -> None:
        """The `wanted is None` branch runs first and still wins.

        A page nobody could read has no loop type of its own, so there is no
        lane to keep it on and the new branch must not invent one.
        """
        unreadable = slip(patient_name="Mona Said", taken_on="30/08/2026",
                          kind="other", analytes=[])
        facts = self._facts(unreadable, code_kind="other", code_loop_id="",
                            code_route="relay_unread")
        self.assertIn("other", facts.kinds)
        decided = self.evidence.guard(facts, _proposal("other", ""))
        self.assertEqual("other", decided.kind)
        self.assertEqual("", decided.loop_id)
        self.assertEqual((), decided.refusals)

    def test_a_monitor_screen_with_no_loop_named_keeps_the_monitor_loop(self
                                                                       ) -> None:
        """The branch is keyed on the loop type the surviving kind wants."""
        facts = self._facts(code_kind="bp_monitor", code_loop_id="monitor-open",
                            code_route="monitor_reading", legible_reading=True)
        decided = self.evidence.guard(facts, _proposal("bp_monitor", ""))
        self.assertEqual("monitor-open", decided.loop_id)
        self.assertEqual("monitor_reading", decided.route)


# --------------------------------------------------------------------------- #
# The scenario as it was reproduced on the live revision
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheOmittedLoopStillMeetsTheIdentityCheck(_PhotoPath):
    """Another patient's slip, sent to a patient with an open lipid order.

    The model reads the name and the date, decides the page is not this order,
    and omits the loop -- which is exactly what its own prompt forbids it to
    decide.  Code puts the page back on the lane and the three checks run.
    """

    REASON = ("The name on the slip (Ahmed Ali) does not match the patient "
              "(Mona Said), and the date is before the order date.")

    async def _drive(self, reading, proposal):
        with self._read(reading), self._ask(proposal):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")
        return self._routing_event()

    async def test_the_identity_mismatch_card_fires(self) -> None:
        await self._drive(self._lipid(printed_name="Ahmed Ali"),
                          _proposal("lab_slip", "", self.REASON))

        card = self._doctor_cards()[-1].card
        self.assertIn("Identity mismatch", card["title"])
        # The card the doctor used to get, on an order that was nine minutes
        # old, said the opposite of the truth.
        self.assertNotIn("no open test", card["title"].lower())
        for line in card["lines"]:
            self.assertNotIn("Nothing was ordered for this", line)

    async def test_the_verifier_ran_at_all(self) -> None:
        event = await self._drive(self._lipid(printed_name="Ahmed Ali"),
                                  _proposal("lab_slip", "", self.REASON))
        self.assertIsNotNone(event.meta["verify"],
                             "with no loop there is no verify.check")
        self.assertEqual("mismatch", event.meta["verify"]["identity"])
        self.assertFalse(event.meta["verify"]["attaches"])

    async def test_the_page_is_kept_on_the_lane_and_the_refusal_is_recorded(self
                                                                           ) -> None:
        event = await self._drive(self._lipid(printed_name="Ahmed Ali"),
                                  _proposal("lab_slip", "", self.REASON))
        packet = event.meta["evidence_packet"]
        self.assertEqual("l1", packet["loop_id"])
        self.assertEqual("attach_to_loop", packet["route"])
        self.assertIs(packet["agreed_with_code"], True)
        self.assertTrue(any("empty loop" in why for why in packet["refused"]),
                        packet["refused"])
        # The model's own sentence survives on the packet either way.
        self.assertEqual(self.REASON, packet["reason"])

    async def test_the_slip_is_still_not_attached_and_the_order_stays_open(self
                                                                          ) -> None:
        """Keeping the lane is not the same as accepting the evidence."""
        event = await self._drive(self._lipid(printed_name="Ahmed Ali"),
                                  _proposal("lab_slip", "", self.REASON))
        self.assertIs(event.meta["attached"], False)
        self.assertIsNone(event.loop_id)
        self.assertEqual([], self.fake.loops["l1"].results or [])
        self.assertEqual("open", self.fake.loops["l1"].state)
        self.assertTrue(any(
            e.kind == "escalation" and "identity check failed" in e.text
            for e in self.fake.events))

    async def test_a_matching_slip_with_the_loop_omitted_is_simply_attached(self
                                                                           ) -> None:
        """The patient's own result, with the model saying nothing about it."""
        event = await self._drive(self._lipid(), _proposal("lab_slip", ""))
        self.assertEqual("l1", event.meta["evidence_packet"]["loop_id"])
        self.assertIs(event.meta["attached"], True)
        self.assertEqual("l1", event.loop_id)
        self.assertEqual("match", event.meta["verify"]["identity"])


# --------------------------------------------------------------------------- #
# What the fix may not cost
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class CodeGradedSafetyIsUnchanged(_PhotoPath):
    """The critical value is graded by the table, whatever the loop ends up as."""

    async def _drive(self, reading, proposal):
        with self._read(reading), self._ask(proposal):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")

    def _red_cards(self) -> list:
        return [m.card for m in self._doctor_cards()
                if (m.card or {}).get("severity") == "red"]

    async def test_a_critical_value_on_an_omitted_loop_is_still_red(self) -> None:
        await self._drive(potassium(self.PRINTED_NAME), _proposal("lab_slip", ""))

        self.assertTrue(self._red_cards(), "the critical value must stay red")
        self.assertTrue(any(
            e.kind == "escalation" and "critical lab value" in e.text
            for e in self.fake.events))

    async def test_a_critical_value_is_red_even_when_the_name_is_wrong(self
                                                                      ) -> None:
        """Identity failure is a yellow verification problem; the value is not."""
        await self._drive(potassium("Ahmed Ali"),
                          _proposal("lab_slip", "", "not this patient"))

        self.assertTrue(self._red_cards())
        self.assertTrue(any(
            e.kind == "escalation" and "critical lab value" in e.text
            for e in self.fake.events))


if __name__ == "__main__":
    unittest.main()
