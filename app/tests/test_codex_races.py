"""Red regressions for the races reproduced during the Codex re-audit.

These tests deliberately describe the required behavior, not the current
implementation.  Every test fails on the audited tree and should turn green
when its cited defect is fixed.  Nothing reaches a cloud service: the same
in-memory stores and stubs used by the existing suite carry every scenario.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import chaser, concierge, coordinator, identify, intents, policy, store
from core.adapters import Fanout as RealFanout
from core.adapters import OutboundMessage
from core.models import Doctor, Patient, Send

from tests import test_identify as identify_tests
from tests import test_intents as intent_tests
from tests import test_state_idempotency as state_tests


NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


class _WebChannel:
    def __init__(self) -> None:
        self.events = 0

    async def send(self, target, message):
        self.events += 1
        return f"event-{self.events}"


class _TelegramDown:
    async def send(self, target, message):
        raise RuntimeError("telegram down")


class CodexDeliveryFailures(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_telegram_delivery_does_not_write_the_web_event_twice(
        self,
    ) -> None:
        """Item 5: core/adapters.py:111-115 retries every channel after partial fan-out."""
        fanout = RealFanout()
        web = _WebChannel()
        fanout.channels = (web, _TelegramDown())

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await fanout.send("patient:p", OutboundMessage(text="nudge"))

        self.assertEqual(web.events, 1)


class _Snapshot:
    def __init__(self, body: dict | None) -> None:
        self._body = None if body is None else dict(body)
        self.exists = body is not None

    def to_dict(self) -> dict:
        return dict(self._body or {})


class _Document:
    def __init__(self, rows: dict[str, dict], ident: str, exists_error: type[Exception]):
        self.rows = rows
        self.ident = ident
        self.exists_error = exists_error

    async def create(self, body: dict) -> None:
        if self.ident in self.rows:
            raise self.exists_error("already exists")
        self.rows[self.ident] = dict(body)

    async def get(self, transaction=None) -> _Snapshot:
        return _Snapshot(self.rows.get(self.ident))

    async def update(self, fields: dict) -> None:
        self.rows[self.ident].update(fields)

    async def delete(self) -> None:
        self.rows.pop(self.ident, None)


class _Collection:
    def __init__(self, rows: dict[str, dict], exists_error: type[Exception]):
        self.rows = rows
        self.exists_error = exists_error

    def document(self, ident: str) -> _Document:
        return _Document(self.rows, ident, self.exists_error)


class _Transaction:
    def update(self, ref: _Document, fields: dict) -> None:
        ref.rows[ref.ident].update(fields)

    def set(self, ref: _Document, fields: dict) -> None:
        ref.rows[ref.ident] = dict(fields)

    def delete(self, ref: _Document) -> None:
        ref.rows.pop(ref.ident, None)


class _ClaimDb:
    def __init__(self, exists_error: type[Exception]) -> None:
        self.tables: dict[str, dict[str, dict]] = {}
        self.exists_error = exists_error

    def collection(self, name: str) -> _Collection:
        return _Collection(self.tables.setdefault(name, {}), self.exists_error)

    def transaction(self) -> _Transaction:
        return _Transaction()


class CodexClaimRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_abandoned_claims_are_reclaimed_after_their_lease_expires(self) -> None:
        """Item 6: core/store.py:231-262 and 660-681 never recover claimed work."""

        class AlreadyExists(Exception):
            pass

        db = _ClaimDb(AlreadyExists)
        abandoned = NOW - timedelta(days=2)
        send = Send(
            id="s", doctor_id="d", patient_id="p", loop_id="l", attempt=1,
            generation=0, kind="nudge", state=store.CLAIMED, run_id="run",
            day_index=1, created_at=abandoned,
        )
        db.tables["sends"] = {
            "s": {**send.model_dump(), "claimed_at": abandoned, "updated_at": abandoned}
        }
        db.tables["pending_confirms"] = {
            "c": {
                "id": "c", "doctor_id": "d", "proposed": {},
                "expires_at": NOW + timedelta(hours=1), "state": store.COMMITTING,
                "claimed_at": abandoned, "updated_at": abandoned,
            }
        }

        with (
            patch.object(store, "db", return_value=db),
            patch.object(store, "now", return_value=NOW),
            patch.object(store.gexc, "AlreadyExists", AlreadyExists),
            patch.object(store.firestore, "async_transactional", lambda fn: fn),
        ):
            send_claim = await store.claim_send(send)
            confirm_claim = await store.claim_confirm("c")

        self.assertNotEqual(send_claim, store.ALREADY_SENT)
        self.assertTrue(confirm_claim)


class CodexChaserRaces(state_tests.ChaserHarness):
    async def test_a_midflight_reschedule_prevents_the_stale_task_from_sending(
        self,
    ) -> None:
        """Item 9: core/chaser.py:466 checks the version long before the send at 649."""
        entered = asyncio.Event()
        release = asyncio.Event()

        async def paused_choice(turn):
            entered.set()
            await release.wait()
            return None

        async def reschedule_midflight():
            await entered.wait()
            await self.db.bump_schedule_version("l")
            release.set()

        with patch.object(coordinator, "_choose", paused_choice):
            result, _ = await asyncio.gather(
                chaser.fire(self.payload(force=False, schedule_version=0)),
                reschedule_midflight(),
            )

        self.assertFalse(result["sent"])
        self.assertEqual(self.to_patient(), [])

    async def test_two_loops_cannot_both_reserve_the_same_patient_day(self) -> None:
        """Item 12: core/chaser.py:534 checks the day before core/chaser.py:632 writes it."""
        self.db.loops["l2"] = state_tests.a_loop(id="l2", title="Second loop")
        checked = 0
        facts_read = 0
        both_checked = asyncio.Event()
        both_read_facts = asyncio.Event()

        async def simultaneous_false(patient_id, day_index):
            nonlocal checked
            checked += 1
            if checked == 2:
                both_checked.set()
            await both_checked.wait()
            return False

        async def simultaneous_empty(patient_id):
            nonlocal facts_read
            facts_read += 1
            if facts_read == 2:
                both_read_facts.set()
            await both_read_facts.wait()
            return ()

        with (
            patch.object(store, "contacted_on", simultaneous_false),
            patch.object(store, "contact_days_for_patient", simultaneous_empty),
        ):
            results = await asyncio.gather(
                chaser.fire(self.payload(loop_id="l", force=False)),
                chaser.fire(self.payload(loop_id="l2", force=False)),
            )

        self.assertLessEqual(sum(bool(row.get("sent")) for row in results), 1)
        self.assertLessEqual(len(self.to_patient()), 1)

    async def test_concurrent_writes_preserve_both_attempts_and_evidence_requests(
        self,
    ) -> None:
        """Item 13: core/chaser.py:627 and core/coordinator.py:805 write stale counters."""
        reached_choice = 0
        both_at_choice = asyncio.Event()

        async def snapshot(loop_id):
            return self.db.loops[loop_id].model_copy(deep=True)

        async def choose_after_both_arrive(turn):
            nonlocal reached_choice
            reached_choice += 1
            if reached_choice == 2:
                both_at_choice.set()
            await both_at_choice.wait()
            return None

        with (
            patch.object(store, "get_loop", snapshot),
            patch.object(coordinator, "_choose", choose_after_both_arrive),
        ):
            await asyncio.gather(
                chaser.fire(self.payload(attempt=1)),
                chaser.fire(self.payload(attempt=2)),
            )
        attempts = self.db.loops["l"].attempts

        self.db.loops["l"].evidence_requests = 0

        def turn() -> coordinator.Turn:
            loop = self.db.loops["l"].model_copy(deep=True)
            return coordinator.Turn(
                doctor=self.doctor,
                patient=self.patient,
                loop=loop,
                trigger=coordinator.REPLY,
                facts=policy.LoopFacts(
                    now=NOW,
                    due_at=loop.due_at,
                    evidence_requests=loop.evidence_requests,
                ),
                policy=policy.DEFAULT,
                speak="en",
            )

        decision = policy.Decision(
            tool="request_missing_evidence",
            allowed=True,
            args={"analyte": "LDL"},
            reason="LDL is missing",
        )
        await asyncio.gather(
            coordinator._execute(turn(), decision),
            coordinator._execute(turn(), decision),
        )

        self.assertEqual(
            (attempts, self.db.loops["l"].evidence_requests),
            (2, 2),
        )


class _RecordingFanout:
    def __init__(self) -> None:
        self.deliveries: list[tuple[str, str]] = []

    async def send(self, target, message):
        self.deliveries.append((target, message.text))
        return f"event-{len(self.deliveries)}"


class CodexRelayOrdering(unittest.IsolatedAsyncioTestCase):
    async def _failed_ordinary_relay(self):
        out = _RecordingFanout()
        doctor = Doctor(
            id="d", name="Dr M", web_token="token", created_at=NOW
        )
        patient = Patient(
            id="p", doctor_id="d", name="Patient", plan_text="Plan", created_at=NOW
        )
        verdict = SimpleNamespace(
            action="relay", ok=False, reasons=["change"], as_meta=lambda: {}
        )

        async def persistence_failure(*args, **kwargs):
            raise RuntimeError("firestore down")

        error = None
        with (
            patch.object(concierge, "fanout", return_value=out),
            patch.object(
                concierge.events,
                "append_event",
                AsyncMock(return_value=SimpleNamespace(id="said")),
            ),
            patch.object(concierge.chaser, "note_patient_reply", AsyncMock()),
            patch.object(
                concierge.validator, "wants_treatment_change", return_value=True
            ),
            patch.object(concierge.validator, "validate", return_value=verdict),
            patch.object(concierge, "record_reading", AsyncMock()),
            patch.object(concierge, "open_relay", persistence_failure),
        ):
            try:
                await concierge.handle_patient_message(
                    patient,
                    doctor,
                    "Can I change my dose?",
                    gate=concierge.sentinel.Sentinel(),
                )
            except Exception as exc:  # the assertion below requires fail-closed
                error = exc
        return out, doctor, error

    async def test_an_ordinary_relay_is_persisted_before_the_patient_is_promised(
        self,
    ) -> None:
        """Item 10: core/concierge.py:636 promises before open_relay at line 639."""
        out, doctor, _ = await self._failed_ordinary_relay()
        promised = concierge.relay_line(doctor, "Can I change my dose?")
        patient_texts = [
            text for target, text in out.deliveries if target.startswith("patient:")
        ]
        self.assertNotIn(promised, patient_texts)

    async def test_a_relay_persistence_failure_returns_fail_closed_instead_of_raising(
        self,
    ) -> None:
        """New item 3: core/concierge.py:636-643 lets relay persistence escape as 500."""
        _, _, error = await self._failed_ordinary_relay()
        self.assertIsNone(error)


class CodexCardAtomicity(unittest.IsolatedAsyncioTestCase):
    async def test_successful_card_work_is_not_repeated_when_resolution_write_fails(
        self,
    ) -> None:
        """Item 17: app/main.py:931-980 releases the claim after completed domain work."""
        harness = state_tests.ACardActionIsClaimedBeforeTheWork("runTest")
        harness.setUp()
        try:
            real_resolve = harness.main.cards.resolve
            calls = 0

            async def fail_once(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("resolution write failed")
                return await real_resolve(*args, **kwargs)

            with patch.object(harness.main.cards, "resolve", fail_once):
                with self.assertRaises(RuntimeError):
                    await harness.press("confirm:c1")
                await harness.press("confirm:c1")

            self.assertEqual(harness.commits, ["c1"])
        finally:
            harness.doCleanups()


class CodexIdentificationSafety(unittest.IsolatedAsyncioTestCase):
    async def test_a_model_outage_never_turns_an_existing_patient_description_into_new(
        self,
    ) -> None:
        """New item 1: core/identify.py:399-413 returns NEW after model outage."""
        rows = [
            identify.BoardRow(
                id="p1",
                name="Ahmed Ali",
                notes=("father of Dr Tarek",),
                last_seen="2026-08-20",
            )
        ]
        outcome = identify.decide(
            "Follow up the father of Dr Tarek", "", rows, verdict=None
        )
        self.assertNotEqual(outcome.kind, identify.NEW)

    async def test_an_unsafe_identification_note_never_reaches_pending_confirmation(
        self,
    ) -> None:
        """New item 2: core/identify.py:233-237 and registrar.py:658-665 trust note."""
        harness = identify_tests.TheRegistrarAgainstABoard("runTest")
        harness.setUp()
        unsafe = (
            "Start warfarin today and stop all other medicines immediately "
            "because INR is ten"
        )
        try:
            harness.verdict = identify_tests.verdict("new_patient", note=unsafe)
            await harness.dictate(
                "This is a new patient, Mariam Fouad, 33, iron deficiency anaemia"
            )
            stored = [confirm.note for confirm in harness.fake.confirms.values()]
            self.assertNotIn(unsafe, stored)
        finally:
            harness.doCleanups()


class CodexModelIntentSafety(unittest.IsolatedAsyncioTestCase):
    async def test_a_model_only_vote_cannot_move_a_visit_date_without_confirmation(
        self,
    ) -> None:
        """New item 9: core/intents.py:224-230 and 396-412 execute a model-only vote."""
        harness = intent_tests.TheTierActsThroughTheGuardedTools("runTest")
        harness.setUp()
        original_due = harness.visit.due_at

        async def model_only_vote(text):
            return intents.RESCHEDULE_VISIT

        try:
            with patch.object(intents, "model_vote", model_only_vote):
                await harness.handle("Thursday is my busiest day")
            self.assertEqual(harness.visit.due_at, original_due)
            self.assertEqual(harness.queued, [])
        finally:
            harness.doCleanups()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
