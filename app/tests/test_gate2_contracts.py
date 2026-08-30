"""Gate 2 contracts for channel normalization and command execution.

This file is deliberately test-first.  The production modules do not exist at
the time it is introduced, so one bootstrap test reports every missing module
as a normal assertion failure and the dependent behavioral tests are skipped.
As soon as all four modules import, every contract below becomes active.

The suite is hermetic: its adapters, authorizer, handlers, and replay ledger are
small in-memory doubles.  It performs no network, model, filesystem, or
Firestore work and tests only the public APIs named in the Gate 2 dossier.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import unittest
from datetime import datetime, timezone
from hashlib import sha256
from types import ModuleType
from typing import Any, Awaitable, Callable
from unittest.mock import patch


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
MODULE_NAMES = (
    "core.channel_contracts",
    "core.adapter_registry",
    "core.command_bus",
    "core.runtime",
)


MODULES: dict[str, ModuleType] = {}
IMPORT_FAILURES: dict[str, str] = {}
for module_name in MODULE_NAMES:
    if importlib.util.find_spec(module_name) is None:
        # A clean assertion below is more useful than aborting test discovery.
        IMPORT_FAILURES[module_name] = "module not found"
    else:
        # Once a module exists, broken nested imports are real errors and must
        # not be disguised as an unimplemented Gate 2 surface.
        MODULES[module_name] = importlib.import_module(module_name)


def missing(*module_names: str) -> bool:
    return any(name in IMPORT_FAILURES for name in module_names)


def export(case: unittest.TestCase, module_name: str, name: str) -> Any:
    module = MODULES[module_name]
    case.assertTrue(
        hasattr(module, name),
        f"{module_name} must export {name}",
    )
    return getattr(module, name)


class Gate2ModulesExist(unittest.TestCase):
    def test_gate2_contract_modules_import(self) -> None:
        detail = "; ".join(
            f"{name} ({IMPORT_FAILURES[name]})" for name in sorted(IMPORT_FAILURES)
        )
        self.assertEqual(
            {},
            IMPORT_FAILURES,
            "Gate 2 production contract modules are not implemented: " + detail,
        )


class ContractFixtures:
    """Builders shared by contract and bus tests; no production behavior mocked."""

    def contracts(self) -> ModuleType:
        return MODULES["core.channel_contracts"]

    def actor(self, kind: str, ident: str) -> Any:
        ActorRef = export(self, "core.channel_contracts", "ActorRef")
        return ActorRef(kind=kind, id=ident)

    def attachment(self, inline_bytes: bytes = b"not-persisted") -> Any:
        InboundAttachment = export(
            self, "core.channel_contracts", "InboundAttachment"
        )
        return InboundAttachment(
            kind="image",
            mime="image/png",
            size=8,
            sha256="a" * 64,
            storage_ref="telegram:file:file-1",
            inline_bytes=inline_bytes,
        )

    def envelope(
        self,
        *,
        transient_payload: Any = None,
        inline_bytes: bytes = b"not-persisted",
    ) -> Any:
        InboundEnvelope = export(
            self, "core.channel_contracts", "InboundEnvelope"
        )
        SignatureVerdict = export(
            self, "core.channel_contracts", "SignatureVerdict"
        )
        return InboundEnvelope(
            provider="telegram",
            provider_account="sanad-bot",
            provider_message_id="update-101",
            received_at=NOW,
            signature_verdict=SignatureVerdict.VERIFIED,
            tenant_id="doctor-1",
            principal=self.actor("doctor", "doctor-1"),
            actor=self.actor("patient", "patient-1"),
            endpoint_id="endpoint-1",
            thread_id="chat-7700",
            reply_to_action="reviewed:loop-1",
            text="Here is the result",
            attachments=(self.attachment(inline_bytes),),
            ordering_key="message-19",
            consent={"proactive_allowed": True, "proxy": False},
            identity={
                "method": "telegram_chat_binding",
                "verified": True,
            },
            raw_payload_ref="telegram:update:update-101",
            synthetic=False,
            transient_payload=transient_payload,
        )

    def command(
        self,
        *,
        kind: str = "PING",
        key: str = "request-1",
        envelope: Any = None,
    ) -> Any:
        Command = export(self, "core.channel_contracts", "Command")
        live_envelope = self.envelope() if envelope is None else envelope
        return Command(
            id="command-1",
            kind=kind,
            idempotency_key=key,
            tenant_id="doctor-1",
            principal=self.actor("doctor", "doctor-1"),
            actor=self.actor("patient", "patient-1"),
            source="telegram",
            endpoint_id="endpoint-1",
            thread_id="chat-7700",
            payload={"envelope": live_envelope.canonical_content()},
            created_at=NOW,
            synthetic=False,
            envelope=live_envelope,
        )

    def result(
        self,
        status: Any,
        *,
        code: str = "handled",
    ) -> Any:
        CommandResult = export(self, "core.channel_contracts", "CommandResult")
        return CommandResult(
            status=status,
            code=code,
            detail="",
            value={"ok": True},
        )


@unittest.skipIf(
    missing("core.channel_contracts"),
    "core.channel_contracts is not implemented yet",
)
class ChannelContractTests(ContractFixtures, unittest.TestCase):
    def test_command_status_is_exactly_the_four_dossier_states(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        expected = {"ACCEPTED", "REJECTED", "CONFLICT", "RETRYABLE"}
        self.assertEqual(expected, set(CommandStatus.__members__))
        self.assertEqual(expected, {member.value for member in CommandStatus})

    def test_delivery_outcome_is_exactly_the_five_dossier_states(self) -> None:
        DeliveryOutcome = export(
            self, "core.channel_contracts", "DeliveryOutcome"
        )
        expected = {
            "DELIVERED",
            "ACCEPTED_BY_PROVIDER",
            "RETRYABLE_FAILURE",
            "PERMANENT_FAILURE",
            "UNKNOWN",
        }
        self.assertEqual(expected, set(DeliveryOutcome.__members__))
        self.assertEqual(expected, {member.value for member in DeliveryOutcome})

    def test_signature_verdict_has_a_verified_value(self) -> None:
        SignatureVerdict = export(
            self, "core.channel_contracts", "SignatureVerdict"
        )
        self.assertEqual("VERIFIED", SignatureVerdict.VERIFIED.value)

    def test_inline_attachment_bytes_are_never_serialized(self) -> None:
        attachment = self.attachment()
        dumped = attachment.model_dump()

        self.assertNotIn("inline_bytes", dumped)
        self.assertEqual("telegram:file:file-1", dumped["storage_ref"])
        self.assertEqual("image/png", dumped["mime"])
        self.assertEqual(8, dumped["size"])

    def test_inbound_envelope_carries_every_dossier_field(self) -> None:
        envelope = self.envelope()
        dumped = envelope.model_dump()
        required = {
            "provider",
            "provider_account",
            "provider_message_id",
            "received_at",
            "signature_verdict",
            "tenant_id",
            "principal",
            "actor",
            "endpoint_id",
            "thread_id",
            "reply_to_action",
            "text",
            "attachments",
            "ordering_key",
            "consent",
            "identity",
            "raw_payload_ref",
            "synthetic",
        }

        self.assertTrue(required.issubset(dumped), required - set(dumped))
        self.assertEqual("doctor-1", envelope.principal.id)
        self.assertEqual("patient-1", envelope.actor.id)
        self.assertNotEqual(envelope.principal, envelope.actor)
        self.assertEqual("reviewed:loop-1", envelope.reply_to_action)
        self.assertFalse(envelope.synthetic)
        self.assertNotIn("inline_bytes", dumped["attachments"][0])

    def test_canonical_content_keeps_security_and_replay_context(self) -> None:
        canonical = self.envelope().canonical_content()

        self.assertEqual("update-101", canonical["provider_message_id"])
        self.assertEqual("VERIFIED", canonical["signature_verdict"])
        self.assertEqual("message-19", canonical["ordering_key"])
        self.assertEqual(
            {"proactive_allowed": True, "proxy": False},
            canonical["consent"],
        )
        self.assertEqual(
            {"method": "telegram_chat_binding", "verified": True},
            canonical["identity"],
        )
        self.assertEqual(
            "telegram:update:update-101", canonical["raw_payload_ref"]
        )
        self.assertNotIn("inline_bytes", canonical["attachments"][0])

    def test_transient_data_is_live_but_absent_from_dumps_repr_and_fingerprint(
        self,
    ) -> None:
        first_raw = {
            "authorization": "Bearer first-transient-secret",
            "body": b"first-transient-bytes",
        }
        second_raw = {
            "authorization": "Bearer second-transient-secret",
            "body": b"second-transient-bytes",
        }
        first_envelope = self.envelope(
            transient_payload=first_raw,
            inline_bytes=b"first-inline-bytes",
        )
        second_envelope = self.envelope(
            transient_payload=second_raw,
            inline_bytes=b"second-inline-bytes",
        )
        first_command = self.command(envelope=first_envelope)
        second_command = self.command(envelope=second_envelope)

        self.assertIs(first_raw, first_envelope.transient_payload)
        self.assertIs(first_envelope, first_command.envelope)
        self.assertEqual(
            b"first-inline-bytes",
            first_command.envelope.attachments[0].inline_bytes,
        )

        envelope_dump = first_envelope.model_dump(mode="python")
        canonical = first_envelope.canonical_content()
        command_dump = first_command.model_dump(mode="python")
        self.assertNotIn("transient_payload", envelope_dump)
        self.assertNotIn("transient_payload", canonical)
        self.assertNotIn("inline_bytes", canonical["attachments"][0])
        self.assertNotIn("envelope", command_dump)
        self.assertNotIn("first-transient-secret", repr(first_envelope))
        self.assertNotIn("first-transient-bytes", repr(first_envelope))
        self.assertNotIn("first-transient-secret", repr(first_command))
        self.assertNotIn("first-inline-bytes", repr(first_command))
        with self.assertRaises(ValueError):
            type(first_command).model_validate({
                **first_command.model_dump(mode="python"),
                "envelope": object(),
            })

        # A replay implementation fingerprints the durable command dump.  Two
        # otherwise identical commands therefore retain one identity even when
        # their process-local provider objects and attachment buffers differ.
        fingerprint = lambda command: sha256(
            command.model_dump_json().encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            first_envelope.canonical_content(),
            second_envelope.canonical_content(),
        )
        self.assertEqual(fingerprint(first_command), fingerprint(second_command))

    def test_command_and_result_preserve_typed_context(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        command = self.command()
        result = self.result(CommandStatus.ACCEPTED)

        self.assertEqual("request-1", command.idempotency_key)
        self.assertEqual("doctor-1", command.principal.id)
        self.assertEqual("patient-1", command.actor.id)
        self.assertEqual("telegram", command.source)
        self.assertEqual("patient-1", command.payload["envelope"]["actor"]["id"])
        self.assertEqual(CommandStatus.ACCEPTED, result.status)
        self.assertEqual({"ok": True}, result.value)

    def test_http_projection_cannot_override_typed_result_fields(self) -> None:
        CommandResult = export(self, "core.channel_contracts", "CommandResult")
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        result = CommandResult(
            status=CommandStatus.REJECTED,
            code="not_authorized",
            detail="refused",
            value={
                "ok": True,
                "status": "ACCEPTED",
                "code": "forged",
                "detail": "forged",
                "kept": 1,
            },
        )

        self.assertEqual(
            {
                "ok": False,
                "status": "REJECTED",
                "code": "not_authorized",
                "detail": "refused",
                "kept": 1,
            },
            result.as_http(),
        )

    def test_outbound_intent_is_serializable_and_channel_neutral(self) -> None:
        OutboundIntent = export(
            self, "core.channel_contracts", "OutboundIntent"
        )
        OutboundAttachment = export(
            self, "core.channel_contracts", "OutboundAttachment"
        )
        NotificationClass = export(
            self, "core.channel_contracts", "NotificationClass"
        )
        intent = OutboundIntent(
            id="intent-1",
            synthetic=False,
            doctor_id="doctor-1",
            recipient_type="patient",
            recipient_id="patient-1",
            patient_id="patient-1",
            notification_class=NotificationClass.SILENT_WORK,
            text="Your result was received.",
            card=None,
            meta={"source_command_id": "command-1"},
            attachments=(OutboundAttachment(
                kind="image",
                mime="image/png",
                storage_ref="gs://private-bucket/result.png",
                name="result.png",
            ),),
            idempotency_key="outbound-1",
            stable_idempotency=True,
            created_at=NOW,
        )
        dumped = intent.model_dump()

        self.assertEqual("outbound-1", dumped["idempotency_key"])
        self.assertEqual("patient-1", dumped["recipient_id"])
        self.assertNotIn("provider", dumped)
        self.assertNotIn("endpoint_id", dumped)
        self.assertEqual(
            "gs://private-bucket/result.png",
            dumped["attachments"][0]["storage_ref"],
        )


class RecordingAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.seen: list[object] = []

    async def normalize(self, raw: object, verified: object) -> object:
        self.seen.append((raw, verified))
        return raw


@unittest.skipIf(
    missing("core.adapter_registry"),
    "core.adapter_registry is not implemented yet",
)
class AdapterRegistryTests(unittest.TestCase):
    def registry(self) -> Any:
        AdapterRegistry = export(
            self, "core.adapter_registry", "AdapterRegistry"
        )
        return AdapterRegistry()

    @staticmethod
    def provider_names(registry: Any) -> set[str]:
        providers = registry.providers
        return set(providers() if callable(providers) else providers)

    def test_registered_adapters_are_retrievable_and_enumerated(self) -> None:
        registry = self.registry()
        telegram = RecordingAdapter("telegram")
        web = RecordingAdapter("web_patient")

        registry.register(telegram)
        registry.register(web)

        self.assertIs(telegram, registry.get("telegram"))
        self.assertIs(web, registry.get("web_patient"))
        self.assertEqual({"telegram", "web_patient"}, self.provider_names(registry))

    def test_duplicate_provider_registration_fails_without_replacement(self) -> None:
        registry = self.registry()
        first = RecordingAdapter("telegram")
        registry.register(first)

        with self.assertRaises(ValueError):
            registry.register(RecordingAdapter("telegram"))

        self.assertIs(first, registry.get("telegram"))

    def test_unknown_provider_fails_closed(self) -> None:
        registry = self.registry()
        with self.assertRaises(LookupError):
            registry.get("whatsapp")

    def test_frozen_registry_rejects_late_registration(self) -> None:
        registry = self.registry()
        telegram = RecordingAdapter("telegram")
        registry.register(telegram)
        registry.freeze()

        with self.assertRaises(RuntimeError):
            registry.register(RecordingAdapter("web_patient"))

        self.assertIs(telegram, registry.get("telegram"))
        self.assertEqual({"telegram"}, self.provider_names(registry))


class MemoryReplayLedger:
    """Atomic public-protocol double used to exercise CommandBus behavior."""

    def __init__(self, claim_type: type) -> None:
        self._claim_type = claim_type
        self._lock = asyncio.Lock()
        self._in_flight: set[str] = set()
        self._completed: dict[str, object] = {}
        self.events: list[str] = []
        self.released: list[str] = []

    async def claim(self, command: object) -> object:
        key = command.idempotency_key
        async with self._lock:
            self.events.append(f"claim:{key}")
            if key in self._completed:
                return self._claim_type(
                    state="COMPLETED", result=self._completed[key]
                )
            if key in self._in_flight:
                return self._claim_type(state="IN_FLIGHT")
            self._in_flight.add(key)
            return self._claim_type(state="CLAIMED")

    async def complete(self, command: object, result: object) -> None:
        key = command.idempotency_key
        async with self._lock:
            self.events.append(f"complete:{key}")
            self._in_flight.discard(key)
            self._completed[key] = result

    async def release(self, command: object) -> None:
        key = command.idempotency_key
        async with self._lock:
            self.events.append(f"release:{key}")
            self._in_flight.discard(key)
            self.released.append(key)


class RecordingAuthorizer:
    def __init__(self, refusal: object | None = None) -> None:
        self.refusal = refusal
        self.calls: list[object] = []

    async def __call__(self, command: object) -> object | None:
        self.calls.append(command)
        return self.refusal


Handler = Callable[[Any], Awaitable[Any]]


@unittest.skipIf(
    missing("core.channel_contracts", "core.command_bus"),
    "Gate 2 contracts or CommandBus are not implemented yet",
)
class CommandBusTests(ContractFixtures, unittest.IsolatedAsyncioTestCase):
    def bus(
        self,
        handlers: dict[str, Handler],
        *,
        authorizer: RecordingAuthorizer | None = None,
        ledger: MemoryReplayLedger | None = None,
    ) -> tuple[Any, RecordingAuthorizer, MemoryReplayLedger]:
        CommandBus = export(self, "core.command_bus", "CommandBus")
        ReplayClaim = export(self, "core.command_bus", "ReplayClaim")
        auth = authorizer or RecordingAuthorizer()
        replay = ledger or MemoryReplayLedger(ReplayClaim)
        return (
            CommandBus(
                handlers=handlers,
                authorizer=auth,
                replay=replay,
            ),
            auth,
            replay,
        )

    def test_authorizer_and_replay_ledger_are_mandatory_dependencies(self) -> None:
        CommandBus = export(self, "core.command_bus", "CommandBus")
        ReplayClaim = export(self, "core.command_bus", "ReplayClaim")
        authorizer = RecordingAuthorizer()
        ledger = MemoryReplayLedger(ReplayClaim)

        with self.assertRaisesRegex(TypeError, "authorizer"):
            CommandBus(handlers={})
        with self.assertRaisesRegex(TypeError, "replay"):
            CommandBus(handlers={}, authorizer=authorizer)
        with self.assertRaisesRegex(TypeError, "authorizer"):
            CommandBus(handlers={}, replay=ledger)

    async def test_authorizer_rejection_never_reaches_a_handler(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        calls = 0

        async def handler(command: object) -> object:
            nonlocal calls
            calls += 1
            return self.result(CommandStatus.ACCEPTED)

        refusal = self.result(CommandStatus.REJECTED, code="not_authorized")
        bus, authorizer, ledger = self.bus(
            {"PING": handler}, authorizer=RecordingAuthorizer(refusal=refusal)
        )
        result = await bus.execute(self.command())

        self.assertEqual(CommandStatus.REJECTED, result.status)
        self.assertEqual(1, len(authorizer.calls))
        self.assertEqual(0, calls)
        self.assertEqual([], ledger.events)

    async def test_concurrent_duplicate_is_claimed_before_the_handler(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        entered = asyncio.Event()
        finish = asyncio.Event()
        ReplayClaim = export(self, "core.command_bus", "ReplayClaim")
        ledger = MemoryReplayLedger(ReplayClaim)
        handler_calls = 0

        async def handler(command: object) -> object:
            nonlocal handler_calls
            handler_calls += 1
            ledger.events.append("handler")
            entered.set()
            await finish.wait()
            return self.result(CommandStatus.ACCEPTED)

        bus, _, _ = self.bus({"PING": handler}, ledger=ledger)
        command = self.command(key="same-provider-message")
        first_task = asyncio.create_task(bus.execute(command))
        await asyncio.wait_for(entered.wait(), timeout=1)

        duplicate = await asyncio.wait_for(bus.execute(command), timeout=1)
        finish.set()
        first = await asyncio.wait_for(first_task, timeout=1)

        self.assertEqual(CommandStatus.ACCEPTED, first.status)
        self.assertEqual(CommandStatus.CONFLICT, duplicate.status)
        self.assertEqual(1, handler_calls)
        self.assertLess(
            ledger.events.index("claim:same-provider-message"),
            ledger.events.index("handler"),
        )

    async def test_completed_duplicate_returns_the_recorded_result(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        handler_calls = 0

        async def handler(command: object) -> object:
            nonlocal handler_calls
            handler_calls += 1
            return self.result(CommandStatus.ACCEPTED, code="first-result")

        bus, _, _ = self.bus({"PING": handler})
        command = self.command(key="completed-request")

        first = await bus.execute(command)
        duplicate = await bus.execute(command)

        self.assertEqual(first, duplicate)
        self.assertEqual("first-result", duplicate.code)
        self.assertEqual(1, handler_calls)

    async def test_retryable_result_releases_the_claim_for_another_attempt(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        attempts = 0

        async def handler(command: object) -> object:
            nonlocal attempts
            attempts += 1
            status = (
                CommandStatus.RETRYABLE if attempts == 1 else CommandStatus.ACCEPTED
            )
            return self.result(status, code=f"attempt-{attempts}")

        bus, _, ledger = self.bus({"PING": handler})
        command = self.command(key="retryable-request")

        first = await bus.execute(command)
        second = await bus.execute(command)

        self.assertEqual(CommandStatus.RETRYABLE, first.status)
        self.assertEqual(CommandStatus.ACCEPTED, second.status)
        self.assertEqual(2, attempts)
        self.assertIn("retryable-request", ledger.released)

    async def test_unknown_command_is_a_typed_rejection(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        bus, authorizer, _ = self.bus({})

        result = await bus.execute(self.command(kind="NOT_REGISTERED"))

        self.assertEqual(CommandStatus.REJECTED, result.status)
        self.assertEqual("unknown_command", result.code)
        self.assertEqual(0, len(authorizer.calls))

    async def test_ambiguous_exception_stays_in_flight_and_propagates(self) -> None:
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")

        class HandlerBroke(RuntimeError):
            pass

        attempts = 0

        async def handler(command: object) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HandlerBroke("unexpected domain failure")
            return self.result(CommandStatus.ACCEPTED, code="recovered")

        bus, _, ledger = self.bus({"PING": handler})
        command = self.command(key="exception-request")

        with self.assertRaisesRegex(HandlerBroke, "unexpected domain failure"):
            await bus.execute(command)
        duplicate = await bus.execute(command)

        self.assertEqual(CommandStatus.CONFLICT, duplicate.status)
        self.assertEqual(1, attempts)
        self.assertNotIn("exception-request", ledger.released)
        self.assertEqual(
            ["claim:exception-request", "claim:exception-request"],
            ledger.events,
        )

    async def test_successful_work_is_not_repeated_if_completion_write_fails(self) -> None:
        """A receipt failure after domain success must not reopen the command.

        This is distinct from a handler exception: the side effect has already
        happened, so releasing its claim would let a retry perform it twice.
        """
        CommandStatus = export(self, "core.channel_contracts", "CommandStatus")
        ReplayClaim = export(self, "core.command_bus", "ReplayClaim")

        class CompletionWriteFailed(RuntimeError):
            pass

        class FailingCompletionLedger(MemoryReplayLedger):
            fail_once = True

            async def complete(self, command: object, result: object) -> None:
                if self.fail_once:
                    self.fail_once = False
                    self.events.append(
                        f"complete_failed:{command.idempotency_key}"
                    )
                    raise CompletionWriteFailed("receipt persistence unavailable")
                await super().complete(command, result)

        handler_calls = 0

        async def handler(command: object) -> object:
            nonlocal handler_calls
            handler_calls += 1
            return self.result(CommandStatus.ACCEPTED)

        ledger = FailingCompletionLedger(ReplayClaim)
        bus, _, _ = self.bus({"PING": handler}, ledger=ledger)
        command = self.command(key="completed-work")

        with self.assertRaisesRegex(
            CompletionWriteFailed, "receipt persistence unavailable"
        ):
            await bus.execute(command)
        duplicate = await bus.execute(command)

        self.assertEqual(CommandStatus.CONFLICT, duplicate.status)
        self.assertEqual(1, handler_calls)
        self.assertNotIn("completed-work", ledger.released)


@unittest.skipIf(
    missing("core.runtime"),
    "core.runtime is not implemented yet",
)
class RuntimeConfigurationTests(unittest.TestCase):
    def runtime(self) -> ModuleType:
        return MODULES["core.runtime"]

    def test_legacy_runtime_defaults_true_and_accepts_exact_values(self) -> None:
        legacy_runtime = export(self, "core.runtime", "legacy_runtime")
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(True, legacy_runtime())
        with patch.dict(os.environ, {"LEGACY_RUNTIME": "true"}, clear=True):
            self.assertIs(True, legacy_runtime())
        with patch.dict(os.environ, {"LEGACY_RUNTIME": "false"}, clear=True):
            self.assertIs(False, legacy_runtime())

    def test_legacy_runtime_rejects_every_noncanonical_value(self) -> None:
        legacy_runtime = export(self, "core.runtime", "legacy_runtime")
        for value in ("", " ", "1", "0", "yes", "no", "on", "off", "maybe"):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ, {"LEGACY_RUNTIME": value}, clear=True
                ):
                    with self.assertRaises((ValueError, RuntimeError)):
                        legacy_runtime()

    def test_legacy_runtime_normalizes_case_and_surrounding_space(self) -> None:
        legacy_runtime = export(self, "core.runtime", "legacy_runtime")
        with patch.dict(os.environ, {"LEGACY_RUNTIME": " TRUE "}, clear=True):
            self.assertIs(True, legacy_runtime())
        with patch.dict(os.environ, {"LEGACY_RUNTIME": " False "}, clear=True):
            self.assertIs(False, legacy_runtime())

    def test_outbox_mode_defaults_off_and_accepts_shadow(self) -> None:
        outbox_mode = export(self, "core.runtime", "outbox_mode")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("off", outbox_mode())
        with patch.dict(os.environ, {"OUTBOX_MODE": "off"}, clear=True):
            self.assertEqual("off", outbox_mode())
        with patch.dict(os.environ, {"OUTBOX_MODE": "shadow"}, clear=True):
            self.assertEqual("shadow", outbox_mode())

    def test_outbox_mode_rejects_active_or_ambiguous_values(self) -> None:
        outbox_mode = export(self, "core.runtime", "outbox_mode")
        for value in ("", " ", "on", "active", "true", "pending", "disabled"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"OUTBOX_MODE": value}, clear=True):
                    with self.assertRaises((ValueError, RuntimeError)):
                        outbox_mode()

    def test_shadow_limits_have_safe_defaults_and_accept_their_boundaries(self) -> None:
        runtime = self.runtime()
        shadow_timeout_seconds = export(
            self, "core.runtime", "shadow_timeout_seconds"
        )
        shadow_max_in_flight = export(
            self, "core.runtime", "shadow_max_in_flight"
        )

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                runtime.DEFAULT_SHADOW_TIMEOUT_MS / 1000,
                shadow_timeout_seconds(),
            )
            self.assertEqual(
                runtime.DEFAULT_SHADOW_MAX_IN_FLIGHT,
                shadow_max_in_flight(),
            )

        for milliseconds in (1, 1000):
            with self.subTest(milliseconds=milliseconds):
                with patch.dict(
                    os.environ,
                    {"OUTBOX_SHADOW_TIMEOUT_MS": str(milliseconds)},
                    clear=True,
                ):
                    self.assertEqual(milliseconds / 1000, shadow_timeout_seconds())

        for maximum in (1, 128):
            with self.subTest(maximum=maximum):
                with patch.dict(
                    os.environ,
                    {"OUTBOX_SHADOW_MAX_IN_FLIGHT": str(maximum)},
                    clear=True,
                ):
                    self.assertEqual(maximum, shadow_max_in_flight())

    def test_shadow_limits_reject_nonintegers_and_out_of_range_values(self) -> None:
        shadow_timeout_seconds = export(
            self, "core.runtime", "shadow_timeout_seconds"
        )
        shadow_max_in_flight = export(
            self, "core.runtime", "shadow_max_in_flight"
        )

        for value in ("", " ", "1.5", "fast", "0", "1001", "-1"):
            with self.subTest(setting="timeout", value=value):
                with patch.dict(
                    os.environ,
                    {"OUTBOX_SHADOW_TIMEOUT_MS": value},
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "OUTBOX_SHADOW_TIMEOUT_MS"
                    ):
                        shadow_timeout_seconds()

        for value in ("", " ", "1.5", "many", "0", "129", "-1"):
            with self.subTest(setting="max_in_flight", value=value):
                with patch.dict(
                    os.environ,
                    {"OUTBOX_SHADOW_MAX_IN_FLIGHT": value},
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "OUTBOX_SHADOW_MAX_IN_FLIGHT"
                    ):
                        shadow_max_in_flight()

    def test_gate2_validation_accepts_only_a_complete_legacy_configuration(
        self,
    ) -> None:
        validate_gate2 = export(self, "core.runtime", "validate_gate2")

        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(validate_gate2())
        with patch.dict(
            os.environ,
            {
                "LEGACY_RUNTIME": "true",
                "OUTBOX_MODE": "shadow",
                "OUTBOX_SHADOW_TIMEOUT_MS": "250",
                "OUTBOX_SHADOW_MAX_IN_FLIGHT": "32",
            },
            clear=True,
        ):
            self.assertIsNone(validate_gate2())

    def test_gate2_validation_rejects_legacy_runtime_false(self) -> None:
        validate_gate2 = export(self, "core.runtime", "validate_gate2")

        with patch.dict(
            os.environ, {"LEGACY_RUNTIME": "false"}, clear=True
        ):
            with self.assertRaisesRegex(
                RuntimeError, "LEGACY_RUNTIME=false.*unavailable"
            ):
                validate_gate2()

    def test_gate2_validation_rejects_every_invalid_shadow_setting(self) -> None:
        validate_gate2 = export(self, "core.runtime", "validate_gate2")
        invalid = (
            ("OUTBOX_MODE", "active"),
            ("OUTBOX_SHADOW_TIMEOUT_MS", "0"),
            ("OUTBOX_SHADOW_MAX_IN_FLIGHT", "129"),
        )

        for setting, value in invalid:
            with self.subTest(setting=setting, value=value):
                with patch.dict(
                    os.environ,
                    {"LEGACY_RUNTIME": "true", setting: value},
                    clear=True,
                ):
                    with self.assertRaisesRegex(RuntimeError, setting):
                        validate_gate2()


if __name__ == "__main__":
    unittest.main()
