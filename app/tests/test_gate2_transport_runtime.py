"""Focused contracts for the first live Gate 2 composition slice."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from typing import Any

from core.adapter_registry import AdapterRegistry
from core.channel_contracts import (
    ActorRef,
    Command,
    CommandResult,
    CommandStatus,
    DeliveryOutcome,
    DeliveryReceipt,
    InboundAttachment,
    InboundEnvelope,
    NotificationClass,
    OutboundIntent,
    SignatureVerdict,
)
from core.command_bus import CommandBus, ReplayClaim
from core.command_replay import command_fingerprint
from core.transport_runtime import (
    CommandSpec,
    InjectedChannelAdapter,
    TransportRuntime,
    command_for,
    legacy_response,
    legacy_result,
)


NOW = datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)
PROVIDERS = ("web", "telegram", "cloud_tasks")


class MemoryReplay:
    def __init__(self) -> None:
        self.claimed: list[Command] = []
        self.completed: list[tuple[Command, CommandResult]] = []
        self.released: list[Command] = []

    async def claim(self, command: Command) -> ReplayClaim:
        self.claimed.append(command)
        return ReplayClaim(state="CLAIMED")

    async def complete(self, command: Command, result: CommandResult) -> None:
        self.completed.append((command, result))

    async def release(self, command: Command) -> None:
        self.released.append(command)


class Normalizer:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls: list[tuple[object, Any]] = []

    async def __call__(self, raw: object, context: Any) -> InboundEnvelope:
        self.calls.append((raw, context))
        return envelope(
            self.provider,
            context["sequence"],
            transient_payload=raw,
        )


def envelope(
    provider: str,
    sequence: int = 1,
    *,
    transient_payload: Any = None,
    inline_bytes: bytes = b"must-not-cross-the-command-boundary",
) -> InboundEnvelope:
    synthetic = provider == "web"
    verdict = SignatureVerdict.SYNTHETIC if synthetic else SignatureVerdict.VERIFIED
    actor_kind = "patient" if provider != "cloud_tasks" else "task"
    return InboundEnvelope(
        provider=provider,
        provider_account=f"{provider}-account",
        provider_message_id=f"message-{sequence}",
        received_at=NOW,
        signature_verdict=verdict,
        tenant_id="doctor-1",
        actor=ActorRef(kind=actor_kind, id=f"{actor_kind}-1"),
        principal=ActorRef(kind=actor_kind, id=f"{actor_kind}-1"),
        endpoint_id=f"endpoint-{provider}",
        thread_id=f"thread-{provider}",
        text="same canonical input",
        attachments=(InboundAttachment(
            kind="image",
            mime="image/png",
            size=8,
            sha256="a" * 64,
            storage_ref=f"{provider}:file:1",
            inline_bytes=inline_bytes,
        ),),
        ordering_key=f"order-{sequence}",
        consent={"allowed": True},
        identity={"method": f"{provider}_binding", "verified": True},
        raw_payload_ref=f"{provider}:payload:message-{sequence}",
        synthetic=synthetic,
        transient_payload=transient_payload,
    )


def outbound_intent() -> OutboundIntent:
    return OutboundIntent(
        id="intent-1",
        synthetic=True,
        doctor_id="doctor-1",
        recipient_type="patient",
        recipient_id="patient-1",
        patient_id="patient-1",
        notification_class=NotificationClass.LEGACY_UNCLASSIFIED,
        text="hello",
        idempotency_key="intent-key",
        stable_idempotency=True,
        created_at=NOW,
    )


class TransportRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def build(
        self,
        *,
        normalizers: dict[str, Any] | None = None,
        handler: Any = None,
    ) -> tuple[TransportRuntime, CommandBus, MemoryReplay, dict[str, Normalizer]]:
        seen = normalizers or {name: Normalizer(name) for name in PROVIDERS}
        replay = MemoryReplay()

        async def allow(_: Command) -> None:
            return None

        async def default_handler(command: Command) -> CommandResult:
            return legacy_result(command.payload["route_body"])

        bus = CommandBus(
            handlers={"LEGACY": handler or default_handler},
            authorizer=allow,
            replay=replay,
        )
        adapters = [
            InjectedChannelAdapter(name, seen[name]) for name in seen
        ]
        return TransportRuntime(bus=bus, adapters=adapters), bus, replay, seen

    async def test_web_telegram_and_tasks_share_one_real_bus(self) -> None:
        handled: list[Command] = []

        async def handler(command: Command) -> CommandResult:
            handled.append(command)
            return legacy_result(command.payload["route_body"])

        runtime, bus, replay, normalizers = self.build(handler=handler)
        bodies = {
            "web": {"ok": True},
            "telegram": {"ok": True},
            "cloud_tasks": {"sent": False, "reason": "stale run id"},
        }

        outcomes = []
        for sequence, provider in enumerate(PROVIDERS, start=1):
            raw = {
                "text": "same canonical input",
                "bot_token": "raw-provider-secret",
                "raw_bytes": b"raw-provider-body",
            }
            context = {
                "sequence": sequence,
                "authorization": "Bearer must-not-be-copied",
            }
            outcomes.append(await runtime.execute(
                provider,
                raw,
                context,
                CommandSpec(
                    id=f"command-{sequence}",
                    idempotency_key=f"request-{sequence}",
                    kind="LEGACY",
                    payload={"route_body": bodies[provider]},
                ),
            ))

        self.assertIs(bus, runtime.bus)
        self.assertEqual(3, len(handled))
        self.assertEqual(handled, replay.claimed)
        self.assertEqual(3, len(replay.completed))
        self.assertEqual(
            ["web", "telegram", "cloud_tasks"],
            [command.source for command in handled],
        )
        self.assertEqual(
            [bodies[name] for name in PROVIDERS],
            [outcome.legacy_response() for outcome in outcomes],
        )
        for provider, normalizer in normalizers.items():
            self.assertEqual(1, len(normalizer.calls), provider)
        rendered = repr([command.model_dump(mode="python") for command in handled])
        self.assertNotIn("raw-provider-secret", rendered)
        self.assertNotIn("must-not-be-copied", rendered)
        self.assertNotIn("raw-provider-body", rendered)
        self.assertNotIn("must-not-cross-the-command-boundary", rendered)

    async def test_claimed_handlers_receive_transient_provider_objects_and_bytes(
        self,
    ) -> None:
        holder: dict[str, MemoryReplay] = {}
        handled: list[tuple[str, object, bytes | None]] = []

        async def handler(command: Command) -> CommandResult:
            # CommandBus must claim before it gives the legacy seam its live
            # provider object or attachment buffer.
            self.assertIs(command, holder["replay"].claimed[-1])
            handled.append((
                command.source,
                command.envelope.transient_payload,
                command.envelope.attachments[0].inline_bytes,
            ))
            return legacy_result({"ok": True})

        runtime, _, replay, _ = self.build(handler=handler)
        holder["replay"] = replay
        raw_by_provider = {
            "telegram": {
                "bot_token": "telegram-transient-secret",
                "update_bytes": b"telegram-update-bytes",
            },
            "cloud_tasks": {
                "authorization": "Bearer task-transient-secret",
                "request_bytes": b"cloud-task-request-bytes",
            },
        }

        for sequence, (provider, raw) in enumerate(
            raw_by_provider.items(), start=1
        ):
            outcome = await runtime.execute(
                provider,
                raw,
                {"sequence": sequence},
                CommandSpec(
                    id=f"command-{provider}",
                    idempotency_key=f"request-{provider}",
                    kind="LEGACY",
                ),
            )
            self.assertIs(raw, outcome.envelope.transient_payload)
            self.assertIs(outcome.envelope, outcome.command.envelope)

        self.assertEqual(
            ["telegram", "cloud_tasks"],
            [source for source, _, _ in handled],
        )
        for provider, raw, inline_bytes in handled:
            self.assertIs(raw_by_provider[provider], raw)
            self.assertEqual(
                b"must-not-cross-the-command-boundary",
                inline_bytes,
            )
        persisted = repr([
            command.model_dump(mode="python") for command in replay.claimed
        ])
        self.assertNotIn("telegram-transient-secret", persisted)
        self.assertNotIn("telegram-update-bytes", persisted)
        self.assertNotIn("task-transient-secret", persisted)
        self.assertNotIn("cloud-task-request-bytes", persisted)

    async def test_registry_contains_real_conforming_adapters_and_is_frozen(self) -> None:
        normalizers = {name: Normalizer(name) for name in PROVIDERS}

        def adapter(name: str) -> InjectedChannelAdapter:
            async def deliver(_: OutboundIntent, __: Any) -> DeliveryReceipt:
                return DeliveryReceipt(provider=name, outcome=DeliveryOutcome.UNKNOWN)

            return InjectedChannelAdapter(
                name,
                normalizers[name],
                deliver=deliver,
            )

        replay = MemoryReplay()

        async def allow(_: Command) -> None:
            return None

        async def handler(_: Command) -> CommandResult:
            return legacy_result({"ok": True})

        bus = CommandBus(
            handlers={"LEGACY": handler}, authorizer=allow, replay=replay
        )
        registered = {name: adapter(name) for name in PROVIDERS}
        runtime = TransportRuntime(bus=bus, adapters=registered.values())

        self.assertEqual(tuple(sorted(PROVIDERS)), runtime.registry.providers)
        for name in PROVIDERS:
            found = runtime.registry.get(name)
            self.assertIs(registered[name], found)
            self.assertIsInstance(
                await found.normalize({"provider": name}, {"sequence": 1}),
                InboundEnvelope,
            )
            receipt = await found.deliver(outbound_intent(), {"id": "endpoint"})
            self.assertEqual(name, receipt.provider)
            self.assertEqual(DeliveryOutcome.UNKNOWN, receipt.outcome)

        with self.assertRaisesRegex(RuntimeError, "registry is frozen"):
            runtime.registry.register(
                InjectedChannelAdapter("whatsapp", Normalizer("whatsapp"))
            )

    async def test_normalizer_must_return_the_registered_provider_envelope(self) -> None:
        async def wrong_type(_: object, __: Any) -> object:
            return object()

        normalizers: dict[str, Any] = {
            "web": wrong_type,
            "telegram": Normalizer("telegram"),
            "cloud_tasks": Normalizer("cloud_tasks"),
        }
        runtime, _, replay, _ = self.build(normalizers=normalizers)
        spec = CommandSpec(id="c", idempotency_key="k", kind="LEGACY")

        with self.assertRaisesRegex(TypeError, "InboundEnvelope"):
            await runtime.execute("web", {}, None, spec)
        self.assertEqual([], replay.claimed)

        async def wrong_provider(_: object, __: Any) -> InboundEnvelope:
            return envelope("telegram")

        normalizers["web"] = wrong_provider
        runtime, _, replay, _ = self.build(normalizers=normalizers)
        with self.assertRaisesRegex(ValueError, "returned envelope for telegram"):
            await runtime.execute("web", {}, None, spec)
        self.assertEqual([], replay.claimed)

    def test_missing_required_provider_refuses_to_build(self) -> None:
        replay = MemoryReplay()

        async def allow(_: Command) -> None:
            return None

        bus = CommandBus(handlers={}, authorizer=allow, replay=replay)
        with self.assertRaisesRegex(ValueError, "cloud_tasks"):
            TransportRuntime(
                bus=bus,
                adapters=(
                    InjectedChannelAdapter("web", Normalizer("web")),
                    InjectedChannelAdapter("telegram", Normalizer("telegram")),
                ),
            )

    def test_command_rejects_raw_bytes_secret_fields_and_envelope_override(self) -> None:
        clean = envelope("web")
        with self.assertRaisesRegex(ValueError, "raw bytes"):
            command_for(
                clean,
                CommandSpec(
                    id="c-bytes",
                    idempotency_key="k-bytes",
                    kind="LEGACY",
                    payload={"opaque": b"not durable"},
                ),
            )
        with self.assertRaisesRegex(ValueError, "transport secret field"):
            command_for(
                clean,
                CommandSpec(
                    id="c-secret",
                    idempotency_key="k-secret",
                    kind="LEGACY",
                    payload={"authorization": "Bearer secret"},
                ),
            )
        with self.assertRaisesRegex(ValueError, "canonical envelope"):
            command_for(
                clean,
                CommandSpec(
                    id="c-envelope",
                    idempotency_key="k-envelope",
                    kind="LEGACY",
                    payload={"envelope": {}},
                ),
            )

    def test_real_replay_fingerprint_ignores_every_transient_value(self) -> None:
        first = command_for(
            envelope(
                "telegram",
                transient_payload={
                    "bot_token": "first-transient-token",
                    "raw_bytes": b"first-provider-buffer",
                },
                inline_bytes=b"first-attachment-buffer",
            ),
            CommandSpec(id="same", idempotency_key="same", kind="LEGACY"),
        )
        second = command_for(
            envelope(
                "telegram",
                transient_payload={
                    "bot_token": "second-transient-token",
                    "raw_bytes": b"second-provider-buffer",
                },
                inline_bytes=b"second-attachment-buffer",
            ),
            CommandSpec(id="same", idempotency_key="same", kind="LEGACY"),
        )

        self.assertEqual(command_fingerprint(first), command_fingerprint(second))

    def test_legacy_projection_is_byte_neutral_and_returns_a_copy(self) -> None:
        bodies = (
            ({"ok": True}, CommandStatus.ACCEPTED),
            (
                {"ok": False, "already": True, "action_id": "confirm:one"},
                CommandStatus.CONFLICT,
            ),
            ({"ok": False, "reason": "refused"}, CommandStatus.REJECTED),
            (
                {"sent": False, "reason": "stale run id"},
                CommandStatus.ACCEPTED,
            ),
        )
        for body, expected_status in bodies:
            with self.subTest(body=body):
                result = legacy_result(body)
                self.assertEqual(expected_status, result.status)
                projected = legacy_response(result)
                self.assertEqual(body, projected)
                self.assertEqual(list(body), list(projected))
                self.assertNotIn("status", projected)
                self.assertNotIn("code", projected)
                self.assertNotIn("detail", projected)
                projected["mutated"] = True
                self.assertEqual(body, legacy_response(result))

        with self.assertRaisesRegex(ValueError, "no explicit legacy"):
            legacy_response(CommandResult.accepted("typed-only"))
        with self.assertRaisesRegex(ValueError, "raw bytes"):
            legacy_result({"opaque": b"provider payload"})

    def test_command_copies_only_canonical_envelope_content(self) -> None:
        incoming = envelope("telegram")
        command = command_for(
            incoming,
            CommandSpec(
                id="command-1",
                idempotency_key="telegram:update:1",
                kind="LEGACY",
                expected_version=4,
                payload={"action_id": "reviewed:loop-1"},
            ),
        )

        self.assertEqual("telegram", command.source)
        self.assertEqual(incoming.tenant_id, command.tenant_id)
        self.assertEqual(incoming.actor, command.actor)
        self.assertEqual(incoming.principal, command.principal)
        self.assertEqual(incoming.endpoint_id, command.endpoint_id)
        self.assertEqual(incoming.thread_id, command.thread_id)
        self.assertEqual(4, command.expected_version)
        self.assertEqual(incoming.canonical_content(), command.payload["envelope"])
        self.assertIs(incoming, command.envelope)
        dumped = command.model_dump(mode="python")
        self.assertNotIn("envelope", dumped)
        self.assertFalse(any(
            isinstance(value, (bytes, bytearray, memoryview))
            for value in _leaves(dumped)
        ))


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for child in value.values() for leaf in _leaves(child)]
    if isinstance(value, (list, tuple)):
        return [leaf for child in value for leaf in _leaves(child)]
    return [value]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
