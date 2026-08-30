"""Provider-neutral composition for the additive Gate 2 ingress seams.

This module owns no provider, database, route, or domain specialist.  A later
composition root supplies adapters and one already-configured ``CommandBus``.
The runtime freezes those adapters into the real registry, asks exactly one of
them for an ``InboundEnvelope``, builds the canonical ``Command``, and executes
that one injected bus.

Raw provider objects and inline attachment bytes travel only on excluded,
repr-hidden fields of the live envelope attached to the command.  The command
payload receives only ``InboundEnvelope.canonical_content()``; this module
rejects any remaining raw bytes or obvious transport-secret fields before the
durable replay boundary.  After the replay claim, the handler can still use the
same live envelope to invoke the legacy path.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from .adapter_registry import AdapterRegistry, ChannelAdapter
from .channel_contracts import (
    Command,
    CommandResult,
    CommandStatus,
    DeliveryReceipt,
    InboundEnvelope,
    OutboundIntent,
)
from .command_bus import CommandBus


GATE2_INGRESS_PROVIDERS = frozenset({"web", "telegram", "cloud_tasks"})
LEGACY_BODY = "legacy_body"

Normalizer = Callable[[object, Any], Awaitable[InboundEnvelope]]
Deliverer = Callable[[OutboundIntent, Any], Awaitable[DeliveryReceipt]]


def _provider_name(provider: str) -> str:
    name = str(provider or "").strip().lower()
    if not name:
        raise ValueError("an adapter needs a provider name")
    return name


def _secret_key(key: object) -> bool:
    clean = str(key).strip().lower().replace("-", "_")
    if clean == "raw_payload_ref":
        return False
    exact = {
        "authorization",
        "bot_token",
        "chat_id",
        "doctor_token",
        "image_bytes",
        "audio_bytes",
        "password",
        "raw_bytes",
        "secret",
        "telegram_chat_id",
        "token",
        "web_token",
    }
    return clean in exact or clean.endswith(
        ("_authorization", "_password", "_secret", "_token", "_chat_id", "_bytes")
    )


def _assert_command_safe(value: Any, path: str = "command") -> None:
    """Reject material that must not cross the durable command boundary."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"raw bytes are forbidden at {path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _secret_key(key):
                raise ValueError(f"transport secret field is forbidden at {path}.{key}")
            _assert_command_safe(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            _assert_command_safe(child, f"{path}[{index}]")


class InjectedChannelAdapter:
    """A real registry adapter whose provider effects arrive by injection."""

    def __init__(
        self,
        provider: str,
        normalize: Normalizer,
        *,
        deliver: Optional[Deliverer] = None,
    ) -> None:
        self.provider = _provider_name(provider)
        self._normalize = normalize
        self._deliver = deliver

    async def normalize(self, raw: object, context: Any) -> InboundEnvelope:
        envelope = await self._normalize(raw, context)
        if not isinstance(envelope, InboundEnvelope):
            raise TypeError("adapter normalizers must return InboundEnvelope")
        if _provider_name(envelope.provider) != self.provider:
            raise ValueError(
                f"adapter {self.provider} returned envelope for {envelope.provider}"
            )
        return envelope

    async def deliver(
        self, intent: OutboundIntent, endpoint: Any
    ) -> DeliveryReceipt:
        if self._deliver is None:
            raise LookupError(
                f"adapter {self.provider} has no outbound delivery binding"
            )
        receipt = await self._deliver(intent, endpoint)
        if not isinstance(receipt, DeliveryReceipt):
            raise TypeError("adapter deliverers must return DeliveryReceipt")
        if _provider_name(receipt.provider) != self.provider:
            raise ValueError(
                f"adapter {self.provider} returned receipt for {receipt.provider}"
            )
        return receipt


@dataclass(frozen=True)
class CommandSpec:
    """Route-owned command facts that cannot be inferred from an envelope."""

    id: str
    idempotency_key: str
    kind: str
    expected_version: Optional[int] = None
    payload: Mapping[str, Any] = field(default_factory=dict)


def command_for(envelope: InboundEnvelope, spec: CommandSpec) -> Command:
    """Build the provider-neutral command passed to authorization and replay."""
    if "envelope" in spec.payload:
        raise ValueError("command payload may not replace the canonical envelope")
    payload = copy.deepcopy(dict(spec.payload))
    payload["envelope"] = envelope.canonical_content()
    command = Command(
        id=spec.id,
        idempotency_key=spec.idempotency_key,
        kind=spec.kind,
        tenant_id=envelope.tenant_id,
        actor=envelope.actor,
        principal=envelope.principal,
        source=_provider_name(envelope.provider),
        endpoint_id=envelope.endpoint_id,
        thread_id=envelope.thread_id,
        expected_version=spec.expected_version,
        payload=payload,
        created_at=envelope.received_at,
        synthetic=envelope.synthetic,
        envelope=envelope,
    )
    _assert_command_safe(command.model_dump(mode="python"))
    return command


def legacy_result(
    body: Mapping[str, Any],
    *,
    code: str = "legacy_result",
    durable_body: Optional[Mapping[str, Any]] = None,
) -> CommandResult:
    """Wrap an existing route body without changing a key or value in it.

    The HTTP projection stays byte-neutral, but an explicit legacy
    ``ok:false`` is never mislabeled as a successful typed command. An
    already-completed action is a conflict; other explicit refusals are
    rejected. Decision bodies such as ``sent:false`` remain accepted because
    the task itself ran and deliberately chose not to send.
    """
    copied = copy.deepcopy(dict(body))
    durable = (
        copy.deepcopy(dict(durable_body))
        if durable_body is not None
        else copy.deepcopy(copied)
    )
    _assert_command_safe({LEGACY_BODY: copied}, path="transient_result")
    _assert_command_safe({LEGACY_BODY: durable}, path="result")
    status = CommandStatus.ACCEPTED
    if copied.get("ok") is False:
        status = (
            CommandStatus.CONFLICT
            if copied.get("already") is True
            else CommandStatus.REJECTED
        )
    return CommandResult(
        status=status,
        code=code,
        value={LEGACY_BODY: durable},
        transient_legacy_body=(copied if durable_body is not None else None),
    )


def legacy_response(result: CommandResult) -> dict[str, Any]:
    """Return the explicit legacy route body, never ``CommandResult.as_http``.

    Requiring a dedicated body makes parity auditable: typed status fields do
    not silently appear in a Gate 0 route, and a legacy ``{"ok": false}`` body
    remains exactly that until its separately tracked Gate 1 UI slice changes.
    """
    body = result.transient_legacy_body
    if body is None:
        body = result.value.get(LEGACY_BODY)
    if not isinstance(body, Mapping):
        raise ValueError("command result has no explicit legacy route body")
    return copy.deepcopy(dict(body))


@dataclass(frozen=True)
class TransportOutcome:
    envelope: InboundEnvelope
    command: Command
    result: CommandResult

    def legacy_response(self) -> dict[str, Any]:
        return legacy_response(self.result)


class TransportRuntime:
    """One frozen adapter registry feeding one injected command bus."""

    def __init__(
        self,
        *,
        bus: CommandBus,
        adapters: Iterable[ChannelAdapter],
        required_providers: Iterable[str] = GATE2_INGRESS_PROVIDERS,
    ) -> None:
        registry = AdapterRegistry()
        for adapter in adapters:
            registry.register(adapter)
        required = {_provider_name(provider) for provider in required_providers}
        missing = required - set(registry.providers)
        if missing:
            raise ValueError(
                "transport runtime is missing ingress adapters: "
                + ", ".join(sorted(missing))
            )
        registry.freeze()
        self._registry = registry
        self._bus = bus

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    @property
    def bus(self) -> CommandBus:
        return self._bus

    async def execute(
        self,
        provider: str,
        raw: object,
        context: Any,
        spec: CommandSpec,
    ) -> TransportOutcome:
        adapter = self._registry.get(provider)
        envelope = await adapter.normalize(raw, context)
        command = command_for(envelope, spec)
        result = await self._bus.execute(command)
        return TransportOutcome(envelope=envelope, command=command, result=result)
