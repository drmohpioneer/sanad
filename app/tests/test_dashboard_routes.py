"""The dashboard's own routes: what they serve, and what they refuse.

Three halves, for the same reason tests/test_coordinator.py has two.

The first half drives core/cards.py and core/views.py, which are pure, so the
rules themselves (is this card open, which cards does this button finish, what
does a board row say about a patient) run anywhere with nothing installed.

The second half reads app/main.py as text. The guarantee that a wrong token is a
404 on the new page IS the shape of the route: it is a dependency, and a route
that lost it would still return a page to anybody.

The third half imports app/main.py and calls the route coroutines against an
in-memory store. That reaches FastAPI and the ADK package, so it skips on a
laptop that has neither and runs in the image. The guard catches every exception
and not only ImportError: this suite is what the Dockerfile runs before the image
is allowed to exist, and a route test must never be the reason a deploy fails.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import cards, views
from core.models import Doctor, Event, LinkToken, Loop, Patient, Report

APP_ROOT = Path(__file__).resolve().parents[1]
MAIN = (APP_ROOT / "main.py").read_text(encoding="utf-8")
REPORT = (APP_ROOT / "core" / "report.py").read_text(encoding="utf-8")
DISPATCH = (APP_ROOT / "core" / "dispatch.py").read_text(encoding="utf-8")

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)


def event(ident: str, *, card=None, kind="card", patient_id="p1", minutes=0,
          text="", meta=None) -> Event:
    body = dict(meta or {})
    if card is not None:
        body["card"] = card
    return Event(id=ident, doctor_id="d", patient_id=patient_id, kind=kind,
                 text=text, meta=body, ts=NOW + timedelta(minutes=minutes))


def confirm_card() -> dict:
    return {"title": "Confirm: Ahmed Ali", "lines": ["lab: Lipid panel"],
            "actions": [{"id": "confirm:c1", "label": "Confirm"},
                        {"id": "cancel:c1", "label": "Cancel"}]}


def values_card(loop_id: str = "l1") -> dict:
    return {"title": "Lab results", "severity": "yellow", "lines": ["LDL 152"],
            "actions": [{"id": f"reviewed:{loop_id}", "label": "Reviewed"},
                        {"id": f"note:{loop_id}", "label": "Send a note",
                         "input": True}]}


def red_card() -> dict:
    return {"title": "EMERGENCY", "severity": "red", "lines": ["chest pain"],
            "actions": []}


# --------------------------------------------------------------------------- #
# 1. The rules, which need nothing installed
# --------------------------------------------------------------------------- #
class WhichCardsStillNeedTheDoctor(unittest.TestCase):
    def test_a_card_with_a_button_on_it_is_open(self) -> None:
        self.assertTrue(cards.is_open(event("e1", card=confirm_card())))

    def test_a_red_card_with_no_buttons_is_open_until_it_is_seen(self) -> None:
        self.assertTrue(cards.is_open(event("e1", card=red_card())))

    def test_a_notice_with_no_buttons_and_no_red_is_not_an_obligation(self) -> None:
        quiet = {"title": "Youssef is unreachable", "severity": "white",
                 "lines": [], "actions": []}
        self.assertFalse(cards.is_open(event("e1", card=quiet)))

    def test_an_event_that_is_not_a_card_is_never_in_the_inbox(self) -> None:
        self.assertFalse(cards.is_open(event("e1", kind="patient_in", text="hi")))

    def test_a_resolved_card_is_finished_however_many_buttons_it_had(self) -> None:
        card = dict(confirm_card())
        card["resolved"] = True
        self.assertFalse(cards.is_open(event("e1", card=card)))

    def test_open_cards_come_back_newest_first(self) -> None:
        rows = [event("old", card=confirm_card()),
                event("new", card=red_card(), minutes=30),
                event("chat", kind="patient_in", minutes=45)]
        self.assertEqual([e.id for e in cards.open_cards(rows)], ["new", "old"])


class WhichCardABlockButtonFinishes(unittest.TestCase):
    def test_a_button_finishes_the_card_it_sits_on(self) -> None:
        rows = [event("e1", card=confirm_card()), event("e2", card=values_card())]
        self.assertEqual([i for i, _ in cards.plan(rows, "confirm:c1", NOW)], ["e1"])

    def test_pressing_either_half_of_a_decision_finishes_the_card(self) -> None:
        """Confirm and Cancel are one decision, and they sit on one card.

        Attach and Open loop are the other pair that works this way. Reviewed
        and Send a note share a card and are NOT such a pair: see the class
        below.
        """
        rows = [event("e1", card=confirm_card())]
        for pressed in ("confirm:c1", "cancel:c1"):
            with self.subTest(pressed=pressed):
                self.assertEqual([i for i, _ in cards.plan(rows, pressed, NOW)],
                                 ["e1"])

    def test_one_press_finishes_every_card_carrying_that_action(self) -> None:
        """`reviewed:<loop>` can sit on two cards for the same obligation."""
        rows = [event("e1", card=values_card("l1")),
                event("e2", card=values_card("l1"), minutes=5),
                event("e3", card=values_card("l9"), minutes=6)]
        self.assertEqual([i for i, _ in cards.plan(rows, "reviewed:l1", NOW)],
                         ["e1", "e2"])

    def test_seen_names_its_own_event_because_a_red_card_has_no_button(self) -> None:
        rows = [event("e1", card=red_card()), event("e2", card=red_card(), minutes=1)]
        self.assertEqual([i for i, _ in cards.plan(rows, "seen:e2", NOW)], ["e2"])

    def test_an_action_nobody_carries_finishes_nothing(self) -> None:
        rows = [event("e1", card=confirm_card())]
        self.assertEqual(cards.plan(rows, "confirm:somethingelse", NOW), [])

    def test_a_card_already_finished_is_not_finished_twice(self) -> None:
        card = dict(confirm_card())
        card["resolved"] = True
        self.assertEqual(cards.plan([event("e1", card=card)], "confirm:c1", NOW), [])

    def test_reviewed_finishes_the_values_card(self) -> None:
        rows = [event("e1", card=values_card("l1"))]
        self.assertEqual([i for i, _ in cards.plan(rows, "reviewed:l1", NOW)],
                         ["e1"])

    def test_the_flag_carries_the_action_and_the_time(self) -> None:
        marked = cards.mark(confirm_card(), "confirm:c1", NOW)
        self.assertIs(marked["resolved"], True)
        self.assertEqual(marked["resolved_by"], "confirm:c1")
        self.assertEqual(marked["resolved_at"], NOW.isoformat())
        self.assertEqual(marked["resolved_at_ms"], int(NOW.timestamp() * 1000))

    def test_marking_leaves_the_card_that_was_on_the_wire_alone(self) -> None:
        original = confirm_card()
        cards.mark(original, "confirm:c1", NOW)
        self.assertNotIn("resolved", original)

    def test_the_flag_survives_being_written_and_read_back(self) -> None:
        """A fresh Event built from the stored body still reads as finished.

        This is the whole point of moving the flag off the browser: the object
        the page held is gone by the time the page is reloaded, so what has to
        carry the answer is the record.
        """
        row = event("e1", card=confirm_card())
        (_, meta), = cards.plan([row], "confirm:c1", NOW)
        reread = Event(**{**row.model_dump(), "meta": meta})
        self.assertTrue(cards.is_resolved(reread))
        self.assertFalse(cards.is_open(reread))
        self.assertEqual(cards.open_cards([reread]), [])
        self.assertIs(cards.row(reread)["meta"]["card"]["resolved"], True)


class ANoteIsNotAReview(unittest.TestCase):
    """The one card action that finishes nothing.

    "Send a note" sits beside "Reviewed" on a lab-values card and sends the
    doctor's line to the patient. The card is his to-do and the result behind it
    is still waiting for the review that closes the loop, so the Inbox must go on
    showing it. Retiring it on a note would hide a result he has not reviewed,
    which is the one thing the Inbox exists to prevent.
    """

    def test_a_note_is_the_only_action_that_finishes_nothing(self) -> None:
        self.assertEqual(cards.SIDE_ACTIONS, ("note",))
        self.assertFalse(cards.retires("note:l1"))
        for pressed in ("reviewed:l1", "confirm:c1", "cancel:c1", "attach:e1",
                        "openloop:e1", "reply:r1", "seen:e1"):
            with self.subTest(pressed=pressed):
                self.assertTrue(cards.retires(pressed))

    def test_a_note_leaves_the_values_card_open(self) -> None:
        rows = [event("e1", card=values_card("l1"))]
        self.assertEqual(cards.plan(rows, "note:l1", NOW), [])
        self.assertTrue(cards.is_open(rows[0]))

    def test_a_note_does_not_finish_the_card_the_review_would_have(self) -> None:
        """Same card, same loop, two buttons, two different answers."""
        rows = [event("e1", card=values_card("l1")),
                event("e2", card=values_card("l1"), minutes=5)]
        self.assertEqual(cards.plan(rows, "note:l1", NOW), [])
        self.assertEqual([i for i, _ in cards.plan(rows, "reviewed:l1", NOW)],
                         ["e1", "e2"])

    def test_the_note_button_still_belongs_to_the_card(self) -> None:
        """It is the card's button. It just is not the card's completion."""
        self.assertTrue(cards.carries(event("e1", card=values_card("l1")),
                                      "note:l1"))

    def test_the_page_mirrors_the_rule_rather_than_inventing_one(self) -> None:
        page = (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("const RETIRES = {note:false};", page)
        self.assertIn('if (RETIRES[verb] !== false && holder', page)


class TheBoardRow(unittest.TestCase):
    def loop(self, ident, state="open", days=None) -> Loop:
        return Loop(id=ident, patient_id="p1", doctor_id="d", type="TEST",
                    title=ident, state=state,
                    due_at=None if days is None else NOW + timedelta(days=days),
                    created_at=NOW, updated_at=NOW)

    def test_next_due_is_the_earliest_deadline_still_live(self) -> None:
        loops = [self.loop("far", days=9), self.loop("near", days=2)]
        self.assertEqual(views.next_due(loops), (NOW + timedelta(days=2)).isoformat())

    def test_a_closed_loop_has_no_deadline_left_to_miss(self) -> None:
        loops = [self.loop("done", "done", days=1),
                 self.loop("gone", "unreachable", days=2),
                 self.loop("live", days=6)]
        self.assertEqual(views.next_due(loops), (NOW + timedelta(days=6)).isoformat())

    def test_no_dated_loop_is_no_next_due(self) -> None:
        self.assertIsNone(views.next_due([self.loop("undated")]))
        self.assertIsNone(views.next_due([]))

    def test_last_event_is_the_newest_one_for_that_patient(self) -> None:
        rows = [event("a", kind="patient_in", minutes=0),
                event("b", kind="agent_out", minutes=20),
                event("c", kind="card", patient_id="p2", minutes=40)]
        seen = views.last_event(rows, "p1")
        self.assertEqual(seen["last_event_ms"],
                         int((NOW + timedelta(minutes=20)).timestamp() * 1000))
        self.assertEqual(seen["last_event_kind"], "agent_out")

    def test_a_patient_nothing_has_happened_to_says_so(self) -> None:
        self.assertEqual(views.last_event([], "p1"),
                         {"last_event_ms": None, "last_event_kind": None})

    def test_last_event_reads_the_record_and_not_a_feed_window(self) -> None:
        """A quiet patient behind two hundred noisy events keeps his last event.

        The page used to derive this from /feed, which is the newest 200 events
        for the whole board, so exactly this patient showed nothing at all.
        """
        quiet = event("quiet", kind="patient_in", minutes=1)
        noise = [event(f"n{i}", kind="agent_out", patient_id="p2", minutes=i + 2)
                 for i in range(300)]
        self.assertEqual(views.last_event([quiet] + noise, "p1")["last_event_kind"],
                         "patient_in")


class WhereAPatientCanBeReached(unittest.TestCase):
    def patient(self, chat_id=None) -> Patient:
        return Patient(id="p1", doctor_id="d", name="Ahmed Ali",
                       channels={"web": True, "telegram_chat_id": chat_id},
                       created_at=NOW)

    def token(self, ident="tok", minutes=0) -> LinkToken:
        return LinkToken(id=ident, doctor_id="d", patient_id="p1",
                         created_at=NOW + timedelta(minutes=minutes))

    def test_a_bound_chat_is_telegram(self) -> None:
        self.assertEqual(views.channel_of(self.patient(546), self.token()), "telegram")

    def test_a_link_with_no_bound_chat_is_the_web_page(self) -> None:
        self.assertEqual(views.channel_of(self.patient(), self.token()), "web")

    def test_no_link_at_all_is_no_channel(self) -> None:
        self.assertEqual(views.channel_of(self.patient(), None), "none")

    def test_the_channel_is_a_fact_about_the_patient_not_the_deployment(self) -> None:
        """Two patients of the same doctor, on the same running service."""
        bound, unbound = self.patient(546), self.patient()
        self.assertNotEqual(views.channel_of(bound, self.token()),
                            views.channel_of(unbound, self.token()))

    def test_the_link_is_the_qr_path_and_the_page_path(self) -> None:
        self.assertEqual(views.link_for(self.token("abc")),
                         {"qr": "/qr/abc.png", "page": "/p/abc"})
        self.assertIsNone(views.link_for(None))

    def test_the_newest_link_per_patient_wins(self) -> None:
        rows = [self.token("old"), self.token("new", minutes=5)]
        self.assertEqual(views.links_by_patient(rows)["p1"].id, "new")


class TheSettingsShape(unittest.TestCase):
    def doctor(self, chat_id=None, policy=None) -> Doctor:
        return Doctor(id="d", name="Dr Mohamed", specialty="cardiology", lang="en",
                      web_token="t", telegram_chat_id=chat_id,
                      policy=policy or {}, created_at=NOW)

    def test_the_route_returns_the_fields_the_dashboard_asks_for(self) -> None:
        from core import policy as policy_module

        shape = views.settings_view(self.doctor(546),
                                    policy_module.for_doctor(self.doctor(546)))
        self.assertEqual(
            set(shape),
            {"name", "specialty", "language", "telegram_bound",
             "telegram_chat_id_present", "policy"},
        )
        self.assertEqual(shape["name"], "Dr Mohamed")
        self.assertEqual(shape["specialty"], "cardiology")
        self.assertEqual(shape["language"], "en")
        self.assertTrue(shape["telegram_bound"])
        self.assertTrue(shape["telegram_chat_id_present"])

    def test_an_unbound_doctor_says_so_both_ways(self) -> None:
        from core import policy as policy_module

        shape = views.settings_view(self.doctor(), policy_module.DEFAULT)
        self.assertFalse(shape["telegram_bound"])
        self.assertFalse(shape["telegram_chat_id_present"])

    def test_the_chat_id_itself_never_leaves_the_server(self) -> None:
        """A console token is not an admin credential, and that is a phone."""
        from core import policy as policy_module

        shape = views.settings_view(self.doctor(100200300), policy_module.DEFAULT)
        self.assertNotIn("100200300", str(shape))

    def test_the_policy_is_the_one_the_guards_enforce(self) -> None:
        from core import policy as policy_module

        doctor = self.doctor(policy={"max_contacts": 3, "followup_reason": "because"})
        shape = views.settings_view(doctor, policy_module.for_doctor(doctor))
        self.assertEqual(shape["policy"]["max_contacts"], 3)
        self.assertEqual(shape["policy"]["followup_reason"], "because")
        self.assertEqual(shape["policy"]["quiet_hours"], "22:00 to 08:00 Cairo")


class TheReportShape(unittest.TestCase):
    def test_a_report_row_is_a_record_and_not_a_parsed_line(self) -> None:
        row = views.report_row(Report(
            id="r1", doctor_id="d", kind="completion", patient_id="p1",
            title="Completion report: Ahmed Ali", body="body\ntext",
            created_at=NOW))
        self.assertEqual(row, {
            "id": "r1", "kind": "completion", "patient_id": "p1",
            "title": "Completion report: Ahmed Ali", "body": "body\ntext",
            "ts_ms": int(NOW.timestamp() * 1000),
        })


# --------------------------------------------------------------------------- #
# 2. The rails: the shape of the routes themselves
# --------------------------------------------------------------------------- #
class TheRoutesAreShapedTheWayTheyHaveToBe(unittest.TestCase):
    def route(self, path: str) -> str:
        """The source of one route handler, from its decorator to the next one."""
        head = f'@app.get("{path}")'
        self.assertIn(head, MAIN)
        return MAIN.split(head, 1)[1].split("@app.", 1)[0]

    def test_the_dashboard_is_behind_the_same_doctor_as_the_console(self) -> None:
        """A wrong token is a 404 because the page is behind the dependency."""
        self.assertIn("Depends(current_doctor)", self.route("/c/{token}/app"))

    def test_the_dashboard_route_serves_the_dashboard(self) -> None:
        self.assertIn("DASHBOARD_HTML", self.route("/c/{token}/app"))
        self.assertIn('DASHBOARD_HTML = os.path.join(WEB, "dashboard.html")', MAIN)

    def test_the_old_console_is_untouched(self) -> None:
        self.assertIn("CONSOLE_HTML", self.route("/c/{token}"))

    def test_every_new_read_route_is_behind_the_dependency(self) -> None:
        for path in ("/c/{token}/cards", "/c/{token}/reports", "/c/{token}/settings"):
            with self.subTest(path=path):
                self.assertIn("Depends(current_doctor)", self.route(path))

    def test_current_doctor_is_a_404_and_not_a_403(self) -> None:
        block = MAIN.split("async def current_doctor", 1)[1].split("@app.", 1)[0]
        self.assertIn('HTTPException(404, "Not Found")', block)

    def test_the_card_is_resolved_after_the_action_runs_and_never_instead(self) -> None:
        """The verb runs first; retiring the card is the last thing.

        The two are in two functions now (codex re-audit 17): `_carry_out` does
        the work, `action` retires the card after it returns. That is the same
        order, and it is what makes the resolve failure recoverable without
        running the work twice, so the assertion follows the call rather than
        the text.
        """
        block = MAIN.split("async def action(", 1)[1]
        self.assertLess(block.index("_carry_out("), block.index("cards.resolve("))
        work = MAIN.split("async def _carry_out(", 1)[1].split("\n@app.", 1)[0]
        self.assertIn('raise HTTPException(400, "unknown action")', work)
        self.assertNotIn("cards.resolve(", work)

    def test_the_domain_work_is_claimed_by_its_action_id(self) -> None:
        """codex re-audit 17. Resolve failing must not let the work run twice."""
        block = MAIN.split("async def action(", 1)[1].split("async def _carry_out",
                                                            1)[0]
        self.assertLess(block.index("store.claim_action("),
                        block.index("_carry_out("))
        self.assertLess(block.index("_carry_out("),
                        block.index("store.release_action("))

    def test_the_cards_route_serves_only_the_open_ones(self) -> None:
        self.assertIn("cards.open_cards(", self.route("/c/{token}/cards"))

    def test_the_feed_still_returns_everything(self) -> None:
        """The inbox is filtered; the history is not."""
        self.assertNotIn("cards.", self.route("/c/{token}/feed"))

    def test_a_report_is_stored_where_a_report_is_written(self) -> None:
        block = REPORT.split("async def send_if_complete", 1)[1]
        self.assertIn('record(doctor, "completion"', block)
        self.assertLess(block.index("record(doctor,"), block.index("fanout()"))

    def test_the_digest_and_the_named_report_are_stored_too(self) -> None:
        block = DISPATCH.split("async def command(", 1)[1]
        self.assertIn('report.record(doctor, "digest"', block)
        self.assertIn('report.record(doctor, "completion"', block)

    def test_no_route_matches_a_report_by_its_wording(self) -> None:
        self.assertNotIn("startswith(\"Digest", MAIN)
        self.assertIn("store.list_reports(", self.route("/c/{token}/reports"))

    def test_the_board_reads_the_record_not_the_feed_window(self) -> None:
        block = self.route("/c/{token}/board")
        self.assertIn("store.list_events(", block)
        self.assertNotIn("last_events(", block)
        self.assertIn("views.last_event(", block)
        self.assertIn("views.next_due(", block)
        self.assertIn("views.reach(", block)


class NoDashesAnywhereTheDoctorCanSeeThem(unittest.TestCase):
    """Every generated title and every generated line, swept and kept swept.

    core/labs.py normalises an en dash out of a printed reference range, which is
    reading a slip rather than writing a sentence, so that one line is allowed to
    contain the character it removes.
    """

    ALLOWED = {("core/labs.py", 'replace("–", "-").replace("—", "-")')}

    def files(self):
        for folder in ("core", "web"):
            for path in sorted((APP_ROOT / folder).iterdir()):
                if path.suffix in (".py", ".html", ".md"):
                    yield path
        yield APP_ROOT / "main.py"

    def test_no_em_dash_and_no_en_dash_in_any_shipped_file(self) -> None:
        for path in self.files():
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1):
                if "—" not in line and "–" not in line:
                    continue
                where = path.relative_to(APP_ROOT).as_posix()
                if any(where == f and snippet in line
                       for f, snippet in self.ALLOWED):
                    continue
                self.fail(f"{where}:{number} carries an em or en dash: {line.strip()}")


# --------------------------------------------------------------------------- #
# 3. The routes themselves, against an in-memory store
# --------------------------------------------------------------------------- #
# Reaches FastAPI and the ADK package. The guard is deliberately `Exception` and
# not `ImportError`: the Dockerfile runs this suite before the image is allowed
# to exist, and a route test must never be the reason a deploy fails.
try:  # pragma: no cover - the image build always has both
    import main as sanad_main
    from fastapi import HTTPException
    ROUTES_MISSING = ""
except Exception as exc:  # pragma: no cover
    ROUTES_MISSING = f"main.py is not importable here: {exc}"


class FakeStore:
    """Just enough Firestore to answer the read routes, in memory.

    Every read hands back a freshly built record, never the object the test is
    holding, so "it persisted" cannot be an object that was mutated in place.
    """

    def __init__(self) -> None:
        self.doctors: dict[str, Doctor] = {}
        self.patients: dict[str, Patient] = {}
        self.loops: dict[str, Loop] = {}
        self.events: dict[str, Event] = {}
        self.reports: dict[str, Report] = {}
        self.tokens: dict[str, LinkToken] = {}
        self.clock = NOW

    # writes ---------------------------------------------------------------
    def now(self):
        return self.clock

    def new_id(self) -> str:
        return f"id{len(self.events) + len(self.reports) + 1}"

    async def update_event(self, event_id: str, **fields) -> None:
        row = self.events[event_id]
        self.events[event_id] = Event(**{**row.model_dump(), **fields})

    async def save_report(self, report: Report) -> Report:
        self.reports[report.id] = report
        return report

    # reads ----------------------------------------------------------------
    async def doctor_by_token(self, token: str):
        for doctor in self.doctors.values():
            if doctor.web_token == token:
                return Doctor(**doctor.model_dump())
        return None

    async def list_events(self, doctor_id: str) -> list[Event]:
        rows = [Event(**e.model_dump()) for e in self.events.values()
                if e.doctor_id == doctor_id]
        return sorted(rows, key=lambda e: e.ts)

    async def list_patients(self, doctor_id: str) -> list[Patient]:
        rows = [Patient(**p.model_dump()) for p in self.patients.values()
                if p.doctor_id == doctor_id]
        return sorted(rows, key=lambda p: p.created_at)

    async def get_patient(self, patient_id: str):
        row = self.patients.get(patient_id)
        return Patient(**row.model_dump()) if row else None

    async def list_loops(self, patient_id: str) -> list[Loop]:
        rows = [Loop(**l.model_dump()) for l in self.loops.values()
                if l.patient_id == patient_id]
        return sorted(rows, key=lambda l: l.created_at)

    async def list_link_tokens(self, doctor_id: str) -> list[LinkToken]:
        rows = [LinkToken(**t.model_dump()) for t in self.tokens.values()
                if t.doctor_id == doctor_id]
        return sorted(rows, key=lambda t: t.created_at)

    async def latest_link_token(self, doctor_id: str):
        rows = await self.list_link_tokens(doctor_id)
        return rows[-1] if rows else None

    async def list_reports(self, doctor_id: str) -> list[Report]:
        rows = [Report(**r.model_dump()) for r in self.reports.values()
                if r.doctor_id == doctor_id]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)


NAMES = ("now", "new_id", "update_event", "save_report", "doctor_by_token",
         "list_events", "list_patients", "get_patient", "list_loops",
         "list_link_tokens", "latest_link_token", "list_reports")


@unittest.skipIf(ROUTES_MISSING, ROUTES_MISSING)
class TheRoutesAgainstAStore(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from core import store

        self.fake = FakeStore()
        self.doctor = Doctor(id="d", name="Dr Mohamed", specialty="cardiology",
                             lang="en", web_token="goodtoken",
                             telegram_chat_id=99, created_at=NOW)
        self.fake.doctors["d"] = self.doctor
        self.fake.patients["p1"] = Patient(
            id="p1", doctor_id="d", name="Ahmed Ali", diagnosis="Heart failure",
            channels={"web": True, "telegram_chat_id": None}, created_at=NOW)
        self.fake.loops["l1"] = Loop(
            id="l1", patient_id="p1", doctor_id="d", type="TEST",
            title="Lipid panel", state="pending_review",
            due_at=NOW + timedelta(days=5), created_at=NOW, updated_at=NOW)
        self.fake.tokens["tok1"] = LinkToken(
            id="tok1", doctor_id="d", patient_id="p1", created_at=NOW)
        for name in NAMES:
            self.enterContext(patch.object(store, name, getattr(self.fake, name)))

    # --- the page itself ---------------------------------------------------
    async def test_a_wrong_token_is_a_404_on_the_app_route(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await sanad_main.current_doctor("wrongtoken")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_the_right_token_serves_the_dashboard(self) -> None:
        doctor = await sanad_main.current_doctor("goodtoken")
        page = await sanad_main.dashboard(doctor)
        self.assertTrue(page.path.endswith("web/dashboard.html"))

    async def test_the_console_still_serves_the_console(self) -> None:
        doctor = await sanad_main.current_doctor("goodtoken")
        page = await sanad_main.console(doctor)
        self.assertTrue(page.path.endswith("web/console.html"))

    # --- the resolved flag -------------------------------------------------
    async def test_the_resolved_flag_survives_a_fresh_feed_read(self) -> None:
        self.fake.events["e1"] = event("e1", card=confirm_card(), patient_id=None)

        before = await sanad_main.open_cards(self.doctor)
        self.assertEqual([c["id"] for c in before["cards"]], ["e1"])

        self.fake.clock = NOW + timedelta(minutes=3)
        marked = await cards.resolve("d", "confirm:c1")
        self.assertEqual(marked, ["e1"])

        after = await sanad_main.open_cards(self.doctor)
        self.assertEqual(after["cards"], [])

        feed = await sanad_main.feed(0, self.doctor)
        card = feed["events"][0]["meta"]["card"]
        self.assertIs(card["resolved"], True)
        self.assertEqual(card["resolved_by"], "confirm:c1")
        self.assertEqual(card["resolved_at"], (NOW + timedelta(minutes=3)).isoformat())

    async def test_the_feed_still_carries_the_card_it_resolved(self) -> None:
        """Resolving retires a card. It never deletes any history."""
        self.fake.events["e1"] = event("e1", card=confirm_card(), patient_id=None,
                                       text="Confirm this record?")
        await cards.resolve("d", "cancel:c1")
        feed = await sanad_main.feed(0, self.doctor)
        self.assertEqual(len(feed["events"]), 1)
        self.assertEqual(feed["events"][0]["text"], "Confirm this record?")

    async def test_seen_finishes_a_red_card_that_has_no_buttons(self) -> None:
        self.fake.events["e1"] = event("e1", card=red_card())
        await cards.resolve("d", "seen:e1")
        self.assertEqual((await sanad_main.open_cards(self.doctor))["cards"], [])

    async def test_a_note_leaves_the_card_in_the_inbox(self) -> None:
        """The route runs the note and the card is still waiting for review."""
        self.fake.events["e1"] = event("e1", card=values_card("l1"))

        self.assertEqual(await cards.resolve("d", "note:l1"), [])

        still = await sanad_main.open_cards(self.doctor)
        self.assertEqual([c["id"] for c in still["cards"]], ["e1"])
        self.assertNotIn("resolved", still["cards"][0]["meta"]["card"])

        self.assertEqual(await cards.resolve("d", "reviewed:l1"), ["e1"])
        self.assertEqual((await sanad_main.open_cards(self.doctor))["cards"], [])

    async def test_resolving_twice_changes_nothing_the_second_time(self) -> None:
        self.fake.events["e1"] = event("e1", card=confirm_card(), patient_id=None)
        first = await cards.resolve("d", "confirm:c1")
        second = await cards.resolve("d", "confirm:c1")
        self.assertEqual((first, second), (["e1"], []))

    # --- reports -----------------------------------------------------------
    async def test_the_reports_route_returns_a_completion_report(self) -> None:
        from core import report

        patient = await self.fake.get_patient("p1")
        self.assertEqual((await sanad_main.reports(self.doctor))["reports"], [])

        await report.record(self.doctor, "completion",
                            report.completion_title(patient),
                            "Completion report: Ahmed Ali\n\nLipid panel: closed",
                            patient_id="p1")

        rows = (await sanad_main.reports(self.doctor))["reports"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "completion")
        self.assertEqual(rows[0]["patient_id"], "p1")
        self.assertEqual(rows[0]["title"], "Completion report: Ahmed Ali")
        self.assertIn("Lipid panel: closed", rows[0]["body"])
        self.assertEqual(rows[0]["ts_ms"], int(NOW.timestamp() * 1000))

    async def test_reports_come_back_newest_first_with_the_digest_among_them(
            self) -> None:
        from core import report

        await report.record(self.doctor, "completion", "Completion report: A", "a",
                            patient_id="p1")
        self.fake.clock = NOW + timedelta(hours=1)
        await report.record(self.doctor, "digest", "Digest for Dr Mohamed", "d")

        rows = (await sanad_main.reports(self.doctor))["reports"]
        self.assertEqual([r["kind"] for r in rows], ["digest", "completion"])
        self.assertIsNone(rows[0]["patient_id"])

    # --- settings ----------------------------------------------------------
    async def test_the_settings_route_shape(self) -> None:
        shape = await sanad_main.doctor_settings(self.doctor)
        self.assertEqual(
            set(shape),
            {"name", "specialty", "language", "telegram_bound",
             "telegram_chat_id_present", "policy"},
        )
        self.assertEqual(shape["name"], "Dr Mohamed")
        self.assertEqual(shape["specialty"], "cardiology")
        self.assertEqual(shape["language"], "en")
        self.assertTrue(shape["telegram_bound"])
        self.assertEqual(shape["policy"]["max_contacts"], 6)

    # --- the board ---------------------------------------------------------
    async def test_last_event_ms_is_on_the_board(self) -> None:
        self.fake.events["e1"] = event("e1", kind="patient_in", minutes=0,
                                       text="hello")
        self.fake.events["e2"] = event("e2", kind="agent_out", minutes=25,
                                       text="reply")

        board = await sanad_main.board_view(self.doctor)
        row, = board["patients"]
        self.assertEqual(row["last_event_ms"],
                         int((NOW + timedelta(minutes=25)).timestamp() * 1000))
        self.assertEqual(row["last_event_kind"], "agent_out")
        self.assertEqual(row["next_due"], (NOW + timedelta(days=5)).isoformat())

    async def test_a_patient_with_no_history_has_no_last_event(self) -> None:
        board = await sanad_main.board_view(self.doctor)
        row, = board["patients"]
        self.assertIsNone(row["last_event_ms"])
        self.assertIsNone(row["last_event_kind"])

    async def test_the_board_carries_the_channel_and_the_link_per_patient(
            self) -> None:
        board = await sanad_main.board_view(self.doctor)
        row, = board["patients"]
        self.assertEqual(row["channel"], "web")
        self.assertEqual(row["link"], {"qr": "/qr/tok1.png", "page": "/p/tok1"})

    async def test_the_patient_view_carries_them_too(self) -> None:
        view = await sanad_main.patient_view("p1", self.doctor)
        self.assertEqual(view["channel"], "web")
        self.assertEqual(view["link"]["page"], "/p/tok1")


# --------------------------------------------------------------------------- #
# rev 17, item 2: the S0 spikes are gone from the deployed surface
# --------------------------------------------------------------------------- #
class NoUnauthenticatedModelRouteExists(unittest.TestCase):
    """A public route that calls Gemini with no check is somebody else's key.

    /spike/gemini took arbitrary text and called Gemini on this project's Vertex
    quota; /spike/voice took an arbitrary upload, shelled it through ffmpeg and
    called Gemini again. Neither had a check of any kind, and the module-level
    ADK session service they shared grew one session per call and dropped none.
    The S0 proof they existed for is written down in research/s0-results.md.
    """

    def test_neither_spike_route_is_registered(self) -> None:
        """The names survive in the module docstring, saying why they went."""
        for route in ('@app.post("/spike', "async def spike_"):
            with self.subTest(route=route):
                self.assertNotIn(route, MAIN)
        self.assertIn("/spike/gemini", MAIN.split('"""', 2)[1])

    def test_no_agent_or_session_service_lives_at_module_level(self) -> None:
        """Every ADK object in the product path is built and dropped per request."""
        for leak in ("InMemorySessionService()", "spike_runner", "spike_agent",
                     "spike_sessions"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, MAIN)

    def test_every_post_route_left_has_a_check_in_front_of_it(self) -> None:
        """Each POST is admin-secret, doctor-token, OIDC, or a patient link."""
        guarded = ("Depends(require_admin)", "doctor: Doctor = Depends",
                   "link_token", "verify_caller", "verify_secret")
        for block in MAIN.split("@app.post(")[1:]:
            path = block.split('"', 2)[1]
            body = block.split("\n\n\n", 1)[0]
            with self.subTest(path=path):
                self.assertTrue(any(word in body for word in guarded),
                                f"POST {path} has no check in front of it")


# --------------------------------------------------------------------------- #
# rev 17, item 16: a reset board runs on the defaults
# --------------------------------------------------------------------------- #
@unittest.skipIf(ROUTES_MISSING, ROUTES_MISSING)
class ResetClearsThePolicyToo(unittest.IsolatedAsyncioTestCase):
    """A knob set for one rehearsal must not survive into the next one.

    Filming a refusal means setting `max_contacts` low for a take. That number
    was stored on the doctor and `POST /admin/reset` never touched it, so the
    next reseeded board ran on a ceiling nobody remembered setting and refused
    things the runbook said would be sent.
    """

    def setUp(self) -> None:
        import os
        from core import store

        self.doctor = Doctor(id="d", name="Test Doctor", web_token="t",
                             policy={"max_contacts": 2, "grace_days": 1},
                             awaiting_relay_id="rl-9",
                             awaiting_note_loop_id="l4", created_at=NOW)
        self.written: dict = {}
        outer = self

        async def doctor_by_name(name):
            return outer.doctor if name == outer.doctor.name else None

        async def wipe_doctor(doctor_id):
            return {"patients": 3, "loops": 5}

        async def update_doctor(doctor_id, **fields):
            outer.written.update(fields)

        async def doctor_chat_bindings():
            return []

        self.enterContext(patch.dict(os.environ, {"ADMIN_SECRET": "s3cret"}))
        self.enterContext(patch.object(store, "doctor_by_name", doctor_by_name))
        self.enterContext(patch.object(store, "wipe_doctor", wipe_doctor))
        self.enterContext(patch.object(store, "update_doctor", update_doctor))
        self.enterContext(patch.object(store, "doctor_chat_bindings",
                                       doctor_chat_bindings))

    async def test_the_stored_policy_is_emptied(self) -> None:
        answer = await sanad_main.reset(name="Test Doctor")
        self.assertTrue(answer["ok"])
        self.assertEqual(self.written["policy"], {})

    async def test_every_half_finished_knob_goes_with_it(self) -> None:
        await sanad_main.reset(name="Test Doctor")
        for knob in ("awaiting_relay_id", "awaiting_note_loop_id",
                     "awaiting_since"):
            with self.subTest(knob=knob):
                self.assertIsNone(self.written[knob])

    async def test_the_answer_says_the_policy_went(self) -> None:
        answer = await sanad_main.reset(name="Test Doctor")
        self.assertIn("core/policy.py", answer["policy"])

    async def test_an_empty_policy_is_the_defaults(self) -> None:
        """Nothing has to be re-typed: absent means the defaults."""
        from core import policy

        self.assertEqual(policy.parse({}), policy.DEFAULT)

    async def test_a_wrong_secret_never_reaches_the_body(self) -> None:
        """The check is a dependency now (H1), so it refuses before the body.

        tests/test_wave_c.py drives `require_admin` itself with a wrong header,
        a right one and a secret in the query string. This asserts the other
        half: the route is behind it, so nothing in the body can run first.
        """
        signature = MAIN.split("async def reset(", 1)[1].split("-> dict:", 1)[0]
        self.assertIn("Depends(require_admin)", signature)
        self.assertEqual(self.written, {})


if __name__ == "__main__":
    unittest.main()
