"""Direct Telegram effects cross the typed, shadow-observed Gate 2 seam."""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import adapters
from core.channel_contracts import DeliveryOutcome, OutboundIntent


APP = Path(__file__).resolve().parents[1]


class DirectTelegramEdgeTests(unittest.IsolatedAsyncioTestCase):
    def shadow_environment(self):
        return patch.dict(
            os.environ,
            {
                "LEGACY_RUNTIME": "true",
                "OUTBOX_MODE": "shadow",
                "OUTBOX_SHADOW_TIMEOUT_MS": "250",
                "OUTBOX_SHADOW_MAX_IN_FLIGHT": "32",
            },
        )

    async def test_known_card_is_observed_without_endpoint_or_credentials(
        self,
    ) -> None:
        provider = AsyncMock(
            return_value={"ok": True, "result": {"message_id": 314}}
        )
        persist = AsyncMock()
        target = adapters.ResolvedTarget(
            doctor_id="doctor-internal",
            patient_id=None,
            synthetic=False,
        )
        text = "Open https://sanad.test/p/patient-secret?start=join-secret"
        card = {
            "title": "Bound",
            "token": "card-secret",
            "lines": ["ordinary"],
        }

        with (
            self.shadow_environment(),
            patch.object(
                adapters, "resolve_target", new=AsyncMock(return_value=target)
            ),
            patch.object(adapters.telegram, "send_card", new=provider),
            patch.object(adapters.outbox, "record_shadow", new=persist),
        ):
            receipt = await adapters.send_card(
                998877,
                text,
                card,
                target_ref="doctor:doctor-web-secret",
            )

        provider.assert_awaited_once_with(998877, text, card)
        persist.assert_awaited_once()
        intent = persist.await_args.args[0]
        self.assertIsInstance(intent, OutboundIntent)
        self.assertEqual("doctor-internal", intent.doctor_id)
        self.assertFalse(intent.synthetic)
        self.assertIn("/p/[REDACTED]", intent.text)
        self.assertIn("start=[REDACTED]", intent.text)
        durable = intent.model_dump_json()
        for forbidden in (
            "998877",
            "doctor-web-secret",
            "patient-secret",
            "join-secret",
            "card-secret",
        ):
            self.assertNotIn(forbidden, durable)
        self.assertEqual(
            DeliveryOutcome.ACCEPTED_BY_PROVIDER,
            receipt.outcome,
        )
        self.assertNotEqual(DeliveryOutcome.DELIVERED, receipt.outcome)
        self.assertEqual("314", receipt.provider_receipt_ref)

    async def test_qr_photo_keeps_bytes_transient_and_context_internal(self) -> None:
        image = b"\x89PNG\r\nraw-qr-secret"
        caption = "Forward https://sanad.test/qr/raw-token.png?start=join-token"
        provider = AsyncMock(return_value={"ok": True, "result": {}})
        persist = AsyncMock()
        target = adapters.ResolvedTarget(
            doctor_id="doctor-internal",
            patient_id=None,
            synthetic=False,
        )
        patient = SimpleNamespace(
            id="patient-internal",
            doctor_id="doctor-internal",
            synthetic=False,
        )

        with (
            self.shadow_environment(),
            patch.object(adapters.telegram, "enabled", return_value=True),
            patch.object(adapters.telegram, "send_photo", new=provider),
            patch.object(
                adapters, "resolve_target", new=AsyncMock(return_value=target)
            ),
            patch.object(
                adapters.store,
                "get_patient",
                new=AsyncMock(return_value=patient),
            ),
            patch.object(adapters.outbox, "record_shadow", new=persist),
        ):
            receipt = await adapters.send_photo(
                445566,
                image,
                caption=caption,
                target_ref="doctor:doctor-web-secret",
                patient_id=patient.id,
            )

        provider.assert_awaited_once_with(445566, image, caption=caption)
        persist.assert_awaited_once()
        intent = persist.await_args.args[0]
        self.assertEqual("patient-internal", intent.patient_id)
        self.assertEqual({"artifact": "patient_qr"}, intent.meta)
        durable = intent.model_dump_json()
        self.assertNotIn("raw-qr-secret", durable)
        self.assertNotIn("445566", durable)
        self.assertNotIn("doctor-web-secret", durable)
        self.assertNotIn("raw-token", durable)
        self.assertNotIn("join-token", durable)
        self.assertEqual(
            DeliveryOutcome.ACCEPTED_BY_PROVIDER,
            receipt.outcome,
        )
        self.assertNotEqual(DeliveryOutcome.DELIVERED, receipt.outcome)

    async def test_unknown_target_still_calls_legacy_provider_once_without_shadow(
        self,
    ) -> None:
        provider = AsyncMock(return_value={"ok": False, "description": "down"})
        persist = AsyncMock()

        with (
            self.shadow_environment(),
            patch.object(adapters.telegram, "send_card", new=provider),
            patch.object(adapters.outbox, "record_shadow", new=persist),
        ):
            receipt = await adapters.send_card(112233, "provider call")

        provider.assert_awaited_once_with(112233, "provider call", None)
        persist.assert_not_awaited()
        self.assertEqual(DeliveryOutcome.RETRYABLE_FAILURE, receipt.outcome)
        self.assertNotEqual(DeliveryOutcome.DELIVERED, receipt.outcome)

    async def test_missing_endpoint_is_unknown_and_never_calls_provider(self) -> None:
        provider = AsyncMock()
        with patch.object(adapters.telegram, "send_card", new=provider):
            receipt = await adapters.send_card(None, "cannot route")
        provider.assert_not_awaited()
        self.assertEqual(DeliveryOutcome.UNKNOWN, receipt.outcome)


class DirectEffectInventoryTests(unittest.TestCase):
    def test_provider_sends_exist_only_inside_the_adapter_edge(self) -> None:
        direct: list[tuple[str, str, int]] = []
        callbacks: list[tuple[str, int]] = []
        for path in sorted((APP / "core").glob("*.py")) + [APP / "main.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (
                    isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "telegram"
                ):
                    continue
                if fn.attr in {"send_card", "send_photo"}:
                    direct.append((path.name, fn.attr, node.lineno))
                elif fn.attr == "answer_callback":
                    callbacks.append((path.name, node.lineno))

        self.assertTrue(direct)
        self.assertEqual({"adapters.py"}, {name for name, _, _ in direct})
        # Callback answers are protocol acknowledgements, not replayable
        # clinical notifications; the only exception stays isolated in router.
        self.assertTrue(callbacks)
        self.assertEqual({"tg_router.py"}, {name for name, _ in callbacks})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
