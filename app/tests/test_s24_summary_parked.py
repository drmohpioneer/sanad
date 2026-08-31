"""The web summary lists the cards the phone stayed quiet for.

S24 finding 3.  ``summary.card`` has taken a ``parked=`` / ``names=`` block
since S24-G, and the Telegram ``/digest`` command was the only caller that
passed it.  ``GET /c/<token>/summary`` called the same function with three
positional arguments and nothing else, so the parked block never rendered on
the web.  A doctor with no bound Telegram chat therefore had a parked
REVIEW_READY card that was reachable on no surface at all, and the runbook
beat that says "or open GET /c/<token>/summary" could not be filmed as
written.

The route is driven here, not read: the coroutine runs against an in-memory
store and the assertions are on what it returned.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import summary
from core.models import Doctor, Event, Loop, Patient


NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)

# Reaches FastAPI and the ADK package, exactly as tests/test_dashboard_routes.py
# does.  The guard is `Exception` and not `ImportError` on purpose: the
# Dockerfile runs this suite before the image is allowed to exist, and a route
# test must never be the reason a deploy fails.
try:  # pragma: no cover - the image build always has both
    import main as sanad_main
    ROUTES_MISSING = ""
except Exception as exc:  # pragma: no cover
    ROUTES_MISSING = f"main.py is not importable here: {exc}"


def parked_card_event(ident: str, title: str, *, patient_id: str = "p1",
                      minutes: int = 0) -> Event:
    """The record a Case Steward hold leaves behind on a REVIEW_READY card."""
    return Event(
        id=ident,
        doctor_id="d",
        patient_id=patient_id,
        kind="card",
        text=title,
        meta={
            "card": {"title": title, "severity": "yellow", "lines": ["LDL 152"],
                     "actions": []},
            "notification_class": "REVIEW_READY",
            summary.PHONE_META: {
                "class": "REVIEW_READY",
                "decision": "parked",
                "release_at": (NOW + timedelta(hours=16)).isoformat(),
            },
        },
        ts=NOW + timedelta(minutes=minutes),
    )


class FakeStore:
    """Only what `summary_view` reads. Nothing here reaches Firestore."""

    def __init__(self) -> None:
        self.doctors: dict[str, Doctor] = {}
        self.patients: dict[str, Patient] = {}
        self.loops: dict[str, Loop] = {}
        self.events: dict[str, Event] = {}
        self.clock = NOW

    def now(self):
        return self.clock

    async def doctor_by_token(self, token: str):
        for doctor in self.doctors.values():
            if doctor.web_token == token:
                return Doctor(**doctor.model_dump())
        return None

    async def list_patients(self, doctor_id: str) -> list[Patient]:
        rows = [Patient(**p.model_dump()) for p in self.patients.values()
                if p.doctor_id == doctor_id]
        return sorted(rows, key=lambda p: p.created_at)

    async def list_loops(self, patient_id: str) -> list[Loop]:
        rows = [Loop(**l.model_dump()) for l in self.loops.values()
                if l.patient_id == patient_id]
        return sorted(rows, key=lambda l: l.created_at)

    async def list_events(self, doctor_id: str) -> list[Event]:
        rows = [Event(**e.model_dump()) for e in self.events.values()
                if e.doctor_id == doctor_id]
        return sorted(rows, key=lambda e: e.ts)

    async def open_relays(self, doctor_id: str) -> list:
        return []

    async def get_settings(self) -> dict:
        return {"run_id": "run1", "time_scale": 86400}


NAMES = ("now", "doctor_by_token", "list_patients", "list_loops",
         "list_events", "open_relays", "get_settings")


@unittest.skipIf(ROUTES_MISSING, ROUTES_MISSING)
class TheWebSummaryShowsWhatWasParked(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from core import store

        self.fake = FakeStore()
        # No telegram_chat_id, which is the whole point: this is the doctor the
        # /digest command cannot reach.
        self.doctor = Doctor(id="d", name="Test Doctor", specialty="cardiology",
                             lang="en", web_token="goodtoken",
                             telegram_chat_id=None, created_at=NOW)
        self.fake.doctors["d"] = self.doctor
        self.fake.patients["p1"] = Patient(
            id="p1", doctor_id="d", name="Hany Fouad", diagnosis="High LDL",
            channels={"web": True, "telegram_chat_id": None}, created_at=NOW)
        self.fake.patients["p2"] = Patient(
            id="p2", doctor_id="d", name="Ahmed Ali", diagnosis="Heart failure",
            channels={"web": True, "telegram_chat_id": None},
            created_at=NOW + timedelta(minutes=1))
        self.fake.loops["l1"] = Loop(
            id="l1", patient_id="p1", doctor_id="d", type="TEST",
            title="Lipid panel", state="pending_review",
            due_at=NOW + timedelta(days=5), created_at=NOW, updated_at=NOW)
        for name in NAMES:
            self.enterContext(patch.object(store, name, getattr(self.fake, name)))

    async def test_a_parked_card_appears_in_the_web_summary(self) -> None:
        self.fake.events["e1"] = parked_card_event("e1", "Lab results")

        body = await sanad_main.summary_view(self.doctor)

        lines = body["card"]["lines"]
        heading = [line for line in lines if line.startswith(summary.PARKED_HEADING)]
        self.assertEqual(len(heading), 1, lines)
        self.assertIn("(1)", heading[0])
        # The patient is named, which is what `names=` is for: a title with no
        # one attached to it is not something a doctor can act on.
        self.assertIn("  · Hany Fouad: Lab results", lines)

    async def test_every_parked_card_is_listed_and_attributed(self) -> None:
        self.fake.events["e1"] = parked_card_event("e1", "Lab results")
        self.fake.events["e2"] = parked_card_event(
            "e2", "Ahmed Ali unreachable", patient_id="p2", minutes=2)

        body = await sanad_main.summary_view(self.doctor)

        lines = body["card"]["lines"]
        self.assertIn(f"{summary.PARKED_HEADING} (2):", lines)
        self.assertIn("  · Hany Fouad: Lab results", lines)
        self.assertIn("  · Ahmed Ali: Ahmed Ali unreachable", lines)

    async def test_the_route_grew_no_new_key_on_the_legacy_response(self) -> None:
        """The Gate 0B goldens seal this shape; the block rides on the card."""
        self.fake.events["e1"] = parked_card_event("e1", "Lab results")

        body = await sanad_main.summary_view(self.doctor)

        self.assertEqual(set(body), {"doctor", "line", "counts", "card"})

    async def test_a_day_with_nothing_parked_is_the_card_it_always_was(self) -> None:
        """The block is absent, not an empty heading with nothing under it."""
        body = await sanad_main.summary_view(self.doctor)

        for line in body["card"]["lines"]:
            self.assertNotIn(summary.PARKED_HEADING, line)

    async def test_a_card_the_digest_already_handed_over_is_not_relisted(self) -> None:
        handed = parked_card_event("e1", "Lab results")
        handed.meta[summary.PHONE_META]["digest_at"] = NOW.isoformat()
        self.fake.events["e1"] = handed

        body = await sanad_main.summary_view(self.doctor)

        for line in body["card"]["lines"]:
            self.assertNotIn(summary.PARKED_HEADING, line)

    async def test_reading_the_summary_does_not_release_what_it_lists(self) -> None:
        """Unlike the digest, this route only reads. The card stays owed.

        `digest.build` clears the parked mark as it delivers. If this route ever
        did the same, opening the summary would silently consume the digest the
        doctor had not read yet.
        """
        self.fake.events["e1"] = parked_card_event("e1", "Lab results")

        first = await sanad_main.summary_view(self.doctor)
        second = await sanad_main.summary_view(self.doctor)

        for body in (first, second):
            self.assertIn(f"{summary.PARKED_HEADING} (1):", body["card"]["lines"])
        self.assertNotIn(
            "digest_at", self.fake.events["e1"].meta[summary.PHONE_META],
            "the web summary may not mark a card as handed over",
        )


if __name__ == "__main__":
    unittest.main()
