"""Gate 3 HTTP authentication, rollout flag, and atomic read boundary."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import main
from core import store
from core.models import Doctor, Event, Loop, Patient
from tests.gate0b.memory import MemoryStore, patched_store


NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
APP_ROOT = Path(__file__).resolve().parents[1]
LEGACY_DASHBOARD_SHA256 = "7e19ff32de7a6bc36dd90013f2b13ed6ce8d3bb896697b1c50cc7f94946183be"


class AdditiveModelFacts(unittest.TestCase):
    def test_default_false_rollout_flag_does_not_change_legacy_doctor_dump(self) -> None:
        row = Doctor(
            id="d",
            name="Doctor",
            web_token="secret",
            created_at=NOW,
        )
        self.assertFalse(row.cockpit_v2_enabled)
        self.assertNotIn("cockpit_v2_enabled", row.model_dump())
        self.assertFalse(row.workspace_facts_enabled)
        self.assertNotIn("workspace_facts_enabled", row.model_dump())

    def test_absent_closed_timestamp_does_not_change_legacy_loop_dumps(self) -> None:
        from core.models import Loop

        row = Loop(
            id="l",
            patient_id="p",
            doctor_id="d",
            type="TEST",
            title="test",
            created_at=NOW,
            updated_at=NOW,
        )
        self.assertIsNone(row.closed_at)
        self.assertNotIn("closed_at", row.model_dump())

    def test_absent_persisted_timestamp_does_not_change_legacy_event_dumps(self) -> None:
        row = Event(id="e", doctor_id="d", kind="system", ts=NOW)
        self.assertIsNone(row.persisted_at)
        self.assertNotIn("persisted_at", row.model_dump())
        read_side = row.model_copy(update={"persisted_at": NOW})
        self.assertEqual(read_side.persisted_at, NOW)
        self.assertNotIn("persisted_at", read_side.model_dump())

    def test_frozen_legacy_dashboard_is_byte_identical(self) -> None:
        digest = hashlib.sha256((APP_ROOT / "web" / "dashboard.html").read_bytes()).hexdigest()
        self.assertEqual(digest, LEGACY_DASHBOARD_SHA256)


class SnapshotRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.memory = MemoryStore(start=NOW)
        self.store_patch = patched_store(self.memory)
        self.store_patch.__enter__()
        self.doctor = await self.memory.create_doctor("Doctor A", specialty="cardiology")
        self.other = await self.memory.create_doctor("Doctor B", specialty="cardiology")
        await self.memory.update_doctor(self.doctor.id, telegram_chat_id=987654321)
        await self.memory.create_patient(
            Patient(
                id="patient-a",
                doctor_id=self.doctor.id,
                name="Only A May See This",
                diagnosis="tenant A diagnosis",
                created_at=NOW,
            )
        )
        await self.memory.create_patient(
            Patient(
                id="patient-b",
                doctor_id=self.other.id,
                name="Only B May See This",
                diagnosis="tenant B diagnosis",
                created_at=NOW,
            )
        )
        self.transport = httpx.ASGITransport(app=main.app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.store_patch.__exit__(None, None, None)

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.doctor.web_token}"}

    async def enable(self, doctor_id: str | None = None) -> None:
        await self.memory.update_doctor(
            doctor_id or self.doctor.id, cockpit_v2_enabled=True
        )

    async def test_missing_wrong_query_and_unflagged_credentials_are_non_enumerating(self) -> None:
        missing = await self.client.get("/api/v2/workspace-snapshot")
        wrong = await self.client.get(
            "/api/v2/workspace-snapshot",
            headers={"Authorization": "Bearer definitely-wrong"},
        )
        query = await self.client.get(
            f"/api/v2/workspace-snapshot?token={self.doctor.web_token}"
        )
        unflagged = await self.client.get(
            "/api/v2/workspace-snapshot", headers=self.auth
        )
        self.assertEqual([missing.status_code, wrong.status_code, query.status_code, unflagged.status_code], [404] * 4)

    async def test_invalid_auth_never_reaches_atomic_workspace_loader(self) -> None:
        loader = AsyncMock(side_effect=AssertionError("loader must not run"))
        with patch.object(store, "read_workspace", loader):
            response = await self.client.get(
                "/api/v2/workspace-snapshot",
                headers={"Authorization": "Bearer wrong"},
            )
        self.assertEqual(response.status_code, 404)
        loader.assert_not_awaited()

    async def test_flagged_doctor_gets_one_no_store_tenant_scoped_snapshot(self) -> None:
        await self.enable()
        loader = AsyncMock(wraps=self.memory.read_workspace)
        with patch.object(store, "read_workspace", loader):
            response = await self.client.get(
                "/api/v2/workspace-snapshot", headers=self.auth
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        loader.assert_awaited_once_with(self.doctor.id)

        raw = response.text
        body = response.json()
        self.assertEqual(body["schema_version"], "2.0")
        self.assertIn("patient-a", raw)
        self.assertIn("Only A May See This", raw)
        self.assertNotIn("patient-b", raw)
        self.assertNotIn("Only B May See This", raw)
        self.assertNotIn(self.doctor.web_token, raw)
        self.assertNotIn("987654321", raw)
        self.assertTrue(body["legacy"]["settings"]["telegram_chat_id_present"])

    async def test_token_rotated_after_authentication_fails_closed(self) -> None:
        await self.enable()
        bundle = await self.memory.read_workspace(self.doctor.id)
        self.assertIsNotNone(bundle)
        rotated = replace(
            bundle,
            doctor=bundle.doctor.model_copy(update={"web_token": "rotated-token"}),
        )
        with patch.object(store, "read_workspace", AsyncMock(return_value=rotated)):
            response = await self.client.get(
                "/api/v2/workspace-snapshot", headers=self.auth
            )
        self.assertEqual(response.status_code, 404)

    async def test_system_health_uses_effective_settings_fallbacks(self) -> None:
        await self.enable()
        await self.memory.set_settings(run_id="", time_scale="not-an-integer")
        with (
            patch.object(main.settings, "ENV_RUN_ID", "deployed-run"),
            patch.object(main.settings, "ENV_TIME_SCALE", 321),
            patch.object(
                main.settings,
                "current",
                AsyncMock(side_effect=AssertionError("must not re-read settings")),
            ) as settings_read,
        ):
            response = await self.client.get(
                "/api/v2/workspace-snapshot", headers=self.auth
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["system"]["run_id"], "deployed-run")
        self.assertEqual(response.json()["system"]["time_scale"], 321)
        self.assertEqual(response.json()["snapshot_id_kind"], "RECORD_VERSION")
        settings_read.assert_not_awaited()

    async def test_cursor_from_another_doctor_fails_closed(self) -> None:
        await self.enable()
        first = await self.client.get("/api/v2/workspace-snapshot", headers=self.auth)
        cursor = first.json()["event_cursor"]
        await self.enable(self.other.id)
        other_auth = {"Authorization": f"Bearer {self.other.web_token}"}
        response = await self.client.get(
            "/api/v2/workspace-snapshot",
            headers=other_auth,
            params={"event_cursor": cursor},
        )
        self.assertEqual(response.status_code, 422)


class DoctorScopedRollout(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.memory = MemoryStore(start=NOW)
        self.store_patch = patched_store(self.memory)
        self.store_patch.__enter__()
        self.doctor = await self.memory.create_doctor("Rollout Doctor")
        self.other = await self.memory.create_doctor("Other Doctor")
        self.transport = httpx.ASGITransport(app=main.app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.store_patch.__exit__(None, None, None)

    async def test_default_app_is_legacy_and_query_cannot_enable_v2(self) -> None:
        legacy = (APP_ROOT / "web" / "dashboard.html").read_bytes()
        ordinary = await self.client.get(f"/c/{self.doctor.web_token}/app")
        attempted = await self.client.get(
            f"/c/{self.doctor.web_token}/app?cockpit_v2=1"
        )
        self.assertEqual(ordinary.status_code, 200)
        self.assertEqual(ordinary.content, legacy)
        self.assertEqual(attempted.content, legacy)

    async def test_admin_only_flag_targets_one_doctor_and_rolls_back_immediately(self) -> None:
        with patch.dict(os.environ, {"ADMIN_SECRET": "gate3-admin"}, clear=False):
            refused = await self.client.post(
                "/admin/doctor-features",
                json={"doctor_id": self.doctor.id, "cockpit_v2_enabled": True},
            )
            enabled = await self.client.post(
                "/admin/doctor-features",
                headers={"X-Sanad-Admin": "gate3-admin"},
                json={"doctor_id": self.doctor.id, "cockpit_v2_enabled": True},
            )
        self.assertEqual(refused.status_code, 404)
        self.assertEqual(enabled.status_code, 200, enabled.text)
        stored = await self.memory.doctor_by_id(self.doctor.id)
        self.assertIsNotNone(stored)
        self.assertTrue(stored.cockpit_v2_enabled)
        self.assertTrue(stored.workspace_facts_enabled)

        v2 = (APP_ROOT / "web" / "dashboard_v2.html").read_bytes()
        doctor_page = await self.client.get(f"/c/{self.doctor.web_token}/app")
        other_page = await self.client.get(f"/c/{self.other.web_token}/app")
        self.assertEqual(doctor_page.content, v2)
        self.assertEqual(other_page.content, (APP_ROOT / "web" / "dashboard.html").read_bytes())

        with patch.dict(os.environ, {"ADMIN_SECRET": "gate3-admin"}, clear=False):
            disabled = await self.client.post(
                "/admin/doctor-features",
                headers={"X-Sanad-Admin": "gate3-admin"},
                json={"doctor_id": self.doctor.id, "cockpit_v2_enabled": False},
            )
        self.assertEqual(disabled.status_code, 200)
        stored = await self.memory.doctor_by_id(self.doctor.id)
        self.assertIsNotNone(stored)
        self.assertFalse(stored.cockpit_v2_enabled)
        self.assertTrue(stored.workspace_facts_enabled)
        rolled_back = await self.client.get(f"/c/{self.doctor.web_token}/app")
        self.assertEqual(rolled_back.content, (APP_ROOT / "web" / "dashboard.html").read_bytes())

    async def test_reset_and_token_rotation_preserve_rollout_flag(self) -> None:
        with patch.dict(os.environ, {"ADMIN_SECRET": "gate3-admin"}, clear=False):
            enabled = await self.client.post(
                "/admin/doctor-features",
                headers={"X-Sanad-Admin": "gate3-admin"},
                json={"doctor_id": self.doctor.id, "cockpit_v2_enabled": True},
            )
            reset = await self.client.post(
                "/admin/reset",
                headers={"X-Sanad-Admin": "gate3-admin"},
                params={"name": self.doctor.name},
            )
            rotated = await self.client.post(
                "/admin/rotate-token",
                headers={"X-Sanad-Admin": "gate3-admin"},
                params={"name": self.doctor.name},
            )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(rotated.status_code, 200, rotated.text)
        stored = await self.memory.doctor_by_id(self.doctor.id)
        self.assertIsNotNone(stored)
        self.assertTrue(stored.cockpit_v2_enabled)
        self.assertTrue(stored.workspace_facts_enabled)


class AtomicWorkspaceRead(unittest.IsolatedAsyncioTestCase):
    async def test_memory_loader_clones_all_maps_under_one_lock_without_public_rereads(self) -> None:
        memory = MemoryStore(start=NOW)
        doctor = await memory.create_doctor("Atomic Doctor")
        await memory.create_patient(
            Patient(id="p", doctor_id=doctor.id, name="P", created_at=NOW)
        )
        public_reads = (
            "doctor_by_id",
            "list_patients",
            "list_loops",
            "list_events",
            "list_reports",
            "list_link_tokens",
            "open_relays",
            "get_settings",
        )
        with ExitStack() as stack:
            for name in public_reads:
                stack.enter_context(
                    patch.object(
                        memory,
                        name,
                        AsyncMock(side_effect=AssertionError(f"nested public read: {name}")),
                    )
                )
            bundle = await memory.read_workspace(doctor.id)
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle.doctor.id, doctor.id)
        self.assertEqual([row.id for row in bundle.patients], ["p"])
        self.assertIsNotNone(bundle.read_at)

    async def test_production_loader_declares_a_read_only_transaction(self) -> None:
        source = inspect.getsource(store.read_workspace)
        self.assertIn("transaction(read_only=True)", source)
        self.assertIn("transaction=transaction", source)
        self.assertIn('if row.state == "open"', source)
        self.assertIn("read_at=doctor_snap.read_time", source)

    async def test_production_close_is_transactional_and_first_transition_only(self) -> None:
        source = inspect.getsource(store.close_loop)
        self.assertIn("@firestore.async_transactional", source)
        self.assertIn("transaction=transaction", source)
        self.assertIn('was_done = row.get("state") == "done"', source)
        self.assertIn('if not was_done and row.get("closed_at") is None', source)

    async def test_close_fact_is_written_once_and_never_moved(self) -> None:
        memory = MemoryStore(start=NOW)
        doctor = await memory.create_doctor("Closure Doctor")
        patient = Patient(
            id="p",
            doctor_id=doctor.id,
            name="Patient",
            created_at=NOW,
        )
        await memory.create_patient(patient)
        row = Loop(
            id="l",
            doctor_id=doctor.id,
            patient_id=patient.id,
            type="TEST",
            title="Lab",
            state="pending_review",
            created_at=NOW,
            updated_at=NOW,
        )
        await memory.create_loop(row)
        first_close = NOW + timedelta(minutes=1)
        later_attempt = NOW + timedelta(hours=2)

        await memory.close_loop(row.id, closed_at=first_close)
        await memory.close_loop(
            row.id,
            closed_at=later_attempt,
            doctor_reviewed=True,
        )

        stored = await memory.get_loop(row.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.state, "done")
        self.assertEqual(stored.closed_at, first_close)
        self.assertTrue(stored.doctor_reviewed)

        already_done = row.model_copy(
            update={"id": "already-done", "state": "done", "closed_at": None}
        )
        await memory.create_loop(already_done)
        await memory.close_loop(already_done.id, closed_at=later_attempt)
        untouched = await memory.get_loop(already_done.id)
        self.assertIsNotNone(untouched)
        self.assertIsNone(untouched.closed_at)

        with self.assertRaisesRegex(Exception, "missing loop"):
            await memory.close_loop("gone", closed_at=later_attempt)


if __name__ == "__main__":
    unittest.main()
