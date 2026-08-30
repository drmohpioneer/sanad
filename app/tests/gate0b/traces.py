"""Trace and snapshot shapes for the Gate 0B replay.

The distinction between an outbound intent, a web event receipt and a provider
delivery is deliberately explicit.  The legacy Fanout marks a channel done
after an adapter returns ``None``; this capture must characterize that behavior
without relabeling the result as delivery.
"""

from __future__ import annotations

import contextvars
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator
from unittest.mock import patch

from .artifacts import canonical


@dataclass
class JourneyTrace:
    beat: str = "setup"
    http: list[dict[str, Any]] = field(default_factory=list)
    outbound: list[dict[str, Any]] = field(default_factory=list)
    delivery: list[dict[str, Any]] = field(default_factory=list)

    def set_beat(self, beat: str) -> None:
        self.beat = beat

    def add_http(
        self,
        *,
        category: str,
        method: str,
        path: str,
        status: int,
        request: Any,
        response: Any,
    ) -> dict[str, Any]:
        row = {
            "sequence": len(self.http) + 1,
            "beat": self.beat,
            "category": category,
            "method": method.upper(),
            "path": path,
            "status": status,
            "request": canonical(request),
            "response": canonical(response),
        }
        self.http.append(row)
        return row

    def add_outbound(self, target: str, message: Any) -> dict[str, Any]:
        row = {
            "sequence": len(self.outbound) + 1,
            "beat": self.beat,
            "target": target,
            "text": str(getattr(message, "text", "")),
            "patient_id": getattr(message, "patient_id", None),
            "has_card": bool(getattr(message, "card", None)),
            "card_title": str((getattr(message, "card", None) or {}).get("title", "")),
            "receipt_key": str(getattr(message, "receipt", "") or ""),
            "outcome": "intent_recorded",
            "web_event_receipt": None,
        }
        self.outbound.append(row)
        return row

    def add_delivery(
        self,
        *,
        outbound_sequence: int | None,
        channel: str,
        target: str,
        outcome: str,
        receipt: str | None,
        detail: str = "",
    ) -> dict[str, Any]:
        row = {
            "sequence": len(self.delivery) + 1,
            "beat": self.beat,
            "outbound_sequence": outbound_sequence,
            "channel": channel,
            "target": target,
            "outcome": outcome,
            "receipt": receipt,
            "detail": detail,
        }
        self.delivery.append(row)
        return row

    def counts(self) -> dict[str, Any]:
        by_category: dict[str, int] = {}
        for row in self.http:
            name = str(row["category"])
            by_category[name] = by_category.get(name, 0) + 1
        by_beat: dict[str, int] = {}
        for row in self.outbound:
            name = str(row["beat"])
            by_beat[name] = by_beat.get(name, 0) + 1
        delivery: dict[str, int] = {}
        for row in self.delivery:
            key = f"{row['channel']}:{row['outcome']}"
            delivery[key] = delivery.get(key, 0) + 1
        return {
            "http_total": len(self.http),
            "http_by_category": by_category,
            "logical_outbound_total": len(self.outbound),
            "logical_outbound_by_beat": by_beat,
            "delivery_by_channel_and_outcome": delivery,
        }

    def cursors(self) -> dict[str, int]:
        return {
            "http": len(self.http),
            "outbound": len(self.outbound),
            "delivery": len(self.delivery),
        }


@contextmanager
def instrument_delivery(trace: JourneyTrace) -> Iterator[None]:
    """Trace the real Fanout and adapters without replacing their behavior."""
    from core import adapters

    current: contextvars.ContextVar[int | None] = contextvars.ContextVar(
        "gate0b_outbound_sequence", default=None
    )
    fanout_send = adapters.Fanout.send
    web_send = adapters.WebAdapter.send
    telegram_send = adapters.TelegramAdapter.send

    async def traced_fanout(self: Any, target: str, message: Any) -> Any:
        outbound = trace.add_outbound(target, message)
        token = current.set(int(outbound["sequence"]))
        try:
            receipt = await fanout_send(self, target, message)
            outbound["web_event_receipt"] = receipt
            return receipt
        finally:
            current.reset(token)

    async def traced_web(self: Any, target: str, message: Any) -> Any:
        sequence = current.get()
        try:
            receipt = await web_send(self, target, message)
        except BaseException as exc:
            trace.add_delivery(
                outbound_sequence=sequence, channel="web", target=target,
                outcome="failed", receipt=None,
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        trace.add_delivery(
            outbound_sequence=sequence, channel="web", target=target,
            outcome="persisted_event" if receipt else "skipped_unresolvable_ref",
            receipt=receipt,
            detail="WebAdapter receipt is a durable event id, not provider delivery",
        )
        return receipt

    async def traced_telegram(self: Any, target: str, message: Any) -> Any:
        sequence = current.get()
        try:
            receipt = await telegram_send(self, target, message)
        except BaseException as exc:
            trace.add_delivery(
                outbound_sequence=sequence, channel="telegram", target=target,
                outcome="failed", receipt=None,
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        # The Gate 0B provider boundary forces Telegram disabled and every actor
        # unbound.  Returning None is therefore a skip, never acceptance.
        trace.add_delivery(
            outbound_sequence=sequence, channel="telegram", target=target,
            outcome="skipped_disabled_unbound", receipt=receipt,
            detail=(
                "No provider endpoint exists. Legacy Fanout may still mark the "
                "channel receipt done after this None return."
            ),
        )
        return receipt

    with ExitStack() as stack:
        stack.enter_context(patch.object(adapters.Fanout, "send", traced_fanout))
        stack.enter_context(patch.object(adapters.WebAdapter, "send", traced_web))
        stack.enter_context(
            patch.object(adapters.TelegramAdapter, "send", traced_telegram)
        )
        yield
