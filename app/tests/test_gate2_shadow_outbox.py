"""Gate 2 contracts for the legacy shadow outbox.

The shadow is an observation of the legacy delivery path, never a second
delivery path.  These tests therefore keep both persistence and channels in
memory and count every call: a shadow write may fail, an existing stable
intent may be observed again, and a legacy provider may fail, but none of
those facts may manufacture, suppress, or relabel a legacy channel call.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

from core import adapters, channel_contracts, outbox, runtime
from core.adapters import OutboundMessage
# The golden journey photographs a seeded lab slip from docs/, which is outside
# the image build context. tests/_fixtures.py is the one place that asks
# whether a fixture family is here.
from tests._fixtures import HAS_SEED


NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)


def _label(value: Any) -> str:
    """Compare string enums and plain strings without prescribing either."""
    return str(getattr(value, "value", value)).upper()


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return value


def _keys(value: Any) -> set[str]:
    value = _dump(value)
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested
            for child in value.values()
            for nested in _keys(child)
        }
    if isinstance(value, (list, tuple)):
        return {nested for child in value for nested in _keys(child)}
    return set()


def _leaves(value: Any) -> list[Any]:
    value = _dump(value)
    if isinstance(value, dict):
        return [leaf for child in value.values() for leaf in _leaves(child)]
    if isinstance(value, (list, tuple)):
        return [leaf for child in value for leaf in _leaves(child)]
    return [value]


class RecordingChannel:
    """A channel double that records one call before returning or failing."""

    def __init__(
        self,
        name: str,
        *,
        receipt: Optional[str] = None,
        failure: Optional[BaseException] = None,
        order: Optional[list[str]] = None,
    ) -> None:
        self.name = name
        self.receipt = receipt
        self.failure = failure
        self.order = order
        self.calls: list[tuple[str, OutboundMessage]] = []

    async def send(
        self, target_ref: str, message: OutboundMessage
    ) -> Optional[str]:
        self.calls.append((target_ref, message.model_copy(deep=True)))
        if self.order is not None:
            self.order.append(self.name)
        if self.failure is not None:
            raise self.failure
        return self.receipt


class FakeIntentStore:
    """The create-if-absent/content-hash contract of the Firestore store."""

    def __init__(self) -> None:
        self.records: dict[str, channel_contracts.OutboxRecord] = {}
        self.create_calls: list[str] = []

    async def create_outbound_intent(
        self, record: channel_contracts.OutboxRecord
    ) -> channel_contracts.OutboxRecord:
        self.create_calls.append(record.id)
        existing = self.records.get(record.id)
        if existing is not None:
            if existing.content_hash != record.content_hash:
                raise ValueError(
                    "conflicting outbound intent: one stable id has two payload hashes"
                )
            return existing.model_copy(deep=True)
        self.records[record.id] = record.model_copy(deep=True)
        return record.model_copy(deep=True)


def _message(text: str = "hello", *, receipt: str = "") -> OutboundMessage:
    return OutboundMessage(
        text=text,
        receipt=receipt,
        patient_id="patient-context",
        card={"title": "A card", "lines": ["one"], "actions": []},
        meta={"audit": {"tier": "test", "generated": "fixed"}},
    )


def _intent(
    message: Optional[OutboundMessage] = None,
    *,
    now: datetime = NOW,
) -> channel_contracts.OutboundIntent:
    return outbox.legacy_intent(
        "doctor-internal",
        "patient",
        "patient-internal",
        message or _message(),
        contextual_patient_id="patient-context",
        now=now,
    )


class LegacyIntentContractTests(unittest.IsolatedAsyncioTestCase):
    def test_intent_contains_internal_ids_but_no_transport_secrets_or_bytes(self) -> None:
        message = _message()
        message.meta.update({
            "telegram_chat_id": 99112233,
            "doctor_token": "doctor-bearer-secret",
            "raw_bytes": b"attachment bytes",
            # Exercise value-type scrubbing independently of a revealing key.
            "opaque_payload": b"unlabelled attachment bytes",
            "opaque_mutable": bytearray(b"mutable attachment bytes"),
            "opaque_view": memoryview(b"view attachment bytes"),
        })
        intent = _intent(message)
        body = intent.model_dump(mode="python")

        self.assertEqual(intent.doctor_id, "doctor-internal")
        self.assertEqual(intent.recipient_type, "patient")
        self.assertEqual(intent.recipient_id, "patient-internal")
        self.assertEqual(intent.patient_id, "patient-context")

        forbidden = {
            "target_ref",
            "web_token",
            "doctor_token",
            "telegram_chat_id",
            "chat_id",
            "audio_bytes",
            "image_bytes",
            "raw_bytes",
        }
        self.assertTrue(
            forbidden.isdisjoint(_keys(body)),
            f"outbound intent persisted a transport-only field: "
            f"{sorted(forbidden & _keys(body))}",
        )
        self.assertFalse(
            any(isinstance(value, (bytes, bytearray, memoryview))
                for value in _leaves(body)),
            "raw attachment bytes belong in referenced storage, not the outbox",
        )
        rendered = repr(body)
        self.assertNotIn("99112233", rendered)
        self.assertNotIn("doctor-bearer-secret", rendered)
        self.assertNotIn("unlabelled attachment bytes", rendered)
        self.assertNotIn("mutable attachment bytes", rendered)
        self.assertNotIn("memory at", rendered)

    def test_receipt_is_stable_but_receiptless_messages_are_explicitly_unstable(
        self,
    ) -> None:
        first = _intent(_message(receipt="wake-receipt"), now=NOW)
        later = _intent(
            _message(receipt="wake-receipt"), now=NOW + timedelta(minutes=5)
        )
        another_receipt = _intent(_message(receipt="other-receipt"), now=NOW)

        self.assertTrue(first.stable_idempotency)
        self.assertEqual(first.id, later.id)
        self.assertEqual(first.idempotency_key, later.idempotency_key)
        self.assertNotEqual(first.id, another_receipt.id)
        self.assertNotEqual(first.idempotency_key, another_receipt.idempotency_key)

        unstable_one = _intent(_message(text="same text"), now=NOW)
        unstable_two = _intent(_message(text="same text"), now=NOW)
        self.assertFalse(unstable_one.stable_idempotency)
        self.assertFalse(unstable_two.stable_idempotency)
        self.assertNotEqual(unstable_one.id, unstable_two.id)
        self.assertNotEqual(
            unstable_one.idempotency_key,
            unstable_two.idempotency_key,
            "text is not an idempotency key: identical legitimate messages "
            "must remain distinct",
        )

    async def test_record_shadow_is_non_dispatchable_and_unknown(self) -> None:
        fake = FakeIntentStore()
        intent = _intent(_message(receipt="stable"))
        with patch.object(
            outbox.store,
            "create_outbound_intent",
            fake.create_outbound_intent,
        ):
            record = await outbox.record_shadow(intent)

        self.assertIsInstance(record, channel_contracts.OutboxRecord)
        self.assertEqual(record.intent, intent)
        self.assertTrue(record.content_hash)
        self.assertEqual(_label(record.state), "SHADOW")
        self.assertEqual(_label(record.delivery), "UNKNOWN")
        self.assertEqual(fake.create_calls, [record.id])

    async def test_stable_create_is_idempotent_and_hash_conflicts_are_rejected(
        self,
    ) -> None:
        fake = FakeIntentStore()
        first = _intent(_message(text="one", receipt="stable"), now=NOW)
        retry = _intent(
            _message(text="one", receipt="stable"),
            now=NOW + timedelta(hours=1),
        )
        conflict = _intent(
            _message(text="different payload", receipt="stable"),
            now=NOW + timedelta(hours=2),
        )

        with patch.object(
            outbox.store,
            "create_outbound_intent",
            fake.create_outbound_intent,
        ):
            first_record = await outbox.record_shadow(first)
            retry_record = await outbox.record_shadow(retry)
            with self.assertRaisesRegex(ValueError, "conflicting outbound intent"):
                await outbox.record_shadow(conflict)

        self.assertEqual(first_record.id, retry_record.id)
        self.assertEqual(first_record.content_hash, retry_record.content_hash)
        self.assertEqual(len(fake.records), 1)

    async def test_record_shadow_never_invokes_a_channel_adapter(self) -> None:
        fake = FakeIntentStore()
        web = AsyncMock(side_effect=AssertionError("shadow invoked WebAdapter"))
        telegram = AsyncMock(
            side_effect=AssertionError("shadow invoked TelegramAdapter")
        )
        with patch.object(
            outbox.store,
            "create_outbound_intent",
            fake.create_outbound_intent,
        ), patch.object(adapters.WebAdapter, "send", web), patch.object(
            adapters.TelegramAdapter, "send", telegram
        ):
            await outbox.record_shadow(_intent(_message(receipt="stable")))

        web.assert_not_awaited()
        telegram.assert_not_awaited()


class FanoutShadowContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Fanout is deliberately a legacy-runtime seam. Keep this module
        # hermetic if a developer's shell is already testing the future mode.
        legacy = patch.object(runtime, "legacy_runtime", return_value=True)
        legacy.start()
        self.addCleanup(legacy.stop)

    async def asyncTearDown(self) -> None:
        # The production registry is process-wide. Leave no pending observer
        # from a failed timing assertion for the next isolated event loop.
        tasks = tuple(adapters._SHADOW_TASKS)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        adapters._SHADOW_TASKS.clear()

    async def test_disabled_legacy_runtime_fails_before_shadow_or_delivery(
        self,
    ) -> None:
        shadow = AsyncMock(
            side_effect=AssertionError("disabled legacy runtime wrote a shadow")
        )
        resolve = AsyncMock(
            side_effect=AssertionError("disabled legacy runtime resolved a recipient")
        )
        fan = adapters.Fanout()
        web = RecordingChannel("web", receipt="web-event")
        telegram = RecordingChannel("telegram")
        fan.channels = (web, telegram)

        with patch.object(runtime, "legacy_runtime", return_value=False), \
                patch.object(runtime, "outbox_mode", return_value="shadow"), \
                patch.object(adapters, "resolve_target", resolve), \
                patch.object(outbox, "record_shadow", shadow):
            with self.assertRaisesRegex(RuntimeError, "replacement sender"):
                await fan.send("patient:patient-internal", _message())

        resolve.assert_not_awaited()
        shadow.assert_not_awaited()
        self.assertEqual(web.calls, [])
        self.assertEqual(telegram.calls, [])

    async def test_shadow_target_resolution_never_persists_the_doctor_token(self) -> None:
        captured: list[channel_contracts.OutboundIntent] = []

        async def capture(intent: channel_contracts.OutboundIntent) -> None:
            captured.append(intent.model_copy(deep=True))

        fan = adapters.Fanout()
        web = RecordingChannel("web", receipt="web-event")
        telegram = RecordingChannel("telegram")
        fan.channels = (web, telegram)
        secret_target = "doctor:do-not-persist-this-token"

        with patch.object(runtime, "outbox_mode", return_value="shadow"), \
                patch.object(
                    adapters,
                    "resolve_target",
                    AsyncMock(return_value=adapters.ResolvedTarget(
                        doctor_id="doctor-internal",
                        patient_id=None,
                        synthetic=False,
                    )),
                ), patch.object(
                    adapters.store,
                    "get_patient",
                    AsyncMock(return_value=SimpleNamespace(
                        id="patient-context",
                        doctor_id="doctor-internal",
                        synthetic=False,
                    )),
                ), patch.object(outbox, "record_shadow", capture):
            receipt = await fan.send(secret_target, _message())

        self.assertEqual(receipt, "web-event")
        self.assertEqual(len(captured), 1)
        rendered = repr(captured[0].model_dump(mode="python"))
        self.assertNotIn("do-not-persist-this-token", rendered)
        self.assertNotIn("target_ref", _keys(captured[0]))
        self.assertEqual(captured[0].doctor_id, "doctor-internal")
        self.assertEqual(captured[0].recipient_id, "doctor-internal")
        self.assertIs(False, captured[0].synthetic)
        self.assertEqual(len(web.calls), 1)
        self.assertEqual(len(telegram.calls), 1)

    async def test_shadow_preserves_resolved_real_and_synthetic_provenance(
        self,
    ) -> None:
        for synthetic in (False, True):
            with self.subTest(synthetic=synthetic):
                captured: list[channel_contracts.OutboundIntent] = []

                async def capture(intent: channel_contracts.OutboundIntent) -> None:
                    captured.append(intent.model_copy(deep=True))

                fan = adapters.Fanout()
                web = RecordingChannel("web", receipt="web-event")
                telegram = RecordingChannel("telegram")
                fan.channels = (web, telegram)
                target = adapters.ResolvedTarget(
                    doctor_id="doctor-internal",
                    patient_id="patient-internal",
                    synthetic=synthetic,
                )

                with patch.object(runtime, "outbox_mode", return_value="shadow"), \
                        patch.object(
                            adapters,
                            "resolve_target",
                            AsyncMock(return_value=target),
                        ), patch.object(outbox, "record_shadow", capture):
                    receipt = await fan.send(
                        "patient:patient-internal",
                        _message(),
                    )

                self.assertEqual(receipt, "web-event")
                self.assertEqual(len(captured), 1)
                self.assertIs(synthetic, captured[0].synthetic)
                self.assertEqual(captured[0].doctor_id, "doctor-internal")
                self.assertEqual(captured[0].patient_id, "patient-internal")
                self.assertEqual(len(web.calls), 1)
                self.assertEqual(len(telegram.calls), 1)

    async def test_resolve_target_derives_patient_provenance_fail_closed(
        self,
    ) -> None:
        cases = (
            (False, False, True, False),
            (True, False, True, True),
            (False, True, True, True),
            (False, False, False, True),
        )
        for (
            patient_synthetic,
            doctor_synthetic,
            doctor_exists,
            expected,
        ) in cases:
            with self.subTest(
                patient_synthetic=patient_synthetic,
                doctor_synthetic=doctor_synthetic,
                doctor_exists=doctor_exists,
            ):
                patient = SimpleNamespace(
                    id="patient-internal",
                    doctor_id="doctor-internal",
                    synthetic=patient_synthetic,
                )
                doctor = (
                    SimpleNamespace(
                        id="doctor-internal",
                        synthetic=doctor_synthetic,
                    )
                    if doctor_exists
                    else None
                )
                with patch.object(
                    adapters.store,
                    "get_patient",
                    AsyncMock(return_value=patient),
                ), patch.object(
                    adapters.store,
                    "doctor_by_id",
                    AsyncMock(return_value=doctor),
                ):
                    target = await adapters.resolve_target(
                        "patient:patient-internal"
                    )

                self.assertIsNotNone(target)
                assert target is not None
                self.assertEqual(target.doctor_id, "doctor-internal")
                self.assertEqual(target.patient_id, "patient-internal")
                self.assertIs(expected, target.synthetic)

    async def test_shadow_failure_preserves_order_result_and_one_legacy_call(self) -> None:
        order: list[str] = []

        async def fail_shadow(_: channel_contracts.OutboundIntent) -> None:
            order.append("shadow")
            raise RuntimeError("shadow Firestore unavailable")

        fan = adapters.Fanout()
        web = RecordingChannel("web", receipt="web-event", order=order)
        telegram = RecordingChannel("telegram", order=order)
        fan.channels = (web, telegram)

        with patch.object(runtime, "outbox_mode", return_value="shadow"), \
                patch.object(
                    adapters,
                    "resolve_target",
                    AsyncMock(return_value=adapters.ResolvedTarget(
                        doctor_id="doctor-internal",
                        patient_id="patient-internal",
                        synthetic=False,
                    )),
                ), patch.object(outbox, "record_shadow", fail_shadow):
            receipt = await fan.send("patient:patient-internal", _message())

        self.assertEqual(receipt, "web-event")
        self.assertEqual(order.count("shadow"), 1)
        self.assertEqual(
            [entry for entry in order if entry != "shadow"],
            ["web", "telegram"],
            "concurrent observation must not reorder legacy channels",
        )
        self.assertEqual(len(web.calls), 1)
        self.assertEqual(len(telegram.calls), 1)

    async def test_outbox_mode_off_performs_no_shadow_write(self) -> None:
        shadow = AsyncMock(side_effect=AssertionError("off mode wrote a shadow"))
        resolve = AsyncMock(
            side_effect=AssertionError("off mode resolved a shadow recipient")
        )
        fan = adapters.Fanout()
        web = RecordingChannel("web", receipt="web-event")
        telegram = RecordingChannel("telegram")
        fan.channels = (web, telegram)

        with patch.object(runtime, "outbox_mode", return_value="off"), \
                patch.object(adapters, "resolve_target", resolve), \
                patch.object(outbox, "record_shadow", shadow):
            receipt = await fan.send("patient:patient-internal", _message())

        self.assertEqual(receipt, "web-event")
        shadow.assert_not_awaited()
        resolve.assert_not_awaited()
        self.assertEqual(len(web.calls), 1)
        self.assertEqual(len(telegram.calls), 1)

    async def test_invalid_request_time_shadow_config_preserves_legacy_delivery(
        self,
    ) -> None:
        cases = (
            {"OUTBOX_MODE": "active"},
            {
                "OUTBOX_MODE": "shadow",
                "OUTBOX_SHADOW_TIMEOUT_MS": "unbounded",
            },
            {
                "OUTBOX_MODE": "shadow",
                "OUTBOX_SHADOW_MAX_IN_FLIGHT": "0",
            },
        )
        for environment in cases:
            with self.subTest(environment=environment):
                shadow = AsyncMock(
                    side_effect=AssertionError(
                        "invalid request config wrote a shadow"
                    )
                )
                resolve = AsyncMock(
                    side_effect=AssertionError(
                        "invalid request config resolved a shadow recipient"
                    )
                )
                fan = adapters.Fanout()
                web = RecordingChannel("web", receipt="web-event")
                telegram = RecordingChannel("telegram")
                fan.channels = (web, telegram)

                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(adapters, "resolve_target", resolve), \
                        patch.object(outbox, "record_shadow", shadow), \
                        patch.object(adapters.log, "exception"):
                    receipt = await fan.send(
                        "patient:patient-internal",
                        _message(),
                    )

                self.assertEqual(receipt, "web-event")
                shadow.assert_not_awaited()
                resolve.assert_not_awaited()
                self.assertEqual(len(web.calls), 1)
                self.assertEqual(len(telegram.calls), 1)

    async def test_hung_shadow_is_bounded_and_cleaned_after_channels_run_once(
        self,
    ) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        never = asyncio.Event()
        order: list[str] = []

        async def hang(_: channel_contracts.OutboxRecord) -> bool:
            order.append("shadow-started")
            started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                order.append("shadow-cancelled")
                cancelled.set()
                raise

        fan = adapters.Fanout()
        web = RecordingChannel("web", receipt="web-event", order=order)
        telegram = RecordingChannel("telegram", order=order)
        fan.channels = (web, telegram)
        target = adapters.ResolvedTarget(
            doctor_id="doctor-internal",
            patient_id="patient-internal",
            synthetic=False,
        )

        with patch.object(runtime, "outbox_mode", return_value="shadow"), \
                patch.object(runtime, "shadow_timeout_seconds", return_value=0.01), \
                patch.object(runtime, "shadow_max_in_flight", return_value=32), \
                patch.object(
                    adapters,
                    "resolve_target",
                    AsyncMock(return_value=target),
                ), patch.object(
                    outbox.store,
                    "create_outbound_intent",
                    hang,
                ), patch.object(adapters.log, "warning"):
            async with asyncio.timeout(0.5):
                receipt = await fan.send(
                    "patient:patient-internal",
                    _message(),
                )

        self.assertEqual(receipt, "web-event")
        self.assertTrue(started.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(web.calls), 1)
        self.assertEqual(len(telegram.calls), 1)
        self.assertLess(order.index("web"), order.index("shadow-cancelled"))
        self.assertLess(order.index("telegram"), order.index("shadow-cancelled"))
        await asyncio.sleep(0)
        self.assertEqual(set(), adapters._SHADOW_TASKS)

    async def test_shadow_capacity_drop_does_not_change_either_delivery(
        self,
    ) -> None:
        persistence_started = asyncio.Event()
        release_persistence = asyncio.Event()
        persisted: list[channel_contracts.OutboxRecord] = []

        async def hold(record: channel_contracts.OutboxRecord) -> bool:
            persisted.append(record.model_copy(deep=True))
            persistence_started.set()
            await release_persistence.wait()
            return True

        target = adapters.ResolvedTarget(
            doctor_id="doctor-internal",
            patient_id="patient-internal",
            synthetic=False,
        )
        resolve = AsyncMock(return_value=target)
        first_fan = adapters.Fanout()
        retry_fan = adapters.Fanout()
        first_web = RecordingChannel("web", receipt="first-event")
        first_telegram = RecordingChannel("telegram")
        retry_web = RecordingChannel("web", receipt="retry-event")
        retry_telegram = RecordingChannel("telegram")
        first_fan.channels = (first_web, first_telegram)
        retry_fan.channels = (retry_web, retry_telegram)

        with patch.object(runtime, "outbox_mode", return_value="shadow"), \
                patch.object(runtime, "shadow_timeout_seconds", return_value=1.0), \
                patch.object(runtime, "shadow_max_in_flight", return_value=1), \
                patch.object(adapters, "resolve_target", resolve), \
                patch.object(
                    outbox.store,
                    "create_outbound_intent",
                    hold,
                ), patch.object(adapters.log, "warning") as warning:
            first_send = asyncio.create_task(
                first_fan.send("patient:patient-internal", _message("first"))
            )
            await persistence_started.wait()

            async with asyncio.timeout(0.5):
                retry_receipt = await retry_fan.send(
                    "patient:patient-internal",
                    _message("retry"),
                )

            release_persistence.set()
            first_receipt = await first_send

        self.assertEqual(first_receipt, "first-event")
        self.assertEqual(retry_receipt, "retry-event")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(resolve.await_count, 1)
        self.assertEqual(len(first_web.calls), 1)
        self.assertEqual(len(first_telegram.calls), 1)
        self.assertEqual(len(retry_web.calls), 1)
        self.assertEqual(len(retry_telegram.calls), 1)
        self.assertTrue(
            any("capacity_drop" in str(call.args[0])
                for call in warning.call_args_list),
            "capacity exhaustion was not observable",
        )
        await asyncio.sleep(0)
        self.assertEqual(set(), adapters._SHADOW_TASKS)

    async def test_legacy_delivery_failure_is_not_relabelled_as_shadow_success(
        self,
    ) -> None:
        fake = FakeIntentStore()
        fan = adapters.Fanout()
        web = RecordingChannel("web", receipt="web-event")
        telegram = RecordingChannel(
            "telegram", failure=RuntimeError("Telegram rejected the send")
        )
        fan.channels = (web, telegram)

        with patch.object(runtime, "outbox_mode", return_value="shadow"), \
                patch.object(
                    adapters,
                    "resolve_target",
                    AsyncMock(return_value=adapters.ResolvedTarget(
                        doctor_id="doctor-internal",
                        patient_id="patient-internal",
                        synthetic=False,
                    )),
                ), patch.object(
                    outbox.store,
                    "create_outbound_intent",
                    fake.create_outbound_intent,
                ):
            with self.assertRaisesRegex(RuntimeError, "Telegram rejected"):
                await fan.send("patient:patient-internal", _message())

        self.assertEqual(len(web.calls), 1)
        self.assertEqual(len(telegram.calls), 1)
        self.assertEqual(len(fake.records), 1)
        record = next(iter(fake.records.values()))
        self.assertEqual(_label(record.state), "SHADOW")
        self.assertEqual(_label(record.delivery), "UNKNOWN")

    async def test_repeated_stable_fanout_key_has_one_shadow_record(self) -> None:
        fake = FakeIntentStore()
        first_fan = adapters.Fanout()
        retry_fan = adapters.Fanout()
        web = RecordingChannel("web", receipt="web-event")
        telegram = RecordingChannel("telegram")
        first_fan.channels = (web, telegram)
        retry_fan.channels = (web, telegram)
        message = _message(text="same stable delivery", receipt="stable-key")
        delivered: set[str] = set()

        async def channels_done(_: str) -> frozenset[str]:
            return frozenset(delivered)

        async def mark_channel_done(_: str, channel: str) -> None:
            delivered.add(channel)

        with patch.object(runtime, "outbox_mode", return_value="shadow"), \
                patch.object(
                    adapters,
                    "resolve_target",
                    AsyncMock(return_value=adapters.ResolvedTarget(
                        doctor_id="doctor-internal",
                        patient_id="patient-internal",
                        synthetic=False,
                    )),
                ), patch.object(
                    outbox.store,
                    "create_outbound_intent",
                    fake.create_outbound_intent,
                ), patch.object(
                    adapters.store,
                    "channels_done",
                    channels_done,
                ), patch.object(
                    adapters.store,
                    "mark_channel_done",
                    mark_channel_done,
                ):
            first = await first_fan.send("patient:patient-internal", message)
            second = await retry_fan.send("patient:patient-internal", message)

        self.assertEqual(first, "web-event")
        self.assertIsNone(second)
        self.assertEqual(len(fake.records), 1)
        self.assertEqual(set(fake.create_calls), set(fake.records))
        self.assertEqual(len(web.calls), 1)
        self.assertEqual(len(telegram.calls), 1)


@HAS_SEED
class GoldenJourneyShadowContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_mode_is_byte_neutral_across_the_golden_journey(
        self,
    ) -> None:
        from tests.gate0b.artifacts import (
            GOLDENS,
            dumps,
            legacy_projection,
            read_json,
        )
        from tests.gate0b.scenario import GoldenJourney

        captured: list[channel_contracts.OutboxRecord] = []

        async def capture(record: channel_contracts.OutboxRecord) -> bool:
            captured.append(record.model_copy(deep=True))
            return True

        with patch.dict(
            os.environ,
            {"LEGACY_RUNTIME": "true", "OUTBOX_MODE": "shadow"},
        ), patch.object(
            outbox.store,
            "create_outbound_intent",
            capture,
        ):
            result = await GoldenJourney().run()

        self.assertGreater(
            len(captured),
            0,
            "the active legacy journey did not reach the shadow outbox seam",
        )
        for record in captured:
            self.assertIsInstance(record, channel_contracts.OutboxRecord)
            self.assertEqual(_label(record.state), "SHADOW")
            self.assertEqual(_label(record.delivery), "UNKNOWN")

        payloads = result.artifact_payloads()
        committed_manifest = read_json(GOLDENS / "manifest.json")
        expected_payloads = {
            relative
            for relative in committed_manifest["artifact_sha256"]
            if relative.endswith(".json")
            and relative.startswith(("beats/", "traces/"))
        }
        self.assertEqual(
            expected_payloads,
            set(payloads),
            "the shadow replay omitted or invented a Gate 0B journey artifact",
        )
        for relative in sorted(expected_payloads):
            committed = GOLDENS / relative
            projected = legacy_projection(payloads[relative], read_json(committed))
            self.assertEqual(
                committed.read_bytes(),
                dumps(projected).encode("utf-8"),
                f"shadow mode changed the Gate 0B artifact {relative}",
            )

        committed_manifest = dict(committed_manifest)
        committed_manifest.pop("artifact_sha256")
        self.assertEqual(
            dumps(committed_manifest).encode("utf-8"),
            dumps(result.manifest).encode("utf-8"),
            "shadow mode changed the Gate 0B manifest contract",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
