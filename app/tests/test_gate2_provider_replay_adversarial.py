"""Provider replay and transient-data attacks against live Gate 2 wiring."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import main as sanad_main
from core import command_replay, live_transport, outbox
from core.adapters import InboundMessage
from core.channel_contracts import ActorRef, Command, CommandResult, CommandStatus
from core.models import Doctor, Patient
from tests.gate0b.memory import MemoryStore, patched_store


NOW = datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc)
TELEGRAM_SECRET_HEADER = "x-telegram-bot-api-secret-token"


def doctor() -> Doctor:
    return Doctor(
        id="doctor-internal",
        name="Dr Test",
        specialty="cardiology",
        web_token="doctor-web-token",
        created_at=NOW,
    )


def patient() -> Patient:
    return Patient(
        id="patient-internal",
        doctor_id="doctor-internal",
        name="Patient Test",
        diagnosis="hypertension",
        plan_text="Measure blood pressure.",
        created_at=NOW,
    )


class RequestStub:
    def __init__(
        self,
        body: Optional[dict[str, Any]] = None,
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self._body = body or {}
        self.headers = headers or {}
        self.base_url = "https://sanad.test/"
        self.query_params: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


class ProviderReplayAdversarialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        previous = sanad_main._TRANSPORT
        sanad_main._TRANSPORT = None
        self.addCleanup(setattr, sanad_main, "_TRANSPORT", previous)

    async def test_same_start_update_runs_once_after_identity_resolution_changes(
        self,
    ) -> None:
        memory = MemoryStore(start=NOW)
        bound = False
        handler_calls = 0
        observed_commands: list[Command] = []
        observed_results: list[CommandResult] = []
        resolved_patient = patient()

        async def find_patient(_: int) -> Optional[Patient]:
            return resolved_patient if bound else None

        async def provider_handler(
            update: dict[str, Any],
            base_url: str,
            *,
            synthetic: bool,
        ) -> None:
            nonlocal bound, handler_calls
            self.assertEqual("/start one-time-patient-token", update["message"]["text"])
            self.assertEqual("https://sanad.test/", base_url)
            self.assertIs(False, synthetic)
            handler_calls += 1
            bound = True

        update = {
            "update_id": 9001,
            "message": {
                "chat": {"id": 880011},
                "text": "/start one-time-patient-token",
            },
        }
        with patched_store(memory):
            runtime = sanad_main.transport_runtime()
            real_execute = runtime.bus.execute

            async def observe(command: Command) -> CommandResult:
                observed_commands.append(command)
                result = await real_execute(command)
                observed_results.append(result)
                return result

            with (
                patch.object(
                    runtime.bus,
                    "execute",
                    new=AsyncMock(side_effect=observe),
                ),
                patch.object(
                    sanad_main.telegram,
                    "WEBHOOK_SECRET",
                    "verified-webhook-secret",
                ),
                patch.object(
                    sanad_main.store,
                    "doctor_by_telegram",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(
                    sanad_main.store,
                    "patient_by_telegram",
                    new=AsyncMock(side_effect=find_patient),
                ),
                patch.object(
                    sanad_main.tg_router,
                    "_handle_update",
                    new=AsyncMock(side_effect=provider_handler),
                ),
            ):
                first = await sanad_main.telegram_webhook(
                    RequestStub(
                        update,
                        headers={
                            TELEGRAM_SECRET_HEADER: "verified-webhook-secret"
                        },
                    )
                )
                second = await sanad_main.telegram_webhook(
                    RequestStub(
                        update,
                        headers={
                            TELEGRAM_SECRET_HEADER: "verified-webhook-secret"
                        },
                    )
                )

        self.assertEqual(first, second)
        self.assertEqual({"ok": True}, first)
        self.assertEqual(1, handler_calls)
        self.assertEqual(2, len(observed_commands))
        self.assertNotEqual(
            observed_commands[0].tenant_id,
            observed_commands[1].tenant_id,
        )
        self.assertEqual("unknown", observed_commands[0].actor.kind)
        self.assertEqual("patient", observed_commands[1].actor.kind)
        self.assertEqual(
            [CommandStatus.ACCEPTED, CommandStatus.ACCEPTED],
            [result.status for result in observed_results],
        )
        self.assertEqual(1, len(memory.command_receipts))

    async def test_same_update_id_with_changed_body_conflicts_without_reexecution(
        self,
    ) -> None:
        memory = MemoryStore(start=NOW)
        handler = AsyncMock()
        observed_commands: list[Command] = []
        observed_results: list[CommandResult] = []
        first_update = {
            "update_id": 9002,
            "message": {"chat": {"id": 880022}, "text": "original body"},
        }
        changed_update = {
            "update_id": 9002,
            "message": {"chat": {"id": 880022}, "text": "changed body"},
        }

        with patched_store(memory):
            runtime = sanad_main.transport_runtime()
            real_execute = runtime.bus.execute

            async def observe(command: Command) -> CommandResult:
                observed_commands.append(command)
                result = await real_execute(command)
                observed_results.append(result)
                return result

            with (
                patch.object(
                    runtime.bus,
                    "execute",
                    new=AsyncMock(side_effect=observe),
                ),
                patch.object(
                    sanad_main.telegram,
                    "WEBHOOK_SECRET",
                    "verified-secret",
                ),
                patch.object(
                    sanad_main.store,
                    "doctor_by_telegram",
                    new=AsyncMock(return_value=doctor()),
                ),
                patch.object(
                    sanad_main.tg_router,
                    "_handle_update",
                    new=handler,
                ),
            ):
                first = await sanad_main.telegram_webhook(
                    RequestStub(
                        first_update,
                        headers={TELEGRAM_SECRET_HEADER: "verified-secret"},
                    )
                )
                second = await sanad_main.telegram_webhook(
                    RequestStub(
                        changed_update,
                        headers={TELEGRAM_SECRET_HEADER: "verified-secret"},
                    )
                )

        self.assertEqual({"ok": True}, first)
        self.assertEqual({"ok": True}, second)
        handler.assert_awaited_once()
        self.assertEqual(2, len(observed_results))
        self.assertEqual(CommandStatus.ACCEPTED, observed_results[0].status)
        self.assertEqual(CommandStatus.CONFLICT, observed_results[1].status)
        self.assertEqual("idempotency_mismatch", observed_results[1].code)
        self.assertNotEqual(
            command_replay.command_fingerprint(observed_commands[0]),
            command_replay.command_fingerprint(observed_commands[1]),
        )
        self.assertEqual(1, len(memory.command_receipts))

    async def test_released_retiring_callback_retries_same_provider_update_once(
        self,
    ) -> None:
        memory = MemoryStore(start=NOW)
        chat_id = 880033
        bound_doctor = doctor().model_copy(
            update={"telegram_chat_id": chat_id},
            deep=True,
        )
        memory.doctors[bound_doctor.id] = bound_doctor
        attempts = 0
        observed_results: list[CommandResult] = []

        async def fail_then_commit(*_: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary registrar failure")

        update = {
            "update_id": 9004,
            "callback_query": {
                "id": "callback-provider-retry",
                "data": "confirm:confirm-provider-retry",
                "message": {"chat": {"id": chat_id}},
            },
        }
        headers = {TELEGRAM_SECRET_HEADER: "verified-callback-secret"}
        commit = AsyncMock(side_effect=fail_then_commit)
        release_action = AsyncMock(side_effect=memory.release_action)
        answer_callback = AsyncMock()

        with patched_store(memory):
            runtime = sanad_main.transport_runtime()
            real_execute = runtime.bus.execute

            async def observe(command: Command) -> CommandResult:
                result = await real_execute(command)
                observed_results.append(result)
                return result

            with (
                patch.object(
                    runtime.bus,
                    "execute",
                    new=AsyncMock(side_effect=observe),
                ),
                patch.object(
                    sanad_main.telegram,
                    "WEBHOOK_SECRET",
                    "verified-callback-secret",
                ),
                patch.object(sanad_main.store, "release_action", release_action),
                patch.object(sanad_main.registrar, "commit", commit),
                patch.object(
                    sanad_main.telegram,
                    "answer_callback",
                    answer_callback,
                ),
            ):
                with self.assertRaises(HTTPException) as first_failure:
                    await sanad_main.telegram_webhook(
                        RequestStub(update, headers=headers)
                    )
                self.assertEqual(503, first_failure.exception.status_code)
                self.assertEqual(
                    {},
                    memory.command_receipts,
                    "typed RETRYABLE must release the outer Telegram claim",
                )
                self.assertEqual(
                    {},
                    memory.card_actions,
                    "the retiring action must be free before provider retry",
                )

                second = await sanad_main.telegram_webhook(
                    RequestStub(update, headers=headers)
                )
                completed_replay = await sanad_main.telegram_webhook(
                    RequestStub(update, headers=headers)
                )

        self.assertEqual({"ok": True}, second)
        self.assertEqual(second, completed_replay)
        self.assertEqual(2, attempts)
        self.assertEqual(1, release_action.await_count)
        self.assertEqual(1, answer_callback.await_count)
        self.assertEqual(
            [
                CommandStatus.RETRYABLE,
                CommandStatus.ACCEPTED,
                CommandStatus.ACCEPTED,
            ],
            [result.status for result in observed_results],
        )
        self.assertEqual(1, len(memory.command_receipts))
        self.assertEqual(
            "COMPLETED",
            next(iter(memory.command_receipts.values()))["state"],
        )
        self.assertIn(
            f"{bound_doctor.id}:confirm:confirm-provider-retry",
            memory.card_actions,
        )

    async def test_failed_action_release_is_never_labeled_retryable(self) -> None:
        memory = MemoryStore(start=NOW)
        chat_id = 880034
        bound_doctor = doctor().model_copy(
            update={"telegram_chat_id": chat_id},
            deep=True,
        )
        memory.doctors[bound_doctor.id] = bound_doctor
        release_failure = RuntimeError("action release storage failed")
        query = {
            "id": "callback-ambiguous-release",
            "data": "confirm:confirm-ambiguous-release",
            "message": {"chat": {"id": chat_id}},
        }

        with patched_store(memory):
            with (
                patch.object(
                    sanad_main.registrar,
                    "commit",
                    new=AsyncMock(
                        side_effect=RuntimeError("temporary registrar failure")
                    ),
                ),
                patch.object(
                    sanad_main.store,
                    "release_action",
                    new=AsyncMock(side_effect=release_failure),
                ),
                patch.object(
                    sanad_main.telegram,
                    "answer_callback",
                    new=AsyncMock(),
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    await sanad_main.tg_router._callback(
                        query,
                        "https://sanad.test/",
                    )

        self.assertIs(release_failure, raised.exception)
        self.assertNotIsInstance(
            raised.exception,
            sanad_main.tg_router.RetryableCallbackError,
        )
        self.assertIn(
            f"{bound_doctor.id}:confirm:confirm-ambiguous-release",
            memory.card_actions,
        )

    async def test_concurrent_and_completed_task_duplicates_execute_once(
        self,
    ) -> None:
        memory = MemoryStore(start=NOW)
        payload = {"loop_id": "loop-1", "run_id": "run-1"}
        legacy_body = {
            "sent": False,
            "reason": "already handled legacy decision",
        }
        task_headers = {
            "authorization": "Bearer google-signed",
            "x-cloudtasks-taskname": "projects/p/locations/l/queues/q/tasks/same-task",
        }
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def blocked_fire(received: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual(payload, received)
            entered.set()
            await finish.wait()
            return legacy_body

        fire = AsyncMock(side_effect=blocked_fire)
        reclaim = AsyncMock(return_value={"sends": 0, "pending_confirms": 0})

        with patched_store(memory):
            with (
                patch.object(
                    sanad_main.tasks,
                    "verify_caller",
                    new=AsyncMock(return_value={"email_verified": True}),
                ),
                patch.object(sanad_main.chaser, "fire", new=fire),
                patch.object(sanad_main.store, "reclaim_stale", new=reclaim),
            ):
                first_call = asyncio.create_task(
                    sanad_main.tasks_nudge(
                        RequestStub(payload, headers=task_headers)
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=1)
                in_flight = await sanad_main.tasks_nudge(
                    RequestStub(payload, headers=task_headers)
                )
                finish.set()
                first = await asyncio.wait_for(first_call, timeout=1)
                completed = await sanad_main.tasks_nudge(
                    RequestStub(payload, headers=task_headers)
                )

        self.assertEqual(legacy_body, first)
        self.assertEqual({"sent": False, "reason": "in_flight"}, in_flight)
        self.assertEqual(first, completed)
        fire.assert_awaited_once_with(payload)
        reclaim.assert_awaited_once()
        self.assertEqual(1, len(memory.command_receipts))

    async def test_retry_safe_chaser_failure_releases_outer_claim_for_resend(
        self,
    ) -> None:
        memory = MemoryStore(start=NOW)
        payload = {"loop_id": "loop-retry", "run_id": "run-retry"}
        task_headers = {
            "authorization": "Bearer google-signed",
            "x-cloudtasks-taskname": "projects/p/l/q/tasks/retry-safe-task",
        }
        resent = {
            "sent": True,
            "attempt": 1,
            "resend": True,
            "loop": "loop-retry",
            "key": "loop-retry:0:nudge:1",
        }
        attempts = 0

        async def fail_then_resend(_: dict[str, Any]) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sanad_main.chaser.RetryableNudgeError(
                    "failed send receipt is durable"
                )
            return resent

        fire = AsyncMock(side_effect=fail_then_resend)
        reclaim = AsyncMock(return_value={"sends": 0, "pending_confirms": 0})
        with patched_store(memory):
            with (
                patch.object(
                    sanad_main.tasks,
                    "verify_caller",
                    new=AsyncMock(return_value={"email_verified": True}),
                ),
                patch.object(sanad_main.chaser, "fire", new=fire),
                patch.object(sanad_main.store, "reclaim_stale", new=reclaim),
            ):
                with self.assertRaises(HTTPException) as first_failure:
                    await sanad_main.tasks_nudge(
                        RequestStub(payload, headers=task_headers)
                    )
                self.assertEqual(503, first_failure.exception.status_code)
                self.assertEqual(
                    {},
                    memory.command_receipts,
                    "typed RETRYABLE must release the outer provider claim",
                )

                second = await sanad_main.tasks_nudge(
                    RequestStub(payload, headers=task_headers)
                )
                completed_replay = await sanad_main.tasks_nudge(
                    RequestStub(payload, headers=task_headers)
                )

        self.assertEqual(resent, second)
        self.assertEqual(resent, completed_replay)
        self.assertEqual(2, attempts)
        self.assertEqual(2, reclaim.await_count)
        self.assertEqual(1, len(memory.command_receipts))

    async def test_cloud_task_names_are_transient_and_replay_uses_opaque_result(
        self,
    ) -> None:
        memory = MemoryStore(start=NOW)
        payload = {"loop_id": "loop-rearm", "run_id": "run-rearm"}
        inbound_task_name = (
            "projects/private-project/locations/private-region/queues/"
            "sanad-chase/tasks/inbound-provider-task"
        )
        rearmed_task_name = (
            "projects/private-project/locations/private-region/queues/"
            "sanad-chase/tasks/newly-enqueued-provider-task"
        )
        legacy_body = {
            "sent": False,
            "reason": "re-armed",
            "task": rearmed_task_name,
        }
        expected_replay = {
            **legacy_body,
            "task": live_transport.opaque(
                "cloud_tasks:task",
                rearmed_task_name,
            ),
        }
        task_headers = {
            "authorization": "Bearer google-signed",
            "x-cloudtasks-taskname": inbound_task_name,
        }
        observed_commands: list[Command] = []
        observed_results: list[CommandResult] = []
        fire = AsyncMock(return_value=legacy_body)
        reclaim = AsyncMock(return_value={"sends": 0, "pending_confirms": 0})
        shadow_write = AsyncMock()

        context = live_transport.TaskContext(task_name=inbound_task_name)
        self.assertNotIn(inbound_task_name, repr(context))
        identity = live_transport.task_identity(payload, inbound_task_name)
        self.assertNotEqual(inbound_task_name, identity)
        self.assertEqual(
            identity,
            live_transport.task_identity(payload, inbound_task_name),
        )

        with patched_store(memory):
            runtime = sanad_main.transport_runtime()
            real_execute = runtime.bus.execute

            async def observe(command: Command) -> CommandResult:
                observed_commands.append(command)
                result = await real_execute(command)
                observed_results.append(result)
                return result

            with (
                patch.object(
                    runtime.bus,
                    "execute",
                    new=AsyncMock(side_effect=observe),
                ),
                patch.object(
                    sanad_main.tasks,
                    "verify_caller",
                    new=AsyncMock(return_value={"email_verified": True}),
                ),
                patch.object(sanad_main.chaser, "fire", new=fire),
                patch.object(sanad_main.store, "reclaim_stale", new=reclaim),
                patch.object(outbox, "record_shadow", new=shadow_write),
            ):
                first = await sanad_main.tasks_nudge(
                    RequestStub(payload, headers=task_headers)
                )
                completed_replay = await sanad_main.tasks_nudge(
                    RequestStub(payload, headers=task_headers)
                )

        self.assertEqual(legacy_body, first)
        self.assertEqual(expected_replay, completed_replay)
        fire.assert_awaited_once_with(payload)
        reclaim.assert_awaited_once()
        shadow_write.assert_not_awaited()
        self.assertEqual([], memory.outbound_ledger)
        self.assertEqual(2, len(observed_commands))
        self.assertEqual(2, len(observed_results))
        self.assertEqual(expected_replay, observed_results[1].value["legacy_body"])
        self.assertEqual(1, len(memory.command_receipts))

        command_dump = repr(
            [command.model_dump(mode="python") for command in observed_commands]
        )
        command_repr = repr(observed_commands)
        result_dump = repr(
            [result.model_dump(mode="python") for result in observed_results]
        )
        result_repr = repr(observed_results)
        receipt_render = repr(memory.command_receipts)
        outbox_render = repr(memory.outbound_ledger)
        for raw_name in (inbound_task_name, rearmed_task_name):
            with self.subTest(raw_name=raw_name):
                self.assertNotIn(raw_name, command_dump)
                self.assertNotIn(raw_name, command_repr)
                self.assertNotIn(raw_name, result_dump)
                self.assertNotIn(raw_name, result_repr)
                self.assertNotIn(raw_name, receipt_render)
                self.assertNotIn(raw_name, outbox_render)

    async def test_transients_reach_claimed_handlers_but_never_durable_content(
        self,
    ) -> None:
        memory = MemoryStore(start=NOW)
        runtime: Any
        events: list[tuple[str, str]] = []
        claimed_commands: list[Command] = []
        seen: dict[str, Any] = {}

        web_token = "web-token-MUST-NOT-PERSIST-4f9d"
        web_bytes = b"web-audio-MUST-NOT-PERSIST-71ac"
        chat_id = 998877665544332211
        start_token = "start-token-MUST-NOT-PERSIST-52be"
        webhook_secret = "webhook-secret-MUST-NOT-PERSIST-10cd"
        task_secret = "task-secret-MUST-NOT-PERSIST-a883"
        task_bytes = b"task-bytes-MUST-NOT-PERSIST-96ee"
        real_provider_update = sanad_main.tg_router.handle_provider_update
        telegram_domain = AsyncMock()

        def current_in_flight_receipt() -> dict[str, Any]:
            rows = [
                row
                for row in memory.command_receipts.values()
                if row.get("state") == "IN_FLIGHT"
            ]
            self.assertEqual(1, len(rows))
            self.assertEqual(
                {"fingerprint", "state", "created_at", "updated_at"},
                set(rows[0]),
            )
            return dict(rows[0])

        async def handle_web(inbound: InboundMessage) -> None:
            self.assertEqual(("claim", "web"), events[-1])
            events.append(("handler", "web"))
            seen["web"] = inbound

        async def handle_telegram(
            update: dict[str, Any],
            _: str,
            *,
            secret_token: Optional[str],
        ) -> None:
            self.assertEqual(("claim", "telegram"), events[-1])
            events.append(("handler", "telegram"))
            seen["telegram"] = (update, secret_token)
            seen["telegram_receipt"] = current_in_flight_receipt()
            await real_provider_update(
                update,
                _,
                secret_token=secret_token,
            )

        async def handle_task(payload: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual(("claim", "cloud_tasks"), events[-1])
            events.append(("handler", "cloud_tasks"))
            seen["cloud_tasks"] = payload
            seen["task_receipt"] = current_in_flight_receipt()
            return {"sent": False, "reason": "observed transient task"}

        with patched_store(memory):
            runtime = sanad_main.transport_runtime()
            replay = runtime.bus._replay
            real_claim = replay.claim

            async def observe_claim(command: Command) -> Any:
                result = await real_claim(command)
                claimed_commands.append(command)
                if result.state == "CLAIMED":
                    events.append(("claim", command.source))
                return result

            web_inbound = InboundMessage(
                channel="web",
                synthetic=True,
                sender_ref=f"doctor:{web_token}",
                text="web transient",
                audio_bytes=web_bytes,
                mime="audio/ogg",
            )
            telegram_update = {
                "update_id": 9003,
                "message": {
                    "chat": {"id": chat_id},
                    "text": f"/start {start_token}",
                },
            }
            task_payload = {
                "loop_id": "",
                "private_value": task_secret,
                "opaque_bytes": task_bytes,
            }

            with (
                patch.object(
                    replay,
                    "claim",
                    new=AsyncMock(side_effect=observe_claim),
                ),
                patch.object(
                    sanad_main.dispatch,
                    "handle_inbound",
                    new=AsyncMock(side_effect=handle_web),
                ),
                patch.object(
                    sanad_main.telegram,
                    "WEBHOOK_SECRET",
                    webhook_secret,
                ),
                patch.object(
                    sanad_main.tg_router,
                    "handle_provider_update",
                    new=AsyncMock(side_effect=handle_telegram),
                ),
                patch.object(
                    sanad_main.tg_router,
                    "_handle_update",
                    new=telegram_domain,
                ),
                patch.object(
                    sanad_main.tasks,
                    "verify_caller",
                    new=AsyncMock(return_value={"email_verified": True}),
                ),
                patch.object(
                    sanad_main.chaser,
                    "fire",
                    new=AsyncMock(side_effect=handle_task),
                ),
            ):
                web_result = await sanad_main._web_message(
                    web_inbound,
                    tenant_id="doctor-internal",
                    actor=ActorRef(kind="doctor", id="doctor-internal"),
                    principal=ActorRef(kind="doctor", id="doctor-internal"),
                    endpoint_id="web:doctor:doctor-internal",
                    thread_id="doctor:doctor-internal",
                    identity_method="doctor_console_token",
                )
                telegram_result = await sanad_main.telegram_webhook(
                    RequestStub(
                        telegram_update,
                        headers={TELEGRAM_SECRET_HEADER: webhook_secret},
                    )
                )
                task_result = await sanad_main.tasks_nudge(
                    RequestStub(
                        task_payload,
                        headers={
                            "authorization": "Bearer auth-secret-not-forwarded",
                            "x-cloudtasks-taskname": "task-with-transients",
                        },
                    )
                )

        self.assertEqual({"ok": True}, web_result)
        self.assertEqual({"ok": True}, telegram_result)
        self.assertEqual(
            {"sent": False, "reason": "observed transient task"},
            task_result,
        )
        self.assertEqual(
            [
                ("claim", "web"),
                ("handler", "web"),
                ("claim", "telegram"),
                ("handler", "telegram"),
                ("claim", "cloud_tasks"),
                ("handler", "cloud_tasks"),
            ],
            events,
        )
        self.assertIs(web_inbound, seen["web"])
        self.assertEqual(web_bytes, seen["web"].audio_bytes)
        self.assertIn(web_token, seen["web"].sender_ref)
        self.assertEqual(telegram_update, seen["telegram"][0])
        self.assertEqual(webhook_secret, seen["telegram"][1])
        self.assertEqual(task_secret, seen["cloud_tasks"]["private_value"])
        self.assertEqual(task_bytes, seen["cloud_tasks"]["opaque_bytes"])
        telegram_domain.assert_awaited_once_with(
            telegram_update,
            "https://sanad.test/",
            synthetic=False,
        )

        self.assertEqual(3, len(claimed_commands))
        web_command = claimed_commands[0]
        self.assertIsNotNone(web_command.envelope)
        assert web_command.envelope is not None
        self.assertEqual(
            web_bytes,
            web_command.envelope.attachments[0].inline_bytes,
        )
        durable_render = repr([
            command.model_dump(mode="python") for command in claimed_commands
        ])
        receipt_render = repr(memory.command_receipts)
        command_repr = repr(claimed_commands)
        forbidden = (
            web_token,
            web_bytes.decode(),
            str(chat_id),
            start_token,
            webhook_secret,
            "auth-secret-not-forwarded",
            task_secret,
            task_bytes.decode(),
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, durable_render)
                self.assertNotIn(value, receipt_render)
                self.assertNotIn(value, command_repr)
                self.assertNotIn(value, repr(seen["telegram_receipt"]))
                self.assertNotIn(value, repr(seen["task_receipt"]))
        self.assertEqual(2, len(memory.command_receipts))
        for row in memory.command_receipts.values():
            self.assertTrue(
                set(row).issubset(
                    {
                        "fingerprint",
                        "state",
                        "result",
                        "created_at",
                        "completed_at",
                        "updated_at",
                    }
                )
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
