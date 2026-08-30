"""Adversarial route-level checks for the live Gate 2 composition root.

These tests call the real route functions, real adapters, and real CommandBus.
Only external authentication and legacy domain effects are replaced with
hermetic doubles; no network or Firestore fallback is possible.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import main as sanad_main
from core import live_transport
from core.adapters import InboundMessage
from core.channel_contracts import Command
from core.models import Doctor, Patient
from tests.gate0b.memory import MemoryStore, patched_store


NOW = datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)


def doctor() -> Doctor:
    return Doctor(
        id="doctor-internal",
        name="Dr Test",
        specialty="cardiology",
        web_token="doctor-web-token-must-stay-transient",
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
        self.json_calls = 0

    async def json(self) -> dict[str, Any]:
        self.json_calls += 1
        return self._body


class LiveCompositionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        previous = sanad_main._TRANSPORT
        sanad_main._TRANSPORT = None
        self.addCleanup(setattr, sanad_main, "_TRANSPORT", previous)

    async def test_main_registry_is_frozen_around_three_adapters_and_one_bus(
        self,
    ) -> None:
        runtime = sanad_main.transport_runtime()

        self.assertIs(runtime, sanad_main.transport_runtime())
        self.assertIs(runtime.bus, sanad_main.transport_runtime().bus)
        self.assertEqual(
            ("cloud_tasks", "telegram", "web"),
            runtime.registry.providers,
        )
        self.assertEqual(
            {
                live_transport.MESSAGE,
                live_transport.ACTION,
                live_transport.TELEGRAM_UPDATE,
                live_transport.NUDGE,
            },
            set(runtime.bus._handlers),
        )
        for provider in runtime.registry.providers:
            self.assertEqual(provider, runtime.registry.get(provider).provider)
        with self.assertRaisesRegex(RuntimeError, "registry is frozen"):
            runtime.registry.register(runtime.registry.get("web"))

    async def test_all_web_routes_cross_the_bus_then_keep_legacy_bodies(
        self,
    ) -> None:
        runtime = sanad_main.transport_runtime()
        real_doctor = doctor()
        real_patient = patient()
        received: list[InboundMessage] = []

        async def handle(inbound: InboundMessage) -> None:
            received.append(inbound)

        action_body = {
            "ok": False,
            "already": True,
            "action_id": "reviewed:loop-1",
            "detail": "legacy body stays exact",
        }
        bus_execute = AsyncMock(wraps=runtime.bus.execute)
        legacy_action = AsyncMock(return_value=action_body)
        request = RequestStub()
        submitted_action = sanad_main.ActionIn(
            action_id="reviewed:loop-1",
            text="unchanged action text",
        )

        with (
            patch.object(runtime.bus, "execute", new=bus_execute),
            patch.object(
                sanad_main.dispatch,
                "handle_inbound",
                new=AsyncMock(side_effect=handle),
            ),
            patch.object(
                sanad_main,
                "patient_from_link",
                new=AsyncMock(return_value=real_patient),
            ),
            patch.object(
                sanad_main.store,
                "get_patient",
                new=AsyncMock(return_value=real_patient),
            ),
            patch.object(sanad_main, "_legacy_action", new=legacy_action),
        ):
            patient_page = await sanad_main.patient_send(
                "patient-link", text="from patient page", file=None
            )
            doctor_console = await sanad_main.doctor_in(
                text="from doctor", file=None, doctor=real_doctor
            )
            patient_console = await sanad_main.patient_in(
                real_patient.id,
                text="simulated patient",
                file=None,
                doctor=real_doctor,
            )
            action = await sanad_main.action(
                submitted_action,
                request,
                real_doctor,
            )

        self.assertEqual(
            [{"ok": True}, {"ok": True}, {"ok": True}, action_body],
            [patient_page, doctor_console, patient_console, action],
        )
        self.assertEqual(4, bus_execute.await_count)
        commands = [call.args[0] for call in bus_execute.await_args_list]
        self.assertTrue(all(isinstance(command, Command) for command in commands))
        self.assertEqual(
            [
                live_transport.MESSAGE,
                live_transport.MESSAGE,
                live_transport.MESSAGE,
                live_transport.ACTION,
            ],
            [command.kind for command in commands],
        )
        self.assertTrue(all(command.source == "web" for command in commands))
        self.assertEqual(
            [
                (
                    "doctor-internal",
                    ("patient", "patient-internal"),
                    ("patient", "patient-internal"),
                    "web:patient:patient-internal",
                    "patient:patient-internal",
                    "patient_link",
                ),
                (
                    "doctor-internal",
                    ("doctor", "doctor-internal"),
                    ("doctor", "doctor-internal"),
                    "web:doctor:doctor-internal",
                    "doctor:doctor-internal",
                    "doctor_console_token",
                ),
                (
                    "doctor-internal",
                    ("patient", "patient-internal"),
                    ("doctor", "doctor-internal"),
                    "web:doctor:doctor-internal",
                    "patient:patient-internal",
                    "doctor_console_simulation",
                ),
                (
                    "doctor-internal",
                    ("doctor", "doctor-internal"),
                    ("doctor", "doctor-internal"),
                    "web:doctor:doctor-internal",
                    "doctor:doctor-internal",
                    "doctor_console_token",
                ),
            ],
            [
                (
                    command.tenant_id,
                    (command.actor.kind, command.actor.id),
                    (command.principal.kind, command.principal.id),
                    command.endpoint_id,
                    command.thread_id,
                    command.payload["envelope"]["identity"]["method"],
                )
                for command in commands
            ],
        )
        self.assertEqual(
            {
                "action_id": submitted_action.action_id,
                "text": submitted_action.text,
            },
            {
                key: commands[-1].payload[key]
                for key in ("action_id", "text")
            },
        )
        self.assertEqual(
            [
                "patient:patient-internal",
                "doctor:doctor-web-token-must-stay-transient",
                "patient:patient-internal",
            ],
            [inbound.sender_ref for inbound in received],
        )
        self.assertEqual(
            ["from patient page", "from doctor", "simulated patient"],
            [inbound.text for inbound in received],
        )
        legacy_action.assert_awaited_once()
        self.assertEqual(
            submitted_action,
            legacy_action.await_args.args[0],
        )
        self.assertIs(
            request,
            legacy_action.await_args.args[1],
        )
        self.assertIs(
            real_doctor,
            legacy_action.await_args.args[2],
        )

    async def test_provider_auth_precedes_normalization_and_bus_execution(
        self,
    ) -> None:
        service_account = "sanad-runtime@project.iam.gserviceaccount.com"

        def verify_identity(token: str) -> dict[str, Any]:
            if token != "google-signed":
                raise ValueError("forged caller")
            return {
                "email_verified": True,
                "email": service_account,
            }

        memory = MemoryStore(start=NOW)
        with patched_store(memory):
            runtime = sanad_main.transport_runtime()
            telegram_adapter = runtime.registry.get("telegram")
            task_adapter = runtime.registry.get("cloud_tasks")
            telegram_normalize = AsyncMock(wraps=telegram_adapter.normalize)
            task_normalize = AsyncMock(wraps=task_adapter.normalize)
            bus_execute = AsyncMock(wraps=runtime.bus.execute)

            forged_telegram = RequestStub(
                {
                    "update_id": 101,
                    "message": {"chat": {"id": 7001}, "text": "forged"},
                },
                headers={"x-telegram-bot-api-secret-token": "wrong"},
            )
            forged_task = RequestStub(
                {"loop_id": "loop-forged"},
                headers={"authorization": "Bearer forged"},
            )

            with (
                patch.object(
                    telegram_adapter, "normalize", new=telegram_normalize
                ),
                patch.object(task_adapter, "normalize", new=task_normalize),
                patch.object(runtime.bus, "execute", new=bus_execute),
                patch.object(
                    sanad_main.telegram,
                    "WEBHOOK_SECRET",
                    "verified-secret",
                ),
                patch.object(
                    sanad_main.tasks,
                    "SERVICE_URL",
                    "https://sanad.test",
                ),
                patch.object(
                    sanad_main.tasks,
                    "SERVICE_ACCOUNT",
                    service_account,
                ),
                patch.object(
                    sanad_main.tasks,
                    "_verify",
                    new=verify_identity,
                ),
            ):
                with self.assertRaises(HTTPException) as telegram_refusal:
                    await sanad_main.telegram_webhook(forged_telegram)
                with self.assertRaises(HTTPException) as task_refusal:
                    await sanad_main.tasks_nudge(forged_task)

                self.assertEqual(404, telegram_refusal.exception.status_code)
                self.assertEqual(403, task_refusal.exception.status_code)
                self.assertEqual(0, forged_telegram.json_calls)
                self.assertEqual(0, forged_task.json_calls)
                telegram_normalize.assert_not_awaited()
                task_normalize.assert_not_awaited()
                bus_execute.assert_not_awaited()

                verified_telegram = RequestStub(
                    {
                        "update_id": 102,
                        "message": {
                            "chat": {"id": 7002},
                            "text": "verified",
                        },
                    },
                    headers={
                        "x-telegram-bot-api-secret-token": "verified-secret"
                    },
                )
                authenticated_task = RequestStub(
                    {"loop_id": "loop-authenticated"},
                    headers={
                        "authorization": "Bearer google-signed",
                        "x-cloudtasks-taskname": "projects/p/locations/l/queues/q/tasks/t-1",
                    },
                )
                telegram_domain = AsyncMock()
                task_result = {
                    "sent": False,
                    "reason": "authenticated legacy result",
                }
                chaser_fire = AsyncMock(return_value=task_result)
                with (
                    patch.object(
                        sanad_main.tg_router,
                        "_handle_update",
                        new=telegram_domain,
                    ),
                    patch.object(sanad_main.chaser, "fire", new=chaser_fire),
                ):
                    telegram_body = await sanad_main.telegram_webhook(
                        verified_telegram
                    )
                    task_body = await sanad_main.tasks_nudge(authenticated_task)

            self.assertEqual({"ok": True}, telegram_body)
            self.assertEqual(task_result, task_body)
            telegram_normalize.assert_awaited_once()
            task_normalize.assert_awaited_once()
            self.assertEqual(2, bus_execute.await_count)
            self.assertEqual(
                ["telegram", "cloud_tasks"],
                [
                    call.args[0].source
                    for call in bus_execute.await_args_list
                ],
            )
            telegram_domain.assert_awaited_once_with(
                {
                    "update_id": 102,
                    "message": {
                        "chat": {"id": 7002},
                        "text": "verified",
                    },
                },
                "https://sanad.test/",
                synthetic=False,
            )
            chaser_fire.assert_awaited_once_with(
                {"loop_id": "loop-authenticated"}
            )

    async def test_inprocess_fallback_enters_the_same_cloud_task_bus(self) -> None:
        payload = {"loop_id": "loop-local", "run_id": "run-local"}
        result_body = {"sent": False, "reason": "local legacy result"}
        memory = MemoryStore(start=NOW)
        with patched_store(memory):
            runtime = sanad_main.transport_runtime()
            bus_execute = AsyncMock(wraps=runtime.bus.execute)
            chaser_fire = AsyncMock(return_value=result_body)
            with (
                patch.object(runtime.bus, "execute", new=bus_execute),
                patch.object(sanad_main.chaser, "fire", new=chaser_fire),
            ):
                body = await sanad_main._local_nudge(
                    payload,
                    "inprocess/manual-task-1",
                )

        self.assertEqual(result_body, body)
        bus_execute.assert_awaited_once()
        command = bus_execute.await_args.args[0]
        self.assertIs(runtime.bus, sanad_main.transport_runtime().bus)
        self.assertEqual("cloud_tasks", command.source)
        self.assertEqual(live_transport.NUDGE, command.kind)
        self.assertIsNotNone(command.envelope)
        assert command.envelope is not None
        self.assertEqual(
            "trusted_inprocess_scheduler",
            command.envelope.identity["method"],
        )
        self.assertEqual(payload, command.envelope.transient_payload)
        chaser_fire.assert_awaited_once_with(payload)

    async def test_local_schedules_get_distinct_identity_but_one_name_replays(
        self,
    ) -> None:
        payload = {"loop_id": "loop-local", "reason": "quiet-hours-rearm"}
        legacy_body = {"sent": False, "reason": "quiet hours"}
        memory = MemoryStore(start=NOW)
        pending: set[asyncio.Task[Any]] = set()
        fire = AsyncMock(return_value=legacy_body)
        reclaim = AsyncMock(return_value={"sends": 0, "pending_confirms": 0})
        previous_handler = sanad_main.tasks._local_handler
        self.addCleanup(
            setattr,
            sanad_main.tasks,
            "_local_handler",
            previous_handler,
        )

        with patched_store(memory):
            with (
                patch.object(sanad_main.tasks, "ENGINE", "inprocess"),
                patch.object(sanad_main.tasks, "_pending", pending),
                patch.object(sanad_main.runtime, "validate_gate2"),
                patch.object(
                    sanad_main.tg_router,
                    "wrong_bindings",
                    new=AsyncMock(return_value=[]),
                ),
                patch.object(sanad_main.chaser, "fire", new=fire),
                patch.object(sanad_main.store, "reclaim_stale", new=reclaim),
            ):
                async with sanad_main.lifespan(sanad_main.app):
                    self.assertIs(
                        sanad_main._local_nudge,
                        sanad_main.tasks._local_handler,
                    )
                    first_name = await sanad_main.tasks.enqueue(
                        "/tasks/nudge", payload, 0.05
                    )
                    second_name = await sanad_main.tasks.enqueue(
                        "/tasks/nudge", payload, 0.05
                    )
                    scheduled = tuple(pending)
                    self.assertEqual(2, len(scheduled))
                    await asyncio.gather(*scheduled)

                    replay = await sanad_main._local_nudge(payload, first_name)

        self.assertTrue(first_name.strip())
        self.assertTrue(second_name.strip())
        self.assertNotEqual(first_name, second_name)
        self.assertEqual(2, fire.await_count)
        self.assertEqual(2, reclaim.await_count)
        self.assertEqual(2, len(memory.command_receipts))
        self.assertEqual(legacy_body, replay)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
