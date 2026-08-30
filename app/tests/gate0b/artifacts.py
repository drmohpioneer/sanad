"""Canonical artifact helpers for the Gate 0B legacy characterization.

This module is test support.  It never imports or changes product state.  The
runner gives it already-captured responses and ledgers; it makes those values
stable on disk and verifies the hashes recorded in the manifest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
GOLDENS = ROOT / "goldens"


def canonical(value: Any) -> Any:
    """Return a JSON-safe value without discarding ids or timestamps."""
    if hasattr(value, "model_dump"):
        return canonical(value.model_dump())
    if is_dataclass(value):
        return canonical(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(canonical(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"Gate 0B cannot serialize {type(value).__name__}")


def dumps(value: Any) -> str:
    return json.dumps(
        canonical(value), ensure_ascii=False, indent=2, sort_keys=True,
        separators=(",", ": "),
    ) + "\n"


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def digest(value: Any) -> str:
    return digest_bytes(dumps(value).encode("utf-8"))


def write_json(path: Path, value: Any) -> str:
    """Write one canonical JSON artifact and return its SHA-256."""
    raw = dumps(value).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return digest_bytes(raw)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def tree_hashes(root: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    skipped = exclude or set()
    return {
        path.relative_to(root).as_posix(): file_digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in skipped
    }


def verify_hashes(root: Path, expected: dict[str, str]) -> list[str]:
    """Return human-readable mismatches; an empty list is acceptance."""
    found = tree_hashes(root, exclude={"manifest.json"})
    problems: list[str] = []
    for name in sorted(set(found) | set(expected)):
        if name not in found:
            problems.append(f"missing artifact: {name}")
        elif name not in expected:
            problems.append(f"unmanifested artifact: {name}")
        elif found[name] != expected[name]:
            problems.append(
                f"hash mismatch: {name}: expected {expected[name]}, got {found[name]}"
            )
    return problems
