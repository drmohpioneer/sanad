"""The Resolver: what it may do about a barrier, and what it cannot do at all.

S19. Three things are tested here, and the middle one is the reason this file
is long:

  the routing table   every row of it, as `check` sees it, driven as pure
                      functions with nothing installed and no key. A row of a
                      table is a claim about behaviour only if something runs it.
  the tool loop       one barrier end to end, with `core/places.search` faked,
                      so that "it found three labs and sent them" and "it found
                      nothing and handed the barrier over with what it tried"
                      are both asserted as what actually reaches the patient
                      and the doctor.
  the fail-soft path  no MAPS_API_KEY is the state this machine is in and may
                      be the state the deployment is in. It has to produce a
                      card, not an exception, and the card has to say the search
                      could not run rather than that nothing was found.

Half of these read the source the way tests/test_coordinator.py does, because
the guarantee IS the shape of the code: a tool that ran the search inside itself
would be a model that holds the name of a real place, and that is exactly the
thing that must not exist.

The other half imports core/resolver.py, which reaches the cloud SDK through
core/coordinator.py and core/concierge.py, so it skips on a laptop that has
none and runs in the image.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core import places, policy, resolver, steward, summary, templates

# core/resolver.py imports nothing but pure modules at module scope, on purpose,
# so the routing table below runs with no cloud SDK installed. core/registrar.py
# does not: it builds an ADK agent at import. The one class that reads it is
# gated the way tests/test_due_dates.py gates the same import.
try:  # pragma: no cover - the image build always has it
    from core import registrar
    REGISTRAR_MISSING = ""
except Exception as exc:  # pragma: no cover
    registrar = None
    REGISTRAR_MISSING = f"cloud SDK not installed: {exc}"

APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (APP_ROOT / "core" / "resolver.py").read_text(encoding="utf-8")
PLACES = (APP_ROOT / "core" / "places.py").read_text(encoding="utf-8")
CONCIERGE = (APP_ROOT / "core" / "concierge.py").read_text(encoding="utf-8")
COORDINATOR = (APP_ROOT / "core" / "coordinator.py").read_text(encoding="utf-8")

TOOL_SECTION = SOURCE.split("# The tools.", 1)[1].split("TOOL_FUNCTIONS = (", 1)[0]
EXECUTE = SOURCE.split("async def _execute(", 1)[1].split(
    "async def _resume(", 1)[0]

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)


def facts(**fields) -> resolver.Facts:
    fields.setdefault("now", NOW)
    fields.setdefault("due_at", NOW + timedelta(days=5))
    return resolver.Facts(**fields)


# --------------------------------------------------------------------------- #
# The routing table, row by row. Pure: no SDK, no key, no records.
# --------------------------------------------------------------------------- #
class TheRoutingTable(unittest.TestCase):
    def test_the_five_classes_are_the_ones_the_spec_names(self) -> None:
        self.assertEqual(set(resolver.ROUTES), {
            "availability", "transport", "cost", "forgot", "in_hospital"})

    def test_the_three_the_resolver_never_touches(self) -> None:
        """asymptomatic, refuses and unclear are the patient arguing with the
        treatment or Sanad failing to read him. Those are the doctor's."""
        for barrier in ("asymptomatic", "refuses", "unclear"):
            with self.subTest(barrier=barrier):
                self.assertNotIn(barrier, resolver.ROUTES)
                for tool in resolver.TOOLS:
                    verdict = resolver.check(tool, {}, facts(barrier=barrier))
                    self.assertTrue(verdict.allowed is False)

    # -- availability ------------------------------------------------------ #
    def test_availability_asks_for_the_area_when_it_is_unknown(self) -> None:
        verdict = resolver.check("ask_patient", {"asks": "area"},
                                 facts(barrier="availability"))
        self.assertTrue(verdict.allowed)

    def test_availability_does_not_ask_for_an_area_it_already_has(self) -> None:
        verdict = resolver.check("ask_patient", {"asks": "area"},
                                 facts(barrier="availability", area="Zagazig"))
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.why, resolver.ALREADY_KNOWN)

    def test_availability_searches_for_a_lab_that_is_open_now(self) -> None:
        verdict = resolver.check("find_places", {},
                                 facts(barrier="availability", area="Zagazig",
                                       loop_type="TEST"))
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.args, {"kind": "lab", "area": "Zagazig",
                                        "open_now": True, "cheap": False})

    def test_a_medication_barrier_searches_for_a_pharmacy(self) -> None:
        verdict = resolver.check("find_places", {},
                                 facts(barrier="availability", area="Mansoura",
                                       loop_type="MEDICATION"))
        self.assertEqual(verdict.args["kind"], "pharmacy")

    def test_no_area_means_ask_first_and_search_second(self) -> None:
        verdict = resolver.check("find_places", {},
                                 facts(barrier="availability"))
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.why, resolver.NO_AREA)

    # -- transport --------------------------------------------------------- #
    def test_transport_searches_the_area_he_named_and_not_for_an_open_one(
            self) -> None:
        """"Nearest" is his own area, because Sanad never holds a location and
        so has no radius to be sane about."""
        verdict = resolver.check("find_places", {},
                                 facts(barrier="transport", area="Minya"))
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.args["area"], "Minya")
        self.assertFalse(verdict.args["open_now"])
        self.assertFalse(verdict.args["cheap"])

    def test_transport_asks_for_the_area_too(self) -> None:
        self.assertTrue(resolver.check(
            "ask_patient", {"asks": "area"}, facts(barrier="transport")).allowed)

    # -- cost -------------------------------------------------------------- #
    def test_cost_asks_about_a_public_lab_and_never_about_an_area(self) -> None:
        allowed = resolver.check("ask_patient", {"asks": "public_lab"},
                                 facts(barrier="cost"))
        refused = resolver.check("ask_patient", {"asks": "area"},
                                 facts(barrier="cost"))
        self.assertTrue(allowed.allowed)
        self.assertFalse(refused.allowed)
        self.assertIn("public_lab", refused.why)

    def test_there_is_no_question_about_money_left_to_ask(self) -> None:
        """The doctor's rule: Sanad never asks a patient for a figure.

        It is a refusal and not a missing template, so a model that asks for
        one anyway is told no rather than sending a sentence that does not
        exist.
        """
        self.assertNotIn("budget", resolver.ASKS)
        verdict = resolver.check("ask_patient", {"asks": "budget"},
                                 facts(barrier="cost"))
        self.assertFalse(verdict.allowed)
        self.assertIn("not something the Resolver may ask about", verdict.why)

    def test_cost_searches_the_public_sector(self) -> None:
        verdict = resolver.check("find_places", {},
                                 facts(barrier="cost", area="Shubra",
                                       public_lab="yes"))
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.args["cheap"])

    def test_the_cost_search_waits_for_the_answer_to_its_own_question(self
                                                                     ) -> None:
        """Being sent to a government hospital without being asked is exactly
        the assumption a patient who said "it is too expensive" did not make."""
        verdict = resolver.check("find_places", {},
                                 facts(barrier="cost", area="Shubra"))
        self.assertFalse(verdict.allowed)
        self.assertEqual(resolver.NO_PUBLIC_LAB, verdict.why)

    def test_cost_is_a_route_the_resolver_has_and_the_flag_still_decides(
            self) -> None:
        """The conservative default escalates cost; a doctor may opt in.

        The default remains part of the frozen Gate 0B characterization. The
        policy flag still gives each doctor an explicit, tested choice.
        """
        self.assertIn("cost", resolver.ROUTES)
        self.assertEqual(("cost",), policy.DEFAULT.escalate_only())
        self.assertEqual(
            (), policy.parse({"cost_escalate_only": False}).escalate_only())
        self.assertIn("if barrier in turn.policy.escalate_only():", SOURCE)

    # -- forgot and in_hospital -------------------------------------------- #
    def test_forgot_has_nothing_to_ask_and_nothing_to_find(self) -> None:
        for barrier in ("forgot", "in_hospital"):
            with self.subTest(barrier=barrier):
                asked = resolver.check("ask_patient", {"asks": "area"},
                                       facts(barrier=barrier))
                found = resolver.check("find_places", {},
                                       facts(barrier=barrier, area="Faiyum"))
                self.assertEqual(asked.why, resolver.NOTHING_TO_ASK)
                self.assertEqual(found.why, resolver.NOT_THIS_ROUTE)

    def test_forgot_puts_the_chase_back_on_the_queue(self) -> None:
        verdict = resolver.check("resume_chase", {"days": 2},
                                 facts(barrier="forgot"))
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.when, NOW + timedelta(days=2))

    def test_a_resume_outside_the_doctors_window_is_refused(self) -> None:
        for days, why in ((0, "not before tomorrow"),
                          (99, "the doctor's window")):
            with self.subTest(days=days):
                verdict = resolver.check("resume_chase", {"days": days},
                                         facts(barrier="forgot"))
                self.assertFalse(verdict.allowed)
                self.assertIn(why, verdict.why)

    # -- the visit --------------------------------------------------------- #
    def test_a_visit_is_never_redirected_to_another_doctor(self) -> None:
        verdict = resolver.check("find_places", {},
                                 facts(barrier="availability", area="Helwan",
                                       loop_type="VISIT"))
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.why, resolver.NO_CLINIC)

    def test_a_day_inside_the_doctors_window_is_allowed(self) -> None:
        verdict = resolver.check(
            "reschedule_visit", {"new_date": "2026-09-01"},
            facts(barrier="transport", loop_type="VISIT"))
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.args["new_date"], "2026-09-01")
        self.assertEqual(verdict.when.date().isoformat(), "2026-09-01")

    def test_a_day_past_the_window_is_refused_and_says_why(self) -> None:
        """Due 2026-09-03, grace seven days: 2026-09-20 is outside."""
        verdict = resolver.check(
            "reschedule_visit", {"new_date": "2026-09-20"},
            facts(barrier="transport", loop_type="VISIT"))
        self.assertFalse(verdict.allowed)
        self.assertIn("end of the doctor's window", verdict.why)

    def test_today_is_refused_and_tomorrow_is_not(self) -> None:
        today = resolver.check("reschedule_visit", {"new_date": "2026-08-29"},
                               facts(barrier="transport", loop_type="VISIT"))
        tomorrow = resolver.check("reschedule_visit",
                                  {"new_date": "2026-08-30"},
                                  facts(barrier="transport", loop_type="VISIT"))
        self.assertEqual(today.why, "not before tomorrow")
        self.assertTrue(tomorrow.allowed)

    def test_a_date_that_is_not_a_date_is_refused_rather_than_guessed(
            self) -> None:
        for bad in ("Monday", "", None, "next week", "2026-13-45"):
            with self.subTest(bad=bad):
                verdict = resolver.check(
                    "reschedule_visit", {"new_date": bad},
                    facts(barrier="transport", loop_type="VISIT"))
                self.assertFalse(verdict.allowed)

    def test_a_test_loop_has_no_day_to_move(self) -> None:
        verdict = resolver.check("reschedule_visit",
                                 {"new_date": "2026-09-01"},
                                 facts(barrier="transport", loop_type="TEST"))
        self.assertEqual(verdict.why, resolver.NOT_A_VISIT)

    # -- the exit ---------------------------------------------------------- #
    def test_the_hand_over_is_always_allowed(self) -> None:
        for barrier in resolver.ROUTES:
            with self.subTest(barrier=barrier):
                verdict = resolver.check("hand_to_doctor", {"barrier": barrier},
                                         facts(barrier=barrier, asked=9,
                                               searched=9))
                self.assertTrue(verdict.allowed)

    def test_an_unreadable_class_on_the_hand_over_becomes_the_real_one(
            self) -> None:
        verdict = resolver.check("hand_to_doctor", {"barrier": "nonsense"},
                                 facts(barrier="cost"))
        self.assertEqual(verdict.args["barrier"], "cost")

    def test_a_tool_that_is_not_a_tool_is_refused(self) -> None:
        verdict = resolver.check("change_the_dose", {"mg": 80},
                                 facts(barrier="cost"))
        self.assertEqual(verdict.why, resolver.UNKNOWN_TOOL)


class OneQuestionPerBarrier(unittest.TestCase):
    """The cap the spec asks for in code, and the reset that makes it fair."""

    def test_the_second_question_on_one_barrier_is_refused(self) -> None:
        verdict = resolver.check("ask_patient", {"asks": "public_lab"},
                                 facts(barrier="cost", asked=1))
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.why, resolver.ONE_QUESTION)

    def test_the_cap_is_one_and_it_is_a_constant_not_a_sentence(self) -> None:
        self.assertEqual(resolver.MAX_QUESTIONS, 1)

    def test_a_third_search_on_one_barrier_is_refused(self) -> None:
        """Two is the cap: the first, and the wider one `_search` makes by
        itself when the first came back empty."""
        self.assertTrue(resolver.check(
            "find_places", {},
            facts(barrier="cost", area="Shubra", public_lab="yes",
                  searched=1)).allowed)
        verdict = resolver.check("find_places", {},
                                 facts(barrier="cost", area="Shubra",
                                       public_lab="yes", searched=2))
        self.assertEqual(verdict.why, resolver.ONE_SEARCH)

    def test_a_new_barrier_gets_its_question_back(self) -> None:
        """One question about one problem, not one question for ever."""
        loop = SimpleNamespace(resolver={"barrier": "cost", "asked": 1,
                                         "solved": True, "tried": ["x"]})
        state = resolver.state_of(loop, "availability")
        self.assertEqual(state["asked"], 0)
        self.assertEqual(state["tried"], [])
        self.assertFalse(state["solved"])

    def test_the_same_barrier_keeps_what_it_spent(self) -> None:
        loop = SimpleNamespace(resolver={"barrier": "cost", "asked": 1,
                                         "public_lab": "yes", "tried": ["x"]})
        state = resolver.state_of(loop, "cost")
        self.assertEqual(state["asked"], 1)
        self.assertEqual(state["public_lab"], "yes")

    def test_a_loop_that_was_never_worked_starts_clean(self) -> None:
        state = resolver.state_of(SimpleNamespace(), "cost")
        self.assertEqual(state["asked"], 0)
        self.assertFalse(state["handed_over"])


class TheAnswerToTheCostQuestion(unittest.TestCase):
    """The yes/no fork, as a pure function. No SDK, no key, no records.

    This is what replaced a question about a budget, so it is tested the way
    the routing table is: every branch of it, driven directly.
    """

    def test_a_plain_no_in_either_language_is_a_refusal(self) -> None:
        for said in ("no", "No.", "nope", "no thanks", "not really",
                     "I would rather not", "لا", "لأ", "لأ شكرا",
                     "مش عايز", "مش هينفع", "لا مش عايز حكومي",
                     "la2", "mesh 3ayez"):
            with self.subTest(said=said):
                self.assertTrue(resolver.declined_public_lab(said))

    def test_a_yes_and_everything_that_is_not_a_no_searches(self) -> None:
        """The wrong way to be wrong here is to hand over a barrier Sanad
        could have answered, so only a refusal is a refusal."""
        for said in ("yes", "yes please", "ok", "تمام", "ماشي", "ايوه",
                     "اه ينفع", "أي حاجة قريبة", "Nasr City", "?", ""):
            with self.subTest(said=said):
                self.assertFalse(resolver.declined_public_lab(said))

    def test_no_problem_is_a_yes_and_not_a_no(self) -> None:
        """The word no begins the commonest agreement there is, in both
        languages, and a substring list without this reads it backwards."""
        for said in ("no problem", "لا مشكلة", "مفيش مشكلة", "ولا يهمك"):
            with self.subTest(said=said):
                self.assertFalse(resolver.declined_public_lab(said))

    def test_it_lands_on_word_edges_and_not_inside_a_word(self) -> None:
        for said in ("now is fine", "nothing stops me", "لازم اروح"):
            with self.subTest(said=said):
                self.assertFalse(resolver.declined_public_lab(said))


class WhatTheLoopIsWaitingFor(unittest.TestCase):
    """core/concierge.py asks this before anything else reads a reply."""

    def test_an_open_question_claims_the_next_message(self) -> None:
        loop = SimpleNamespace(resolver={"barrier": "cost",
                                         "asks": "public_lab"})
        self.assertEqual(resolver.waiting_for(loop), "public_lab")

    def test_a_barrier_that_is_finished_claims_nothing(self) -> None:
        for state in ({"asks": "public_lab", "solved": True},
                      {"asks": "public_lab", "handed_over": True},
                      {"asks": ""}, {}):
            with self.subTest(state=state):
                self.assertEqual(
                    resolver.waiting_for(SimpleNamespace(resolver=state)), "")


# --------------------------------------------------------------------------- #
# The summary, which is the only thing outside this slice that changed
# --------------------------------------------------------------------------- #
class ASolvedBarrierIsProgressingAndOnlyAHandOverNeedsTheDoctor(
        unittest.TestCase):
    def loop(self, **fields):
        fields.setdefault("id", "l")
        fields.setdefault("patient_id", "p")
        fields.setdefault("state", "waiting_patient")
        fields.setdefault("barrier", "availability")
        fields.setdefault("paused", False)
        fields.setdefault("results", [])
        fields.setdefault("readings", [])
        fields.setdefault("resolver", {})
        return SimpleNamespace(**fields)

    def test_a_barrier_the_resolver_answered_is_still_progressing(self) -> None:
        loop = self.loop(resolver={"barrier": "availability", "solved": True,
                                   "handed_over": False})
        self.assertEqual(summary.classify(loop, set()), "progressing")

    def test_a_barrier_it_handed_over_is_the_doctors(self) -> None:
        loop = self.loop(resolver={"barrier": "transport", "solved": False,
                                   "handed_over": True})
        self.assertEqual(summary.classify(loop, set()), "needs_help")

    def test_a_barrier_with_a_question_outstanding_is_not_the_doctors_yet(
            self) -> None:
        """The Resolver asked and the patient has not answered. Nobody is
        waiting on the doctor, so his Inbox must not say one is."""
        loop = self.loop(resolver={"barrier": "cost", "asks": "public_lab",
                                   "solved": False, "handed_over": False})
        self.assertEqual(summary.classify(loop, set()), "progressing")

    def test_a_barrier_nobody_worked_is_what_it_always_was(self) -> None:
        """Every barrier before S19, and every barrier on a deployment with the
        Resolver switched off."""
        self.assertEqual(summary.classify(self.loop(), set()), "needs_help")

    def test_a_paused_loop_needs_the_doctor_whatever_else_it_carries(self) -> None:
        loop = self.loop(paused=True, resolver={"barrier": "cost",
                                                "solved": True})
        self.assertEqual(summary.classify(loop, set()), "needs_help")

    def test_a_critical_value_still_wins_over_everything(self) -> None:
        loop = self.loop(resolver={"barrier": "cost", "solved": True})
        self.assertEqual(summary.classify(loop, {"l"}), "critical")

    def test_the_classifier_is_still_total(self) -> None:
        import itertools

        loops = [
            self.loop(id=f"l{i}", state=state, barrier=barrier, paused=paused,
                      resolver=({"solved": solved, "handed_over": handed}
                                if barrier else {}))
            for i, (state, barrier, paused, solved, handed) in enumerate(
                itertools.product(
                    ("open", "waiting_patient", "received", "pending_review",
                     "done", "unreachable"),
                    ("", "cost", "transport"), (False, True), (False, True),
                    (False, True)))
        ]
        counts = summary.compute(loops)
        self.assertEqual(sum(counts.buckets.values()), len(loops))
        self.assertEqual(counts.lost, 0)

    def test_a_board_that_has_never_met_the_resolver_reads_as_it_did(self) -> None:
        """Resolver integration does not silently rewrite frozen seed data.

        The Gate 0B replay serializes these records, so seeded areas and solved
        Resolver records must be introduced only through an explicit baseline
        review. Every existing seeded loop therefore keeps its classification.
        """
        from core import background

        _, loops, _, _ = background.records("testdoctor00000000", NOW)
        for loop in loops:
            with self.subTest(loop=loop.title):
                self.assertEqual({}, loop.resolver)
                self.assertFalse(summary.resolver_holds(loop))
        counts = background.expected("testdoctor00000000", NOW)
        self.assertEqual(counts.lost, 0)


# --------------------------------------------------------------------------- #
# The area, which the patient answers for himself in this tree
# --------------------------------------------------------------------------- #
class TheAreaIsAskedForAndNeverInvented(unittest.TestCase):
    """An area has one implemented source: the patient's own answer.

    The Resolver asks once and stores that response. It does not invent an
    area, and this build does not extract one from doctor dictation.
    """

    def test_the_only_writer_of_an_area_is_the_answer_to_the_question(self
                                                                     ) -> None:
        self.assertIn("await store.update_patient(patient.id, area=answer)",
                      SOURCE)
        other = [name for name in ("registrar.py", "extractor.py",
                                   "identify.py", "concierge.py")
                 if "update_patient(" in
                 (APP_ROOT / "core" / name).read_text(encoding="utf-8")
                 and "area=" in
                 (APP_ROOT / "core" / name).read_text(encoding="utf-8")]
        self.assertEqual([], other)

    def test_a_patient_starts_with_no_area_and_that_is_a_supported_state(self
                                                                        ) -> None:
        from core.models import Patient

        person = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                         created_at=NOW)
        self.assertEqual("", person.area)
        # And an empty area is omitted from the wire, so a record that never
        # met the Resolver serializes as it always did.
        self.assertNotIn("area", person.model_dump())
        self.assertIn("area", person.model_copy(
            update={"area": "Nasr City"}).model_dump())


# --------------------------------------------------------------------------- #
# Source rails: the shape of the code IS the guarantee
# --------------------------------------------------------------------------- #
class TheToolSurface(unittest.TestCase):
    def test_the_five_tools_exist_and_there_is_no_sixth(self) -> None:
        for name in resolver.TOOLS:
            with self.subTest(name=name):
                self.assertIn(f"async def {name}(", TOOL_SECTION)
        self.assertEqual(TOOL_SECTION.count("async def "), len(resolver.TOOLS))

    def test_no_tool_writes_sends_or_searches_anything_itself(self) -> None:
        """The model chooses; code decides, and then code acts. A tool that ran
        the search inside itself would be a model that holds the name of a real
        place, which is the one thing this slice must not allow."""
        for forbidden in ("places.search", "store.update_loop", "fanout()",
                          "append_event", "tasks.enqueue", "update_patient"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, TOOL_SECTION)

    def test_every_tool_goes_through_the_guards(self) -> None:
        self.assertEqual(TOOL_SECTION.count("propose("), len(resolver.TOOLS))

    def test_the_search_happens_after_the_turn_has_ended(self) -> None:
        self.assertIn("await places.search(", EXECUTE)

    def test_only_one_action_per_barrier_turn(self) -> None:
        propose = SOURCE.split("    def propose(", 1)[1].split(
            "_attempt:", 1)[0]
        self.assertIn("if self.verdict is not None:", propose)
        self.assertIn("ONE_ACTION", propose)

    def test_the_guard_is_registered_with_the_framework_as_well(self) -> None:
        choose = SOURCE.split("async def _choose(", 1)[1]
        self.assertIn("before_tool_callback=before_tool", choose)
        self.assertEqual(tuple(resolver.GUARD_ARGS), resolver.TOOLS)

    def test_the_registered_tools_are_the_declared_list_in_order(self) -> None:
        self.assertEqual(tuple(f.__name__ for f in resolver.TOOL_FUNCTIONS),
                         resolver.TOOLS)

    def test_it_fails_soft_into_the_path_that_existed_before_it(self) -> None:
        choose = SOURCE.split("async def choose(", 1)[1].split(
            "# Reading and writing", 1)[0]
        self.assertIn("except Exception:", choose)
        self.assertIn("return None", choose)
        self.assertIn("asyncio.wait_for", SOURCE)

    def test_the_coordinator_asks_it_before_it_escalates(self) -> None:
        branch = COORDINATOR.split('elif tool == "classify_barrier":', 1)[1]
        self.assertLess(branch.index("resolver.handoff("),
                        branch.index("turn.policy.escalate_only()"))
        self.assertIn("if worked is not None:", branch)

    def test_the_patient_never_reads_a_sentence_a_model_wrote(self) -> None:
        sends = SOURCE.count('f"patient:{turn.patient.id}"')
        self.assertEqual(sends, 1)     # `_tell`, and nothing else
        self.assertIn("templates.render(key", SOURCE)

    def test_the_places_block_is_appended_and_never_interpolated(self) -> None:
        """No template carries a field an address could go into, so the block
        is its own paragraph under the sentence, the way the plan is."""
        tell = SOURCE.split("async def _tell(", 1)[1].split(
            "# ------", 1)[0]
        self.assertIn('text = f"{text}\\n{block}"', tell)
        self.assertEqual(templates.ALLOWED_FIELDS,
                         {"patient", "doctor", "date", "analyte"})

    def test_the_concierge_claims_an_answer_before_it_reads_a_reading(
            self) -> None:
        turn = CONCIERGE.split("async def handle_patient_message", 1)[1].split(
            "async def open_relay", 1)[0]
        self.assertLess(turn.index("resolver.on_answer("),
                        turn.index("if not change_reason and not is_reading(text):"))
        self.assertLess(turn.index("if image_bytes"),
                        turn.index("resolver.on_answer("))


class EveryResolverEventSaysWhoDecidedIt(unittest.TestCase):
    def test_the_label_carries_a_model_and_code_and_never_a_model_alone(
            self) -> None:
        label = resolver.DECIDED_BY_RESOLVER.lower()
        self.assertIn("model", label)
        self.assertIn("code", label)

    def test_the_audit_line_names_the_resolver_and_not_the_coordinator(
            self) -> None:
        line = resolver.Verdict(tool="find_places", allowed=True,
                                reason="the lab is closed").audit()
        self.assertTrue(line.startswith("resolver: find_places accepted"))
        self.assertIn("decided_by:", line)


# The rest imports what drives a whole turn, which reaches the cloud SDK. The
# image has it and a laptop may not, so this half skips there.
try:
    from core import concierge, coordinator  # noqa: F401
    from core.models import Doctor, Loop, Patient
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class OneBarrierEndToEnd(unittest.IsolatedAsyncioTestCase):
    """The tool loop, with the model stubbed and the search faked.

    Everything the Resolver touches is faked here, so what is being tested is
    what actually reaches the patient, what reaches the doctor, and what is
    written on the loop and the event.
    """

    def setUp(self) -> None:
        from core import adapters as adapters_module
        from core import events as events_module, lang, settings
        from core import store as store_module
        from core import tasks as tasks_module
        from unittest.mock import patch

        self.sent: list = []
        self.written: list = []
        self.queued: list = []
        self.relays: dict = {}
        self.doctor = Doctor(id="d", name="Dr Mohamed", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", area="Nasr City", created_at=NOW)
        self.loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                         title="Lipid panel", details={"test_name": "Lipid panel"},
                         state="waiting_patient", due_at=NOW + timedelta(days=5),
                         created_at=NOW, updated_at=NOW)
        outer = self

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.card))
                return f"event/{len(outer.sent)}"

        async def update_loop(loop_id, **fields):
            for key, value in fields.items():
                setattr(outer.loop, key, value)

        async def update_patient(patient_id, **fields):
            for key, value in fields.items():
                setattr(outer.patient, key, value)

        async def bump_schedule_version(loop_id):
            outer.loop.schedule_version = int(outer.loop.schedule_version or 0) + 1
            return outer.loop.schedule_version

        async def save_relay(relay):
            outer.relays[relay.id] = relay
            return relay

        async def append_event(doctor_id, kind, text="", **kw):
            outer.written.append((kind, text, kw.get("meta", {})))
            return SimpleNamespace(id=f"ev/{len(outer.written)}")

        async def enqueue(path, payload, delay):
            outer.queued.append((path, payload, delay))
            return f"task/{len(outer.queued)}"

        async def current():
            return "run1", 86400

        async def for_patient(*a, **kw):
            return "en"

        ledger: list = []

        async def add_contact(loop_id, day_index):
            outer.loop.contacts = int(outer.loop.contacts or 0) + 1

        async def note_contact(patient_id, doctor_id, day_index, kind,
                               loop_id=""):
            ledger.append(kind)
            return len(ledger)

        async def contact_days_for_patient(patient_id):
            return ()

        async def list_loops(patient_id):
            return [outer.loop]

        self.kinds = ledger
        self.search = places.fake()
        self.patches = [
            # core/resolver.py imports the adapters inside `_tell`, so that the
            # routing table above runs with nothing installed. The patch goes
            # where the name really lives.
            patch.object(adapters_module, "fanout", lambda: Fanout()),
            patch.object(coordinator, "fanout", lambda: Fanout()),
            patch.object(concierge, "fanout", lambda: Fanout()),
            patch.object(places, "search", self.search),
            # Adapted for this tree. core/resolver.py reads the same
            # `_sanad_hermetic` probe core/auditor.py reads before it builds an
            # agent, because the hermetic boundary raises a BaseException out
            # of the GenAI client that `choose` could not catch. The tests that
            # drive a model turn say there is one; the tests that assert the
            # fail-soft path leave it alone and get exactly what a deployment
            # with no model gets.
            patch.object(resolver, "_model_ready", lambda: True),
            patch.object(store_module, "update_loop", update_loop),
            patch.object(store_module, "update_patient", update_patient),
            patch.object(store_module, "add_contact", add_contact),
            patch.object(store_module, "note_contact", note_contact),
            patch.object(store_module, "contact_days_for_patient",
                         contact_days_for_patient),
            patch.object(store_module, "bump_schedule_version",
                         bump_schedule_version),
            patch.object(store_module, "save_relay", save_relay),
            patch.object(store_module, "list_loops", list_loops),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(events_module, "append_event", append_event),
            patch.object(tasks_module, "enqueue", enqueue),
            patch.object(settings, "current", current),
            patch.object(lang, "for_patient", for_patient),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    # -- helpers ----------------------------------------------------------- #
    async def barrier(self, tool: str, args: dict, barrier: str = "availability",
                      message: str = "The lab in my area is closed"):
        """One classify_barrier from the Coordinator, with the model stubbed."""
        from unittest.mock import patch

        turn = await coordinator._turn_for(
            self.loop, self.patient, self.doctor, coordinator.REPLY, message)
        decision = policy.check(
            "classify_barrier", {"barrier": barrier, "resume_in_days": 0},
            turn.facts, turn.policy, reason="the patient says the lab is closed")
        self.assertTrue(decision.allowed)

        async def stub(attempt):
            attempt.propose(tool, args, "the Resolver's stated reason")
            return attempt.verdict

        with patch.object(resolver, "_choose", stub):
            return await coordinator._execute(turn, decision)

    def to_patient(self) -> list:
        return [text for ref, text, card in self.sent
                if ref.startswith("patient:") and not card]

    def cards(self) -> list:
        return [card for _, _, card in self.sent if card]

    def resolver_meta(self) -> dict:
        for _, _, meta in self.written:
            if "resolver" in meta and meta["resolver"].get("tool"):
                return meta["resolver"]
        self.fail("no event carried meta.resolver")

    # -- the happy path ---------------------------------------------------- #
    async def test_three_labs_reach_the_patient_and_the_doctor_hears_nothing(
            self) -> None:
        self.search.answers.append(
            places.found("Alfa Lab", "Beta Lab", "Gamma Lab", area="Nasr City"))
        result = await self.barrier("find_places", {})
        self.assertTrue(result["answered"])
        self.assertEqual(self.cards(), [])
        told = self.to_patient()
        self.assertEqual(len(told), 1)
        self.assertTrue(told[0].startswith(
            templates.render("places_found", "en", "m")))
        for name in ("Alfa Lab", "Beta Lab", "Gamma Lab"):
            self.assertIn(name, told[0])
        self.assertIn("https://www.google.com/maps/place/", told[0])

    async def test_the_search_it_made_is_the_one_the_table_says(self) -> None:
        self.search.answers.append(places.found("Alfa Lab"))
        await self.barrier("find_places", {})
        self.assertEqual(self.search.calls, [{"kind": "lab", "area": "Nasr City",
                                              "open_now": True, "cheap": False}])

    async def test_a_solved_barrier_is_written_on_the_loop_as_solved(
            self) -> None:
        self.search.answers.append(places.found("Alfa Lab"))
        await self.barrier("find_places", {})
        self.assertTrue(self.loop.resolver["solved"])
        self.assertFalse(self.loop.resolver["handed_over"])
        self.assertTrue(resolver.solved(self.loop))
        self.assertEqual(summary.classify(self.loop, set()), "progressing")

    async def test_it_puts_the_obligation_back_on_the_queue(self) -> None:
        self.search.answers.append(places.found("Alfa Lab"))
        await self.barrier("find_places", {})
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0][0], coordinator.NUDGE_PATH)
        self.assertFalse(self.loop.paused)

    async def test_the_message_costs_one_contact_named_resolver(self) -> None:
        self.search.answers.append(places.found("Alfa Lab"))
        await self.barrier("find_places", {})
        self.assertEqual(self.kinds, ["resolver"])
        self.assertEqual(self.loop.contacts, 1)

    async def test_the_event_carries_the_shape_the_dashboard_reads(self) -> None:
        self.search.answers.append(places.found("Alfa Lab", "Beta Lab"))
        await self.barrier("find_places", {})
        meta = self.resolver_meta()
        self.assertEqual(sorted(meta), ["args", "results", "tool", "tried"])
        self.assertEqual(meta["tool"], "find_places")
        self.assertEqual(meta["results"], 2)
        self.assertEqual(meta["args"], {"kind": "lab", "area": "Nasr City",
                                        "open_now": True, "cheap": False})
        self.assertIn("2 found", meta["tried"][-1])

    async def test_every_resolver_event_says_who_decided_it(self) -> None:
        self.search.answers.append(places.found("Alfa Lab"))
        await self.barrier("find_places", {})
        labels = [meta.get("decided_by") for _, _, meta in self.written
                  if "resolver" in meta]
        self.assertTrue(labels)
        for label in labels:
            self.assertEqual(label, resolver.DECIDED_BY_RESOLVER)

    # -- failing, and then adapting ---------------------------------------- #
    async def test_an_empty_search_is_widened_once_before_anybody_gives_up(
            self) -> None:
        """The difference between an agent and a lookup, and it is code.

        "Open labs near Nasr City" found nothing, so the second search is the
        same area with "open now" relaxed, because a laboratory that is shut
        this evening is still one he can use tomorrow.
        """
        self.search.answers.append(places.Search(query="open labs"))
        self.search.answers.append(places.found("Alfa Lab", "Beta Lab",
                                                area="Nasr City"))
        result = await self.barrier("find_places", {})
        self.assertEqual(self.search.calls, [
            {"kind": "lab", "area": "Nasr City", "open_now": True,
             "cheap": False},
            {"kind": "lab", "area": "Nasr City", "open_now": False,
             "cheap": False},
        ])
        self.assertEqual(self.cards(), [])
        self.assertTrue(result["answered"])
        self.assertTrue(self.loop.resolver["solved"])
        self.assertIn("Alfa Lab", " ".join(self.to_patient()))

    async def test_both_attempts_and_both_counts_are_on_the_record(self) -> None:
        self.search.answers.append(places.Search(query="open labs in Nasr City"))
        self.search.answers.append(places.found("Alfa Lab", area="Nasr City"))
        await self.barrier("find_places", {})
        tried = self.loop.resolver["tried"]
        self.assertEqual(len(tried), 2)
        self.assertIn("nothing found", tried[0])
        self.assertIn("1 found", tried[1])

    async def test_a_widened_search_is_never_introduced_as_a_cheaper_one(
            self) -> None:
        """The cost route drops the public-sector bias on its second attempt,
        so the sentence has to stop promising a cheaper option with it."""
        # Adapted for this tree, twice over and neither of them about the
        # widening: the doctor has cleared `cost_escalate_only`, which is left
        # at its committed default here, and the patient has already answered
        # the one question the cost route asks, without which the search guard
        # refuses before any of this runs.
        self.doctor = self.doctor.model_copy(
            update={"policy": {"cost_escalate_only": False}})
        self.loop.resolver = {"barrier": "cost", "asks": "", "asked": 1,
                              "public_lab": "yes", "searched": 0, "tried": [],
                              "solved": False, "handed_over": False}
        self.search.answers.append(places.Search(query="government labs"))
        self.search.answers.append(places.found("Any Lab", area="Nasr City"))
        await self.barrier("find_places", {}, barrier="cost",
                           message="it is too expensive")
        self.assertEqual([c["cheap"] for c in self.search.calls], [True, False])
        told = self.to_patient()[-1]
        self.assertTrue(told.startswith(
            templates.render("places_found", "en", "m")))
        self.assertNotIn(templates.render("places_cheap", "en", "m"), told)

    async def test_an_area_with_nowhere_wider_says_so_instead_of_guessing(
            self) -> None:
        """Transport has no constraint left to relax, and Minya has no wider
        area Sanad can name honestly, so it hands over after one search."""
        self.patient.area = "Minya"
        self.search.answers.append(places.Search(
            query="medical laboratory in Minya"))
        await self.barrier("find_places", {}, barrier="transport",
                           message="the transport is difficult for me")
        self.assertEqual(len(self.search.calls), 1)
        lines = " ".join(self.cards()[0]["lines"])
        self.assertIn("No wider area is known for Minya.", lines)

    async def test_a_search_that_could_not_run_is_never_retried(self) -> None:
        """Asking an unreachable API twice is repeating, not adapting."""
        self.search.answers.append(places.Search(
            query="medical laboratory in Nasr City", error=places.NO_KEY))
        await self.barrier("find_places", {})
        self.assertEqual(len(self.search.calls), 1)

    async def test_two_searches_is_the_cap(self) -> None:
        self.assertEqual(resolver.MAX_SEARCHES, 2)
        self.search.answers.append(places.Search(query="open labs"))
        self.search.answers.append(places.Search(query="any labs"))
        await self.barrier("find_places", {})
        self.assertEqual(len(self.search.calls), 2)
        self.assertEqual(self.loop.resolver["searched"], 2)
        self.assertEqual(
            resolver.check("find_places", {},
                           facts(barrier="availability", area="Nasr City",
                                 searched=2)).why,
            resolver.ONE_SEARCH)

    # -- zero results ------------------------------------------------------ #
    async def test_zero_results_hands_the_barrier_over_in_the_same_turn(
            self) -> None:
        self.search.answers.append(places.Search(
            query="medical laboratory in Nasr City"))
        self.search.answers.append(places.Search(
            query="medical laboratory in Nasr City"))
        result = await self.barrier("find_places", {})
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(self.cards()[0]["severity"], "yellow")
        self.assertTrue(self.loop.resolver["handed_over"])
        self.assertFalse(resolver.solved(self.loop))
        self.assertEqual(summary.classify(self.loop, set()), "needs_help")
        self.assertTrue(result["answered"])

    async def test_the_card_prints_what_was_tried_before_the_doctor(self) -> None:
        self.search.answers.append(places.Search(
            query="medical laboratory in Nasr City"))
        await self.barrier("find_places", {})
        lines = " ".join(self.cards()[0]["lines"])
        self.assertIn("The Resolver tried this before you:", lines)
        self.assertIn("Searched for medical laboratory in Nasr City: nothing "
                      "found.", lines)

    async def test_the_hand_over_card_is_the_one_the_doctor_can_answer(
            self) -> None:
        self.search.answers.append(places.Search(query="medical laboratory"))
        await self.barrier("find_places", {})
        actions = self.cards()[0]["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["label"], "Answer")
        self.assertTrue(actions[0]["id"].startswith("reply:"))
        relay = list(self.relays.values())[-1]
        self.assertEqual(relay.loop_id, self.loop.id)
        self.assertEqual(relay.reason, "barrier: availability")

    async def test_the_escalation_event_carries_the_tool_loop_too(self) -> None:
        self.search.answers.append(places.Search(query="medical laboratory"))
        await self.barrier("find_places", {})
        escalations = [meta for kind, _, meta in self.written
                       if kind == "escalation"]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(sorted(escalations[0]["resolver"]),
                         ["args", "results", "tool", "tried"])
        self.assertEqual(escalations[0]["decided_by"],
                         resolver.DECIDED_BY_RESOLVER)

    # -- no key ------------------------------------------------------------ #
    async def test_no_maps_key_is_a_hand_over_that_says_so(self) -> None:
        """The state this machine is in. It must never read as "nothing found"."""
        self.search.answers.append(places.Search(
            query="medical laboratory in Nasr City", error=places.NO_KEY))
        await self.barrier("find_places", {})
        lines = " ".join(self.cards()[0]["lines"])
        self.assertIn("MAPS_API_KEY", lines)
        self.assertNotIn("nothing found", lines)
        self.assertTrue(self.loop.resolver["handed_over"])

    # -- the one question -------------------------------------------------- #
    async def test_it_asks_for_the_area_and_records_that_it_did(self) -> None:
        self.patient.area = ""
        await self.barrier("ask_patient", {"asks": "area"})
        self.assertEqual(self.to_patient(),
                         [templates.render("ask_area", "en", "m")])
        self.assertEqual(self.loop.resolver["asks"], "area")
        self.assertEqual(self.loop.resolver["asked"], 1)
        self.assertEqual(self.cards(), [])
        # Neither solved nor handed over: it is being worked, and the board
        # says so rather than putting it in front of the doctor.
        self.assertFalse(self.loop.resolver["solved"])
        self.assertFalse(self.loop.resolver["handed_over"])
        self.assertEqual(summary.classify(self.loop, set()), "progressing")

    async def test_a_second_question_on_the_same_barrier_is_refused(self) -> None:
        self.patient.area = ""
        await self.barrier("ask_patient", {"asks": "area"})
        self.sent.clear()
        result = await self.barrier("ask_patient", {"asks": "area"})
        # The guard refused, the Resolver chose nothing, and the S6 barrier
        # branch answered instead: no second question reached the patient.
        self.assertNotIn(templates.render("ask_area", "en", "m"),
                         self.to_patient())
        self.assertIsNotNone(result)

    async def test_the_answer_to_that_question_becomes_the_patients_area(
            self) -> None:
        from unittest.mock import patch

        self.patient.area = ""
        await self.barrier("ask_patient", {"asks": "area"})
        self.sent.clear()
        self.search.answers.append(places.found("Alfa Lab", "Beta Lab"))

        async def stub(attempt):
            attempt.propose("find_places", {}, "he told me his area")
            return attempt.verdict

        with patch.object(resolver, "_choose", stub):
            answered = await resolver.on_answer(
                self.patient, self.doctor, [self.loop], "Nasr City")
        self.assertIsNotNone(answered)
        self.assertEqual(self.patient.area, "Nasr City")
        self.assertEqual(self.search.calls[-1]["area"], "Nasr City")
        self.assertIn("Alfa Lab", " ".join(self.to_patient()))

    # -- the cost question, which is a yes or a no -------------------------- #
    async def ask_about_the_public_lab(self) -> None:
        """The one question the cost barrier gets, and the words it is sent in.

        This doctor has explicitly cleared `cost_escalate_only`, which remains
        conservative by default (see
        TheRoutingTable.test_cost_is_a_route_the_resolver_has_and_the_flag_
        still_decides).
        """
        self.doctor = self.doctor.model_copy(
            update={"policy": {"cost_escalate_only": False}})
        await self.barrier("ask_patient", {"asks": "public_lab"},
                           barrier="cost", message="It is too expensive")
        self.assertEqual(
            self.to_patient(),
            [templates.render("ask_public_lab", "en", "m")])
        self.assertEqual(self.loop.resolver["asks"], "public_lab")

    @staticmethod
    async def no_model_turn(attempt):
        raise AssertionError("a yes or a no is read in code, not by a model")

    async def test_the_cost_question_never_asks_him_what_he_can_pay(
            self) -> None:
        """The doctor's rule, as far down as the sentence that reaches him."""
        await self.ask_about_the_public_lab()
        asked = self.to_patient()[0]
        self.assertEqual(
            asked,
            "Would a public hospital lab work for you? They are usually much "
            "cheaper.")
        self.assertFalse(any(ch.isdigit() for ch in asked))

    async def test_a_yes_searches_the_public_sector_with_no_model_turn(
            self) -> None:
        """The answer to a yes/no question is a fork, and the fork is code."""
        from unittest.mock import patch

        await self.ask_about_the_public_lab()
        self.search.answers.append(places.found("Public Lab", cheap=True))
        with patch.object(resolver, "_choose", self.no_model_turn):
            answered = await resolver.on_answer(
                self.patient, self.doctor, [self.loop], "yes")
        self.assertIsNotNone(answered)
        self.assertEqual(self.loop.resolver["public_lab"], "yes")
        self.assertTrue(self.search.calls[-1]["cheap"])
        told = self.to_patient()[-1]
        self.assertTrue(told.startswith(
            templates.render("places_cheap", "en", "m")))
        self.assertEqual(self.cards(), [])

    async def test_anything_that_is_not_a_refusal_is_a_yes(self) -> None:
        """A barrier Sanad could answer is never handed over on a maybe."""
        from unittest.mock import patch

        await self.ask_about_the_public_lab()
        self.search.answers.append(places.found("Public Lab", cheap=True))
        with patch.object(resolver, "_choose", self.no_model_turn):
            await resolver.on_answer(self.patient, self.doctor, [self.loop],
                                     "tamam")
        self.assertEqual(self.loop.resolver["public_lab"], "yes")
        self.assertTrue(self.search.calls[-1]["cheap"])

    async def test_a_no_hands_it_over_and_spends_no_search_at_all(self) -> None:
        from unittest.mock import patch

        await self.ask_about_the_public_lab()
        with patch.object(resolver, "_choose", self.no_model_turn):
            answered = await resolver.on_answer(
                self.patient, self.doctor, [self.loop], "no")
        self.assertIsNotNone(answered)
        self.assertEqual(self.search.calls, [])
        self.assertEqual(self.loop.resolver["public_lab"], "no")
        self.assertTrue(self.loop.resolver["handed_over"])
        self.assertFalse(resolver.solved(self.loop))
        self.assertEqual(summary.classify(self.loop, set()), "needs_help")

    async def test_the_card_says_he_declined_the_public_laboratory(
            self) -> None:
        from unittest.mock import patch

        await self.ask_about_the_public_lab()
        with patch.object(resolver, "_choose", self.no_model_turn):
            await resolver.on_answer(self.patient, self.doctor, [self.loop],
                                     "لأ مش هينفع")
        lines = " ".join(self.cards()[-1]["lines"])
        self.assertIn("Offered a public laboratory and the patient declined",
                      lines)
        self.assertIn("The patient will not use a public laboratory.", lines)
        self.assertIn("لأ مش هينفع", lines)

    async def test_the_yes_and_the_no_are_both_on_the_record(self) -> None:
        from unittest.mock import patch

        await self.ask_about_the_public_lab()
        self.search.answers.append(places.found("Public Lab", cheap=True))
        with patch.object(resolver, "_choose", self.no_model_turn):
            await resolver.on_answer(self.patient, self.doctor, [self.loop],
                                     "yes ok")
        tried = self.loop.resolver["tried"]
        self.assertEqual(tried[0], resolver.ASKED_LINES["public_lab"])
        self.assertIn("the patient agreed, in his own words: yes ok", tried[1])

    async def test_an_answer_is_claimed_before_the_tiers_below_read_it(
            self) -> None:
        """"no" is a refusal of a public laboratory here and a refusal of the
        treatment itself everywhere else, which is a card on the doctor."""
        loop = SimpleNamespace(resolver={"barrier": "cost",
                                         "asks": "public_lab"})
        self.assertEqual(resolver.waiting_for(loop), "public_lab")
        self.assertFalse(concierge.is_reading("no"))

    async def test_a_message_with_nothing_outstanding_is_never_intercepted(
            self) -> None:
        self.assertIsNone(await resolver.on_answer(
            self.patient, self.doctor, [self.loop], "150"))

    # -- the other rows ---------------------------------------------------- #
    async def test_a_forgotten_dose_is_a_delay_and_never_a_card(self) -> None:
        result = await self.barrier("resume_chase", {"days": 2},
                                    barrier="forgot",
                                    message="I forgot last week's dose")
        self.assertTrue(result["answered"])
        self.assertEqual(self.cards(), [])
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(len(self.to_patient()), 1)
        self.assertTrue(self.loop.resolver["solved"])

    async def test_a_visit_moves_to_the_day_the_patient_asked_for(self) -> None:
        self.loop.type = "VISIT"
        result = await self.barrier("reschedule_visit",
                                    {"new_date": "2026-09-01"},
                                    barrier="transport",
                                    message="Can I come on Monday instead?")
        self.assertTrue(result["answered"])
        self.assertEqual(self.loop.due_at.date().isoformat(), "2026-09-01")
        self.assertEqual(self.cards(), [])
        self.assertEqual(len(self.queued), 1)

    async def test_a_day_outside_the_window_never_moves_anything(self) -> None:
        self.loop.type = "VISIT"
        due = self.loop.due_at
        await self.barrier("reschedule_visit", {"new_date": "2026-10-20"},
                           barrier="transport",
                           message="Can I come next month?")
        self.assertEqual(self.loop.due_at, due)

    async def test_the_model_may_give_up_and_the_card_still_says_what_it_tried(
            self) -> None:
        result = await self.barrier("hand_to_doctor", {"barrier": "transport"},
                                    barrier="transport",
                                    message="I cannot get anywhere at all")
        self.assertEqual(len(self.cards()), 1)
        self.assertTrue(self.loop.resolver["handed_over"])
        self.assertTrue(result["answered"])


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheHandoffIsFailSoft(unittest.IsolatedAsyncioTestCase):
    """None means core/coordinator.py does what it did before S19."""

    def setUp(self) -> None:
        self.doctor = Doctor(id="d", name="Dr Mohamed", web_token="t",
                             created_at=NOW)
        self.turn = SimpleNamespace(
            policy=policy.DEFAULT, doctor=self.doctor,
            loop=SimpleNamespace(id="l", type="TEST", title="Lipid panel",
                                 due_at=NOW + timedelta(days=5), resolver={}),
        )

    async def test_a_barrier_that_is_not_the_resolvers_is_left_alone(self) -> None:
        for barrier in ("asymptomatic", "refuses", "unclear", ""):
            with self.subTest(barrier=barrier):
                self.assertIsNone(
                    await resolver.handoff(self.turn, None, barrier))

    async def test_a_doctor_who_keeps_cost_for_himself_still_does(self) -> None:
        self.turn.policy = policy.parse({"cost_escalate_only": True})
        self.assertIsNone(await resolver.handoff(self.turn, None, "cost"))

    async def test_switched_off_is_the_s6_path_and_not_a_broken_one(self) -> None:
        from unittest.mock import patch

        with patch.object(resolver, "ENABLED", False):
            self.assertIsNone(
                await resolver.handoff(self.turn, None, "availability"))

    async def test_a_model_that_cannot_be_used_stands_down(self) -> None:
        attempt = resolver.Attempt(
            turn=self.turn, facts=facts(barrier="cost"), barrier="cost")

        async def exploded(_attempt):
            raise RuntimeError("the model is down")

        from unittest.mock import patch

        with patch.object(resolver, "_model_ready", lambda: True), \
                patch.object(resolver, "_choose", exploded):
            self.assertIsNone(await resolver.choose(attempt))
        self.assertTrue(attempt.model_failed)

    async def test_no_model_client_at_all_is_the_s6_path_and_writes_nothing(
            self) -> None:
        """No model client takes the explicit no-action path.

        The probe runs before an Attempt exists, so a turn where nothing was
        proposed, chosen or refused does not manufacture a Resolver event.
        """
        self.assertFalse(resolver._model_ready())
        self.assertIsNone(
            await resolver.handoff(self.turn, None, "availability"))
        self.assertIn("if not _model_ready():", SOURCE)


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ARefusedCallNeverEntersTheToolBody(unittest.IsolatedAsyncioTestCase):
    """The point of using ADK's own hook as well as the guard in the body."""

    class FakeTool:
        def __init__(self, name):
            self.name = name

    def setUp(self) -> None:
        self.entered: list = []
        self.attempt = resolver.Attempt(
            turn=SimpleNamespace(), facts=facts(barrier="cost", asked=1),
            barrier="cost")
        token = resolver._attempt.set(self.attempt)
        self.addCleanup(resolver._attempt.reset, token)

    async def as_adk_would(self, name: str, args: dict):
        answer = resolver.before_tool(tool=self.FakeTool(name), args=args,
                                      tool_context=None)
        if answer is not None:
            return answer
        self.entered.append(name)
        function = dict(zip(resolver.TOOLS, resolver.TOOL_FUNCTIONS))[name]
        return await function(**args)

    async def test_a_second_question_is_refused_before_the_body_runs(self) -> None:
        answer = await self.as_adk_would(
            "ask_patient", {"asks": "public_lab",
                            "reason": "would a public lab do"})
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["reason"], resolver.ONE_QUESTION)
        self.assertEqual(self.entered, [])
        self.assertIsNone(self.attempt.verdict)

    async def test_an_allowed_call_reaches_the_body_and_is_ruled_on_once(
            self) -> None:
        answer = await self.as_adk_would(
            "hand_to_doctor", {"barrier": "cost", "reason": "nothing left"})
        self.assertEqual(answer["status"], "accepted")
        self.assertEqual(self.entered, ["hand_to_doctor"])
        self.assertEqual(self.attempt.verdict.tool, "hand_to_doctor")
        self.assertEqual(self.attempt.refusals, [])

    async def test_a_second_different_tool_is_still_one_action(self) -> None:
        await self.as_adk_would("hand_to_doctor",
                                {"barrier": "cost", "reason": "first"})
        answer = await self.as_adk_would(
            "resume_chase", {"days": 2, "reason": "and this too"})
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["reason"], policy.ONE_ACTION)

    async def test_a_tool_with_no_barrier_in_context_is_refused_outright(
            self) -> None:
        resolver._attempt.set(None)
        answer = resolver.before_tool(tool=self.FakeTool("find_places"),
                                      args={"reason": "x"}, tool_context=None)
        self.assertEqual(answer["reason"], resolver.NO_ATTEMPT)

    async def test_a_broken_hook_falls_through_to_the_in_tool_guard(self) -> None:
        from unittest.mock import patch

        def explode(*a, **kw):
            raise RuntimeError("the hook is broken")

        with patch.object(resolver.Attempt, "precheck", explode):
            self.assertIsNone(resolver.before_tool(
                tool=self.FakeTool("find_places"), args={"reason": "x"},
                tool_context=None))


# --------------------------------------------------------------------------- #
# S24-F: the Case Steward reviews what the Resolver proposes
# --------------------------------------------------------------------------- #
class TheStewardReviewsTheResolversMoves(OneBarrierEndToEnd):
    """The Resolver goes through the Coordinator's hook, and through no other.

    Two of its five tools end in one of the Coordinator's own seven guarded
    actions: the hand-over is an `escalate_barrier` and putting the chase back
    on the queue is a `schedule_next_contact`. Both are put to the Steward by
    calling core/coordinator._stewarded, which is the same function the
    Coordinator's own proposal goes through, so the cohort gate, the bounded
    turn, the fixed sentence bank and the fail-open are all one implementation
    and cannot drift. The other three tools are not one of the seven, and
    core/steward.py rail 1 refuses to judge anything that is not: they stay
    guarded by `resolver.check` and by nothing else, which is asserted here.
    """

    def enrol(self) -> None:
        self.doctor = self.doctor.model_copy(
            update={"workspace_facts_enabled": True})

    def answers(self, verdict: str, tool: str = ""):
        from unittest.mock import patch

        async def ask(_facts):
            return verdict, tool

        return patch.object(steward, "_ask", ask)

    # -- the cohort gate, which is what protects the golden replay ---------- #
    async def test_a_doctor_off_the_cohort_never_constructs_a_steward_turn(
            self) -> None:
        from unittest.mock import patch

        def refuse(*a, **kw):
            raise AssertionError("a doctor off the cohort was reviewed")

        self.search.answers.append(places.Search(query="labs"))
        self.search.answers.append(places.Search(query="labs"))
        with patch.object(steward, "review", refuse):
            await self.barrier("find_places", {})
        for _, _, meta in self.written:
            with self.subTest(meta=meta):
                self.assertNotIn("steward", meta)

    async def test_the_hand_over_is_put_to_the_steward_on_the_cohort(self
                                                                    ) -> None:
        self.enrol()
        self.search.answers.append(places.Search(query="labs"))
        self.search.answers.append(places.Search(query="labs"))
        with self.answers(steward.APPROVE):
            await self.barrier("find_places", {})
        note = self.resolver_meta()
        self.assertTrue(note["tried"])
        line = [meta for _, _, meta in self.written if "steward" in meta]
        self.assertTrue(line, "no event carried the steward's line")
        self.assertEqual(steward.APPROVE, line[-1]["steward"]["verdict"])
        self.assertEqual(steward.AGREED, line[-1]["steward"]["note"])

    async def test_an_approved_hand_over_is_the_card_it_always_was(self) -> None:
        self.enrol()
        self.search.answers.append(places.Search(query="labs"))
        self.search.answers.append(places.Search(query="labs"))
        with self.answers(steward.APPROVE):
            await self.barrier("find_places", {})
        card = self.cards()[-1]
        self.assertTrue(card["title"].startswith("Barrier needs you"))
        self.assertIn("The Resolver tried this before you:",
                      " ".join(card["lines"]))

    async def test_a_hold_is_timing_and_never_drops_the_resume(self) -> None:
        """Rail 3, on this caller too: a hold delays when the doctor is told
        and can never delete the card, the queue row or the count.

        Driven on the resume, which is a `schedule_next_contact`: that is the
        one of this caller's two guarded tools a hold is allowed to reach.
        """
        self.enrol()
        self.search.answers.append(places.found("Alfa Lab"))
        with self.answers(steward.HOLD):
            result = await self.barrier("find_places", {})
        self.assertTrue(result["answered"])
        self.assertEqual(1, len(self.queued))
        line = [meta for _, _, meta in self.written if "steward" in meta][-1]
        self.assertEqual(steward.HOLD, line["steward"]["verdict"])
        self.assertEqual(steward.PARKED, line["steward"]["note"])

    async def test_a_hand_over_is_never_held_either(self) -> None:
        """The other half of core/policy.STEWARD_KEEPS, and the half the hold
        branch used to skip.

        The patient has already been told his doctor knows, so a card parked to
        a morning digest makes that sentence false for the rest of the day. A
        hold off a hand-over is refused by core/policy.STEWARD_NEVER_DELAYS,
        and the card goes out now.
        """
        self.enrol()
        self.assertTrue(policy.steward_never_delays("escalate_barrier"))
        self.search.answers.append(places.Search(query="labs"))
        self.search.answers.append(places.Search(query="labs"))
        with self.answers(steward.HOLD):
            await self.barrier("find_places", {})
        self.assertEqual(1, len(self.cards()))
        line = [meta for _, _, meta in self.written if "steward" in meta][-1]
        self.assertEqual(steward.APPROVE, line["steward"]["verdict"])
        self.assertEqual(steward.KEEPS_THE_TIMING, line["steward"]["note"])
        self.assertNotIn("release_at", line["steward"])

    async def test_a_hand_over_is_never_revised_away_from(self) -> None:
        """core/policy.STEWARD_KEEPS, one file upstream, and it is why this
        caller can be safe without a rule of its own."""
        self.enrol()
        self.assertTrue(policy.steward_keeps("escalate_barrier"))
        self.search.answers.append(places.Search(query="labs"))
        self.search.answers.append(places.Search(query="labs"))
        with self.answers(steward.REVISE, "schedule_next_contact"):
            await self.barrier("find_places", {})
        self.assertEqual(1, len(self.cards()))
        line = [meta for _, _, meta in self.written if "steward" in meta][-1]
        self.assertEqual(steward.APPROVE, line["steward"]["verdict"])
        self.assertEqual(steward.KEEPS_THE_HANDOVER, line["steward"]["note"])

    async def test_a_revise_the_resolver_has_no_body_for_leaves_the_plan(
            self) -> None:
        """The resume is a `schedule_next_contact`, so a revise off it may name
        one of the Coordinator's other six. The Resolver has no body for one,
        and a steward that cannot produce a move this file can make has not
        produced a move: the plan stands, in the Steward's own words."""
        self.enrol()
        self.search.answers.append(places.found("Alfa Lab"))
        with self.answers(steward.REVISE, "pause_loop"):
            result = await self.barrier("find_places", {})
        self.assertTrue(result["answered"])
        self.assertEqual([], self.cards())
        self.assertEqual(1, len(self.queued))
        line = [meta for _, _, meta in self.written if "steward" in meta][-1]
        self.assertEqual(steward.APPROVE, line["steward"]["verdict"])
        self.assertEqual(steward.OUT_OF_POLICY, line["steward"]["note"])

    async def test_a_steward_that_cannot_be_reached_is_todays_behaviour(self
                                                                       ) -> None:
        """Rail 4, fail-open, and identical to the Coordinator's: an outage is
        a second opinion that is missing, not a gate that is down."""
        from unittest.mock import patch

        self.enrol()
        self.search.answers.append(places.found("Alfa Lab"))

        async def down(_facts):
            raise RuntimeError("the model is down")

        with patch.object(steward, "_ask", down):
            result = await self.barrier("find_places", {})
        self.assertTrue(result["answered"])
        self.assertEqual(1, len(self.queued))
        line = [meta for _, _, meta in self.written if "steward" in meta][-1]
        self.assertEqual(steward.APPROVE, line["steward"]["verdict"])
        self.assertIs(False, line["steward"]["asked_the_model"])

    # -- source rails ------------------------------------------------------- #
    def test_there_is_exactly_one_steward_seam_and_it_is_the_coordinators(
            self) -> None:
        self.assertIn("await coordinator._stewarded(turn, decision)", SOURCE)
        self.assertNotIn("steward_module.review(", SOURCE)
        self.assertNotIn("await steward.review(", SOURCE)

    def test_the_three_tools_outside_the_seven_are_guarded_by_code_only(self
                                                                       ) -> None:
        """core/steward.py rail 1 refuses anything that is not one of the
        Coordinator's seven, so asking it about these would be asking a
        question whose answer is always the same sentence."""
        for tool in ("ask_patient", "find_places", "reschedule_visit"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, policy.TOOLS)
        for tool in ("escalate_barrier", "schedule_next_contact"):
            with self.subTest(tool=tool):
                self.assertIn(tool, policy.TOOLS)



if __name__ == "__main__":
    unittest.main()
