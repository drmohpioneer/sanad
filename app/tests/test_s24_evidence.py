"""S24-E: the Evidence Orchestrator, and the five rails it is not allowed to move.

The routing decision on the photo path moved out of a table and under one
bounded Gemini turn (core/evidence.py). What did NOT move is the whole point of
this file, so each rail is driven rather than described:

  1. the Sentinel is upstream of the photograph and of this agent, and a code
     hit ends the turn before either is reached;
  2. core/verify.check runs downstream and overrules the agent: a slip the
     orchestrator nominated for a loop still detaches when the printed name is
     somebody else's, and the doctor's card still refuses the attach button;
  3. a proposal code refuses - a closed loop, a loop on another patient's
     record, a monitor loop for a lab slip, an invented id - is refused with the
     guard printed on the packet, and the table's own choice stands;
  4. wrong-patient behaviour is byte-identical to the behaviour before this
     agent existed;
  5. a model that is down, slow or unusable produces today's code routing
     verbatim: same route, same event metadata, same label, and no packet.

The comparisons in `LegacyRoutingIsUntouched` are made against a run of the same
scenario with the agent standing down, which IS the pre-S24 code path, so
"byte-identical" here is measured and not asserted from memory.
"""

from __future__ import annotations

import ast
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.models import Doctor, Event, Loop, Patient

from tests.test_decided_by import bucket
from tests.test_wave_c import (
    EXTRACTOR_MISSING,
    NOW,
    FakeStore,
    Recorder,
    STORE_NAMES,
    slip,
)

APP_ROOT = Path(__file__).resolve().parents[1]
CORE = APP_ROOT / "core"


def _proposal(kind: str = "lab_slip", loop_id: str = "", reason: str = ""):
    from core.evidence import EvidenceProposal

    return EvidenceProposal(kind=kind, loop_id=loop_id, reason=reason)


# --------------------------------------------------------------------------- #
# The guards, as pure functions over facts code built
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheGuardsRefuseWhatWasNotOffered(unittest.TestCase):
    """core/evidence.guard. No model, no store, no network."""

    def setUp(self) -> None:
        from core import evidence

        self.evidence = evidence
        self.loops = [
            Loop(id="test-open", patient_id="p1", doctor_id="d", type="TEST",
                 title="Lipid panel", state="open",
                 details={"test_name": "Lipid panel"},
                 created_at=NOW - timedelta(days=2), updated_at=NOW),
            Loop(id="test-closed", patient_id="p1", doctor_id="d", type="TEST",
                 title="Old potassium", state="done",
                 details={"test_name": "Potassium"},
                 created_at=NOW - timedelta(days=40), updated_at=NOW),
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
        return self.evidence.facts_for(
            "Mona Said", reading, self.loops, **fields)

    def test_only_open_loops_are_ever_offered(self) -> None:
        offered = [offer.id for offer in self._facts().offers]
        self.assertEqual(["test-open", "monitor-open"], offered)
        self.assertNotIn("test-closed", offered)

    def test_the_contract_of_each_offer_comes_from_the_doctors_own_order(self
                                                                        ) -> None:
        offer = next(o for o in self._facts().offers if o.id == "test-open")
        self.assertIn("LDL", offer.required)
        self.assertEqual("2026-08-27", offer.ordered_on)

    def test_a_closed_loop_is_refused_and_the_table_choice_stands(self) -> None:
        facts = self._facts()
        decided = self.evidence.guard(
            facts, _proposal("lab_slip", "test-closed"))
        self.assertEqual("test-open", decided.loop_id)
        self.assertEqual("attach_to_loop", decided.route)
        self.assertEqual(1, len(decided.refusals))
        self.assertIn("test-closed", decided.refusals[0])
        self.assertIn("not an open obligation", decided.refusals[0])

    def test_another_patients_loop_is_refused_the_same_way(self) -> None:
        """An id from somebody else's record is simply not on the list."""
        facts = self._facts()
        decided = self.evidence.guard(
            facts, _proposal("lab_slip", "loop-of-another-patient"))
        self.assertEqual("test-open", decided.loop_id)
        self.assertIn("loop-of-another-patient", decided.refusals[0])
        self.assertIn("this patient's record", decided.refusals[0])

    def test_a_lab_slip_cannot_be_filed_on_a_monitoring_chart(self) -> None:
        facts = self._facts()
        decided = self.evidence.guard(
            facts, _proposal("lab_slip", "monitor-open"))
        self.assertEqual("test-open", decided.loop_id)
        self.assertIn("cannot be filed on a MONITOR", decided.refusals[0])

    def test_a_kind_that_was_never_a_candidate_is_refused(self) -> None:
        facts = self._facts()
        self.assertNotIn("bp_monitor", facts.kinds)
        decided = self.evidence.guard(
            facts, _proposal("bp_monitor", "monitor-open"))
        self.assertEqual("lab_slip", decided.kind)
        self.assertEqual("test-open", decided.loop_id)
        self.assertTrue(any("candidate kinds" in why for why in decided.refusals))

    def test_a_page_that_parsed_into_values_is_never_offered_the_relay_exit(
            self) -> None:
        """The one direction the model may not move a page: off the lab lane.

        The relay lane does not call core/labs.assess, does not consult the
        critical-value table and never reaches core/escalate, so a slip whose
        rows already parsed cannot be answered "other" at all. It is not on the
        candidate list, and a proposal naming it is refused like any other kind
        that was never offered.
        """
        facts = self._facts()
        self.assertNotIn("other", facts.kinds)
        self.assertEqual(("lab_slip",), facts.kinds)
        decided = self.evidence.guard(facts, _proposal("other", "test-open"))
        self.assertEqual("lab_slip", decided.kind)
        self.assertEqual("test-open", decided.loop_id)
        self.assertEqual("attach_to_loop", decided.route)
        self.assertTrue(any("candidate kinds" in why for why in decided.refusals))

    def test_a_legible_monitor_screen_is_not_offered_the_relay_exit_either(self
                                                                          ) -> None:
        """Two readable pressures are values too, and they route the same way."""
        reading = slip(kind="bp_monitor", systolic="118", diastolic="76",
                       pulse="70")
        facts = self._facts(reading=reading, code_kind="bp_monitor",
                            code_loop_id="monitor-open",
                            code_route="monitor_reading", legible_reading=True)
        self.assertNotIn("other", facts.kinds)
        self.assertEqual(("bp_monitor",), facts.kinds)
        decided = self.evidence.guard(facts, _proposal("other", "monitor-open"))
        self.assertEqual("bp_monitor", decided.kind)
        self.assertEqual("monitor-open", decided.loop_id)
        self.assertEqual("monitor_reading", decided.route)

    def test_a_relayed_kind_never_carries_a_loop(self) -> None:
        """The relay exit is still there for a page with nothing on it to read.

        Nothing parsed off this one - no analyte rows, no legible pressures -
        so "other" is a candidate, the model may take it, and a relay still
        never carries an obligation however it was proposed.
        """
        facts = self._facts(reading=slip(kind="prescription"),
                            code_kind="prescription", code_loop_id="",
                            code_route="relay")
        self.assertEqual(("prescription", "other"), facts.kinds)
        decided = self.evidence.guard(facts, _proposal("other", "test-open"))
        self.assertEqual("other", decided.kind)
        self.assertEqual("", decided.loop_id)
        self.assertEqual("relay", decided.route)
        self.assertTrue(any("relayed unread" in why for why in decided.refusals))

    def test_the_route_is_recomputed_by_the_table_and_never_proposed(self) -> None:
        """No field of the proposal schema can name a route at all."""
        from core.evidence import EvidenceProposal

        self.assertEqual({"kind", "loop_id", "reason"},
                         set(EvidenceProposal.model_fields))
        facts = self._facts()
        for loop_id, expected in (("test-open", "attach_to_loop"),
                                  ("", "unexpected_result")):
            with self.subTest(loop_id=loop_id):
                decided = self.evidence.guard(
                    facts, _proposal("lab_slip", loop_id))
                self.assertEqual(expected, decided.route)

    def test_an_accepted_choice_reports_what_the_contract_still_misses(self
                                                                      ) -> None:
        loops = [Loop(id="test-open", patient_id="p1", doctor_id="d",
                      type="TEST", title="Lipid panel", state="open",
                      details={"test_name": "Lipid panel",
                               "analytes": ["LDL", "HDL", "Triglycerides"]},
                      created_at=NOW - timedelta(days=2), updated_at=NOW)]
        self.loops = loops
        decided = self.evidence.guard(
            self._facts(), _proposal("lab_slip", "test-open"))
        self.assertEqual(("HDL", "Triglycerides"), decided.missing)

    def test_the_candidates_come_from_the_page_not_from_the_label(self) -> None:
        """A "prescription" carrying analyte rows is still a candidate slip.

        The promotion direction stays open, because it adds the value table
        rather than removing it. The demotion direction is gone: the rows
        parsed, so "other" is not on the list at all.
        """
        reading = slip(kind="prescription",
                       analytes=[{"analyte": "Potassium", "value": "6.4"}])
        facts = self._facts(reading=reading, code_kind="prescription",
                            code_loop_id="", code_route="relay")
        self.assertEqual(("prescription", "lab_slip"), facts.kinds)
        decided = self.evidence.guard(
            facts, _proposal("lab_slip", "test-open"))
        self.assertEqual("attach_to_loop", decided.route)
        self.assertEqual((), decided.refusals)


# --------------------------------------------------------------------------- #
# Rail 5, at the boundary: no model in this process, so no turn
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheTurnStandsDownWhenThereIsNoModel(unittest.IsolatedAsyncioTestCase):
    async def test_an_unmocked_boundary_answers_none_and_never_calls_out(self
                                                                        ) -> None:
        """The hermetic double is not a model, so `_ask` does not touch it.

        `sanad_test_guard` refuses an unmocked GenAI call with a BaseException
        on purpose, so a production `except Exception` cannot swallow it. This
        seam therefore does not reach for the client at all in a test process,
        which is why the frozen Gate 0B journey replays the code route.
        """
        from core import evidence

        self.assertTrue(evidence._hermetic())
        facts = evidence.facts_for(
            "Mona Said", slip(analytes=[{"analyte": "LDL", "value": "160"}]),
            [], code_kind="lab_slip", code_loop_id="",
            code_route="unexpected_result")
        self.assertIsNone(await evidence._ask(facts))
        self.assertIsNone(await evidence.decide(facts))

    async def test_a_turn_that_raises_is_a_stand_down_and_not_an_outage(self
                                                                       ) -> None:
        from core import evidence

        facts = evidence.facts_for(
            "Mona Said", slip(analytes=[{"analyte": "LDL", "value": "160"}]),
            [], code_kind="lab_slip", code_loop_id="",
            code_route="unexpected_result")

        async def boom(_facts):
            raise RuntimeError("vertex is down")

        with patch.object(evidence, "_ask", boom):
            self.assertIsNone(await evidence.decide(facts))

    async def test_the_call_carries_a_deadline(self) -> None:
        """A hung turn is a lost turn, not a patient waiting on a spinner."""
        from core import bounded, evidence

        self.assertGreater(evidence.DEADLINE, 0)
        source = (CORE / "evidence.py").read_text(encoding="utf-8")
        self.assertIn("bounded.within(", source)
        self.assertIn("DEADLINE", source)
        self.assertTrue(hasattr(bounded, "within"))


# --------------------------------------------------------------------------- #
# The photo path, driven
# --------------------------------------------------------------------------- #
class _PhotoPath(unittest.IsolatedAsyncioTestCase):
    """The Wave C harness, with the orchestrator seam under this test's control."""

    PRINTED_NAME = "Mona Said"

    def setUp(self) -> None:
        from core import chaser, coordinator, storage, store, extractor

        self.extractor = extractor
        self.fake = FakeStore()
        self.doctor = Doctor(id="d", name="Dr Mohamed", specialty="cardiology",
                             lang="en", web_token="tok", created_at=NOW)
        self.patient = Patient(id="p1", doctor_id="d", name="Mona Said",
                               sex="f", diagnosis="high LDL",
                               channels={"web": True}, created_at=NOW)
        self.fake.doctors["d"] = self.doctor
        self.fake.patients["p1"] = self.patient
        self.fake.loops["l1"] = Loop(
            id="l1", patient_id="p1", doctor_id="d", type="TEST",
            title="Lipid panel", state="open",
            details={"test_name": "Lipid panel"},
            created_at=NOW - timedelta(days=2), updated_at=NOW)
        self.fake.loops["l-done"] = Loop(
            id="l-done", patient_id="p1", doctor_id="d", type="TEST",
            title="Old potassium", state="done",
            details={"test_name": "Potassium"},
            created_at=NOW - timedelta(days=40), updated_at=NOW)

        self.out = Recorder()
        for name in STORE_NAMES:
            self.enterContext(patch.object(store, name, getattr(self.fake, name)))
        self.enterContext(patch.object(extractor, "fanout", lambda: self.out))
        self.enterContext(patch.object(
            storage, "put_image", self._async(lambda *a, **k: "gs://labs/x.jpg")))
        self.enterContext(patch.object(
            extractor.settings, "current", self._async(lambda: ("run1", 86400))))
        self.enterContext(patch.object(
            chaser, "supersede_ladder", AsyncMock(return_value=1)))
        self.enterContext(patch.object(coordinator, "on_evidence", AsyncMock()))

    @staticmethod
    def _async(fn):
        async def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapper

    def _read(self, reading):
        async def read_photo(image, mime="image/jpeg"):
            return reading, {"rotated": 0, "attempts": 1}
        return patch.object(self.extractor, "read_photo", read_photo)

    @staticmethod
    def _ask(proposal):
        from core import evidence

        async def ask(_facts):
            return proposal

        return patch.object(evidence, "_ask", ask)

    def _lipid(self, printed_name=None, taken_on="30/08/2026"):
        return slip(
            patient_name=self.PRINTED_NAME if printed_name is None
            else printed_name,
            taken_on=taken_on, lab_name="Nile Lab",
            analytes=[{"analyte": "LDL Cholesterol", "value": "160",
                       "unit": "mg/dL", "ref_range": "<100", "flag": "H"}])

    # -- readers ---------------------------------------------------------- #
    def _routing_event(self) -> Event:
        rows = [e for e in self.fake.events
                if e.kind == "system" and "lab slip read" in e.text]
        self.assertEqual(1, len(rows), [e.text for e in self.fake.events])
        return rows[0]

    def _doctor_cards(self) -> list:
        return [m for m in self.out.to("doctor:") if getattr(m, "card", None)]

    def _shape(self) -> list:
        """Everything this run produced, with the S24 packet lifted out.

        The packet and the routing label are the only two things this slice is
        allowed to add, so removing them has to leave the pre-S24 output.
        """
        events = []
        for row in self.fake.events:
            meta = {k: v for k, v in row.meta.items() if k != "evidence_packet"}
            meta.pop("decided_by", None)
            events.append((row.kind, row.text, row.patient_id, row.loop_id,
                           meta, row.media))
        sent = []
        for ref, message in self.out.sent:
            meta = {k: v for k, v in (message.meta or {}).items()
                    if k != "decided_by"}
            sent.append((ref, message.text, getattr(message, "card", None), meta))
        loops = {i: l.model_dump() for i, l in sorted(self.fake.loops.items())}
        return [events, sent, loops]


@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheDecisionIsRecordedWithItsPacket(_PhotoPath):
    async def test_an_accepted_disposition_writes_the_packet_and_the_label(self
                                                                          ) -> None:
        from core import evidence

        with self._read(self._lipid()), self._ask(
                _proposal("lab_slip", "l1", "the LDL row answers the lipid panel")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")

        event = self._routing_event()
        packet = event.meta["evidence_packet"]
        self.assertEqual("lab_slip", packet["kind"])
        self.assertEqual("l1", packet["loop_id"])
        self.assertEqual("attach_to_loop", packet["route"])
        self.assertEqual([], packet["refused"])
        self.assertTrue(packet["agreed_with_code"])
        self.assertEqual(["l1"], packet["offered"])
        self.assertEqual("the LDL row answers the lipid panel", packet["reason"])
        self.assertEqual(evidence.DECIDED_BY, packet["decided_by"])
        self.assertEqual(self.extractor.DECIDED_EVIDENCE_AGENT,
                         event.meta["decided_by"])
        self.assertIs(event.meta["attached"], True)

    async def test_the_packet_validates_as_its_own_type(self) -> None:
        from core.models import EvidencePacket

        with self._read(self._lipid()), self._ask(_proposal("lab_slip", "l1")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")
        raw = dict(self._routing_event().meta["evidence_packet"])
        raw.pop("synthetic")
        self.assertEqual(raw, EvidencePacket(**raw).model_dump())

    async def test_the_packet_carries_the_provenance_of_every_decider(self) -> None:
        with self._read(self._lipid()), self._ask(_proposal("lab_slip", "l1")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")
        provenance = self._routing_event().meta["evidence_packet"]["provenance"]
        self.assertIn("gemini", provenance["proposed_by"])
        self.assertIn("core/photos.py", provenance["routed_by"])
        self.assertTrue(any("core/verify.py" in line
                            for line in provenance["gated_by"]))

    async def test_a_refused_proposal_prints_the_guard_on_the_record(self) -> None:
        """Rail 3, on the wire: the refusal is evidence, not a swallowed log."""
        with self._read(self._lipid()), self._ask(
                _proposal("lab_slip", "l-done", "the potassium loop")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")

        packet = self._routing_event().meta["evidence_packet"]
        self.assertEqual("l1", packet["loop_id"], "the table's choice stands")
        self.assertEqual(1, len(packet["refused"]))
        self.assertIn("l-done", packet["refused"][0])
        self.assertIn("code guard refused", packet["refused"][0])
        # The outcome agrees with the table because the table is what ran; the
        # proposal that did not is on the record beside it.
        self.assertTrue(packet["agreed_with_code"])
        self.assertIs(self._routing_event().meta["attached"], True)
        self.assertEqual("l1", self._routing_event().loop_id)

    async def test_a_critical_slip_the_agent_calls_other_stays_on_the_lab_lane(
            self) -> None:
        """The blocker, driven: a diverting answer cannot take the table away.

        The relay lane never calls core/labs.assess, so a potassium of 6.4 that
        the orchestrator answered "other" for would have become a yellow
        "passed on unread" card with no values on it and nothing said to the
        patient. `candidate_kinds` no longer offers "other" for a page whose
        rows parsed, so the guard refuses the kind, the lab lane runs, and the
        critical-value table and the escalation run with it.
        """
        reading = slip(patient_name=self.PRINTED_NAME, taken_on="30/08/2026",
                       analytes=[{"analyte": "Potassium", "value": "6.4",
                                  "unit": "mmol/L", "flag": "H"}])
        with self._read(reading), self._ask(
                _proposal("other", "", "this looks like a pharmacy page")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")

        packet = self._routing_event().meta["evidence_packet"]
        self.assertEqual("lab_slip", packet["kind"])
        self.assertIn(packet["route"], ("attach_to_loop", "unexpected_result"),
                      "the lab lane is the only lane a parsed slip can take")
        self.assertTrue(any("candidate kinds" in why
                            for why in packet["refused"]),
                        packet["refused"])

        # The value table ran, and it ran on the doctor's phone and the record.
        self.assertIn("escalation", [e.kind for e in self.fake.events])
        self.assertTrue(any("critical" in e.text.lower()
                            for e in self.fake.events),
                        [e.text for e in self.fake.events])
        self.assertTrue(any("CRITICAL LAB" in (card.card.get("title") or "")
                            for card in self._doctor_cards()),
                        [c.card.get("title") for c in self._doctor_cards()])
        # And nothing anywhere was passed on unread.
        self.assertFalse(any("unread" in (m.text or "")
                             for _ref, m in self.out.sent))
        told = [m.text for ref, m in self.out.sent if ref.startswith("patient:")]
        self.assertTrue(any("123" in (t or "") for t in told), told)

    async def test_a_monitor_screen_carries_its_packet_too(self) -> None:
        self.fake.loops["m1"] = Loop(
            id="m1", patient_id="p1", doctor_id="d", type="MONITOR",
            title="Blood pressure", state="open",
            details={"metric": "BP", "schedule": "twice a day", "days": 7},
            created_at=NOW - timedelta(days=1), updated_at=NOW)
        reading = slip(kind="bp_monitor", systolic="118", diastolic="76",
                       pulse="70")
        with self._read(reading), self._ask(_proposal("bp_monitor", "m1")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")

        row = next(e for e in self.fake.events if "monitor reading" in e.text)
        self.assertEqual("monitor_reading", row.meta["route"])
        self.assertEqual(self.extractor.DECIDED_EVIDENCE_AGENT,
                         row.meta["decided_by"])
        self.assertEqual("m1", row.meta["evidence_packet"]["loop_id"])
        self.assertEqual(1, len(self.fake.loops["m1"].readings or []))


# --------------------------------------------------------------------------- #
# Rail 2: the verifier is downstream, in code, and it wins
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheVerifierOverrulesTheAgent(_PhotoPath):
    async def test_a_nominated_loop_still_detaches_on_an_identity_mismatch(self
                                                                          ) -> None:
        with self._read(self._lipid(printed_name="Mohamed Sayed")), self._ask(
                _proposal("lab_slip", "l1", "the only open lipid panel")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")

        event = self._routing_event()
        self.assertEqual("l1", event.meta["evidence_packet"]["loop_id"])
        self.assertIs(event.meta["attached"], False,
                      "the agent nominated a loop and code refused to attach")
        self.assertIsNone(event.loop_id)
        self.assertEqual("mismatch", event.meta["verify"]["identity"])
        self.assertFalse(event.meta["verify"]["attaches"])

        self.assertTrue(any(
            e.kind == "escalation" and "identity check failed" in e.text
            for e in self.fake.events))
        card = self._doctor_cards()[-1].card
        self.assertIn("Identity mismatch", card["title"])
        self.assertEqual([], [a["id"] for a in card["actions"]
                              if a["id"].startswith(("attach:", "openloop:"))])
        self.assertEqual([], self.fake.loops["l1"].results or [])
        self.assertEqual("open", self.fake.loops["l1"].state)

    async def test_a_slip_from_before_the_order_never_satisfies_it(self) -> None:
        """The date check is code and it runs after the disposition."""
        with self._read(self._lipid(taken_on="01/01/2026")), self._ask(
                _proposal("lab_slip", "l1")):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")
        verdict = self._routing_event().meta["verify"]
        self.assertEqual("before_order", verdict["dated"])
        self.assertFalse(verdict["satisfies"])
        self.assertNotEqual("pending_review", self.fake.loops["l1"].state)

    async def test_the_agent_has_no_way_to_speak_to_the_verifier_at_all(self
                                                                       ) -> None:
        """A structural rail: nothing in core/evidence.py can call verify.check.

        It may read `required_analytes` and `missing_analytes`, which are the
        contract as the doctor wrote it. The verdict itself is made in
        core/extractor.py, after this file has finished.
        """
        tree = ast.parse((CORE / "evidence.py").read_text(encoding="utf-8"))
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and getattr(node.func.value, "id", "") == "verify"
        }
        self.assertEqual({"required_analytes", "missing_analytes"}, called)


# --------------------------------------------------------------------------- #
# Rails 4 and 5: nothing that mattered before this agent moved
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class LegacyRoutingIsUntouched(_PhotoPath):
    async def _run(self, reading, ask):
        """One scenario on a fresh harness, returning its whole shape."""
        self.setUp()
        with self._read(reading), ask:
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")
        return self._shape(), list(self.fake.events)

    @staticmethod
    def _down(kind: str):
        from core import evidence

        async def ask(_facts):
            raise TimeoutError("the orchestrator did not answer")

        async def never(_facts):
            return None

        return patch.object(evidence, "_ask",
                            ask if kind == "raises" else never)

    async def test_a_model_that_times_out_routes_exactly_as_the_code_did(self
                                                                        ) -> None:
        reading = self._lipid()
        legacy, _ = await self._run(reading, self._down("none"))
        timed_out, events = await self._run(reading, self._down("raises"))
        self.assertEqual(legacy, timed_out)
        for row in events:
            self.assertNotIn("evidence_packet", row.meta)

    async def test_the_fail_open_path_writes_the_label_it_always_wrote(self
                                                                      ) -> None:
        await self._run(self._lipid(), self._down("raises"))
        self.assertEqual(self.extractor.DECIDED_LAB_EVENT,
                         self._routing_event().meta["decided_by"])
        self.assertEqual(
            [self.extractor.DECIDED_LABS],
            [m.meta["decided_by"] for m in self._doctor_cards()])

    async def test_an_agreeing_agent_changes_nothing_but_the_packet(self) -> None:
        """The whole of the S24 diff on an ordinary slip: one meta key, one label."""
        reading = self._lipid()
        legacy, _ = await self._run(reading, self._down("none"))
        agreed, _ = await self._run(reading, self._ask(_proposal("lab_slip", "l1")))
        self.assertEqual(legacy, agreed)

    async def test_wrong_patient_behaviour_is_byte_identical(self) -> None:
        """Rail 4. The quarantine is the same quarantine it was."""
        reading = self._lipid(printed_name="Mohamed Sayed")
        legacy, _ = await self._run(reading, self._down("none"))
        with_agent, _ = await self._run(
            reading, self._ask(_proposal("lab_slip", "l1")))
        self.assertEqual(legacy, with_agent)

    async def test_an_unreadable_photo_never_reaches_the_agent_at_all(self
                                                                     ) -> None:
        from core import evidence

        seen: list = []

        async def spy(facts):
            seen.append(facts)
            return None

        async def unreadable(image, mime="image/jpeg"):
            return None, {"error": "the model returned nothing readable"}

        with patch.object(self.extractor, "read_photo", unreadable), \
                patch.object(evidence, "decide", spy):
            await self.extractor.handle_photo(self.patient, self.doctor, b"x")
        self.assertEqual([], seen)
        self.assertTrue(any("relayed unread" in e.text
                            for e in self.fake.events))


# --------------------------------------------------------------------------- #
# Rail 1: the Sentinel is upstream of the photograph and of this agent
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheSentinelStillRunsFirst(unittest.IsolatedAsyncioTestCase):
    async def test_a_code_sentinel_hit_never_reaches_the_photo_or_the_agent(
            self) -> None:
        from core import chaser, concierge, escalate, evidence, extractor

        photos_read: list = []
        turns: list = []

        async def handle_photo(*args, **kwargs):
            photos_read.append(args)

        async def decide(facts):
            turns.append(facts)
            return None

        out = Recorder()
        doctor = Doctor(id="d", name="Dr Mohamed", specialty="cardiology",
                        lang="en", web_token="tok", created_at=NOW)
        patient = Patient(id="p1", doctor_id="d", name="Mona Said", sex="f",
                          channels={"web": True}, created_at=NOW)

        with patch.object(extractor, "handle_photo", handle_photo), \
                patch.object(evidence, "decide", decide), \
                patch.object(concierge, "fanout", lambda: out), \
                patch.object(concierge.events, "append_event",
                             AsyncMock(return_value=SimpleNamespace(id="e1"))), \
                patch.object(chaser, "note_patient_reply", AsyncMock()), \
                patch.object(chaser, "revive_unreachable", AsyncMock()), \
                patch.object(escalate, "told_or_fail_closed",
                             AsyncMock(return_value=True)):
            await concierge.handle_patient_message(
                patient, doctor, "I have terrible chest pain",
                image_bytes=b"x")

        self.assertEqual([], photos_read, "a photo was read after an emergency")
        self.assertEqual([], turns, "the orchestrator ran after an emergency")
        self.assertTrue(any("123" in m.text for m in out.to("patient:")))

    def test_the_source_order_keeps_the_gate_in_front_of_the_agent(self) -> None:
        """The statement order is the guarantee (tests/test_gate_order.py)."""
        concierge = (CORE / "concierge.py").read_text(encoding="utf-8")
        turn = concierge.split("async def handle_patient_message", 1)[1].split(
            "async def open_relay", 1)[0]
        self.assertLess(turn.index("gate.fired"), turn.index("if image_bytes"))

        extractor = (CORE / "extractor.py").read_text(encoding="utf-8")
        claimed = extractor.split("async def _handle_photo_claimed", 1)[1].split(
            "async def escalate_bp", 1)[0]
        # The picture is read, then routed by the table, then the turn happens.
        for earlier, later in (("read_photo(image)", "photos.route("),
                               ("photos.route(", "evidence.decide(")):
            self.assertLess(claimed.index(earlier), claimed.index(later))

        # And the verifier is downstream of all three: it is not called on the
        # routing path at all, only inside `_handle_lab`, after it.
        tree = ast.parse((CORE / "extractor.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_handle_photo_claimed"):
                continue
            calls = {
                call.func.attr for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and getattr(call.func.value, "id", "") == "verify"
            }
            self.assertEqual(set(), calls)
            break
        else:  # pragma: no cover - the function has to exist
            self.fail("core/extractor.py has no _handle_photo_claimed")


# --------------------------------------------------------------------------- #
# The label, and the audit taxonomy it has to survive
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheLabelSaysWhoDecided(unittest.TestCase):
    def test_the_two_copies_of_the_label_cannot_drift(self) -> None:
        from core import evidence, extractor

        self.assertEqual(evidence.DECIDED_BY, extractor.DECIDED_EVIDENCE_AGENT)

    def test_it_names_the_orchestrator_and_the_gates(self) -> None:
        from core import evidence

        self.assertTrue(evidence.DECIDED_BY.startswith(
            "evidence-orchestrator (gemini) + gates"))
        self.assertIn("core/verify.py", evidence.DECIDED_BY)

    def test_it_is_never_bucketed_as_decided_by_a_model_alone(self) -> None:
        """rev 18 item 3: the count that has to stay zero."""
        from core import evidence

        self.assertEqual("model", bucket(evidence.DECIDED_BY))

    def test_the_fallback_label_is_pure_code(self) -> None:
        from core import extractor

        for label in (extractor.DECIDED_ROUTE, extractor.DECIDED_LAB_EVENT,
                      extractor.DECIDED_UNREAD):
            with self.subTest(label=label):
                self.assertEqual("code", bucket(label))
        self.assertEqual(extractor.DECIDED_ROUTE,
                         extractor.route_decided_by(None))
        self.assertEqual(extractor.DECIDED_EVIDENCE_AGENT,
                         extractor.route_decided_by({"kind": "lab_slip"}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
