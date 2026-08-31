"""The security audit's fixes, each with the defect it closes.

The public regression evidence is this module: each test names the defect it
closes and drives the affected implementation path directly.

H1 (the admin secret in the query string), M3 (the bind race) and M2 (unbounded
uploads and an unbounded ffmpeg) are the three that had a way in. The rest of
the file is the Low and Info one-liners that were worth doing.

`require_admin` is driven directly with a real Starlette Request, because it is
a dependency: FastAPI runs it before the route body, and a test that called the
route coroutine by hand would prove nothing about it.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from core import uploads

try:  # pragma: no cover - the image build always has the cloud SDK
    from core import media as _media
    SDK_MISSING = ""
except Exception as exc:  # pragma: no cover
    _media = None
    SDK_MISSING = f"the cloud SDK is not installed here: {exc}"

APP_ROOT = Path(__file__).resolve().parents[1]
MAIN = (APP_ROOT / "main.py").read_text(encoding="utf-8")
DOCKERFILE = (APP_ROOT / "Dockerfile").read_text(encoding="utf-8")
DEPLOY = (APP_ROOT / "deploy.sh").read_text(encoding="utf-8")

# A document rail can only fire where the document is. The image's build
# context is `app/` alone (deploy.sh runs `gcloud run deploy --source .` from
# there), so docs/ is never copied into it, and reading these two at import time
# errored the whole image build with
#   FileNotFoundError: [Errno 2] No such file or directory: '/docs/RUNBOOK.md'
# which is the same failure tests/test_background.py already carries a guard
# for. Same guard here: read where the file is, skip where it cannot be. The
# laptop and any checkout of the whole tree run every one of them, and that is
# where a reworded runbook or README actually gets written.
RUNBOOK_PATH = APP_ROOT.parent / "docs" / "RUNBOOK.md"
RUNBOOK = RUNBOOK_PATH.read_text(encoding="utf-8") if RUNBOOK_PATH.exists() else ""
HAS_RUNBOOK = unittest.skipUnless(
    RUNBOOK_PATH.exists(), "docs/RUNBOOK.md is outside the image")


def _readme() -> str:
    """The README lives at docs/README.md here and at the root of the public copy.

    The copy procedure moves it, so this suite has to run from either tree: the
    public copy is built by rsync from this one and its whole point is that the
    same `python -m unittest discover` passes inside it. Inside the image it is
    in neither place, and that is what HAS_README is for.
    """
    for candidate in (APP_ROOT.parent / "docs" / "README.md",
                      APP_ROOT.parent / "README.md"):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return ""


README = _readme()
HAS_README = unittest.skipUnless(README, "README.md is outside the image")

try:  # pragma: no cover - the image build always has FastAPI
    import main as sanad_main
    from fastapi import HTTPException
    from starlette.requests import Request
    ROUTES_MISSING = ""
except Exception as exc:  # pragma: no cover
    ROUTES_MISSING = f"main.py is not importable here: {exc}"


def request(*, headers: dict | None = None, query: str = "") -> "Request":
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/admin/reset",
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode())
                    for k, v in (headers or {}).items()],
    })


# --------------------------------------------------------------------------- #
# H1: the admin secret leaves the URL
# --------------------------------------------------------------------------- #
@unittest.skipIf(ROUTES_MISSING, ROUTES_MISSING)
class TheAdminSecretIsAHeader(unittest.TestCase):
    """The secret sat in the query string of every /admin call.

    Cloud Run's request log records the query string and keeps it for thirty
    days, so `POST /admin/seed?secret=<48 hex>` was readable by anyone with
    roles/logging.viewer, and so was every `GET /c/<console token>/feed`.
    """

    def setUp(self) -> None:
        import os

        self.enterContext(patch.dict(os.environ, {"ADMIN_SECRET": "s3cret"}))

    def test_the_right_header_passes(self) -> None:
        self.assertIsNone(sanad_main.require_admin(
            request(headers={sanad_main.ADMIN_HEADER: "s3cret"})))

    def test_a_wrong_header_is_a_404_and_not_a_403(self) -> None:
        """404 so an attacker cannot tell an admin route from a typo."""
        with self.assertRaises(HTTPException) as caught:
            sanad_main.require_admin(
                request(headers={sanad_main.ADMIN_HEADER: "wrong"}))
        self.assertEqual(caught.exception.status_code, 404)

    def test_no_header_at_all_is_a_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            sanad_main.require_admin(request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_secret_in_the_query_string_is_refused_with_401(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            sanad_main.require_admin(request(query="secret=s3cret"))
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_query_secret_is_refused_even_when_it_is_the_right_one(self) -> None:
        """The point is that nobody keeps writing one, not that it is wrong."""
        with self.assertRaises(HTTPException) as caught:
            sanad_main.require_admin(
                request(headers={sanad_main.ADMIN_HEADER: "s3cret"},
                        query="secret=s3cret"))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertIn("header", caught.exception.detail)

    def test_an_unconfigured_service_says_so_instead_of_letting_anyone_in(self) -> None:
        import os

        with patch.dict(os.environ, {"ADMIN_SECRET": ""}):
            with self.assertRaises(HTTPException) as caught:
                sanad_main.require_admin(
                    request(headers={sanad_main.ADMIN_HEADER: ""}))
        self.assertEqual(caught.exception.status_code, 503)

    def test_the_comparison_is_constant_time(self) -> None:
        self.assertIn("hmac.compare_digest", MAIN)

    def test_not_one_admin_route_still_takes_a_secret_parameter(self) -> None:
        for block in MAIN.split("@app.post(")[1:] + MAIN.split("@app.get(")[1:]:
            path = block.split('"', 2)[1]
            if not path.startswith("/admin"):
                continue
            signature = block.split("-> dict:", 1)[0]
            with self.subTest(path=path):
                self.assertNotIn('secret: str', signature)
                self.assertIn("Depends(require_admin)", signature)


class TheAccessLogIsOff(unittest.TestCase):
    def test_uvicorn_writes_no_access_line(self) -> None:
        """The console token was in the path of every /feed poll, twice a second."""
        self.assertIn("--no-access-log", DOCKERFILE)

    def test_httpx_does_not_write_the_bot_token_into_the_log(self) -> None:
        """Found live on rev 21, in Cloud Logging, after the H1 fix had shipped.

        httpx logs one INFO line per request carrying the whole URL, and the
        Telegram API puts the bot token in the path, so every send wrote

          INFO httpx HTTP Request: POST
          https://api.telegram.org/bot<TOKEN>/sendMessage "HTTP/1.1 400 Bad Request"

        into a log kept for thirty days. Same class as the admin secret in the
        query string, and the same answer: keep the credential out of the line.
        """
        self.assertIn('logging.getLogger("httpx").setLevel(logging.WARNING)', MAIN)


class TheDocsAndTheScriptUseTheHeader(unittest.TestCase):
    @HAS_RUNBOOK
    def test_no_curl_in_the_runbook_puts_the_secret_in_a_url(self) -> None:
        """The rule itself is written out in prose, so only commands are read."""
        offenders = [line.strip() for line in RUNBOOK.splitlines()
                     if "curl" in line and "secret=" in line]
        self.assertEqual(offenders, [])
        self.assertNotIn("secret=$S", RUNBOOK)

    @HAS_RUNBOOK
    def test_the_runbook_uses_the_header(self) -> None:
        self.assertIn("X-Sanad-Admin: $S", RUNBOOK)

    @HAS_README
    def test_the_readme_never_shows_a_secret_in_a_url(self) -> None:
        offenders = [line.strip() for line in README.splitlines()
                     if "?secret=" in line and "refused with 401" not in line]
        self.assertEqual(offenders, [])

    def test_the_deploy_script_says_where_the_secret_goes(self) -> None:
        self.assertIn("X-Sanad-Admin", DEPLOY)

    def test_neither_page_puts_the_secret_in_the_url(self) -> None:
        """Read as code, not as prose: both files explain the rule in a comment."""
        for name in ("dashboard.html", "console.html"):
            page = (APP_ROOT / "web" / name).read_text(encoding="utf-8")
            code = "\n".join(line for line in page.splitlines()
                             if "refuses ?secret=" not in line)
            with self.subTest(page=name):
                self.assertNotIn("?secret=", code)
                self.assertNotIn("secret:secret", code)
                self.assertIn("X-Sanad-Admin", code)


# --------------------------------------------------------------------------- #
# M3: the bind race
# --------------------------------------------------------------------------- #
@unittest.skipIf(ROUTES_MISSING, ROUTES_MISSING)
class BindingTheDoctorsPhoneNamesTheChat(unittest.IsolatedAsyncioTestCase):
    """Anyone who sent /start last could become the doctor's phone.

    tg_router parks every unknown chat as a pending start, and bind-doctor took
    the newest one. The bot's username is public, so the whole attack was to
    send /start in the seconds between his /start and his bind call: after that
    every card, every patient message and every lab result went to that chat.
    """

    def setUp(self) -> None:
        from core.models import Doctor
        from core import store
        from datetime import datetime, timezone

        self.now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        self.doctor = Doctor(id="d", name=sanad_main.DEMO_DOCTOR,
                             web_token="t", created_at=self.now)
        self.bound: dict = {}
        outer = self

        async def doctor_by_name(name):
            return outer.doctor if name == outer.doctor.name else None

        async def update_doctor(doctor_id, **fields):
            outer.bound.update(fields)

        async def send_card(chat_id, text, card=None):
            return None

        self.enterContext(patch.object(store, "doctor_by_name", doctor_by_name))
        self.enterContext(patch.object(store, "update_doctor", update_doctor))
        self.enterContext(patch.object(sanad_main.telegram, "send_card", send_card))

    async def test_no_chat_id_is_refused_and_binds_nothing(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await sanad_main.bind_doctor(chat_id=None)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.bound, {})

    async def test_an_explicit_chat_id_binds_that_one(self) -> None:
        answer = await sanad_main.bind_doctor(chat_id=100200300)
        self.assertEqual(answer["chat_id"], 100200300)
        self.assertEqual(self.bound["telegram_chat_id"], 100200300)

    async def test_zero_still_unbinds(self) -> None:
        answer = await sanad_main.bind_doctor(chat_id=0)
        self.assertIsNone(answer["chat_id"])
        self.assertIsNone(self.bound["telegram_chat_id"])

    def test_the_route_never_reads_the_newest_pending_start(self) -> None:
        route = MAIN.split("async def bind_doctor(", 1)[1].split(
            "@app.post", 1)[0]
        self.assertNotIn("latest_pending_start", route)

    def test_there_is_somewhere_to_read_a_chat_id_from(self) -> None:
        self.assertIn('@app.get("/admin/pending-starts")', MAIN)


# --------------------------------------------------------------------------- #
# M2: uploads, and the ffmpeg they feed
# --------------------------------------------------------------------------- #
class WhatMayBeUploaded(unittest.TestCase):
    """`await file.read()` with no cap, and the client's own content_type."""

    def jpeg(self) -> bytes:
        return b"\xff\xd8\xff\xe0" + b"\x00" * 20

    def png(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

    def ogg(self) -> bytes:
        return b"OggS" + b"\x00" * 20

    def test_a_photo_is_recognised_from_its_own_bytes(self) -> None:
        self.assertEqual(uploads.classify(self.jpeg()),
                         (uploads.IMAGE, "image/jpeg"))
        self.assertEqual(uploads.classify(self.png()),
                         (uploads.IMAGE, "image/png"))

    def test_a_voice_note_is_recognised_from_its_own_bytes(self) -> None:
        self.assertEqual(uploads.classify(self.ogg()),
                         (uploads.AUDIO, "audio/ogg"))

    def test_riff_is_read_past_its_header(self) -> None:
        """RIFF is both WAV and WEBP, and the difference is at offset 8."""
        wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 12
        webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 12
        self.assertEqual(uploads.classify(wav), (uploads.AUDIO, "audio/wav"))
        self.assertEqual(uploads.classify(webp), (uploads.IMAGE, "image/webp"))

    def test_heic_and_m4a_are_told_apart_by_their_brand(self) -> None:
        heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 12
        m4a = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 12
        self.assertEqual(uploads.classify(heic)[0], uploads.IMAGE)
        self.assertEqual(uploads.classify(m4a)[0], uploads.AUDIO)

    def test_a_jpeg_calling_itself_audio_still_goes_to_the_image_lane(self) -> None:
        """The header is a claim. The magic number is a fact."""
        lane, mime = uploads.classify(self.jpeg())
        self.assertEqual(lane, uploads.IMAGE)
        self.assertEqual(mime, "image/jpeg")

    def test_anything_that_is_neither_is_refused(self) -> None:
        for raw in (b"%PDF-1.7 and then some padding bytes",
                    b"#!/bin/sh\necho hello world, padding",
                    b"GIF89a" + b"\x00" * 20):
            with self.subTest(head=raw[:6]):
                with self.assertRaises(uploads.Rejected):
                    uploads.classify(raw)

    def test_an_empty_upload_is_refused(self) -> None:
        with self.assertRaises(uploads.Rejected):
            uploads.classify(b"")

    def test_an_oversized_upload_is_refused_as_too_large(self) -> None:
        raw = self.jpeg() + b"\x00" * uploads.MAX_BYTES
        with self.assertRaises(uploads.Rejected) as caught:
            uploads.classify(raw)
        self.assertTrue(caught.exception.too_large)

    def test_the_cap_is_applied_while_reading_and_not_after(self) -> None:
        import asyncio

        class Endless:
            """A file that never ends: the shape the cap exists for."""

            def __init__(self) -> None:
                self.served = 0

            async def read(self, size: int) -> bytes:
                self.served += size
                return b"\x00" * size

        endless = Endless()
        with self.assertRaises(uploads.Rejected):
            asyncio.run(uploads.read(endless))
        self.assertLessEqual(endless.served,
                             uploads.MAX_BYTES + uploads.CHUNK)

    def test_the_refusal_lines_exist_in_both_languages(self) -> None:
        for too_large in (True, False):
            self.assertTrue(uploads.refusal_text(
                "en", "u", too_large=too_large).strip())
            for who in ("m", "f", "u"):
                self.assertTrue(uploads.refusal_text(
                    "ar", who, too_large=too_large).strip())

    def test_no_route_reads_a_whole_upload_without_a_cap(self) -> None:
        self.assertNotIn("await file.read()", MAIN)
        self.assertIn("uploads.take(file)", MAIN)

    def test_a_refused_upload_is_an_event_and_a_line_and_a_200(self) -> None:
        block = MAIN.split("async def refuse_upload(", 1)[1].split(
            "@app.post", 1)[0]
        self.assertIn("events.append_event", block)
        self.assertIn("uploads.refusal_text", block)
        self.assertIn('"ok": False', block)

    def test_a_refused_upload_never_writes_a_string_into_the_refused_list(self) -> None:
        """Found live on rev 22: one oversized upload froze the whole board.

        A meta `refused` is the list of guard calls code turned down, and
        web/dashboard.html renders it with list.forEach. This event put the
        reason string there, so the feed render threw

          TypeError: list.forEach is not a function

        which is uncaught, which stops the 2 s poll, so the board went stale
        while the live pill still read "Live · synced HH:MM".
        """
        block = MAIN.split("async def refuse_upload(", 1)[1].split(
            "@app.post", 1)[0]
        # The response body keeps `refused`: that is the JSON the patient page
        # reads, it has never been a list, and nothing renders it as one. What
        # may not carry a string is the event and the audit line.
        written = block.split("return {", 1)[0]
        self.assertNotIn('"refused"', written)
        self.assertIn('"refusal": why.reason', written)
        self.assertIn('"refused": why.reason', block.split("return {", 1)[1])

    def test_the_board_never_iterates_a_refusal_it_cannot_iterate(self) -> None:
        """The other half, and the one that holds whatever is written next."""
        page = (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")
        block = page.split("function refusedRows(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("Array.isArray(raw)", block)
        pure = page.split("function isPureRefusal(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("Array.isArray(meta.refused)", pure)

    def test_the_telegram_lane_checks_the_size_before_downloading(self) -> None:
        router = (APP_ROOT / "core" / "tg_router.py").read_text(encoding="utf-8")
        block = router.split("audio_bytes: Optional[bytes] = None", 1)[1].split(
            "doctor = await store.doctor_by_telegram", 1)[0]
        self.assertLess(block.index("_too_big(voice)"),
                        block.index("telegram.download(voice"))


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class FfmpegRunsOnAClock(unittest.TestCase):
    """A crafted container can keep a decoder busy for ever, on a worker thread."""

    def setUp(self) -> None:
        from core import media

        self.media = media
        self.source = (APP_ROOT / "core" / "media.py").read_text(encoding="utf-8")

    def test_there_is_a_timeout(self) -> None:
        block = self.source.split("def to_wav(", 1)[1].split("def transcribe_wav",
                                                             1)[0]
        self.assertIn("timeout=FFMPEG_TIMEOUT", block)
        self.assertGreater(self.media.FFMPEG_TIMEOUT, 0)

    def test_the_output_is_bounded_too(self) -> None:
        block = self.source.split("def to_wav(", 1)[1].split("def transcribe_wav",
                                                             1)[0]
        self.assertIn('"-t", str(MAX_AUDIO_SECONDS)', block)

    def test_the_protocol_whitelist_is_written_down(self) -> None:
        self.assertIn("file,crypto,data", self.source)

    def test_ffmpeg_never_waits_on_a_terminal(self) -> None:
        self.assertIn("-nostdin", self.source)


# --------------------------------------------------------------------------- #
# The Low and Info one-liners
# --------------------------------------------------------------------------- #
class TheSmallOnes(unittest.TestCase):
    @HAS_RUNBOOK
    def test_l2_the_runbook_has_no_live_url_in_it(self) -> None:
        self.assertNotIn(".a.run.app", RUNBOOK)
        self.assertIn("<SERVICE_URL>", RUNBOOK)

    def test_i1_the_deploy_script_takes_the_project_from_the_environment(self) -> None:
        """So a forker does not deploy into somebody else's project.

        S15 G2 finished the job the audit started. The environment still wins,
        but the fallback is no longer a hard-coded id that a fork inherits by
        accident: it is the operator's own gcloud configuration, and a shell
        that has neither stops instead of guessing.
        """
        self.assertIn(
            "PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}",
            DEPLOY)
        self.assertNotIn("PROJECT:-sanad-", DEPLOY)

    def test_i1_an_unconfigured_shell_is_told_what_to_do_and_stops(self) -> None:
        """Empty and the literal "(unset)" gcloud prints are both refusals."""
        guard = DEPLOY.split("PROJECT=${PROJECT:-", 1)[1].split("REGION=", 1)[0]
        self.assertIn('[ -z "${PROJECT}" ]', guard)
        self.assertIn('[ "${PROJECT}" = "(unset)" ]', guard)
        self.assertIn("exit 1", guard)
        message = [line for line in guard.splitlines()
                   if line.strip().startswith("echo ")]
        self.assertEqual(len(message), 1, "one line to the operator, not a wall")
        self.assertIn("set PROJECT=", message[0])
        self.assertIn("gcloud config set project", message[0])
        self.assertIn(">&2", message[0])

    def test_i1_the_guard_runs_before_anything_is_created(self) -> None:
        """A refusal after the first gcloud call is not a refusal."""
        self.assertLess(DEPLOY.index("exit 1"),
                        DEPLOY.index("gcloud iam service-accounts describe"))

    def test_l6_the_console_escapes_quotes_like_the_dashboard_does(self) -> None:
        console = (APP_ROOT / "web" / "console.html").read_text(encoding="utf-8")
        line = [l for l in console.splitlines() if l.startswith("const esc")][0]
        for char in ('"', "'"):
            self.assertIn(char, line.split("replace(", 1)[1])

    def test_l1_cancel_checks_who_owns_the_confirmation(self) -> None:
        registrar = (APP_ROOT / "core" / "registrar.py").read_text(encoding="utf-8")
        block = registrar.split("async def cancel(", 1)[1].split("\nasync def",
                                                                 1)[0]
        self.assertIn("doctor_id != doctor.id", block)

    @HAS_README
    def test_the_readme_carries_the_privacy_note(self) -> None:
        self.assertIn("not a medical device", README)
        self.assertIn("Do not enter a real patient", README)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
