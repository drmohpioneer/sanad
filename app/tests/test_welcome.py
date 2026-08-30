"""The first thing a patient ever reads, and the fact that he reads it once.

rev 17 item 9. Until then a patient who scanned the QR opened an empty grey
chat: nothing said who this was, and the plan his doctor had confirmed an hour
earlier was never sent to him at all. The Telegram bind did have a hello, but it
was written straight to Telegram rather than as an event, so the web page could
not show it either.

Three bubbles now, on whichever channel he opens first, through the fanout so
both channels and the doctor's own feed hold the same first conversation:

  who this is, and that it is not a doctor
  the doctor's confirmed plan text, word for word
  what happens next

The bit that has to be right is the second open. A page reload that sent the
plan a second time would be a bot that looks broken inside ten seconds, so the
flag is written before the first message leaves, and that is what is asserted
below.

core/links.py reaches the cloud SDK at import, so this skips on a laptop with
none and runs in the image, exactly as tests/test_chaser.py does.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from core import names, templates

try:
    from core import lang, links, store as store_module
    from core.models import Doctor, Patient
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
PLAN = ("Lipid panel in two weeks.\n"
        "Bisoprolol 2.5 mg once a day.\nCome back in a month.")


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheChatOpensWithTheDoctorsPlanInIt(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        outer = self
        self.sent: list = []
        self.receipts: dict[str, str] = {}
        self.speak = "ar"
        self.doctor = Doctor(id="d", name="دكتور محمد", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", plan_text=PLAN, created_at=NOW)

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.meta or {}))

        async def update_patient(patient_id, **fields):
            for key, value in fields.items():
                setattr(outer.patient, key, value)

        async def for_patient(*a, **kw):
            return outer.speak

        async def claim_send(send):
            state = outer.receipts.get(send.id)
            if state is None:
                outer.receipts[send.id] = "claimed"
                return store_module.CLAIMED
            if state == "failed":
                outer.receipts[send.id] = "claimed"
                return store_module.RESEND
            return store_module.ALREADY_SENT

        async def mark_send(send_id, state, error=""):
            outer.receipts[send_id] = state

        async def send_state(send_id):
            return outer.receipts.get(send_id, "")

        self.patches = [
            patch.object(links, "fanout", lambda: Fanout()),
            patch.object(store_module, "update_patient", update_patient),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(store_module, "claim_send", claim_send),
            patch.object(store_module, "mark_send", mark_send),
            patch.object(store_module, "send_state", send_state),
            patch.object(lang, "for_patient", for_patient),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def texts(self) -> list:
        return [text for ref, text, _ in self.sent if ref == "patient:p"]

    async def test_three_bubbles_and_the_middle_one_is_the_plan_verbatim(
            self) -> None:
        self.assertTrue(await links.welcome(self.patient, self.doctor))
        said = self.texts()
        self.assertEqual(len(said), 3)
        self.assertIn("سند", said[0])
        self.assertIn("مش دكتور", said[0])
        self.assertIn(self.doctor.name, said[0])
        self.assertTrue(said[1].endswith(PLAN),
                        "the plan must arrive word for word")
        self.assertIn(self.doctor.name, said[2])

    async def test_it_greets_him_in_arabic_letters(self) -> None:
        await links.welcome(self.patient, self.doctor)
        self.assertIn(names.vocative("Ahmed Ali", "ar"), self.texts()[0])
        self.assertNotIn("Ahmed", self.texts()[0])

    async def test_an_english_speaker_gets_the_english_three(self) -> None:
        self.speak = "en"
        await links.welcome(self.patient, self.doctor)
        said = self.texts()
        self.assertIn("I am Sanad", said[0])
        self.assertIn("Ahmed", said[0])
        self.assertTrue(said[1].endswith(PLAN))

    async def test_a_reload_sends_nothing_the_second_time(self) -> None:
        self.assertTrue(await links.welcome(self.patient, self.doctor))
        self.assertEqual(len(self.texts()), 3)
        self.assertFalse(await links.welcome(self.patient, self.doctor))
        self.assertEqual(len(self.texts()), 3)

    async def test_completion_is_written_only_after_every_word_lands(self) -> None:
        """A partial delivery stays retryable instead of consuming onboarding."""
        seen: list = []

        class Watching:
            async def send(self, ref, msg):
                seen.append(self.patient_flag())

            def patient_flag(self):
                return self.outer.patient.welcomed_at

        watcher = Watching()
        watcher.outer = self
        with patch.object(links, "fanout", lambda: watcher):
            await links.welcome(self.patient, self.doctor)
        self.assertTrue(seen)
        self.assertTrue(all(flag is None for flag in seen))
        self.assertIsNotNone(self.patient.welcomed_at)

    async def test_a_mid_send_failure_retries_only_the_unfinished_steps(self) -> None:
        calls = 0

        class Flaky:
            async def send(inner, ref, msg):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("channel down")
                self.sent.append((ref, msg.text, msg.meta or {}))

        with patch.object(links, "fanout", lambda: Flaky()):
            with self.assertRaises(RuntimeError):
                await links.welcome(self.patient, self.doctor)
            self.assertIsNone(self.patient.welcomed_at)
            self.assertTrue(await links.welcome(self.patient, self.doctor))
        self.assertEqual(len(self.texts()), 3)

    async def test_a_patient_with_no_plan_still_gets_hello(self) -> None:
        self.patient.plan_text = ""
        await links.welcome(self.patient, self.doctor)
        self.assertEqual(len(self.texts()), 2)

    async def test_every_bubble_says_it_came_from_a_template(self) -> None:
        await links.welcome(self.patient, self.doctor)
        for ref, text, meta in self.sent:
            with self.subTest(text=text[:24]):
                self.assertEqual(meta["audit"]["tier"], "onboarding")
                self.assertIn("audit", meta)
        generated = [m["audit"]["generated"] for _, _, m in self.sent]
        self.assertEqual(generated, ["code template",
                                     "the doctor's own confirmed plan text",
                                     "code template"])

    async def test_nothing_here_is_generated(self) -> None:
        """The two framing bubbles are templates and the third is the doctor."""
        await links.welcome(self.patient, self.doctor)
        said = self.texts()
        self.assertEqual(
            said[0],
            templates.render("welcome", "ar", "m",
                             patient=names.vocative("Ahmed Ali", "ar"),
                             doctor=self.doctor.name))
        self.assertEqual(
            said[2],
            templates.render("welcome_next", "ar", "m", doctor=self.doctor.name))


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class BothDoorsOpenTheSameWay(unittest.TestCase):
    """The web page and the Telegram bind call the one function."""

    def test_the_page_welcomes_on_first_open(self) -> None:
        from pathlib import Path

        main = (Path(__file__).resolve().parents[1] / "main.py"
                ).read_text(encoding="utf-8")
        route = main.split('@app.get("/p/{link_token}")', 1)[1].split(
            "@app.get", 1)[0]
        self.assertIn("links.welcome(patient, doctor)", route)

    def test_the_telegram_bind_uses_it_too_and_writes_no_hello_of_its_own(
            self) -> None:
        from pathlib import Path

        router = (Path(__file__).resolve().parents[1] / "core" / "tg_router.py"
                  ).read_text(encoding="utf-8")
        self.assertIn("links.welcome(patient, doctor)", router)
        self.assertNotIn("def _welcome(", router)


if __name__ == "__main__":
    unittest.main()
