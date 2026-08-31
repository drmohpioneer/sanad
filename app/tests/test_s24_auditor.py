"""S24: the Closure Auditor, and the things it is not allowed to do.

The auditor is the only agent in Sanad whose whole job is to say no, so every
rail here is written as the failure it is there to catch rather than as the
feature it is there to prove:

  1. it may refuse           a named gap holds the Coordinator's close open,
                             the gap is on the trail, and the loop is left
                             exactly as it was found;
  2. it may never approve    a close core/verify.py refused is refused here in
                             code before the model is reached, and core/policy.py
                             has already refused it upstream of that, so such a
                             close never reaches the auditor as approvable;
  3. it writes no state      the module calls no store, no event writer and no
                             outbound send, checked as syntax and not as prose;
  4. it fails open           an outage, a timeout or an exception closes the
                             loop exactly as the system did before this file
                             existed, with one log line;
  5. doctor authority        "Reviewed" is the doctor's own tap on his own
                             patient's card, danger card included, and it is
                             never held up. A gap a model named still lands on
                             the trail, which is what lets him reopen it;
  6. nothing it says is text a model-authored gap is flattened, capped and
                             deduplicated before it reaches an event or a
                             sentence, and it is never registered as a template
                             a patient's message could be rendered from;
  7. nobody is named         no patient name and no free text from the verifier
                             is in the payload, by construction and not by the
                             order the fields happen to be written in.

Off the cohort there is no agent at all: a doctor who was never enrolled in the
v2 facts closes exactly as he did before this file existed, and pays none of
this turn's deadline for it.

The model is mocked at one seam, `core.auditor._ask`, which is the same shape
the rest of the suite mocks core/intents.model_vote and core/validator._yes_no
with. Nothing here reaches the cloud.
"""

from __future__ import annotations

import ast
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import auditor, bounded, monitoring, policy, templates, timing
from core.models import Doctor, Loop, Patient

APP_ROOT = Path(__file__).resolve().parents[1]
AUDITOR_SOURCE = (APP_ROOT / "core" / "auditor.py").read_text(encoding="utf-8")
CONCIERGE_SOURCE = (APP_ROOT / "core" / "concierge.py").read_text(encoding="utf-8")
COORDINATOR_SOURCE = (APP_ROOT / "core" / "coordinator.py").read_text(
    encoding="utf-8")

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)

GAP = "evening BP day 6 missing"

# The value printed on the slip in every fixture below. It has to reach the
# model: an auditor that cannot see a single number is not auditing anything.
PRINTED = "180"


def doctor(enrolled: bool = True) -> Doctor:
    return Doctor(id="d", name="Dr Mohamed", web_token="t", created_at=NOW,
                  workspace_facts_enabled=enrolled)


def patient() -> Patient:
    return Patient(id="p", doctor_id="d", name="Ahmed Ali", sex="male",
                   created_at=NOW)


def lab_loop(**fields) -> Loop:
    base = dict(
        id="l", patient_id="p", doctor_id="d", type="TEST",
        title="Lipid panel", details={"test_name": "Lipid panel"},
        state="pending_review", due_at=NOW + timedelta(days=5),
        # The keys are core/extractor.py's own. A fixture written to a key the
        # product does not write is a fixture that proves nothing.
        results=[{"analyte": "LDL", "value": PRINTED, "unit": "mg/dL",
                  "ref_range": "0-100", "flag": "H", "level": "above_target",
                  "target": None, "baseline": None,
                  "line": f"LDL {PRINTED} mg/dL"}],
        doctor_reviewed=True, created_at=NOW, updated_at=NOW,
    )
    base.update(fields)
    return Loop(**base)


# The doctor asked on this day, at half past midnight Cairo, so the seven days
# core/monitoring.py counts are seven whole local days and the slot names it
# prints are the ones a reader would name themselves.
ASKED_ON = datetime(2026, 8, 25, 0, 30, tzinfo=timing.CAIRO)


def week_of_readings(skip: tuple[int, int] = (5, 20)) -> list[dict]:
    """Twice a day for a week, minus one slot. Default: the evening of day 6."""
    rows = []
    for day in range(7):
        for hour in (8, 20):
            if (day, hour) == skip:
                continue
            at = (ASKED_ON + timedelta(days=day)).replace(hour=hour, minute=0)
            rows.append({"at": at.astimezone(timezone.utc).isoformat(),
                         "value": "138/85", "number": 138.0})
    return rows


def monitor_loop(readings) -> Loop:
    return Loop(
        id="m", patient_id="p", doctor_id="d", type="MONITOR",
        title="Blood pressure twice daily",
        details={"metric": "blood pressure", "schedule": "twice daily",
                 "days": 7},
        state="pending_review", due_at=NOW + timedelta(days=7),
        readings=readings, doctor_reviewed=True,
        created_at=ASKED_ON.astimezone(timezone.utc),
        updated_at=NOW,
    )


async def refuses(_facts):
    return False, GAP


async def passes(_facts):
    return True, ""


def event(text: str, loop_id: str = "l") -> SimpleNamespace:
    return SimpleNamespace(text=text, loop_id=loop_id)


# --------------------------------------------------------------------------- #
# The auditor on its own
# --------------------------------------------------------------------------- #
class TheAuditorAnswersAndNothingElse(unittest.IsolatedAsyncioTestCase):
    async def test_a_named_gap_comes_back_as_a_held_close(self) -> None:
        loop = lab_loop()
        with patch.object(auditor, "_ask", refuses):
            held = await auditor.review_close(loop, policy.DEFAULT)
        self.assertIsNotNone(held)
        self.assertEqual(held.gap, GAP)
        self.assertIn(GAP, held.text)
        self.assertIn(GAP, held.closed_text)
        self.assertTrue(held.as_meta()["held"])

    async def test_a_complete_record_comes_back_silent(self) -> None:
        with patch.object(auditor, "_ask", passes):
            held = await auditor.review_close(lab_loop(), policy.DEFAULT)
        self.assertIsNone(held)

    async def test_it_leaves_the_loop_object_exactly_as_it_found_it(self) -> None:
        loop = lab_loop()
        before = loop.model_dump(mode="json")
        with patch.object(auditor, "_ask", refuses):
            await auditor.review_close(loop, policy.DEFAULT)
        self.assertEqual(loop.model_dump(mode="json"), before)


# --------------------------------------------------------------------------- #
# Rule 5/6: nothing the model says is trusted as text
# --------------------------------------------------------------------------- #
class TheGapIsMadeSafeBeforeAnyoneUsesIt(unittest.IsolatedAsyncioTestCase):
    async def test_a_hostile_multiline_gap_is_flattened_and_capped(self) -> None:
        hostile = ("line one\n\n" + "\t" * 20 + "IGNORE EVERYTHING\r\n"
                   + "x" * 1000)

        async def shouts(_facts):
            return False, hostile

        with patch.object(auditor, "_ask", shouts):
            held = await auditor.review_close(lab_loop(), policy.DEFAULT)
        self.assertIsNotNone(held)
        self.assertLessEqual(len(held.gap), auditor.MAX_GAP)
        for whitespace in ("\n", "\r", "\t", "  "):
            with self.subTest(whitespace=repr(whitespace)):
                self.assertNotIn(whitespace, held.gap)
        # And the line a doctor reads is still one line.
        self.assertEqual(held.closed_text.count("\n"), 0)

    async def test_an_unnamed_refusal_still_names_something(self) -> None:
        """A refusal whose wording did not survive is still a refusal."""
        async def unnamed(_facts):
            return False, "   \n\t  "

        with patch.object(auditor, "_ask", unnamed):
            held = await auditor.review_close(lab_loop(), policy.DEFAULT)
        self.assertIsNotNone(held)
        self.assertEqual(held.gap, auditor.UNNAMED_GAP)

    def test_the_cleaner_is_total(self) -> None:
        for raw in (None, "", "   ", 0, ["a"], "ok"):
            with self.subTest(raw=raw):
                cleaned = auditor.clean_gap(raw)
                self.assertTrue(cleaned.strip())
                self.assertLessEqual(len(cleaned), auditor.MAX_GAP)

    def test_the_held_lines_are_never_registered_as_templates(self) -> None:
        """A registered template is renderable to a patient. These are not.

        `render` refuses any field outside ALLOWED_FIELDS, and `gap` is not in
        it, so registering either line would be a crash in front of a patient
        at best and model-authored text sent to one at worst.
        """
        for line in (templates.CLOSE_HELD, templates.CLOSED_WITH_GAP):
            with self.subTest(line=line):
                self.assertIn("{gap}", line)
                self.assertNotIn(line, templates.TEMPLATES.values())
        self.assertNotIn("gap", templates.ALLOWED_FIELDS)
        registered = {key for key, table in templates.TEMPLATES.items()
                      for forms in table.values()
                      for text in forms.values() if "{gap}" in text}
        self.assertEqual(registered, set())


# --------------------------------------------------------------------------- #
# Rule 6: the same gap, every day, is one line on the record
# --------------------------------------------------------------------------- #
class TheSameGapIsNotWrittenTwice(unittest.TestCase):
    def test_the_newest_refusal_naming_this_gap_stops_another(self) -> None:
        self.assertTrue(auditor.already_noted(
            GAP, ["something else", f"{auditor.REFUSED}{GAP}"]))

    def test_a_different_gap_is_a_new_line(self) -> None:
        self.assertFalse(auditor.already_noted(
            GAP, [f"{auditor.REFUSED}morning BP day 2 missing"]))

    def test_a_loop_that_has_never_been_refused_is_not_deduplicated(self) -> None:
        self.assertFalse(auditor.already_noted(GAP, []))
        self.assertFalse(auditor.already_noted(GAP, ["reviewed and closed"]))

    def test_only_the_newest_refusal_counts(self) -> None:
        """It was refused for this, then for something else. Say it again."""
        self.assertFalse(auditor.already_noted(GAP, [
            f"{auditor.REFUSED}{GAP}",
            f"{auditor.REFUSED}morning BP day 2 missing",
        ]))


# --------------------------------------------------------------------------- #
# Rail 2: it may never approve what the code verifier refused
# --------------------------------------------------------------------------- #
class ItCanNeverApproveAnUnverifiedClose(unittest.IsolatedAsyncioTestCase):
    async def test_a_verifier_failure_is_refused_without_asking_the_model(
            self) -> None:
        asked: list = []

        async def never(facts):
            asked.append(facts)
            return True, ""       # the worst answer a model could give here

        loop = lab_loop(verified={"satisfies": False, "identity": "match",
                                  "missing": ["HbA1c"]})
        with patch.object(auditor, "_ask", never):
            held = await auditor.review_close(loop, policy.DEFAULT)
        self.assertEqual(asked, [], "the model was asked about a refused close")
        self.assertIsNotNone(held)
        self.assertEqual(held.gap, auditor.NOT_VERIFIED)
        self.assertFalse(held.as_meta()["asked_the_model"])

    def test_the_guard_upstream_refuses_it_before_a_turn_can_execute(
            self) -> None:
        """core/policy.py is supreme and unmoved: such a close is not a call."""
        facts = policy.LoopFacts(now=NOW, has_evidence=True,
                                 doctor_reviewed=True, verified_satisfies=False)
        decision = policy.check("close_verified_loop", {}, facts)
        self.assertTrue(decision.refused)

    def test_the_auditor_is_asked_inside_execute_and_never_before_the_guard(
            self) -> None:
        """_execute only ever runs on a decision core/policy.py accepted."""
        execute = COORDINATOR_SOURCE.split("async def _execute", 1)[1]
        branch = execute.split('elif tool == "close_verified_loop":', 1)[1]
        self.assertIn("auditor.review_close(", branch)
        self.assertEqual(COORDINATOR_SOURCE.count("auditor.review_close("), 1)
        choose = COORDINATOR_SOURCE.split("async def _choose", 1)[1].split(
            "async def choose", 1)[0]
        self.assertNotIn("auditor", choose)

    def test_the_two_labels_it_can_write_never_say_a_model_decided_alone(
            self) -> None:
        """The dashboard's bucketing rule, applied to this file's own labels."""
        from tests.test_decided_by import bucket

        self.assertEqual(bucket(auditor.DECIDED_BY_AUDITOR), "model")
        self.assertEqual(bucket(auditor.DECIDED_BY_VERIFIER), "code")


# --------------------------------------------------------------------------- #
# Rail 3: it writes no state
# --------------------------------------------------------------------------- #
class ItWritesNothingItself(unittest.TestCase):
    def test_the_module_calls_no_store_no_event_writer_and_no_send(self) -> None:
        forbidden = {"store", "events", "fanout", "tasks", "chaser", "adapters",
                     "outbox"}
        tree = ast.parse(AUDITOR_SOURCE)
        touched: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in forbidden:
                    touched.append(f"{node.value.id}.{node.attr}")
            if isinstance(node, ast.Name) and node.id in forbidden:
                touched.append(node.id)
        self.assertEqual(sorted(set(touched)), [])

    def test_it_imports_nothing_that_could_write(self) -> None:
        tree = ast.parse(AUDITOR_SOURCE)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported.update(alias.name for alias in node.names)
        for name in ("store", "events", "tasks", "adapters", "outbox",
                     "chaser", "report"):
            with self.subTest(name=name):
                self.assertNotIn(name, imported)


# --------------------------------------------------------------------------- #
# Rail 4: model down, timed out, or broken. The close proceeds.
# --------------------------------------------------------------------------- #
class AModelThatCannotAnswerIsNotAGate(unittest.IsolatedAsyncioTestCase):
    async def test_an_exception_is_a_pass_and_one_log_line(self) -> None:
        async def broken(_facts):
            raise RuntimeError("vertex is down")

        with patch.object(auditor, "_ask", broken):
            with self.assertLogs("sanad.auditor", level="WARNING") as logs:
                held = await auditor.review_close(lab_loop(), policy.DEFAULT)
        self.assertIsNone(held)
        self.assertEqual(len(logs.records), 1)

    async def test_a_timeout_is_a_pass(self) -> None:
        async def hangs(_facts):
            raise bounded.TimedOut("the closure auditor", bounded.VOTE)

        with patch.object(auditor, "_ask", hangs):
            self.assertIsNone(await auditor.review_close(lab_loop(),
                                                         policy.DEFAULT))

    async def test_no_model_client_at_all_is_a_pass(self) -> None:
        """The hermetic suite is an outage, and the whole suite proves it."""
        with self.assertLogs("sanad.auditor", level="INFO") as logs:
            held = await auditor.review_close(lab_loop(), policy.DEFAULT)
        self.assertIsNone(held)
        self.assertEqual(len(logs.records), 1)
        self.assertFalse(auditor._model_ready())

    async def test_a_broken_record_is_a_pass_and_not_a_crash(self) -> None:
        """Fact-building throws on a shape it has never seen. Still a pass."""
        class Nothing:
            pass

        self.assertIsNone(await auditor.review_close(Nothing(), policy.DEFAULT))


# --------------------------------------------------------------------------- #
# The facts it is given are counted in code, and carry nobody's name
# --------------------------------------------------------------------------- #
class TheFactsAreBuiltByCode(unittest.TestCase):
    def payload(self, loop, time_scale=None) -> str:
        """What actually goes over the wire, not what the dict looks like."""
        return json.dumps(auditor.facts_for_close(loop, policy.DEFAULT,
                                                  time_scale),
                          ensure_ascii=False, sort_keys=True, default=str)

    def test_the_slips_own_values_reach_the_prompt(self) -> None:
        facts = auditor.facts_for_close(lab_loop(), policy.DEFAULT)
        row = facts["results_on_the_record"][0]
        self.assertEqual(row["analyte"], "LDL")
        self.assertEqual(row["value"], PRINTED)
        self.assertEqual(row["unit"], "mg/dL")
        self.assertIn(PRINTED, self.payload(lab_loop()))

    def test_a_monitoring_loop_carries_the_slot_that_is_missing(self) -> None:
        facts = auditor.facts_for_close(monitor_loop(week_of_readings()),
                                        policy.DEFAULT)
        counted = facts["monitoring"]
        self.assertEqual(counted["expected"], 14)
        self.assertEqual(counted["received"], 13)
        self.assertEqual(counted["missing"], "evening on day 6")
        # The count comes from core/monitoring.py, not from this file.
        self.assertEqual(
            counted,
            monitoring.summary(monitor_loop(week_of_readings())).as_dict())

    def test_the_scale_is_handed_through_to_the_slot_count(self) -> None:
        """wave A F11: real days would count days nobody was asked on."""
        loop = monitor_loop(week_of_readings())
        real = auditor.facts_for_close(loop, policy.DEFAULT)["monitoring"]
        rehearsal = auditor.facts_for_close(loop, policy.DEFAULT,
                                            60)["monitoring"]
        self.assertEqual(real, monitoring.summary(loop).as_dict())
        self.assertEqual(rehearsal, monitoring.summary(loop, 60).as_dict())
        self.assertNotEqual(real, rehearsal)

    def test_the_contract_the_doctor_confirmed_is_what_is_audited(self) -> None:
        facts = auditor.facts_for_close(lab_loop(), policy.DEFAULT)
        self.assertIn("Lipid panel", facts["contract"]["objective"])
        self.assertIn("due 2026-09-05", facts["contract"]["deadline"]["in_words"])
        self.assertEqual(facts["results_count"], 1)
        self.assertEqual(facts["verifier"],
                         "the verifier never saw this loop")

    def test_nobody_is_named_in_the_facts(self) -> None:
        """The patient's name is not a fact the auditor needs, so it is not one
        it is given."""
        payload = self.payload(lab_loop())
        self.assertNotIn("Ahmed", payload)
        self.assertNotIn("Mohamed", payload)

    def test_the_verifiers_own_prose_never_reaches_the_prompt(self) -> None:
        """core/verify.py quotes the printed name in its reasons. It is data
        this audit does not need, so the shape it is given is an allowlist of
        coded fields and not a list of fields to remember to remove."""
        loop = lab_loop(verified={
            "satisfies": True, "identity": "match",
            "identity_why": "the slip prints Ahmed Ali and the record says "
                            "Ahmed Ali",
            "dated": "ok", "required": ["ldl"], "missing": [],
            "attaches": True,
            "reasons": ["identity: the slip prints Ahmed Ali"],
            "some_future_why": "Ahmed Ali again",
        })
        payload = self.payload(loop)
        self.assertNotIn("Ahmed", payload)
        self.assertNotIn("identity_why", payload)
        self.assertNotIn("reasons", payload)
        self.assertNotIn("some_future_why", payload)
        # The coded verdict still arrives whole.
        verdict = auditor.facts_for_close(loop, policy.DEFAULT)["verifier"]
        self.assertEqual(verdict, {"satisfies": True, "identity": "match",
                                   "dated": "ok", "required": ["ldl"],
                                   "missing": [], "attaches": True})


# --------------------------------------------------------------------------- #
# Rail 1: the Coordinator's own close
# --------------------------------------------------------------------------- #
try:
    from core import concierge, coordinator          # noqa: F401
    from core import events as events_module
    from core import settings as settings_module
    from core import store as store_module
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheCoordinatorsCloseGoesThroughIt(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.loop = lab_loop()
        self.written: list = []
        self.closed: list = []
        self.history: list = []
        outer = self

        async def append_event(doctor_id, kind, text="", **fields):
            outer.written.append((text, fields.get("meta", {})))
            outer.history.append(event(text, fields.get("loop_id", "")))
            return None

        async def last_events(doctor_id, *a, **kw):
            return list(outer.history)

        async def update_loop(loop_id, **fields):
            outer.closed.append(("update_loop", fields))
            for key, value in fields.items():
                setattr(outer.loop, key, value)

        async def close_loop(loop_id, **fields):
            outer.closed.append(("close_loop", fields))
            outer.loop.state = "done"

        self.patches = [
            patch.object(events_module, "append_event", append_event),
            patch.object(events_module, "last_events", last_events),
            patch.object(store_module, "update_loop", update_loop),
            patch.object(store_module, "close_loop", close_loop),
            patch.object(store_module, "now", lambda: NOW),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def turn(self, enrolled: bool = True) -> "coordinator.Turn":
        return coordinator.Turn(
            doctor=doctor(enrolled), patient=patient(), loop=self.loop,
            trigger=coordinator.REPLY,
            facts=policy.LoopFacts(now=NOW, has_evidence=True,
                                   doctor_reviewed=True),
            policy=policy.DEFAULT,
        )

    async def close(self, enrolled: bool = True) -> dict:
        turn = self.turn(enrolled)
        decision = policy.check("close_verified_loop", {}, turn.facts,
                                reason="the doctor reviewed it")
        self.assertTrue(decision.allowed)
        return await coordinator._execute(turn, decision)

    def refusals(self) -> list:
        return [row for row in self.written
                if row[0].startswith(auditor.REFUSED)]

    async def test_a_refusal_holds_the_close_and_names_the_gap(self) -> None:
        with patch.object(auditor, "_ask", refuses):
            answer = await self.close()
        self.assertEqual(self.closed, [], "the loop was closed against a gap")
        self.assertEqual(self.loop.state, "pending_review")
        self.assertNotIn("state", answer["detail"])
        self.assertEqual(answer["detail"]["held"], GAP)

        self.assertEqual(len(self.refusals()), 1)
        text, meta = self.refusals()[0]
        self.assertIn(GAP, text)
        self.assertEqual(meta["decided_by"], auditor.DECIDED_BY_AUDITOR)
        self.assertIn(GAP, meta["note"])

    async def test_three_held_closes_with_the_same_gap_write_one_event(
            self) -> None:
        for _ in range(3):
            with patch.object(auditor, "_ask", refuses):
                answer = await self.close()
            self.assertEqual(self.loop.state, "pending_review")
        self.assertEqual(len(self.refusals()), 1)
        self.assertFalse(answer["detail"]["noted"])
        # A different gap is still worth a line.
        async def other(_facts):
            return False, "morning BP day 2 missing"

        with patch.object(auditor, "_ask", other):
            await self.close()
        self.assertEqual(len(self.refusals()), 2)

    async def test_a_pass_closes_exactly_as_today(self) -> None:
        with patch.object(auditor, "_ask", passes):
            answer = await self.close()
        self.assertEqual([kind for kind, _ in self.closed], ["close_loop"])
        self.assertEqual(self.loop.state, "done")
        self.assertEqual(answer["detail"]["state"], "done")
        self.assertEqual(self.refusals(), [])

    async def test_a_model_outage_closes_exactly_as_today(self) -> None:
        async def broken(_facts):
            raise RuntimeError("vertex is down")

        with patch.object(auditor, "_ask", broken):
            answer = await self.close()
        self.assertEqual(self.loop.state, "done")
        self.assertEqual(answer["detail"]["state"], "done")

    async def test_a_doctor_off_the_cohort_never_constructs_the_model_turn(
            self) -> None:
        """No agent, no deadline, and the legacy write, exactly as before."""
        asked: list = []

        async def never(facts):
            asked.append(facts)
            return False, GAP

        with patch.object(auditor, "_ask", never):
            answer = await self.close(enrolled=False)
        self.assertEqual(asked, [])
        self.assertEqual([kind for kind, _ in self.closed], ["update_loop"])
        self.assertEqual(self.loop.state, "done")
        self.assertEqual(answer["detail"]["state"], "done")
        self.assertEqual(self.refusals(), [])

    async def test_a_monitoring_loop_with_a_missing_slot_is_refused(self) -> None:
        self.loop = monitor_loop(week_of_readings())
        seen: list = []

        async def reads_the_slots(facts):
            seen.append(facts)
            return False, f"missing {facts['monitoring']['missing']}"

        with patch.object(auditor, "_ask", reads_the_slots):
            answer = await self.close()
        self.assertEqual(self.closed, [])
        self.assertEqual(self.loop.state, "pending_review")
        self.assertIn("evening", answer["detail"]["held"])
        self.assertEqual(seen[0]["monitoring"]["received"], 13)


# --------------------------------------------------------------------------- #
# Rail 5: the doctor's own tap
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheDoctorsTapIsNeverHeldUp(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.loop = lab_loop(doctor_reviewed=False)
        self.written: list = []
        self.closed: list = []
        self.sent: list = []
        self.scale = 86400
        outer = self

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text))
                return None

        async def append_event(doctor_id, kind, text="", **fields):
            outer.written.append((text, fields.get("meta", {})))
            return None

        async def get_loop(loop_id):
            return outer.loop

        async def get_patient(patient_id):
            return patient()

        async def update_loop(loop_id, **fields):
            outer.closed.append(("update_loop", fields))
            for key, value in fields.items():
                setattr(outer.loop, key, value)

        async def close_loop(loop_id, **fields):
            outer.closed.append(("close_loop", fields))
            outer.loop.state = "done"
            outer.loop.doctor_reviewed = True

        async def send_if_complete(*a, **kw):
            return None

        async def current():
            return "run1", outer.scale

        self.patches = [
            patch.object(concierge, "fanout", lambda: Fanout()),
            patch.object(concierge.events, "append_event", append_event),
            patch.object(concierge.report, "send_if_complete", send_if_complete),
            patch.object(settings_module, "current", current),
            patch.object(store_module, "get_loop", get_loop),
            patch.object(store_module, "get_patient", get_patient),
            patch.object(store_module, "update_loop", update_loop),
            patch.object(store_module, "close_loop", close_loop),
            patch.object(store_module, "now", lambda: NOW),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def noted(self) -> list:
        return [row for row in self.written if GAP in row[0]]

    async def test_a_gap_never_blocks_the_v2_close_and_lands_on_the_trail(
            self) -> None:
        with patch.object(auditor, "_ask", refuses):
            await concierge.mark_reviewed(doctor(), "l")
        self.assertEqual([kind for kind, _ in self.closed], ["close_loop"])
        self.assertTrue(self.closed[0][1]["doctor_reviewed"])
        self.assertEqual(self.loop.state, "done")
        self.assertEqual(len(self.noted()), 1)
        self.assertEqual(self.noted()[0][1]["decided_by"],
                         auditor.DECIDED_BY_AUDITOR)
        # He is told, in the wording of a close that happened.
        self.assertIn(GAP, self.sent[-1][1])
        self.assertIn("Closed.", self.sent[-1][1])
        self.assertNotIn("completing the record first", self.sent[-1][1])

    async def test_a_doctor_off_the_cohort_never_constructs_the_model_turn(
            self) -> None:
        """The legacy write, and no agent anywhere near his tap."""
        asked: list = []

        async def never(facts):
            asked.append(facts)
            return False, GAP

        with patch.object(auditor, "_ask", never):
            await concierge.mark_reviewed(doctor(enrolled=False), "l")
        self.assertEqual(asked, [])
        self.assertEqual([kind for kind, _ in self.closed], ["update_loop"])
        self.assertTrue(self.loop.doctor_reviewed)
        self.assertEqual(self.noted(), [])
        self.assertEqual(self.sent[-1][1], "Lipid panel: closed.")

    async def test_the_verifiers_own_refusal_is_on_the_trail_and_not_in_his_face(
            self) -> None:
        """It fires on every slip that prints no name, which is most of them.

        The card he just tapped already carries the verifier's own line, so
        repeating it as "Sanad is completing the record first" would be both
        noise and untrue: the loop closed.
        """
        self.loop = lab_loop(doctor_reviewed=False,
                             verified={"satisfies": False,
                                       "identity": "not_printed"})
        asked: list = []

        async def never(facts):
            asked.append(facts)
            return True, ""

        with patch.object(auditor, "_ask", never):
            await concierge.mark_reviewed(doctor(), "l")
        self.assertEqual(asked, [])
        self.assertEqual([kind for kind, _ in self.closed], ["close_loop"])
        self.assertEqual(self.sent[-1][1], "Lipid panel: closed.")
        trail = [row for row in self.written
                 if auditor.NOT_VERIFIED in row[0]]
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0][1]["decided_by"], auditor.DECIDED_BY_VERIFIER)

    async def test_a_pass_leaves_his_tap_exactly_as_it_was(self) -> None:
        with patch.object(auditor, "_ask", passes):
            await concierge.mark_reviewed(doctor(), "l")
        self.assertEqual(self.sent[-1][1], "Lipid panel: closed.")
        self.assertEqual(self.noted(), [])

    async def test_the_slots_are_counted_at_the_scale_the_reminders_used(
            self) -> None:
        """wave A F11 again, at the one call site that could have missed it."""
        self.scale = 60
        seen: dict = {}

        async def review_close(loop, pol, *, time_scale=None):
            seen["time_scale"] = time_scale
            return None

        with patch.object(auditor, "review_close", review_close):
            await concierge.mark_reviewed(doctor(), "l")
        self.assertEqual(seen["time_scale"], 60)

    async def test_the_auditor_is_asked_before_the_write_not_after(self) -> None:
        reviewed = CONCIERGE_SOURCE.split("async def mark_reviewed", 1)[1]
        self.assertLess(reviewed.index("auditor.review_close("),
                        reviewed.index("store.close_loop("))
        self.assertLess(reviewed.index("auditor.review_close("),
                        reviewed.index("store.update_loop("))
        self.assertEqual(reviewed.count("auditor.review_close("), 1)

    async def test_the_review_flag_is_still_set_in_exactly_one_place(self) -> None:
        self.assertEqual(CONCIERGE_SOURCE.count("doctor_reviewed=True"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
