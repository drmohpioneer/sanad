"""Immutable rollout switches for the additive S23 transport seams.

These switches are deliberately process environment, not mutable demo settings.
An invalid value refuses to choose a runtime instead of accidentally enabling a
new sender.
"""

from __future__ import annotations

import os
from typing import Literal, cast


DEFAULT_SHADOW_TIMEOUT_MS = 250
DEFAULT_SHADOW_MAX_IN_FLIGHT = 32


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise RuntimeError(f"{name} must be exactly true or false")


def legacy_runtime() -> bool:
    """True until a revision explicitly opts into the replacement runtime."""
    return _boolean("LEGACY_RUNTIME", True)


def outbox_mode() -> Literal["off", "shadow"]:
    """Gate 2 supports observation only; no outbox worker is active."""
    raw = os.environ.get("OUTBOX_MODE")
    value = "off" if raw is None else raw.strip().lower()
    if value not in {"off", "shadow"}:
        raise RuntimeError("OUTBOX_MODE must be exactly off or shadow")
    return cast(Literal["off", "shadow"], value)


def _bounded_integer(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer from {low} to {high}") from exc
    if value < low or value > high:
        raise RuntimeError(f"{name} must be an integer from {low} to {high}")
    return value


def shadow_timeout_seconds() -> float:
    milliseconds = _bounded_integer(
        "OUTBOX_SHADOW_TIMEOUT_MS", DEFAULT_SHADOW_TIMEOUT_MS, 1, 1000
    )
    return milliseconds / 1000


def shadow_max_in_flight() -> int:
    return _bounded_integer(
        "OUTBOX_SHADOW_MAX_IN_FLIGHT", DEFAULT_SHADOW_MAX_IN_FLIGHT, 1, 128
    )


def validate_gate2() -> None:
    """Reject a rollout configuration before the service can mutate state."""
    if not legacy_runtime():
        raise RuntimeError(
            "LEGACY_RUNTIME=false is unavailable until the replacement runtime is active"
        )
    outbox_mode()
    shadow_timeout_seconds()
    shadow_max_in_flight()
