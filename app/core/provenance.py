"""Fail-closed provenance helpers for synthetic actors, missions, and evidence.

Top-level persisted models use Pydantic ``StrictBool`` fields. Evidence is
still represented by small embedded dictionaries in the legacy schema, so it
needs one equally strict rule at that boundary: only the literal boolean
``False`` means non-synthetic. A missing, null, string, integer, or otherwise
malformed value is treated as synthetic when an old record is hydrated.

Derived records are non-synthetic only when every origin is explicitly false.
That keeps a competition fixture synthetic even if somebody sends it through a
provider-authenticated channel, and keeps web-simulated input synthetic even
when the actor itself is not.
"""

from __future__ import annotations

from typing import Any


_MISSING = object()


def derived(*origins: object) -> bool:
    """Return False only when every supplied origin is the literal False."""
    if not origins:
        return True
    return any(origin is not False for origin in origins)


def evidence(row: Any, *, synthetic: object = _MISSING) -> dict[str, Any]:
    """Copy one embedded evidence row and give it a literal provenance flag.

    New writers pass ``synthetic=`` explicitly. Legacy hydration omits that
    argument, preserving an existing literal boolean and conservatively
    replacing every missing or malformed value with ``True``.
    """
    if not isinstance(row, dict):
        raise ValueError("evidence row must be a dictionary")
    out = dict(row)
    if synthetic is _MISSING:
        held = out.get("synthetic", _MISSING)
        out["synthetic"] = held if type(held) is bool else True
        return out
    if type(synthetic) is not bool:
        raise TypeError("evidence synthetic provenance must be a literal boolean")
    out["synthetic"] = synthetic
    return out


def evidence_rows(rows: Any) -> list[dict[str, Any]]:
    """Normalize legacy evidence collections without mutating their input."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError("evidence rows must be a list or tuple")
    return [evidence(row) for row in rows]
