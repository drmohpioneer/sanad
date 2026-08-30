"""A provider-neutral registry populated only by the composition root."""

from __future__ import annotations

from typing import Any, Protocol

from .channel_contracts import DeliveryReceipt, InboundEnvelope, OutboundIntent


class ChannelAdapter(Protocol):
    provider: str

    async def normalize(self, raw: object, context: Any) -> InboundEnvelope: ...

    async def deliver(self, intent: OutboundIntent, endpoint: Any) -> DeliveryReceipt: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        self._frozen = False

    @staticmethod
    def _provider(adapter: ChannelAdapter) -> str:
        provider = str(getattr(adapter, "provider", "") or "").strip().lower()
        if not provider:
            raise ValueError("an adapter needs a provider name")
        return provider

    def register(self, adapter: ChannelAdapter) -> None:
        if self._frozen:
            raise RuntimeError("the adapter registry is frozen")
        provider = self._provider(adapter)
        if provider in self._adapters:
            raise ValueError(f"adapter already registered: {provider}")
        self._adapters[provider] = adapter

    def freeze(self) -> None:
        self._frozen = True

    def get(self, provider: str) -> ChannelAdapter:
        name = (provider or "").strip().lower()
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise LookupError(f"unknown channel provider: {name or '<blank>'}") from exc

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
