"""Build the Gate 3 doctor workspace from one immutable record bundle.

This module is intentionally a pure projection.  It imports no route and reads
no store: all legacy shadows, canonical queues, counters, patient summaries,
and event pages are derived from the same ``WorkspaceRecords`` value.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel

from . import (
    board,
    cards,
    contract,
    links,
    monitoring,
    policy,
    settings as runtime_settings,
    summary,
    timing,
    views,
)
from .models import Event, LinkToken, Loop, Patient, Relay
from .workspace_records import WorkspaceRecords


SCHEMA_VERSION = "2.0"
DEFAULT_PATIENT_LIMIT = 50
MAX_PATIENT_LIMIT = 100
DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 1000


class InvalidCursor(ValueError):
    """The supplied event cursor is malformed or belongs to another doctor."""


class UnknownPatient(ValueError):
    """A selected patient is not part of this doctor's atomic workspace."""


class InvalidWorkspace(ValueError):
    """The atomic bundle crossed a tenant boundary and cannot be projected.

    ``failures`` carries kind/count pairs and nothing else.  The route turns
    them into a degraded 503 body and one server log line, so this value has
    to stay free of record ids: link ids are bearer credentials, and which
    storage invariant failed is the whole diagnosis anyway.
    """

    def __init__(
        self,
        message: str,
        failures: Optional[Mapping[str, int]] = None,
    ) -> None:
        super().__init__(message)
        self.failures: dict[str, int] = {
            str(kind): int(count)
            for kind, count in sorted((failures or {}).items())
        }


def _jsonable(value: Any) -> Any:
    """Detached, deterministic JSON data without ever retaining raw bytes."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, bytes):
        # No workspace record currently carries inline bytes.  This guard keeps
        # a future additive field from putting them on a doctor-facing wire.
        return {
            "redacted_bytes_sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    return value


# Exact key names, matched case-insensitively and never as substrings.  A
# substring test silently deleted clinical fields whose names merely contain a
# credential word: ``{"secretions": "bloody"}`` lost the finding because
# "secret" is inside "secretions", and the doctor read a record with a fact
# missing and no marker saying so.  A credential that appears as a value, or
# inside arbitrary text, is caught by the separate value pass in
# ``_scrub_sensitive_values``; this set only removes fields whose *name* is a
# credential field name.
_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "chat_id",
        "credential",
        "password",
        "secret",
        "secret_key",
        "telegram_chat_id",
        "token",
        "web_token",
    }
)


@dataclass(frozen=True)
class _SensitiveValues:
    credentials: tuple[str, ...] = ()
    chat_ids: tuple[str, ...] = ()


_EMPTY_SENSITIVE_VALUES = _SensitiveValues()
_REDACTED_CREDENTIAL = "[REDACTED_CREDENTIAL]"
_REDACTED_CHAT_ID = "[REDACTED_CHAT_ID]"
_MINTED_TOKEN = re.compile(r"[0-9a-f]{32}")
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_]+")


def _scrub_sensitive_values(
    value: Any,
    sensitive: _SensitiveValues,
    *,
    scrub_keys: bool = True,
) -> Any:
    """Remove known bearer and private channel identifiers from record data."""
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = (
                _scrub_sensitive_values(key, sensitive)
                if scrub_keys
                else key
            )
            if safe_key in cleaned and safe_key != key:
                raise InvalidWorkspace(
                    "private-value redaction key collision",
                    {"redaction_key_collision": 1},
                )
            cleaned[safe_key] = _scrub_sensitive_values(
                item, sensitive, scrub_keys=scrub_keys
            )
        return cleaned
    if isinstance(value, list):
        return [
            _scrub_sensitive_values(item, sensitive, scrub_keys=scrub_keys)
            for item in value
        ]
    if isinstance(value, int) and not isinstance(value, bool):
        if str(value) in sensitive.chat_ids:
            return _REDACTED_CHAT_ID
        return value
    if isinstance(value, float):
        # Firestore distinguishes Integer and Double, and arbitrary metadata
        # may carry an exact chat identifier in either representation.  Only
        # finite integral doubles can be the same numeric identifier; ordinary
        # clinical decimal measurements remain untouched.
        if (
            math.isfinite(value)
            and value.is_integer()
            and str(int(value)) in sensitive.chat_ids
        ):
            return _REDACTED_CHAT_ID
        return value
    if isinstance(value, str):
        scrubbed = value
        for credential in sensitive.credentials:
            scrubbed = scrubbed.replace(credential, _REDACTED_CREDENTIAL)
        for chat_id in sensitive.chat_ids:
            scrubbed = re.sub(
                rf"(?<!\d){re.escape(chat_id)}(?!\d)",
                _REDACTED_CHAT_ID,
                scrubbed,
            )
        return scrubbed
    return value


def _credential_values(records: WorkspaceRecords) -> tuple[str, ...]:
    values = {
        str(value)
        for value in (
            records.doctor.web_token,
            *(token.id for token in records.link_tokens),
        )
        if str(value)
    }
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def _sensitive_values(records: WorkspaceRecords) -> _SensitiveValues:
    chat_ids: set[str] = set()
    if records.doctor.telegram_chat_id is not None:
        chat_ids.add(str(records.doctor.telegram_chat_id))
    for patient in records.patients:
        channels = patient.channels or {}
        if not isinstance(channels, Mapping):
            continue
        chat_id = channels.get("telegram_chat_id")
        if chat_id is not None and not isinstance(chat_id, bool):
            chat_ids.add(str(chat_id))
    return _SensitiveValues(
        credentials=_credential_values(records),
        chat_ids=tuple(sorted(chat_ids, key=lambda value: (-len(value), value))),
    )


def _without_private_values(
    value: Any,
    sensitive: _SensitiveValues = _EMPTY_SENSITIVE_VALUES,
) -> Any:
    """Remove private identifiers from record data and snapshot identity.

    The snapshot id is a version of clinical/read-side content.  Rotating a
    console or patient bearer credential, or binding a private Telegram ID,
    must neither expose it through a hash oracle nor manufacture a new clinical
    version.
    """
    value = _jsonable(value)
    if isinstance(value, dict):
        cleaned = {
            key: _without_private_values(item, sensitive)
            for key, item in value.items()
            if key.lower() not in _CREDENTIAL_KEYS
        }
        return _scrub_sensitive_values(cleaned, sensitive)
    if isinstance(value, list):
        cleaned = [
            _without_private_values(item, sensitive) for item in value
        ]
        return _scrub_sensitive_values(cleaned, sensitive)
    return _scrub_sensitive_values(value, sensitive)


def _wire_value(
    value: Any,
    sensitive: _SensitiveValues = _EMPTY_SENSITIVE_VALUES,
) -> Any:
    """Sanitize arbitrary record data, including its data-owned mapping keys."""
    return _without_private_values(value, sensitive)


def _wire_projection(
    value: Any,
    sensitive: _SensitiveValues = _EMPTY_SENSITIVE_VALUES,
) -> Any:
    """Sanitize values inside a projection whose mapping keys are server-owned."""
    return _scrub_sensitive_values(
        _jsonable(value),
        sensitive,
        scrub_keys=False,
    )


def _iso_key(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def _record_key(row: Any) -> tuple[str, str]:
    stamp = (
        getattr(row, "created_at", None)
        or getattr(row, "ts", None)
        or getattr(row, "updated_at", None)
    )
    return (_iso_key(stamp), str(getattr(row, "id", "")))


def _sorted_records(rows: Iterable[Any]) -> list[Any]:
    return sorted(rows, key=_record_key)


def _version_source(records: WorkspaceRecords) -> dict[str, Any]:
    """Order-independent, credential-free source identity.

    ``as_of`` and runtime health are intentionally absent.  Both can change
    between reads without a persisted workspace record changing.
    """

    sensitive = _sensitive_values(records)

    def dumped(rows: Iterable[Any]) -> list[Any]:
        return [
            _without_private_values(
                row.model_dump(mode="json"), sensitive
            )
            for row in sorted(rows, key=lambda item: str(getattr(item, "id", "")))
        ]

    doctor_source = records.doctor.model_dump(mode="json")
    doctor_source["telegram_bound"] = records.doctor.telegram_chat_id is not None

    patient_sources: list[dict[str, Any]] = []
    for patient in sorted(records.patients, key=lambda row: row.id):
        source = patient.model_dump(mode="json")
        channels = dict(source.get("channels") or {})
        channels["telegram_bound"] = channels.get("telegram_chat_id") is not None
        channels.pop("telegram_chat_id", None)
        source["channels"] = channels
        patient_sources.append(_without_private_values(source, sensitive))

    # Link ids are patient bearer credentials rather than record identity.
    # Sort the scrubbed facts themselves so rotating those credentials cannot
    # influence the version indirectly through input ordering either.
    scrubbed_links = [
        _without_private_values(
            token.model_dump(mode="json", exclude={"id"}), sensitive
        )
        for token in records.link_tokens
    ]
    scrubbed_links.sort(
        key=lambda row: json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )

    return {
        "doctor": _without_private_values(doctor_source, sensitive),
        "patients": patient_sources,
        "loops": dumped(records.loops),
        "events": dumped(records.events),
        "reports": dumped(records.reports),
        "link_tokens": scrubbed_links,
        "open_relays": dumped(records.open_relays),
        "settings": _without_private_values(records.settings, sensitive),
    }


def _snapshot_id(records: WorkspaceRecords) -> str:
    encoded = json.dumps(
        _version_source(records),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "ws2:" + hashlib.sha256(encoded).hexdigest()


def _assert_scoped(records: WorkspaceRecords) -> None:
    doctor_id = records.doctor.id
    patient_ids = {patient.id for patient in records.patients}
    loops_by_id = {loop.id: loop for loop in records.loops}
    events_by_id = {event.id: event for event in records.events}
    relays_by_id = {relay.id: relay for relay in records.open_relays}
    credential_values = _credential_values(records)
    private_chat_ids = _sensitive_values(records).chat_ids
    failures: dict[str, int] = {}

    def reject(kind: str) -> None:
        failures[kind] = failures.get(kind, 0) + 1

    if len(patient_ids) != len(records.patients):
        reject("duplicate_patient")
    if len(loops_by_id) != len(records.loops):
        reject("duplicate_loop")
    if any(len(value) < 8 for value in credential_values):
        reject("weak_credential")
    redaction_markers = (_REDACTED_CREDENTIAL, _REDACTED_CHAT_ID)
    marker_alias = any(
        records.doctor.web_token in marker
        or marker in records.doctor.web_token
        for marker in redaction_markers
    )
    if (
        marker_alias
        or (
            not _MINTED_TOKEN.fullmatch(records.doctor.web_token)
            and _PLAIN_IDENTIFIER.fullmatch(records.doctor.web_token)
        )
    ):
        # Production console credentials are 32 lowercase hex characters.
        # Legacy/test tokens with punctuation remain readable, but a public
        # schema word such as ``snapshot_id`` must never be accepted as a
        # bearer: it cannot be both a fixed response key and a secret.
        reject("guessable_doctor_credential")
    if any(not _MINTED_TOKEN.fullmatch(token.id) for token in records.link_tokens):
        # Every patient link has always been minted by ``new_id`` or
        # ``derived_id``, both of which produce this exact opaque form.  Failing
        # closed here prevents a human-readable credential from aliasing a
        # clinical/schema key and changing the projection while it is scrubbed.
        reject("unminted_link_credential")

    structural_ids = {
        records.doctor.id,
        *patient_ids,
        *loops_by_id,
        *(event.id for event in records.events),
        *(report.id for report in records.reports),
        *(relay.id for relay in records.open_relays),
    }
    # These prefixed identities are real foreign keys on the canonical wire,
    # even though they are synthesized rather than stored.  A legacy bearer
    # such as ``patient:p1`` must not be allowed to alias one: provenance-local
    # redaction would otherwise change a patient row reference while the rows
    # mapping retained its server-owned key.
    structural_ids.update(f"patient:{ident}" for ident in patient_ids)
    structural_ids.update(f"loop:{ident}" for ident in loops_by_id)
    structural_ids.update(f"event:{ident}" for ident in events_by_id)
    structural_ids.update(f"relay:{ident}" for ident in relays_by_id)
    structural_ids.update(f"seen:{ident}" for ident in events_by_id)
    if any(
        credential in ident
        for ident in structural_ids
        for credential in credential_values
    ):
        reject("credential_id_alias")
    if any(
        chat_id in ident
        for ident in structural_ids
        for chat_id in private_chat_ids
    ):
        reject("private_chat_id_alias")

    for patient in records.patients:
        if patient.doctor_id != doctor_id:
            reject("patient_scope")
    for loop in records.loops:
        if loop.doctor_id != doctor_id:
            reject("loop_scope")
        if loop.patient_id not in patient_ids:
            reject("loop_patient")

    if records.read_at is not None:
        read_stamp = _nanos(records.read_at)
        for event in records.events:
            if event.persisted_at is None:
                reject("event_persistence_time")
            elif _nanos(event.persisted_at) > read_stamp:
                reject("event_after_read_boundary")

    for event in records.events:
        if event.doctor_id != doctor_id:
            reject("event_scope")
        if event.patient_id is not None and event.patient_id not in patient_ids:
            reject("event_patient")
        if event.loop_id is not None:
            owner = loops_by_id.get(event.loop_id)
            if owner is None:
                reject("event_loop")
            elif event.patient_id is not None and owner.patient_id != event.patient_id:
                reject("event_loop_patient")
        raw_card = cards.card_of(event)
        if event.kind == "card" and not raw_card:
            # WebAdapter writes kind="card" iff it persisted a real, non-empty
            # card mapping. Treating a corrupted card payload as a plain event
            # would silently remove a doctor obligation from every queue.
            reject("card_payload_shape")
        resolved_value = raw_card.get("resolved") if raw_card else None
        if raw_card and "resolved" in raw_card and not isinstance(
            resolved_value, bool
        ):
            reject("card_resolution_shape")
        strictly_resolved = resolved_value is True
        raw_actions = raw_card.get("actions", []) if raw_card else []
        if not isinstance(raw_actions, list) or any(
            not isinstance(action, dict) for action in raw_actions
        ):
            # ``is_open`` intentionally ignores malformed non-object actions.
            # That compatibility behavior cannot be allowed to make an
            # unresolved doctor obligation disappear from every canonical
            # queue. Explicitly resolved history is non-executable and may
            # retain its older shape.
            if raw_card and not strictly_resolved:
                reject("card_action_shape")
            raw_actions = []
        if (
            _notification_class(event) in {"DANGER", "URGENT_SLA"}
            and not strictly_resolved
            and not cards.is_open(event)
        ):
            # The typed fact is authoritative, but an unresolved typed alert
            # also has to be executable in the frozen card protocol.  Silently
            # dropping a DANGER/URGENT_SLA fact because its legacy presentation
            # was malformed would make every canonical queue under-count it.
            reject("typed_notification_actionability")
        for action in raw_actions:
            action_id = str(action.get("id") or "")
            if any(value in action_id for value in credential_values):
                reject("credential_action_alias")
            if any(value in action_id for value in private_chat_ids):
                reject("private_chat_action_alias")
            # Closed card actions are immutable history, not executable rows.
            # Their relay or proposal target may have been consumed normally.
            if not cards.is_open(event):
                continue
            verb, separator, ident = action_id.partition(":")
            if not separator or not verb or not ident:
                reject("action_reference_shape")
                continue
            if verb in {"reviewed", "note"}:
                target = loops_by_id.get(ident)
                if (
                    ":" in ident
                    or target is None
                    or event.patient_id is None
                    or target.patient_id != event.patient_id
                    or (
                        event.loop_id is not None
                        and event.loop_id != target.id
                    )
                ):
                    reject("action_loop_reference")
            elif verb == "reply":
                target = relays_by_id.get(ident)
                if ":" in ident or event.patient_id is None:
                    reject("action_relay_reference")
                elif target is None:
                    # Reachable, benign history rather than a broken bundle.
                    # Retiring a card is bookkeeping about work that already
                    # happened (app/main.py: the answer is sent, then
                    # ``cards.resolve`` runs outside the claim), and the
                    # Telegram "Answer" flow closes the same relay without
                    # touching the console card at all.  Either way the relay
                    # leaves ``open_relays`` while the card is still flagged
                    # open.  ``_reconciled_cards`` below projects this card as
                    # consumed history; rejecting here would destroy an entire
                    # workspace over one finished question.
                    pass
                elif (
                    target.patient_id != event.patient_id
                    or (
                        event.loop_id is not None
                        and event.loop_id != target.loop_id
                    )
                ):
                    # A target that exists but belongs to another patient or
                    # loop is a real cross-boundary reference, not a consumed
                    # one.  That still fails closed.
                    reject("action_relay_reference")
            elif verb in {"attach", "openloop", "seen"}:
                target = events_by_id.get(ident)
                if (
                    ":" in ident
                    or target is None
                    or event.patient_id is None
                    or target.patient_id != event.patient_id
                ):
                    reject("action_event_reference")
            elif verb == "openpatient":
                if ":" in ident or ident not in patient_ids:
                    reject("action_patient_reference")
            elif verb == "existing":
                patient_id, inner_separator, confirm_id = ident.partition(":")
                if (
                    not inner_separator
                    or ":" in confirm_id
                    or patient_id not in patient_ids
                    or not confirm_id
                ):
                    reject("action_existing_reference")
            elif verb in {"confirm", "cancel", "newpatient"}:
                if ":" in ident:
                    reject("action_confirmation_reference")
            else:
                reject("action_verb")

    for report in records.reports:
        if report.doctor_id != doctor_id:
            reject("report_scope")
        if report.patient_id is not None and report.patient_id not in patient_ids:
            reject("report_patient")

    for token in records.link_tokens:
        if token.doctor_id != doctor_id:
            reject("link_scope")
        if token.patient_id not in patient_ids:
            reject("link_patient")

    for relay in records.open_relays:
        if relay.doctor_id != doctor_id:
            reject("relay_scope")
        if relay.patient_id not in patient_ids:
            reject("relay_patient")
        if relay.state != "open":
            reject("relay_state")
        if relay.loop_id is not None:
            owner = loops_by_id.get(relay.loop_id)
            if owner is None:
                reject("relay_loop")
            elif owner.patient_id != relay.patient_id:
                reject("relay_loop_patient")

    for kind, rows in (
        ("event", records.events),
        ("report", records.reports),
        ("link", records.link_tokens),
        ("relay", records.open_relays),
    ):
        if len({row.id for row in rows}) != len(rows):
            reject(f"duplicate_{kind}")

    if failures:
        # Do not put record ids in an exception that may reach structured logs:
        # link ids are bearer credentials, and the kind/count is enough to
        # diagnose which storage invariant failed.
        detail = ", ".join(
            f"{kind}={count}" for kind, count in sorted(failures.items())
        )
        raise InvalidWorkspace(
            "workspace tenant/reference validation failed: " + detail,
            failures,
        )


# ---------------------------------------------------------------------------
# Composite event cursor
# ---------------------------------------------------------------------------
_CURSOR_PREFIX = "ws2e"
_CURSOR_FLOOR = -(1 << 127)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _CursorState:
    lower: int
    upper: int
    position_stamp: int
    position_id: str
    open_batch: bool


def _doctor_binding(doctor_id: str) -> str:
    return hashlib.sha256(f"sanad:workspace:{doctor_id}".encode()).hexdigest()[:24]


def _nanos(value: datetime) -> int:
    fraction = int(getattr(value, "nanosecond", value.microsecond * 1000))
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    utc = aware.astimezone(timezone.utc)
    whole_second = datetime(
        utc.year,
        utc.month,
        utc.day,
        utc.hour,
        utc.minute,
        utc.second,
        tzinfo=timezone.utc,
    )
    delta = whole_second - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + fraction


def _event_key(event: Event) -> tuple[int, str]:
    return (_nanos(event.persisted_at or event.ts), event.id)


def _encode_cursor(doctor_id: str, state: _CursorState) -> str:
    payload = json.dumps(
        {
            "a": state.lower,
            "b": state.upper,
            "d": _doctor_binding(doctor_id),
            "i": state.position_id,
            "o": state.open_batch,
            "t": state.position_stamp,
            "v": 4,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{_CURSOR_PREFIX}.{body}"


def _decode_cursor(cursor: str, doctor_id: str) -> _CursorState:
    try:
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise ValueError
        prefix, body = cursor.split(".", 1)
        if prefix != _CURSOR_PREFIX or not body:
            raise ValueError
        padded = body + "=" * (-len(body) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if set(payload) != {"a", "b", "d", "i", "o", "t", "v"}:
            raise ValueError
        if payload["v"] != 4:
            raise ValueError
        if payload["d"] != _doctor_binding(doctor_id):
            raise InvalidCursor("event cursor belongs to another doctor")
        lower = payload["a"]
        upper = payload["b"]
        stamp = payload["t"]
        event_id = payload["i"]
        open_batch = payload["o"]
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (lower, upper, stamp)
        ):
            raise ValueError
        if not isinstance(event_id, str) or len(event_id) > 1024:
            raise ValueError
        if not isinstance(open_batch, bool) or upper < lower:
            raise ValueError
        if open_batch:
            if not event_id or not lower < stamp <= upper:
                raise ValueError
        elif event_id or lower != upper or stamp != upper:
            raise ValueError
        return _CursorState(lower, upper, stamp, event_id, open_batch)
    except InvalidCursor:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise InvalidCursor("invalid event cursor") from None


# ---------------------------------------------------------------------------
# Legacy projection, built from the same records as the v2 queues
# ---------------------------------------------------------------------------
def _loops_by_patient(records: WorkspaceRecords) -> dict[str, list[Loop]]:
    grouped: dict[str, list[Loop]] = {patient.id: [] for patient in records.patients}
    for row in _sorted_records(records.loops):
        grouped.setdefault(row.patient_id, []).append(row)
    return grouped


def _tokens_by_patient(
    tokens: Sequence[LinkToken],
) -> dict[str, LinkToken]:
    newest: dict[str, LinkToken] = {}
    for token in _sorted_records(tokens):
        prior = newest.get(token.patient_id)
        if prior is None or (token.created_at, token.id) > (prior.created_at, prior.id):
            newest[token.patient_id] = token
    return newest


def _legacy_loop(
    loop: Loop,
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    return _wire_projection(
        {
            "id": loop.id,
            "synthetic": loop.synthetic,
            "type": loop.type,
            "title": loop.title,
            "state": loop.state,
            "details": _wire_value(loop.details, sensitive),
            "due_at": loop.due_at.isoformat() if loop.due_at else None,
        },
        sensitive,
    )


def _legacy_board(
    records: WorkspaceRecords,
    patients: Sequence[Patient],
    loops_by_patient: Mapping[str, Sequence[Loop]],
    tokens_by_patient: Mapping[str, LinkToken],
    ordered_events: Sequence[Event],
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    patient_rows: list[dict[str, Any]] = []
    for patient in patients:
        loops = list(loops_by_patient.get(patient.id, ()))
        token = tokens_by_patient.get(patient.id)
        patient_rows.append(
            {
                "id": patient.id,
                "synthetic": patient.synthetic,
                "name": patient.name,
                "diagnosis": patient.diagnosis,
                "status": patient.status,
                "plan": patient.plan_text,
                "next_due": views.next_due(loops),
                **views.last_event(ordered_events, patient.id),
                "channel": views.channel_of(patient, token),
                "link": None,
                "link_available": token is not None,
                "loops": [
                    _legacy_loop(loop, sensitive) for loop in loops
                ],
            }
        )
    counts = board.tally(
        loop_row["state"]
        for patient_row in patient_rows
        for loop_row in patient_row["loops"]
    )

    latest = max(
        records.link_tokens,
        key=lambda token: (token.created_at, token.id),
        default=None,
    )
    qr = None
    if latest is not None:
        owner = next((row for row in patients if row.id == latest.patient_id), None)
        qr = {
            "available": True,
            "patient": owner.name if owner else "",
        }
    return _wire_projection(
        {
            "doctor": records.doctor.name,
            "synthetic": records.doctor.synthetic,
            "patients": patient_rows,
            "counts": counts,
            "qr": qr,
        },
        sensitive,
    )


def _legacy_event(
    event: Event,
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    return _wire_projection(
        {
            "id": event.id,
            "synthetic": event.synthetic,
            "kind": event.kind,
            "patient_id": event.patient_id,
            "text": event.text,
            "media": _wire_value(event.media, sensitive),
            "meta": _wire_value(event.meta, sensitive),
            "ts_ms": int(event.ts.timestamp() * 1000),
        },
        sensitive,
    )


def _time_scale(records: WorkspaceRecords) -> int:
    try:
        return max(
            1,
            int(records.settings.get("time_scale") or runtime_settings.ENV_TIME_SCALE),
        )
    except (TypeError, ValueError):
        return runtime_settings.ENV_TIME_SCALE


def _legacy_patient_detail(
    records: WorkspaceRecords,
    patient: Patient,
    patient_loops: Sequence[Loop],
    token: Optional[LinkToken],
    legacy_history: Sequence[Event],
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    doctor_policy = policy.for_doctor(records.doctor)
    channel = views.channel_of(patient, token)
    return _wire_projection({
        "channel": channel,
        "link": None,
        "link_available": token is not None,
        "patient": {
            "id": patient.id,
            "synthetic": patient.synthetic,
            "name": patient.name,
            "age": patient.age,
            "sex": patient.sex,
            "diagnosis": patient.diagnosis,
            "plan": patient.plan_text,
            "targets": _wire_value(patient.targets, sensitive),
            "baseline": _wire_value(patient.baseline, sensitive),
            "status": patient.status,
            "results": _wire_value(patient.results, sensitive),
            # Keep this doctor-only projection aligned with the existing
            # doctor detail route without introducing a second direct field
            # touch that the patient-path source invariant would flag.
            "notes": _wire_value(
                patient.model_dump(mode="python").get("notes", []),
                sensitive,
            ),
        },
        "loops": [
            {
                **_legacy_loop(loop, sensitive),
                "attempts": loop.attempts,
                "results": _wire_value(loop.results, sensitive),
                "readings": _wire_value(loop.readings, sensitive),
                "contacts": loop.contacts,
                "barrier": loop.barrier,
                "paused": loop.paused,
                "doctor_reviewed": loop.doctor_reviewed,
                "verified": _wire_value(loop.verified, sensitive),
            }
            for loop in patient_loops
        ],
        "contracts": [
            _wire_value(
                contract.render(
                    loop,
                    doctor_policy,
                    records.doctor.name,
                    patient.name,
                ),
                sensitive,
            )
            for loop in patient_loops
        ],
        "monitoring": [
            {
                "loop_id": loop.id,
                "title": loop.title,
                **monitoring.summary(loop, _time_scale(records)).as_dict(),
            }
            for loop in patient_loops
            if monitoring.is_monitoring(loop)
        ],
        "timeline": [
            {
                "kind": event.kind,
                "text": event.text,
                "meta": _wire_value(event.meta, sensitive),
                "synthetic": event.synthetic,
                "ts_ms": int(event.ts.timestamp() * 1000),
            }
            for event in legacy_history
            if event.patient_id == patient.id
        ],
    }, sensitive)


def _legacy_summary(
    records: WorkspaceRecords,
    legacy_history: Sequence[Event],
    as_of: datetime,
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    counts = summary.compute(
        list(records.loops),
        list(legacy_history),
        list(records.open_relays),
        on=summary.today(as_of),
    )
    cairo_now = as_of.astimezone(timing.CAIRO)
    return _wire_projection(
        {
            "doctor": records.doctor.name,
            "line": summary.line(counts),
            "counts": counts.as_dict(),
            "card": summary.card(counts, records.doctor.name, cairo_now),
        },
        sensitive,
    )


# ---------------------------------------------------------------------------
# Canonical rows, queues, counters, patient page, and BP selector
# ---------------------------------------------------------------------------
def _loop_row(loop: Loop, sensitive: _SensitiveValues) -> dict[str, Any]:
    return _wire_projection(
        {
            "id": f"loop:{loop.id}",
            "source_id": loop.id,
            "row_type": "loop",
            "patient_id": loop.patient_id,
            "synthetic": loop.synthetic,
            "type": loop.type,
            "title": loop.title,
            "state": loop.state,
            "details": _wire_value(loop.details, sensitive),
            "due_at": loop.due_at.isoformat() if loop.due_at else None,
            "barrier": loop.barrier,
            "paused": bool(loop.paused),
            "doctor_reviewed": bool(loop.doctor_reviewed),
            "closed_at": (
                loop.closed_at.isoformat()
                if getattr(loop, "closed_at", None) is not None
                else None
            ),
            "updated_at": loop.updated_at.isoformat(),
        },
        sensitive,
    )


def _event_row(event: Event, sensitive: _SensitiveValues) -> dict[str, Any]:
    original_card = cards.card_of(event)
    normalized_actions = []
    for action in cards.actions_of(event):
        normalized = dict(action)
        verb, _, _ = str(normalized.get("id") or "").partition(":")
        if verb == cards.SEEN:
            # ``seen`` has no domain target; cards.claim resolves it by the id
            # of the card event carrying the button. Older identity-mismatch
            # cards named their evidence event instead, making the visible
            # button unclaimable. The canonical projection binds it to the
            # actual card without rewriting the immutable legacy event.
            normalized["id"] = f"seen:{event.id}"
        normalized_actions.append(normalized)
    raw_card = (
        {**original_card, "actions": normalized_actions}
        if original_card
        else {}
    )
    if (
        cards.is_open(event)
        and str(raw_card.get("severity") or "").lower() == "red"
        and not raw_card.get("actions")
    ):
        # Legacy red cards synthesize this acknowledgement in JavaScript. The
        # v2 browser is rule-free, so its canonical row must carry the action.
        raw_card = {
            **raw_card,
            "actions": [{"id": f"seen:{event.id}", "label": "Seen"}],
        }
    return _wire_projection(
        {
            "id": f"event:{event.id}",
            "source_id": event.id,
            "row_type": "event",
            "patient_id": event.patient_id,
            "loop_id": event.loop_id,
            "synthetic": event.synthetic,
            "kind": event.kind,
            "text": event.text,
            "media": _wire_value(event.media, sensitive),
            "meta": _wire_value(event.meta, sensitive),
            "card": _wire_value(raw_card, sensitive),
            "ts": event.ts.isoformat(),
            "ts_ms": int(event.ts.timestamp() * 1000),
        },
        sensitive,
    )


def _relay_row(relay: Relay, sensitive: _SensitiveValues) -> dict[str, Any]:
    return _wire_projection(
        {
            "id": f"relay:{relay.id}",
            "source_id": relay.id,
            "row_type": "relay",
            "patient_id": relay.patient_id,
            "loop_id": relay.loop_id,
            "question": relay.question,
            "reason": relay.reason,
            "created_at": relay.created_at.isoformat(),
        },
        sensitive,
    )


def _patient_record_row(patient: Patient) -> dict[str, Any]:
    """The minimal identity row behind the true-total patient metric.

    Full clinical summaries live only in the requested patient page.  Keeping
    all patient names and diagnoses here would make the page limit cosmetic.
    """
    return {
        "id": f"patient:{patient.id}",
        "source_id": patient.id,
        "row_type": "patient_ref",
        "status": patient.status,
        "synthetic": patient.synthetic,
    }


def _queue(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["id"])
    return {
        "count": len(ordered),
        "row_ids": [row["id"] for row in ordered],
        "rows": ordered,
    }


def _metric(row_ids: Iterable[str]) -> dict[str, Any]:
    ordered = sorted(set(row_ids))
    return {"count": len(ordered), "row_ids": ordered}


def _notification_class(event: Event) -> str:
    raw = (event.meta or {}).get("notification_class")
    if isinstance(raw, Enum):
        raw = raw.value
    return str(raw or "").strip().upper()


def _on_cairo_day(value: Any, as_of: datetime) -> bool:
    if not isinstance(value, datetime):
        return False
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    at = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
    return (
        aware.astimezone(timezone.utc) <= at.astimezone(timezone.utc)
        and aware.astimezone(timing.CAIRO).date() == summary.today(as_of)
    )


_RELAY_CONSUMED = "relay_consumed"
_LOOP_CLOSED = "loop_closed"


def _reconciled_cards(
    events: Sequence[Event],
    relays_by_id: Mapping[str, Relay],
    loops_by_id: Mapping[str, Loop],
) -> dict[str, str]:
    """Open cards whose obligation was already carried out, and by what.

    Retiring a card is bookkeeping about work that has already happened. The
    card action route sends the answer or closes the loop first and calls
    ``cards.resolve`` afterwards, deliberately outside the claim, so a failure
    there leaves a finished obligation on a card that still says open. The
    Telegram "Answer" flow reaches the same state from the other side: it
    closes the relay and never touches the console card.

    Both are consumed history, not a corrupt workspace. The projection marks
    them resolved and keeps them out of every queue, because a button whose
    target is gone is a dead obligation in a doctor's inbox. This is the only
    tolerance: a target that still exists but belongs to another patient or
    loop is a cross-boundary reference and still fails closed in
    ``_assert_scoped``.
    """
    reconciled: dict[str, str] = {}
    for event in events:
        if not cards.is_open(event):
            continue
        for action in cards.actions_of(event):
            verb, separator, ident = str(action.get("id") or "").partition(":")
            if not separator or not ident:
                continue
            if verb == "reply" and ident not in relays_by_id:
                reconciled[event.id] = _RELAY_CONSUMED
                break
            if verb == "reviewed":
                target = loops_by_id.get(ident)
                if target is not None and target.state == "done":
                    reconciled[event.id] = _LOOP_CLOSED
                    break
    return reconciled


def _canonical_patient(
    patient: Patient,
    patient_loops: Sequence[Loop],
    ordered_events: Sequence[Event],
    usable_token: Optional[LinkToken],
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    channels = patient.channels or {}
    telegram = (
        isinstance(channels, dict)
        and channels.get("telegram_chat_id") is not None
    )
    if telegram:
        reachability = "TELEGRAM"
    elif usable_token is not None:
        reachability = "WEB"
    else:
        reachability = "NONE"
    return _wire_projection(
        {
            "id": patient.id,
            "source_id": patient.id,
            "row_id": f"patient:{patient.id}",
            "synthetic": patient.synthetic,
            "name": patient.name,
            "diagnosis": patient.diagnosis,
            "status": patient.status,
            "plan": patient.plan_text,
            "next_due": views.next_due(patient_loops),
            **views.last_event(ordered_events, patient.id),
            "reachability": reachability,
            "link_available": usable_token is not None,
            "loop_row_ids": sorted(f"loop:{loop.id}" for loop in patient_loops),
        },
        sensitive,
    )


def _bp_metric(loop: Loop) -> bool:
    if not monitoring.is_monitoring(loop):
        return False
    raw = str((loop.details or {}).get("metric") or "").strip().lower()
    normalized = " ".join(
        "".join(character if character.isalnum() else " " for character in raw).split()
    )
    return normalized in {"bp", "blood pressure", "bloodpressure"}


def _bp_tile(
    loops: Sequence[Loop],
    patients_by_id: Mapping[str, Patient],
    time_scale: int,
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    candidates = [loop for loop in loops if _bp_metric(loop)]
    if not candidates:
        return {"loop": None, "patient": None, "summary": None}
    # A live BP contract wins; within the same lane the most recently changed
    # one wins.  The id makes equal timestamps deterministic.
    candidates.sort(
        key=lambda loop: (
            loop.state in ("open", "waiting_patient", "received", "pending_review"),
            _iso_key(loop.updated_at),
            loop.id,
        ),
        reverse=True,
    )
    chosen = candidates[0]
    owner = patients_by_id.get(chosen.patient_id)
    return _wire_projection({
        "loop": {
            "id": chosen.id,
            "row_id": f"loop:{chosen.id}",
            "patient_id": chosen.patient_id,
            "title": chosen.title,
            "state": chosen.state,
            "details": _wire_value(chosen.details, sensitive),
            "readings": _wire_value(chosen.readings, sensitive),
        },
        "patient": (
            {
                "id": owner.id,
                "source_id": owner.id,
                "name": owner.name,
                "diagnosis": owner.diagnosis,
            }
            if owner is not None
            else None
        ),
        "summary": monitoring.summary(chosen, time_scale).as_dict(),
    }, sensitive)


def _agent_event(
    event: Event,
    sensitive: _SensitiveValues,
) -> dict[str, Any]:
    legacy = _legacy_event(event, sensitive)
    return _wire_projection(
        {
            **legacy,
            "loop_id": event.loop_id,
            "channel": event.channel,
            "ts": event.ts.isoformat(),
            "card": _wire_value(cards.card_of(event), sensitive),
            "legacy": legacy,
        },
        sensitive,
    )


def _parity_check(
    legacy_ids: Iterable[str],
    canonical_ids: Iterable[str],
    *,
    relation: str = "EQUALS",
) -> dict[str, Any]:
    legacy = sorted(set(legacy_ids))
    canonical = sorted(set(canonical_ids))
    matched = (
        set(canonical).issubset(legacy)
        if relation == "SUBSET_OF_LEGACY"
        else legacy == canonical
    )
    return {
        "status": "MATCH" if matched else "MISMATCH",
        "relation": relation,
        "legacy_row_ids": legacy,
        "canonical_row_ids": canonical,
    }


def build_snapshot(
    records: WorkspaceRecords,
    as_of: datetime,
    *,
    patient_offset: int = 0,
    patient_limit: int = DEFAULT_PATIENT_LIMIT,
    event_cursor: Optional[str] = None,
    event_limit: int = DEFAULT_EVENT_LIMIT,
    selected_patient_id: Optional[str] = None,
    delivery_health: Optional[Mapping[str, Any]] = None,
    system_health: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Return one canonical, versioned doctor workspace projection."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    cursor_state = (
        _decode_cursor(event_cursor, records.doctor.id)
        if event_cursor is not None
        else _CursorState(
            _CURSOR_FLOOR,
            _CURSOR_FLOOR,
            _CURSOR_FLOOR,
            "",
            False,
        )
    )
    # Cursor ownership is checked before bundle validation.  This makes a
    # foreign cursor fail with the public cursor error even if a faulty caller
    # also handed us a malformed bundle; no tenant facts are disclosed.
    _assert_scoped(records)
    sensitive = _sensitive_values(records)

    patients = _sorted_records(records.patients)
    patients_by_id = {patient.id: patient for patient in patients}
    if selected_patient_id and selected_patient_id not in patients_by_id:
        raise UnknownPatient(selected_patient_id)
    ordered_loops = _sorted_records(records.loops)
    ordered_events = sorted(
        records.events,
        key=lambda event: (_nanos(event.ts), event.id),
    )
    persisted_events = sorted(records.events, key=_event_key)
    read_boundary = (
        _nanos(records.read_at)
        if records.read_at is not None
        else max([
            _nanos(as_of),
            *(_event_key(event)[0] for event in persisted_events),
        ])
    )
    if read_boundary < cursor_state.upper:
        raise InvalidCursor("event cursor is newer than this workspace read")
    if cursor_state.open_batch:
        batch_lower = cursor_state.lower
        batch_upper = cursor_state.upper
        batch_position = (
            cursor_state.position_stamp,
            cursor_state.position_id,
        )
    else:
        batch_lower = cursor_state.lower
        batch_upper = read_boundary
        batch_position = None
    ordered_reports = sorted(
        records.reports,
        key=lambda report: (_iso_key(report.created_at), report.id),
        reverse=True,
    )
    loops_by_patient = _loops_by_patient(records)
    legacy_tokens = _tokens_by_patient(records.link_tokens)
    usable_tokens = _tokens_by_patient(
        tuple(token for token in records.link_tokens if links.usable(token, as_of))
    )

    canonical_patients = [
        _canonical_patient(
            patient,
            loops_by_patient.get(patient.id, ()),
            ordered_events,
            usable_tokens.get(patient.id),
            sensitive,
        )
        for patient in patients
    ]
    offset = max(0, int(patient_offset or 0))
    limit = min(MAX_PATIENT_LIMIT, max(1, int(patient_limit or DEFAULT_PATIENT_LIMIT)))
    patient_page = canonical_patients[offset : offset + limit]

    rows: dict[str, dict[str, Any]] = {}
    loop_rows = {loop.id: _loop_row(loop, sensitive) for loop in ordered_loops}
    for loop in ordered_loops:
        owner = patients_by_id.get(loop.patient_id)
        loop_rows[loop.id]["patient_name"] = _wire_projection(
            owner.name if owner is not None else "",
            sensitive,
        )
    rows.update((row["id"], row) for row in loop_rows.values())
    for patient in patients:
        row = _patient_record_row(patient)
        rows[row["id"]] = row

    reconciled_cards = _reconciled_cards(
        ordered_events,
        {relay.id: relay for relay in records.open_relays},
        {loop.id: loop for loop in ordered_loops},
    )
    open_card_events = [
        event
        for event in cards.open_cards(ordered_events)
        if event.id not in reconciled_cards
    ]
    event_rows = {
        event.id: _event_row(event, sensitive)
        for event in open_card_events
    }
    for event in open_card_events:
        owner = patients_by_id.get(event.patient_id or "")
        event_rows[event.id]["patient_name"] = _wire_projection(
            owner.name if owner is not None else "",
            sensitive,
        )
        event_rows[event.id]["notification_class"] = _notification_class(event)
    rows.update((row["id"], row) for row in event_rows.values())

    # Consumed cards stay renderable as history: the doctor should still be
    # able to read the card whose answer was already sent.  The stored event is
    # never rewritten, so the reconciliation lives only in this projection, and
    # it says which obligation was consumed rather than pretending the card was
    # pressed.  The buttons are kept as the immutable record shows them; the
    # ``resolved`` flag is what makes them non-executable, exactly as it does
    # for a card the doctor did press.
    for event in ordered_events:
        marker = reconciled_cards.get(event.id)
        if marker is None:
            continue
        row = _event_row(event, sensitive)
        owner = patients_by_id.get(event.patient_id or "")
        row["patient_name"] = _wire_projection(
            owner.name if owner is not None else "",
            sensitive,
        )
        row["notification_class"] = _notification_class(event)
        row["card"] = {
            **row["card"],
            "resolved": True,
            "reconciled": marker,
        }
        rows[row["id"]] = row
    relay_rows = {
        relay.id: _relay_row(relay, sensitive) for relay in records.open_relays
    }
    for relay in records.open_relays:
        owner = patients_by_id.get(relay.patient_id)
        relay_rows[relay.id]["patient_name"] = _wire_projection(
            owner.name if owner is not None else "",
            sensitive,
        )
    rows.update((row["id"], row) for row in relay_rows.values())

    danger = [
        event_rows[event.id]
        for event in open_card_events
        if _notification_class(event) == "DANGER"
    ]
    urgent = [
        event_rows[event.id]
        for event in open_card_events
        if _notification_class(event) == "URGENT_SLA"
    ]
    unclassified_red = [
        event_rows[event.id]
        for event in open_card_events
        if str(cards.card_of(event).get("severity") or "").lower() == "red"
        and _notification_class(event) not in ("DANGER", "URGENT_SLA")
    ]
    classified_card_ids = {
        row["source_id"] for row in danger + urgent + unclassified_red
    }
    doctor_actions = [
        event_rows[event.id]
        for event in open_card_events
        if event.id not in classified_card_ids
    ]

    active_states = ("open", "waiting_patient")
    blocked_loops = [
        loop for loop in ordered_loops
        if loop.state in active_states and (loop.paused or bool(loop.barrier))
    ]
    blocked_loop_ids = {loop.id for loop in blocked_loops}
    working_loops = [
        loop for loop in ordered_loops
        if loop.state in active_states and loop.id not in blocked_loop_ids
    ]
    legacy_review_loops = [
        loop for loop in ordered_loops
        if loop.state in ("received", "pending_review")
    ]
    review_loops = [
        loop
        for loop in legacy_review_loops
        if (loop.verified or {}).get("satisfies") is True
    ]
    review_loop_ids = {loop.id for loop in review_loops}
    verification_unknown_loops = [
        loop for loop in legacy_review_loops if loop.id not in review_loop_ids
    ]
    deadline_loops = [loop for loop in ordered_loops if loop.state == "unreachable"]
    closed_on_this_day = [
        loop for loop in ordered_loops
        if loop.state == "done" and _on_cairo_day(getattr(loop, "closed_at", None), as_of)
    ]
    # "Closed today" is read as work that is finished and correct.  A doctor
    # can press Reviewed on evidence the verifier did not pass, and counting
    # that inside the same number is false reassurance in the one tile he
    # trusts most.  Same rule as review_ready above: the verifier's own fact
    # decides, an absent or false ``satisfies`` is never a pass, and the
    # unverified closes stay visible in their own queue instead of vanishing.
    closed_today_loops = [
        loop for loop in closed_on_this_day
        if (loop.verified or {}).get("satisfies") is True
    ]
    reviewed_unverified_loops = [
        loop for loop in closed_on_this_day
        if (loop.verified or {}).get("satisfies") is not True
    ]

    # A loop-backed relay is one view of the blocked loop, not a second case.
    standalone_relays = [
        relay
        for relay in records.open_relays
        if not relay.loop_id or relay.loop_id not in blocked_loop_ids
    ]
    blocked_rows = [loop_rows[loop.id] for loop in blocked_loops] + [
        relay_rows[relay.id] for relay in standalone_relays
    ]
    terminal_rows = [
        loop_rows[loop.id] for loop in legacy_review_loops + deadline_loops
    ]

    queues = {
        "danger": _queue(danger),
        "urgent_review": _queue(urgent),
        "unclassified_red": _queue(unclassified_red),
        "doctor_actions": _queue(doctor_actions),
        "sanad_working": _queue(loop_rows[loop.id] for loop in working_loops),
        "blocked": _queue(blocked_rows),
        "review_ready": _queue(loop_rows[loop.id] for loop in review_loops),
        "verification_unknown": _queue(
            loop_rows[loop.id] for loop in verification_unknown_loops
        ),
        "deadline_outcomes": _queue(loop_rows[loop.id] for loop in deadline_loops),
        "terminal_waiting_review": _queue(terminal_rows),
        "closed_today": _queue(loop_rows[loop.id] for loop in closed_today_loops),
        "reviewed_unverified": _queue(
            loop_rows[loop.id] for loop in reviewed_unverified_loops
        ),
    }
    active_patient_ids = [
        f"patient:{patient.id}" for patient in patients if patient.status == "active"
    ]
    queues["active_patients"] = _queue(rows[row_id] for row_id in active_patient_ids)
    metrics = {
        "danger_unacknowledged": _metric(queues["danger"]["row_ids"]),
        "review_ready": _metric(queues["review_ready"]["row_ids"]),
        "deadline_outcomes": _metric(queues["deadline_outcomes"]["row_ids"]),
        "sanad_working": _metric(queues["sanad_working"]["row_ids"]),
        "terminal_waiting_review": _metric(
            queues["terminal_waiting_review"]["row_ids"]
        ),
        "closed_today": _metric(queues["closed_today"]["row_ids"]),
        "reviewed_unverified": _metric(queues["reviewed_unverified"]["row_ids"]),
        "active_patients_total": _metric(active_patient_ids),
    }

    # Legacy event readers expose the newest 200 events.  This remains a
    # shadow only; the canonical cursor below starts at the oldest unseen row
    # and can page a burst without dropping its first 51 records.
    legacy_history = ordered_events[-DEFAULT_EVENT_LIMIT:]
    legacy_board = _legacy_board(
        records,
        patients,
        loops_by_patient,
        legacy_tokens,
        ordered_events,
        sensitive,
    )
    loaded_patient_ids = {row["source_id"] for row in patient_page}
    # The shadow keeps the exact full-board counts but must not defeat the
    # canonical patient page by returning every full legacy patient summary.
    legacy_board = {
        **legacy_board,
        "patients": [
            row
            for row in legacy_board["patients"]
            if row["id"] in loaded_patient_ids
        ],
        "patient_total": len(patients),
        "patient_offset": offset,
        "patient_limit": limit,
        "patient_has_more": offset + len(patient_page) < len(patients),
        "qr": {"available": legacy_board["qr"] is not None},
        "scope": "LOADED_PATIENT_PAGE_WITH_FULL_BOARD_COUNTS",
    }
    settings_row = _wire_projection(
        views.settings_view(records.doctor, policy.for_doctor(records.doctor)),
        sensitive,
    )
    selected = patients_by_id.get(selected_patient_id or "")
    legacy = {
        "board": legacy_board,
        "cards": {
            "cards": [
                _wire_value(cards.row(event), sensitive)
                for event in open_card_events
            ]
        },
        "reports": {
            "reports": [
                _wire_value(views.report_row(report), sensitive)
                for report in ordered_reports
            ]
        },
        "settings": settings_row,
        "feed": {
            "events": [
                _legacy_event(event, sensitive)
                for event in legacy_history
            ]
        },
        "summary": _legacy_summary(records, legacy_history, as_of, sensitive),
        "patient": (
            _legacy_patient_detail(
                records,
                selected,
                loops_by_patient.get(selected.id, ()),
                legacy_tokens.get(selected.id),
                legacy_history,
                sensitive,
            )
            if selected is not None
            else None
        ),
    }

    legacy_red = [
        f"loop:{loop.id}"
        for loop in ordered_loops
        if board.colour(loop.state) == "red"
    ]
    legacy_white = [
        f"loop:{loop.id}"
        for loop in ordered_loops
        if board.colour(loop.state) == "white"
    ]
    legacy_yellow = [
        f"loop:{loop.id}"
        for loop in ordered_loops
        if board.colour(loop.state) == "yellow"
    ]
    legacy_green = sorted(
        f"loop:{loop.id}"
        for loop in ordered_loops
        if board.colour(loop.state) == "green"
    )
    working_plus_blocked = metrics["sanad_working"]["row_ids"] + [
        f"loop:{loop.id}" for loop in blocked_loops
    ]
    canonical_open_card_ids = (
        queues["danger"]["row_ids"]
        + queues["urgent_review"]["row_ids"]
        + queues["unclassified_red"]["row_ids"]
        + queues["doctor_actions"]["row_ids"]
    )
    legacy_open_card_ids = [f"event:{event.id}" for event in open_card_events]
    shadow_checks = {
        "legacy_red_partitioned_by_verification": _parity_check(
            legacy_red,
            metrics["review_ready"]["row_ids"]
            + queues["verification_unknown"]["row_ids"],
        ),
        "legacy_white_equals_deadline": _parity_check(
            legacy_white, metrics["deadline_outcomes"]["row_ids"]
        ),
        "legacy_yellow_equals_working_plus_blocked": _parity_check(
            legacy_yellow, working_plus_blocked
        ),
        "closed_today_is_subset_of_legacy_green": _parity_check(
            legacy_green,
            metrics["closed_today"]["row_ids"],
            relation="SUBSET_OF_LEGACY",
        ),
        # The split above must partition today's closes, not lose any: both
        # halves together are still nothing but legacy-green loops.
        "reviewed_closes_are_subset_of_legacy_green": _parity_check(
            legacy_green,
            metrics["closed_today"]["row_ids"]
            + metrics["reviewed_unverified"]["row_ids"],
            relation="SUBSET_OF_LEGACY",
        ),
        "legacy_open_cards_covered": _parity_check(
            legacy_open_card_ids,
            canonical_open_card_ids,
        ),
    }

    fresh = []
    for event in persisted_events:
        event_key = _event_key(event)
        if not batch_lower < event_key[0] <= batch_upper:
            continue
        if batch_position is not None and event_key <= batch_position:
            continue
        fresh.append(event)
    event_page_limit = min(MAX_EVENT_LIMIT, max(1, int(event_limit or DEFAULT_EVENT_LIMIT)))
    event_page = fresh[:event_page_limit]
    event_has_more = len(fresh) > len(event_page)
    if event_has_more:
        next_stamp, next_event_id = _event_key(event_page[-1])
        next_cursor_state = _CursorState(
            batch_lower,
            batch_upper,
            next_stamp,
            next_event_id,
            True,
        )
    else:
        next_cursor_state = _CursorState(
            batch_upper,
            batch_upper,
            batch_upper,
            "",
            False,
        )
    next_cursor = _encode_cursor(records.doctor.id, next_cursor_state)
    event_items = [
        _agent_event(event, sensitive) for event in event_page
    ]
    recent_events = [
        _agent_event(event, sensitive)
        for event in persisted_events[-DEFAULT_EVENT_LIMIT:]
    ]

    selected_canonical = None
    if selected is not None:
        selected_canonical = {
            **_legacy_patient_detail(
                records,
                selected,
                loops_by_patient.get(selected.id, ()),
                usable_tokens.get(selected.id),
                legacy_history,
                sensitive,
            ),
            "reachability": next(
                row["reachability"]
                for row in canonical_patients
                if row["source_id"] == selected.id
            ),
        }

    delivery = (
        _wire_value(delivery_health, sensitive)
        if delivery_health is not None
        else {
            "status": "UNKNOWN",
            "basis": "no persisted delivery receipt projection was supplied",
        }
    )
    system = (
        _wire_value(system_health, sensitive)
        if system_health is not None
        else {"status": "UNKNOWN"}
    )

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": _snapshot_id(records),
        "snapshot_id_kind": "RECORD_VERSION",
        "as_of": as_of.isoformat(),
        "event_cursor": next_cursor,
        "doctor": _wire_projection(
            {
                "id": records.doctor.id,
                "synthetic": records.doctor.synthetic,
                "name": records.doctor.name,
                "specialty": records.doctor.specialty,
                "language": records.doctor.lang,
                "permissions": {
                    "view_workspace": True,
                    "submit_doctor_commands": True,
                    "act_on_clinical_cards": True,
                    "admin": False,
                },
            },
            sensitive,
        ),
        "metrics": metrics,
        "queues": queues,
        "rows": dict(sorted(rows.items())),
        "patients": {
            "items": patient_page,
            "total": len(canonical_patients),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(patient_page) < len(canonical_patients),
        },
        "selected_patient": selected_canonical,
        "bp_tile": _bp_tile(
            ordered_loops,
            patients_by_id,
            _time_scale(records),
            sensitive,
        ),
        "agent_events": {
            "items": event_items,
            "recent": recent_events,
            "count": len(event_items),
            "has_more": event_has_more,
            "cursor": next_cursor,
        },
        "delivery": delivery,
        "system": system,
        "health": {"delivery": delivery, "system": system},
        "legacy": legacy,
        "shadow": {
            "mode": "OBSERVE_ONLY",
            "authoritative": False,
            "legacy": {
                "board": {
                    "red_row_ids": sorted(legacy_red),
                    "white_row_ids": sorted(legacy_white),
                    "yellow_row_ids": sorted(legacy_yellow),
                    "green_row_ids": legacy_green,
                },
                "open_card_row_ids": sorted(legacy_open_card_ids),
            },
            "checks": shadow_checks,
            "status": (
                "MATCH"
                if all(check["status"] == "MATCH" for check in shadow_checks.values())
                else "MISMATCH"
            ),
        },
    }
    return snapshot
