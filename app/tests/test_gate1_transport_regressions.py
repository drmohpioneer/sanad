"""Gate 1: transport and cursor bugs captured before their seams are changed.

These are intentionally behavioral tests of the current public boundaries.  No
network, browser, model, or Firestore service is used: provider responses, event
storage, and Telegram callbacks are local doubles, while the framework-free
dashboard follows the source-contract style used by its established tests.

Each case describes the behavior the reshape must provide.  On the legacy
runtime the tests expose the live defects from the S23 correction dossier:

* an HTTP 200 body containing ``{"ok": false}`` resolves as success in the UI;
* a millisecond-only cursor drops a later event in the same millisecond;
* taking the newest 200 rows loses the beginning of a larger between-poll burst;
* Telegram's ``ok:false`` body is recorded as a delivered channel;
* Telegram's reply mode is global and consumes a web dictation; and
* Telegram callbacks do domain work without the action idempotency claim.

Normal builds report these as expected failures.  Set
``SANAD_GATE1_STRICT=1`` to expose the raw failures; once a fix lands, an
unexpected success forces deliberate removal of that test's marker.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import adapters, dispatch, doctor_actions, events, tg_router
from core.adapters import InboundMessage, OutboundMessage
from core.models import Doctor, Event, Relay


APP_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")
DASHBOARD_SCRIPT = DASHBOARD.split("<script>", 1)[1].split("</script>", 1)[0]
NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)


def gate1_live_bug(test):
    """Expect assertion failures only; fixture/runtime errors remain hard errors."""
    if os.environ.get("SANAD_GATE1_STRICT") == "1":
        return test

    def record(case: unittest.TestCase, error=None) -> None:
        outcome = case._outcome
        if outcome is None:
            raise RuntimeError("Gate 1 outcome is unavailable")
        if error is None:
            if outcome.success:
                case._addUnexpectedSuccess(outcome.result)
        else:
            case._addExpectedFailure(outcome.result, error)
        outcome.success = False

    if inspect.iscoroutinefunction(test):
        @functools.wraps(test)
        async def async_wrapper(case, *args, **kwargs):
            try:
                await test(case, *args, **kwargs)
            except case.failureException:
                record(case, sys.exc_info())
            else:
                record(case)
        return async_wrapper

    @functools.wraps(test)
    def wrapper(case, *args, **kwargs):
        try:
            test(case, *args, **kwargs)
        except case.failureException:
            record(case, sys.exc_info())
        else:
            record(case)
    return wrapper


def javascript_code(source: str) -> str:
    """Blank comments and literals while preserving offsets and line breaks."""
    out = list(source)
    index = 0

    def blank(start: int, stop: int) -> None:
        for position in range(start, stop):
            if out[position] != "\n":
                out[position] = " "

    while index < len(source):
        if source.startswith("//", index):
            stop = source.find("\n", index + 2)
            stop = len(source) if stop < 0 else stop
            blank(index, stop)
            index = stop
            continue
        if source.startswith("/*", index):
            stop = source.find("*/", index + 2)
            stop = len(source) if stop < 0 else stop + 2
            blank(index, stop)
            index = stop
            continue
        quote = source[index]
        if quote in ("'", '"', "`"):
            stop = index + 1
            escaped = False
            while stop < len(source):
                char = source[stop]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    stop += 1
                    break
                stop += 1
            blank(index, stop)
            index = stop
            continue
        index += 1
    return "".join(out)


def javascript_function(name: str) -> str:
    """Return one named function body from the dashboard's executable source."""
    matches = list(
        re.finditer(
            rf"\b(?:async\s+)?function\s+{re.escape(name)}\s*\(",
            DASHBOARD_SCRIPT,
        )
    )
    if len(matches) != 1:
        raise AssertionError(f"expected one JavaScript function {name}, found {len(matches)}")
    opening = DASHBOARD_SCRIPT.find("{", matches[0].end())
    if opening < 0:
        raise AssertionError(f"JavaScript function {name} has no body")
    tail = javascript_code(DASHBOARD_SCRIPT[opening:])
    depth = 0
    for index, char in enumerate(tail):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return tail[1:index]
    raise AssertionError(f"JavaScript function {name} has an unclosed body")


def rejects_false_command(body: str) -> bool:
    """Whether a parsed JSON payload, rather than the HTTP response, is guarded."""
    payloads = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*await\s+"
            r"[A-Za-z_$][\w$]*\.json\s*\(",
            body,
        )
    }
    return any(
        re.search(
            rf"\bif\s*\([^)]*(?:"
            rf"{re.escape(payload)}\.ok\s*(?:===|==)\s*false|"
            rf"!\s*{re.escape(payload)}\.ok"
            rf")[^)]*\)\s*(?:\{{[^}}]*\bthrow\b|\bthrow\b)",
            body,
            re.DOTALL,
        )
        for payload in payloads
    )


def an_event(ident: str, at: datetime) -> Event:
    return Event(
        id=ident,
        doctor_id="d",
        kind="system",
        text=ident,
        ts=at,
    )


def a_doctor(**fields: object) -> Doctor:
    values: dict[str, object] = {
        "id": "d",
        "name": "Dr Test",
        "web_token": "tok",
        "telegram_chat_id": 7700,
        "created_at": NOW,
    }
    values.update(fields)
    return Doctor(**values)


class HttpBodySuccessIsNotTransportSuccess(unittest.TestCase):
    """A successful HTTP exchange can still carry a rejected command."""

    @gate1_live_bug
    def test_dashboard_post_rejects_http_200_with_ok_false(self) -> None:
        """The post helper must reject before a caller clears its input.

        The shipping image has no JavaScript runtime, so this uses the same
        source-contract approach as the existing dashboard suite.  Both POST
        helpers must parse the command body and explicitly turn ``ok:false``
        into an exception instead of returning the rejected payload as success.
        """
        post_bodies = {
            name: javascript_function(name) for name in ("jpostForm", "jpostJson")
        }
        called_names = {
            match.group(1)
            for body in post_bodies.values()
            for match in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", body)
        }
        declared_names = {
            match.group(1)
            for match in re.finditer(
                r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                DASHBOARD_SCRIPT,
            )
        }
        validators = {
            name
            for name in called_names & declared_names
            if name not in {"jpostForm", "jpostJson"}
            and rejects_false_command(javascript_function(name))
        }

        missing: list[str] = []
        for name in ("jpostForm", "jpostJson"):
            body = post_bodies[name]
            inline = rejects_false_command(body)
            shared = any(
                re.search(
                    rf"\breturn\s+(?:await\s+)?{re.escape(validator)}\s*\(\s*r\s*\)",
                    body,
                )
                or re.search(
                    rf"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                    rf"await\s+{re.escape(validator)}\s*\(\s*r\s*\)\s*;"
                    rf"[\s\S]*?\breturn\s+\1\s*;",
                    body,
                )
                for validator in validators - {name}
            )
            if not (inline or shared):
                missing.append(name)
        self.assertEqual(
            [],
            missing,
            "POST helpers do not reject HTTP ok:false: " + ", ".join(missing),
        )


class EventCursorDoesNotLoseBursts(unittest.IsolatedAsyncioTestCase):
    """Polling must make forward progress without skipping event identities."""

    @gate1_live_bug
    async def test_a_later_event_in_the_same_millisecond_is_not_dropped(self) -> None:
        first = an_event("e-first", NOW + timedelta(microseconds=200))
        later = an_event("e-later", NOW + timedelta(microseconds=700))
        # The fixed seam supplies an identity-bearing cursor.  Falling back to
        # the legacy millisecond value keeps this a behavioral failure today,
        # while the nonzero offset also catches the old raw-float replay mutant.
        cursor = getattr(events, "event_cursor", events.ts_ms)(first)

        with patch.object(
            events.store,
            "list_events",
            AsyncMock(return_value=[first, later]),
        ):
            page = await events.last_events("d", cursor)

        self.assertEqual(
            [later.id],
            [row.id for row in page],
            "the cursor must return the unseen same-ms event once, without replaying "
            "the event already represented by the cursor",
        )

    @gate1_live_bug
    async def test_more_than_200_events_are_reachable_across_pages(self) -> None:
        burst = [
            an_event(f"e-{index:03d}", NOW + timedelta(milliseconds=index + 1))
            for index in range(250)
        ]
        cursor = 0
        observed: list[str] = []

        with patch.object(
            events.store,
            "list_events",
            AsyncMock(return_value=burst),
        ):
            for _ in range(3):
                page = await events.last_events("d", cursor, limit=200)
                if not page:
                    break
                observed.extend(row.id for row in page)
                next_cursor = max(events.ts_ms(row) for row in page)
                self.assertGreater(next_cursor, cursor, "the feed cursor did not advance")
                cursor = next_cursor

        self.assertEqual(
            observed,
            [row.id for row in burst],
            "the newest-200 slice made the beginning of the burst unreachable",
        )


class ProviderRejectionIsNotDelivery(unittest.IsolatedAsyncioTestCase):
    """A Telegram response body is part of the transport outcome."""

    async def test_telegram_ok_false_is_not_marked_delivered(self) -> None:
        marked: list[str] = []

        async def mark_channel_done(send_id: str, channel: str) -> None:
            self.assertEqual(send_id, "send-1")
            marked.append(channel)

        send_card = AsyncMock(
            return_value={"ok": False, "description": "Bad Request: chat not found"}
        )
        out = adapters.Fanout()
        out.channels = (adapters.TelegramAdapter(),)

        with (
            patch.object(
                adapters.store,
                "channels_done",
                AsyncMock(return_value=frozenset()),
            ),
            patch.object(adapters.store, "mark_channel_done", mark_channel_done),
            patch.object(
                adapters.telegram,
                "chat_id_for",
                AsyncMock(return_value=7700),
            ),
            patch.object(adapters.telegram, "enabled", return_value=True),
            patch.object(adapters.telegram, "send_card", send_card),
        ):
            for _ in range(2):
                try:
                    await out.send(
                        "doctor:tok",
                        OutboundMessage(
                            text="critical result",
                            receipt="send-1",
                        ),
                    )
                except Exception:
                    # Raising is a valid legacy-adapter representation of a
                    # retryable provider rejection.  Gate 2 may instead return a
                    # typed RETRYABLE result.  Neither may mark the channel done.
                    pass

        self.assertEqual(
            2,
            send_card.await_count,
            "Telegram ok:false was cached as locally delivered and suppressed retry",
        )
        self.assertNotIn(
            "telegram",
            marked,
            "Telegram ok:false was persisted as proof of delivery",
        )


class AnAnswerWindowIsOnTheRecordAndNotOnTheChannel(unittest.IsolatedAsyncioTestCase):
    """S24-C. A compose window belongs to the card, not to the door it opened.

    S23 scoped it to its channel, which fixed one bug and made another: a
    doctor who tapped "Answer" on his phone and then typed the answer into the
    console was not answering anything, and the console could not close a
    window it could not see. The window is global again and the record is what
    is protected: the answer runs the same action claim the button runs, so
    two answers to one question are one send and one addendum, whichever
    surfaces they arrive on.

    The cost is the S23 defect coming back on its own terms: while a window is
    open, the doctor's next typed message on ANY surface is read as the
    answer. The ten-minute expiry and /cancel are what bound it, and /cancel
    now works from either surface too.
    """

    async def test_a_phone_window_is_answered_from_the_web_console(self) -> None:
        doctor = a_doctor()
        relay = Relay(
            id="relay-1",
            doctor_id=doctor.id,
            patient_id="patient-1",
            question="Can I stop the medicine?",
            created_at=NOW,
        )

        async def update_doctor(doctor_id: str, **fields: object) -> None:
            self.assertEqual(doctor_id, doctor.id)
            for key, value in fields.items():
                setattr(doctor, key, value)

        async def answer_relay(_doctor: Doctor, _relay_id: str, _text: str) -> None:
            doctor.awaiting_relay_id = None

        doctor_reply = AsyncMock(side_effect=answer_relay)
        registrar_inbound = AsyncMock()

        with (
            patch.object(
                tg_router.store,
                "doctor_by_telegram",
                AsyncMock(return_value=doctor),
            ),
            patch.object(tg_router.store, "update_doctor", update_doctor),
            patch.object(tg_router.store, "now", return_value=NOW),
            patch.object(tg_router.telegram, "send_card", AsyncMock()),
            patch.object(tg_router.telegram, "answer_callback", AsyncMock()),
            patch.object(
                dispatch.store,
                "doctor_by_token",
                AsyncMock(return_value=doctor),
            ),
            patch.object(
                dispatch.store,
                "get_relay",
                AsyncMock(return_value=relay),
            ),
            patch.object(
                doctor_actions.store, "list_events", AsyncMock(return_value=[])
            ),
            patch.object(
                doctor_actions.store, "claim_action", AsyncMock(return_value=True)
            ),
            patch.object(dispatch.events, "append_event", AsyncMock()),
            patch.object(dispatch.concierge, "doctor_reply", doctor_reply),
            patch.object(dispatch.registrar, "handle_doctor", registrar_inbound),
        ):
            await tg_router._callback(
                {
                    "id": "callback-reply",
                    "data": "reply:relay-1",
                    "message": {"chat": {"id": 7700}},
                },
                "https://sanad.example/",
            )
            await dispatch.handle_inbound(
                InboundMessage(
                    channel="web",
                    sender_ref=f"doctor:{doctor.web_token}",
                    text="Yes, continue it until we review.",
                )
            )

        registrar_inbound.assert_not_awaited()
        doctor_reply.assert_awaited_once_with(
            doctor,
            relay.id,
            "Yes, continue it until we review.",
        )

    async def test_a_phone_window_is_cancelled_from_the_web_console(self) -> None:
        doctor = a_doctor(
            awaiting_note_loop_id="loop-1",
            awaiting_since=NOW,
            awaiting_channel="telegram",
        )
        note_to_patient = AsyncMock()
        registrar_inbound = AsyncMock()
        said: list[str] = []

        async def update_doctor(doctor_id: str, **fields: object) -> None:
            self.assertEqual(doctor_id, doctor.id)
            for key, value in fields.items():
                setattr(doctor, key, value)

        async def send(_ref: str, message: OutboundMessage) -> None:
            said.append(message.text)

        with (
            patch.object(
                dispatch.store,
                "doctor_by_token",
                AsyncMock(return_value=doctor),
            ),
            patch.object(dispatch.store, "update_doctor", update_doctor),
            patch.object(dispatch.store, "now", return_value=NOW),
            patch.object(dispatch.events, "append_event", AsyncMock()),
            patch.object(dispatch.concierge, "note_to_patient", note_to_patient),
            patch.object(dispatch.registrar, "handle_doctor", registrar_inbound),
            patch.object(dispatch, "fanout", lambda: SimpleNamespace(send=send)),
        ):
            await dispatch.handle_inbound(
                InboundMessage(
                    channel="web",
                    sender_ref=f"doctor:{doctor.web_token}",
                    text="/cancel",
                )
            )

        note_to_patient.assert_not_awaited()
        registrar_inbound.assert_not_awaited()
        self.assertEqual([dispatch.CANCELLED], said)
        self.assertIsNone(doctor.awaiting_note_loop_id)
        self.assertIsNone(doctor.awaiting_channel)


class TelegramCallbacksUseActionIdempotency(unittest.IsolatedAsyncioTestCase):
    """Every surface must claim the action before doing domain work."""

    async def test_two_callbacks_for_one_action_carry_it_out_once(self) -> None:
        doctor = a_doctor()
        commit = AsyncMock()
        claimed: set[tuple[str, str]] = set()
        claim_lock = asyncio.Lock()

        async def claim_action(doctor_id: str, action_id: str) -> bool:
            key = doctor_id, action_id
            async with claim_lock:
                if key in claimed:
                    return False
                claimed.add(key)
                return True

        def query(callback_id: str) -> dict[str, object]:
            return {
                "id": callback_id,
                "data": "confirm:confirm-1",
                "message": {"chat": {"id": 7700}},
            }

        with (
            patch.object(
                tg_router.store,
                "doctor_by_telegram",
                AsyncMock(return_value=doctor),
            ),
            patch.object(tg_router.store, "claim_action", claim_action),
            patch.object(
                doctor_actions.store, "list_events", AsyncMock(return_value=[])
            ),
            patch.object(doctor_actions.registrar, "commit", commit),
            patch.object(tg_router.telegram, "answer_callback", AsyncMock()),
        ):
            await asyncio.gather(
                tg_router._callback(query("callback-1"), "https://sanad.example/"),
                tg_router._callback(query("callback-2"), "https://sanad.example/"),
            )

        self.assertEqual(
            commit.await_count,
            1,
            "Telegram callbacks bypassed the action claim and ran one action twice",
        )

    async def test_failed_callback_releases_the_action_for_a_retry(self) -> None:
        doctor = a_doctor()
        claimed: set[tuple[str, str]] = set()
        released: list[tuple[str, str]] = []
        attempts = 0

        async def claim_action(doctor_id: str, action_id: str) -> bool:
            key = doctor_id, action_id
            if key in claimed:
                return False
            claimed.add(key)
            return True

        async def release_action(doctor_id: str, action_id: str) -> None:
            key = doctor_id, action_id
            released.append(key)
            claimed.remove(key)

        async def commit(*_args: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary registrar failure")

        query = {
            "id": "callback-confirm",
            "data": "confirm:confirm-1",
            "message": {"chat": {"id": 7700}},
        }
        answer_callback = AsyncMock()
        with (
            patch.object(
                tg_router.store,
                "doctor_by_telegram",
                AsyncMock(return_value=doctor),
            ),
            patch.object(tg_router.store, "claim_action", claim_action),
            patch.object(tg_router.store, "release_action", release_action),
            patch.object(
                doctor_actions.store, "list_events", AsyncMock(return_value=[])
            ),
            patch.object(doctor_actions.registrar, "commit", commit),
            patch.object(tg_router.telegram, "answer_callback", answer_callback),
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary registrar failure"):
                await tg_router._callback(query, "https://sanad.example/")
            await tg_router._callback(query, "https://sanad.example/")

        self.assertEqual(2, attempts)
        self.assertEqual(released, [(doctor.id, "confirm:confirm-1")])
        self.assertIn((doctor.id, "confirm:confirm-1"), claimed)
        answer_callback.assert_awaited_once_with("callback-confirm", "confirmed")


if __name__ == "__main__":
    unittest.main()
