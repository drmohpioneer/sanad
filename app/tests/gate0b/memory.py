"""A deterministic, transaction-shaped replacement for :mod:`core.store`.

Gate 0B must exercise Sanad's real routes and domain code without allowing a
Firestore fallback.  ``MemoryStore`` implements the store surface used by the
nine-beat legacy run and its dashboard reads.  Records are copied on both write
and read, and compound claims run under one ``asyncio.Lock`` to preserve the
all-or-nothing property that the production Firestore transactions buy.

This is deliberately a test adapter, not a second product repository.  The
``patched_store`` context manager temporarily replaces functions on the real
``core.store`` module; all callers continue to import and call that module.
Unknown reads retain the production API's ``None`` result, while mutations that
would be a Firestore ``NotFound`` fail with ``MissingRecordError``.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional, Union

from core.models import (
    Doctor,
    Event,
    LinkToken,
    Loop,
    Patient,
    PendingConfirm,
    PendingStart,
    Relay,
    Report,
    Send,
)


UTC = timezone.utc
DEFAULT_NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
DEFAULT_TICK = timedelta(microseconds=1)
SANAD_NS = uuid.UUID("8f6d1b7e-3c22-5a41-9e0d-7a3f2b6c4d15")

CLAIMED = "claimed"
ALREADY_SENT = "already sent"
RESEND = "resend"
MAX_RESENDS = 1
CLAIM_LEASE = timedelta(minutes=5)
PATIENT_TURN_LEASE = CLAIM_LEASE
PENDING = "pending"
COMMITTING = "committing"
LADDER, COORDINATOR, RELUCTANCE, INTENT = (
    "ladder",
    "coordinator",
    "reluctance",
    "intent",
)
NO_DAY_LEFT = "one message per patient per day"
NO_CONTACTS_LEFT = "the contact limit on this loop is spent"
RESET_COLLECTIONS = (
    "patients",
    "loops",
    "events",
    "pending_confirms",
    "link_tokens",
    "relays",
    "sends",
    "reports",
    "contacts",
    "card_actions",
)


class MissingRecordError(KeyError):
    """A mutation targeted a record Firestore would report as missing."""


def _clone(value: Any) -> Any:
    if hasattr(value, "model_copy"):
        return value.model_copy(deep=True)
    return copy.deepcopy(value)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _claimed_at(row: Any) -> Optional[datetime]:
    if hasattr(row, "model_dump"):
        row = row.model_dump()
    if not isinstance(row, dict):
        return None
    for key in ("claimed_at", "created_at"):
        value = row.get(key)
        if isinstance(value, datetime):
            return _aware(value)
        if isinstance(value, str) and value.strip():
            try:
                return _aware(datetime.fromisoformat(value))
            except ValueError:
                pass
    return None


def _updated(record: Any, fields: dict[str, Any]) -> Any:
    return record.model_copy(update=_clone(fields), deep=True)


class MemoryStore:
    """In-memory state with the public coroutine surface of ``core.store``."""

    PROJECT = "sanad-gate0b-local"

    def __init__(
        self,
        *,
        start: datetime = DEFAULT_NOW,
        tick: timedelta = DEFAULT_TICK,
    ) -> None:
        if tick <= timedelta(0):
            raise ValueError("MemoryStore tick must be positive")
        self._clock = _aware(start)
        self._tick = tick
        self._id_counter = 0
        self._token_counter = 0
        self._lock = asyncio.Lock()

        self.doctors: dict[str, Doctor] = {}
        self.patients: dict[str, Patient] = {}
        self.loops: dict[str, Loop] = {}
        self.sends: dict[str, Send] = {}
        self.events: dict[str, Event] = {}
        self.reports: dict[str, Report] = {}
        self.confirms: dict[str, PendingConfirm] = {}
        self.link_tokens: dict[str, LinkToken] = {}
        self.relays: dict[str, Relay] = {}
        self.pending_starts: dict[str, PendingStart] = {}

        self.contacts: dict[str, dict[str, Any]] = {}
        self.card_actions: dict[str, dict[str, Any]] = {}
        self.patient_turns: dict[str, dict[str, Any]] = {}
        self.photo_receipts: dict[str, dict[str, Any]] = {}
        self.confirm_claims: dict[str, dict[str, Any]] = {}
        self.command_receipts: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {}

        # Neutral instrumentation holders.  Scenario adapters own their schemas.
        self.task_ledger: list[Any] = []
        self.model_ledger: list[Any] = []
        self.outbound_ledger: list[Any] = []
        self.tasks = self.task_ledger
        self.models = self.model_ledger
        self.outbound = self.outbound_ledger

    # ------------------------------------------------------------------
    # Determinism and diagnostics
    # ------------------------------------------------------------------
    def db(self) -> Any:
        raise AssertionError(
            "Gate 0B attempted to use Firestore; a core.store binding is missing"
        )

    def now(self) -> datetime:
        value = self._clock
        self._clock += self._tick
        return value

    def peek_now(self) -> datetime:
        return self._clock

    def advance(self, amount: timedelta) -> datetime:
        if amount < timedelta(0):
            raise ValueError("MemoryStore clock cannot move backwards")
        self._clock += amount
        return self._clock

    def new_id(self) -> str:
        self._id_counter += 1
        return f"id-{self._id_counter:08d}"

    def new_web_token(self) -> str:
        self._token_counter += 1
        return f"web-{self._token_counter:08d}"

    @staticmethod
    def derived_id(*parts: str) -> str:
        return uuid.uuid5(SANAD_NS, ":".join(parts)).hex

    @classmethod
    def photo_claim_id(cls, patient_id: str, day_index: int, digest: str) -> str:
        return cls.derived_id("photo", patient_id, str(day_index), digest)

    @staticmethod
    def contact_id(patient_id: str, day_index: int) -> str:
        return f"{patient_id}:{day_index}"

    def claim_expired(
        self, row: Any, at: Optional[datetime] = None
    ) -> bool:
        taken = _claimed_at(row)
        if taken is None:
            return False
        return (_aware(at) if at is not None else self.now()) - taken > CLAIM_LEASE

    def record_task(self, value: Any) -> None:
        self.task_ledger.append(_clone(value))

    def record_model(self, value: Any) -> None:
        self.model_ledger.append(_clone(value))

    def record_outbound(self, value: Any) -> None:
        self.outbound_ledger.append(_clone(value))

    def clear_ledgers(self) -> None:
        self.task_ledger.clear()
        self.model_ledger.clear()
        self.outbound_ledger.clear()

    def patched(self) -> Any:
        return patched_store(self)

    def _missing(self, collection: str, ident: str) -> MissingRecordError:
        return MissingRecordError(f"missing {collection} record: {ident}")

    # ------------------------------------------------------------------
    # Doctors and patients
    # ------------------------------------------------------------------
    async def create_doctor(
        self, name: str, lang: str = "en", specialty: str = "general practice"
    ) -> Doctor:
        async with self._lock:
            doctor = Doctor(
                id=self.new_id(),
                name=name,
                specialty=specialty,
                lang=lang,
                web_token=self.new_web_token(),
                created_at=self.now(),
            )
            self.doctors[doctor.id] = _clone(doctor)
            return _clone(doctor)

    async def doctor_by_name(self, name: str) -> Optional[Doctor]:
        async with self._lock:
            return next(
                (_clone(row) for row in self.doctors.values() if row.name == name),
                None,
            )

    async def doctor_by_id(self, doctor_id: str) -> Optional[Doctor]:
        async with self._lock:
            row = self.doctors.get(doctor_id)
            return _clone(row) if row is not None else None

    async def doctor_by_telegram(self, chat_id: int) -> Optional[Doctor]:
        async with self._lock:
            return next(
                (
                    _clone(row)
                    for row in self.doctors.values()
                    if row.telegram_chat_id == chat_id
                ),
                None,
            )

    async def doctor_by_token(self, token: str) -> Optional[Doctor]:
        async with self._lock:
            return next(
                (
                    _clone(row)
                    for row in self.doctors.values()
                    if row.web_token == token
                ),
                None,
            )

    async def update_doctor(self, doctor_id: str, **fields: Any) -> None:
        async with self._lock:
            row = self.doctors.get(doctor_id)
            if row is None:
                raise self._missing("doctor", doctor_id)
            self.doctors[doctor_id] = _updated(row, fields)

    async def create_patient(self, patient: Patient) -> Patient:
        async with self._lock:
            self.patients[patient.id] = _clone(patient)
            return _clone(patient)

    async def get_patient(self, patient_id: str) -> Optional[Patient]:
        async with self._lock:
            row = self.patients.get(patient_id)
            return _clone(row) if row is not None else None

    async def list_patients(self, doctor_id: str) -> list[Patient]:
        async with self._lock:
            rows = [
                _clone(row)
                for row in self.patients.values()
                if row.doctor_id == doctor_id
            ]
            return sorted(rows, key=lambda row: row.created_at)

    async def patient_by_telegram(self, chat_id: int) -> Optional[Patient]:
        rows = await self.patients_by_telegram(chat_id)
        return rows[0] if rows else None

    async def patients_by_telegram(self, chat_id: int) -> list[Patient]:
        async with self._lock:
            return [
                _clone(row)
                for row in self.patients.values()
                if (row.channels or {}).get("telegram_chat_id") == chat_id
            ]

    async def update_patient(self, patient_id: str, **fields: Any) -> None:
        async with self._lock:
            row = self.patients.get(patient_id)
            if row is None:
                raise self._missing("patient", patient_id)
            self.patients[patient_id] = _updated(row, fields)

    async def claim_opt_out(self, patient_id: str) -> bool:
        async with self._lock:
            row = self.patients.get(patient_id)
            if row is None or row.proactive_paused:
                return False
            self.patients[patient_id] = _updated(
                row, {"proactive_paused": True, "opt_out_at": self.now()}
            )
            return True

    async def doctor_chat_bindings(self) -> list[dict[str, Any]]:
        async with self._lock:
            out: list[dict[str, Any]] = []
            for doctor in self.doctors.values():
                if doctor.telegram_chat_id is None:
                    continue
                for patient in self.patients.values():
                    if (patient.channels or {}).get("telegram_chat_id") != doctor.telegram_chat_id:
                        continue
                    out.append(
                        {
                            "patient_id": patient.id,
                            "patient_name": patient.name,
                            "doctor_id": doctor.id,
                            "doctor_name": doctor.name,
                            "chat_id": doctor.telegram_chat_id,
                        }
                    )
            return _clone(out)

    # ------------------------------------------------------------------
    # Loops and atomic loop fields
    # ------------------------------------------------------------------
    async def create_loop(self, loop: Loop) -> Loop:
        async with self._lock:
            self.loops[loop.id] = _clone(loop)
            return _clone(loop)

    async def get_loop(self, loop_id: str) -> Optional[Loop]:
        async with self._lock:
            row = self.loops.get(loop_id)
            return _clone(row) if row is not None else None

    async def list_loops(self, patient_id: str) -> list[Loop]:
        async with self._lock:
            rows = [
                _clone(row)
                for row in self.loops.values()
                if row.patient_id == patient_id
            ]
            return sorted(rows, key=lambda row: row.created_at)

    async def update_loop(self, loop_id: str, **fields: Any) -> None:
        async with self._lock:
            row = self.loops.get(loop_id)
            if row is None:
                raise self._missing("loop", loop_id)
            values = dict(fields)
            values.setdefault("updated_at", self.now())
            self.loops[loop_id] = _updated(row, values)

    async def add_contact(self, loop_id: str, day_index: int) -> None:
        async with self._lock:
            loop = self.loops.get(loop_id)
            if loop is None:
                raise self._missing("loop", loop_id)
            days = list(loop.contact_days or [])
            if day_index not in days:
                days.append(day_index)
            self.loops[loop_id] = _updated(
                loop,
                {
                    "contacts": int(loop.contacts or 0) + 1,
                    "contact_days": days,
                    "updated_at": self.now(),
                },
            )

    async def refund_contact(self, loop_id: str) -> int:
        async with self._lock:
            loop = self.loops.get(loop_id)
            if loop is None:
                raise self._missing("loop", loop_id)
            value = max(0, int(loop.contacts or 0) - 1)
            self.loops[loop_id] = _updated(
                loop, {"contacts": value, "updated_at": self.now()}
            )
            return value

    async def append_reading(self, loop_id: str, row: dict[str, Any]) -> None:
        await self._append_union(loop_id, "readings", [row])

    async def append_result(
        self,
        loop_id: str,
        rows: Union[dict[str, Any], list[dict[str, Any]]],
    ) -> None:
        batch = [rows] if isinstance(rows, dict) else list(rows)
        if batch:
            await self._append_union(loop_id, "results", batch)

    async def _append_union(
        self, loop_id: str, field: str, values: list[dict[str, Any]]
    ) -> None:
        async with self._lock:
            loop = self.loops.get(loop_id)
            if loop is None:
                raise self._missing("loop", loop_id)
            merged = _clone(list(getattr(loop, field) or []))
            for value in values:
                candidate = _clone(value)
                if candidate not in merged:
                    merged.append(candidate)
            self.loops[loop_id] = _updated(
                loop, {field: merged, "updated_at": self.now()}
            )

    async def _bump_field(
        self, loop_id: str, field: str, also: Optional[dict[str, Any]] = None
    ) -> int:
        async with self._lock:
            loop = self.loops.get(loop_id)
            if loop is None:
                raise self._missing("loop", loop_id)
            value = int(getattr(loop, field) or 0) + 1
            fields = {field: value, "updated_at": self.now(), **(also or {})}
            self.loops[loop_id] = _updated(loop, fields)
            return value

    async def add_evidence_request(self, loop_id: str) -> int:
        return await self._bump_field(loop_id, "evidence_requests")

    async def add_reluctance(self, loop_id: str) -> int:
        return await self._bump_field(loop_id, "reluctance")

    async def bump_generation(self, loop_id: str) -> int:
        return await self._bump_field(loop_id, "generation", {"attempts": 0})

    async def bump_schedule_version(self, loop_id: str) -> int:
        return await self._bump_field(loop_id, "schedule_version")

    async def claim_delivery(
        self, loop_id: str, schedule_version: int, generation: int, at: datetime
    ) -> Optional[int]:
        async with self._lock:
            loop = self.loops.get(loop_id)
            if loop is None:
                return None
            if int(loop.schedule_version or 0) != int(schedule_version):
                return None
            if int(loop.generation or 0) != int(generation):
                return None
            attempts = int(loop.attempts or 0) + 1
            self.loops[loop_id] = _updated(
                loop,
                {
                    "attempts": attempts,
                    "state": "waiting_patient",
                    "last_attempt_at": at,
                    "updated_at": self.now(),
                },
            )
            return attempts

    async def claim_resume(self, loop_id: str, note: str) -> bool:
        async with self._lock:
            loop = self.loops.get(loop_id)
            if loop is None or not (loop.paused or loop.barrier):
                return False
            self.loops[loop_id] = _updated(
                loop,
                {
                    "paused": False,
                    "barrier": "",
                    "barrier_note": note,
                    "updated_at": self.now(),
                },
            )
            return True

    # ------------------------------------------------------------------
    # Patient turn, photo, send, and contact claims
    # ------------------------------------------------------------------
    async def claim_patient_turn(self, patient_id: str, owner: str) -> bool:
        async with self._lock:
            row = self.patient_turns.get(patient_id)
            stamp = self.now()
            if row is None:
                self.patient_turns[patient_id] = {
                    "patient_id": patient_id,
                    "state": CLAIMED,
                    "claimed_at": stamp,
                    "claimed_by": owner,
                }
                return True
            taken = _claimed_at(row)
            if taken is None or stamp - taken <= PATIENT_TURN_LEASE:
                return False
            self.patient_turns[patient_id] = {
                **row,
                "state": CLAIMED,
                "claimed_at": stamp,
                "claimed_by": owner,
                "reclaimed": True,
            }
            return True

    async def release_patient_turn(self, patient_id: str, owner: str) -> None:
        async with self._lock:
            row = self.patient_turns.get(patient_id)
            if row is not None and row.get("claimed_by") == owner:
                del self.patient_turns[patient_id]

    async def claim_photo(
        self, patient_id: str, day_index: int, digest: str, owner: str
    ) -> bool:
        async with self._lock:
            ident = self.photo_claim_id(patient_id, day_index, digest)
            row = self.photo_receipts.get(ident)
            stamp = self.now()
            if row is None:
                self.photo_receipts[ident] = {
                    "patient_id": patient_id,
                    "day_index": day_index,
                    "digest": digest,
                    "state": CLAIMED,
                    "claimed_at": stamp,
                    "claimed_by": owner,
                }
                return True
            taken = _claimed_at(row)
            if (
                row.get("state") != CLAIMED
                or taken is None
                or stamp - taken <= CLAIM_LEASE
            ):
                return False
            self.photo_receipts[ident] = {
                **row,
                "claimed_at": stamp,
                "claimed_by": owner,
                "reclaimed": True,
            }
            return True

    async def complete_photo(
        self, patient_id: str, day_index: int, digest: str, owner: str
    ) -> None:
        async with self._lock:
            ident = self.photo_claim_id(patient_id, day_index, digest)
            row = self.photo_receipts.get(ident)
            if row is not None and row.get("claimed_by") == owner:
                self.photo_receipts[ident] = {
                    **row,
                    "state": "complete",
                    "completed_at": self.now(),
                }

    async def release_photo(
        self, patient_id: str, day_index: int, digest: str, owner: str
    ) -> None:
        async with self._lock:
            ident = self.photo_claim_id(patient_id, day_index, digest)
            row = self.photo_receipts.get(ident)
            if row is not None and row.get("claimed_by") == owner:
                del self.photo_receipts[ident]

    async def claim_send(self, send: Send, owner: str = "") -> str:
        async with self._lock:
            owner = owner or self.new_id()
            held = self.sends.get(send.id)
            stamp = self.now()
            if held is None:
                stored = _updated(
                    _clone(send),
                    {"claimed_at": stamp, "claimed_by": owner},
                )
                self.sends[send.id] = stored
                return CLAIMED
            taken = _claimed_at(held)
            if (
                held.state == CLAIMED
                and taken is not None
                and stamp - taken > CLAIM_LEASE
            ):
                self.sends[send.id] = _updated(
                    held,
                    {"claimed_at": stamp, "claimed_by": owner},
                )
                return CLAIMED
            if held.state != "failed" or int(held.resends or 0) >= MAX_RESENDS:
                return ALREADY_SENT
            self.sends[send.id] = _updated(
                held,
                {
                    "state": CLAIMED,
                    "error": "",
                    "claimed_at": stamp,
                    "claimed_by": owner,
                    "resends": int(held.resends or 0) + 1,
                },
            )
            return RESEND

    async def channels_done(self, send_id: str) -> frozenset[str]:
        async with self._lock:
            if not send_id:
                return frozenset()
            held = self.sends.get(send_id)
            if held is None:
                return frozenset()
            return frozenset(
                name
                for name in ("web", "telegram")
                if getattr(held, f"{name}_done", False)
            )

    async def mark_channel_done(self, send_id: str, channel: str) -> None:
        if not send_id or channel not in ("web", "telegram"):
            return
        async with self._lock:
            held = self.sends.get(send_id)
            if held is None:
                raise self._missing("send", send_id)
            self.sends[send_id] = _updated(held, {f"{channel}_done": True})

    async def mark_send(self, send_id: str, state: str, error: str = "") -> None:
        async with self._lock:
            held = self.sends.get(send_id)
            if held is None:
                raise self._missing("send", send_id)
            self.sends[send_id] = _updated(
                held, {"state": state, "error": error}
            )

    async def send_state(self, send_id: str) -> str:
        async with self._lock:
            held = self.sends.get(send_id)
            return str(held.state or "") if held is not None else ""

    async def sends_for_patient(self, patient_id: str) -> list[Send]:
        async with self._lock:
            rows = [
                _clone(row)
                for row in self.sends.values()
                if row.patient_id == patient_id
            ]
            return sorted(rows, key=lambda row: row.created_at)

    async def release_send(self, send_id: str) -> None:
        async with self._lock:
            self.sends.pop(send_id, None)

    async def note_contact(
        self,
        patient_id: str,
        doctor_id: str,
        day_index: int,
        kind: str,
        loop_id: str = "",
    ) -> int:
        async with self._lock:
            return self._note_contact_locked(
                patient_id, doctor_id, day_index, kind, loop_id
            )

    def _note_contact_locked(
        self,
        patient_id: str,
        doctor_id: str,
        day_index: int,
        kind: str,
        loop_id: str = "",
    ) -> int:
        ident = self.contact_id(patient_id, day_index)
        row = _clone(
            self.contacts.get(
                ident,
                {
                    "patient_id": patient_id,
                    "doctor_id": doctor_id,
                    "day_index": day_index,
                    "count": 0,
                    "kinds": [],
                    "loops": [],
                },
            )
        )
        row["count"] = int(row.get("count") or 0) + 1
        if kind not in row["kinds"]:
            row["kinds"].append(kind)
        if loop_id and loop_id not in row["loops"]:
            row["loops"].append(loop_id)
        row["last_at"] = self.now()
        self.contacts[ident] = row
        return row["count"]

    async def reserve_contact(
        self,
        patient_id: str,
        doctor_id: str,
        day_index: int,
        loop_id: str,
        kind: str,
        *,
        max_contacts: Optional[int] = None,
        allow_same_day: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            ident = self.contact_id(patient_id, day_index)
            if ident in self.contacts and not allow_same_day:
                return {"ok": False, "why": NO_DAY_LEFT}
            loop = self.loops.get(loop_id)
            if loop is None:
                raise self._missing("loop", loop_id)
            contacts = int(loop.contacts or 0)
            if max_contacts is not None and contacts >= max_contacts:
                return {
                    "ok": False,
                    "why": NO_CONTACTS_LEFT,
                    "contacts": contacts,
                    "limit": max_contacts,
                }
            count = self._note_contact_locked(
                patient_id, doctor_id, day_index, kind, loop_id
            )
            days = list(loop.contact_days or [])
            if day_index not in days:
                days.append(day_index)
            self.loops[loop_id] = _updated(
                loop,
                {
                    "contacts": contacts + 1,
                    "contact_days": days,
                    "updated_at": self.now(),
                },
            )
            return {"ok": True, "count": count, "contacts": contacts + 1}

    async def refund_day(
        self, patient_id: str, day_index: int, loop_id: str = ""
    ) -> int:
        async with self._lock:
            ident = self.contact_id(patient_id, day_index)
            row = self.contacts.get(ident)
            if row is None:
                return 0
            updated = _clone(row)
            count = max(0, int(updated.get("count") or 0) - 1)
            if count == 0:
                del self.contacts[ident]
                return 0
            updated["count"] = count
            updated["loops"] = [
                value
                for value in updated.get("loops", [])
                if isinstance(value, str) and value != loop_id
            ]
            updated["last_at"] = self.now()
            self.contacts[ident] = updated
            return count

    async def add_contact_kind(
        self, patient_id: str, day_index: int, kind: str
    ) -> None:
        if not kind:
            return
        async with self._lock:
            ident = self.contact_id(patient_id, day_index)
            row = self.contacts.get(ident)
            if row is None:
                raise self._missing("contact", ident)
            updated = _clone(row)
            if kind not in updated["kinds"]:
                updated["kinds"].append(kind)
            self.contacts[ident] = updated

    async def contacted_on(self, patient_id: str, day_index: int) -> bool:
        async with self._lock:
            return self.contact_id(patient_id, day_index) in self.contacts

    async def contact_days_for_patient(self, patient_id: str) -> tuple[int, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    int(row.get("day_index") or 0)
                    for row in self.contacts.values()
                    if row.get("patient_id") == patient_id
                )
            )

    # ------------------------------------------------------------------
    # Settings, events, card claims, and action claims
    # ------------------------------------------------------------------
    async def get_settings(self) -> dict[str, Any]:
        async with self._lock:
            return _clone(self.settings)

    async def set_settings(self, **fields: Any) -> dict[str, Any]:
        async with self._lock:
            self.settings.update(
                {key: _clone(value) for key, value in fields.items() if value is not None}
            )
            return _clone(self.settings)

    async def add_event(self, event: Event) -> Event:
        async with self._lock:
            self.events[event.id] = _clone(event)
            return _clone(event)

    async def list_events(self, doctor_id: str) -> list[Event]:
        async with self._lock:
            rows = [
                _clone(row)
                for row in self.events.values()
                if row.doctor_id == doctor_id
            ]
            return sorted(rows, key=lambda row: row.ts)

    async def get_event(self, event_id: str) -> Optional[Event]:
        async with self._lock:
            row = self.events.get(event_id)
            return _clone(row) if row is not None else None

    async def update_event(self, event_id: str, **fields: Any) -> None:
        async with self._lock:
            row = self.events.get(event_id)
            if row is None:
                raise self._missing("event", event_id)
            self.events[event_id] = _updated(row, fields)

    async def claim_card_action(
        self, event_id: str, action_id: str, at: datetime
    ) -> bool:
        async with self._lock:
            event = self.events.get(event_id)
            if event is None:
                return False
            meta = _clone(event.meta or {})
            card = _clone(meta.get("card") or {})
            if card.get("resolved"):
                return False
            if card.get("claimed_by"):
                taken = _claimed_at(card)
                if taken is None or _aware(at) - taken <= CLAIM_LEASE:
                    return False
            card["claimed_by"] = action_id
            card["claimed_at"] = at.isoformat()
            meta["card"] = card
            self.events[event_id] = _updated(event, {"meta": meta})
            return True

    async def release_card_action(self, event_id: str) -> None:
        async with self._lock:
            event = self.events.get(event_id)
            if event is None:
                return
            meta = _clone(event.meta or {})
            card = _clone(meta.get("card") or {})
            card.pop("claimed_by", None)
            card.pop("claimed_at", None)
            meta["card"] = card
            self.events[event_id] = _updated(event, {"meta": meta})

    async def claim_action(self, doctor_id: str, action_id: str) -> bool:
        async with self._lock:
            ident = f"{doctor_id}:{action_id}"
            if ident in self.card_actions:
                return False
            self.card_actions[ident] = {
                "doctor_id": doctor_id,
                "action_id": action_id,
                "at": self.now(),
            }
            return True

    async def release_action(self, doctor_id: str, action_id: str) -> None:
        async with self._lock:
            self.card_actions.pop(f"{doctor_id}:{action_id}", None)

    async def claim_command_receipt(
        self, receipt_id: str, fingerprint: str, at: datetime
    ) -> dict[str, Any]:
        async with self._lock:
            row = self.command_receipts.get(receipt_id)
            if row is None:
                self.command_receipts[receipt_id] = {
                    "fingerprint": fingerprint,
                    "state": "IN_FLIGHT",
                    "created_at": at,
                    "updated_at": at,
                }
                return {"state": "CLAIMED"}
            if row.get("fingerprint") != fingerprint:
                return {"state": "MISMATCH"}
            state = row.get("state")
            if state == "COMPLETED":
                return {
                    "state": "COMPLETED",
                    "result": _clone(row.get("result")),
                }
            if state == "IN_FLIGHT":
                return {"state": "IN_FLIGHT"}
            raise ValueError(f"unknown command receipt state: {state!r}")

    async def get_command_receipt(
        self, receipt_id: str
    ) -> Optional[dict[str, Any]]:
        async with self._lock:
            row = self.command_receipts.get(receipt_id)
            return _clone(row) if row is not None else None

    async def complete_command_receipt(
        self,
        receipt_id: str,
        fingerprint: str,
        result: dict[str, Any],
        at: datetime,
    ) -> None:
        async with self._lock:
            row = self.command_receipts.get(receipt_id)
            if row is None:
                raise ValueError("cannot complete a missing command receipt")
            if row.get("fingerprint") != fingerprint:
                raise ValueError("command receipt fingerprint mismatch")
            state = row.get("state")
            if state == "COMPLETED":
                if row.get("result") != result:
                    raise ValueError("command receipt completion result mismatch")
                return
            if state != "IN_FLIGHT":
                raise ValueError(f"unknown command receipt state: {state!r}")
            self.command_receipts[receipt_id] = {
                **row,
                "state": "COMPLETED",
                "result": _clone(result),
                "completed_at": at,
                "updated_at": at,
            }

    async def release_command_receipt(
        self, receipt_id: str, fingerprint: str
    ) -> None:
        async with self._lock:
            row = self.command_receipts.get(receipt_id)
            if row is None:
                return
            if row.get("fingerprint") != fingerprint:
                raise ValueError("command receipt fingerprint mismatch")
            state = row.get("state")
            if state == "IN_FLIGHT":
                del self.command_receipts[receipt_id]
                return
            if state != "COMPLETED":
                raise ValueError(f"unknown command receipt state: {state!r}")

    async def reclaim_stale(
        self, at: Optional[datetime] = None
    ) -> dict[str, int]:
        async with self._lock:
            stamp = _aware(at) if at is not None else self.now()
            freed = {"sends": 0, "pending_confirms": 0}
            for ident, send in list(self.sends.items()):
                taken = _claimed_at(send)
                if (
                    send.state == CLAIMED
                    and taken is not None
                    and stamp - taken > CLAIM_LEASE
                ):
                    del self.sends[ident]
                    freed["sends"] += 1
            for ident, confirm in list(self.confirms.items()):
                claim = self.confirm_claims.get(ident, {})
                taken = _claimed_at(claim)
                if (
                    confirm.state == COMMITTING
                    and taken is not None
                    and stamp - taken > CLAIM_LEASE
                ):
                    self.confirms[ident] = _updated(
                        confirm, {"state": PENDING}
                    )
                    self.confirm_claims[ident] = {**claim, "reclaimed": True}
                    freed["pending_confirms"] += 1
            return freed

    # ------------------------------------------------------------------
    # Reports, confirms, links, relays, and pending starts
    # ------------------------------------------------------------------
    async def save_report(self, report: Report) -> Report:
        async with self._lock:
            self.reports[report.id] = _clone(report)
            return _clone(report)

    async def list_reports(self, doctor_id: str) -> list[Report]:
        async with self._lock:
            rows = [
                _clone(row)
                for row in self.reports.values()
                if row.doctor_id == doctor_id
            ]
            return sorted(rows, key=lambda row: row.created_at, reverse=True)

    async def save_confirm(self, confirm: PendingConfirm) -> PendingConfirm:
        async with self._lock:
            self.confirms[confirm.id] = _clone(confirm)
            self.confirm_claims.pop(confirm.id, None)
            return _clone(confirm)

    async def get_confirm(self, confirm_id: str) -> Optional[PendingConfirm]:
        async with self._lock:
            row = self.confirms.get(confirm_id)
            return _clone(row) if row is not None else None

    async def delete_confirm(self, confirm_id: str) -> None:
        async with self._lock:
            self.confirms.pop(confirm_id, None)
            self.confirm_claims.pop(confirm_id, None)

    async def claim_confirm(self, confirm_id: str, owner: str = "") -> bool:
        async with self._lock:
            confirm = self.confirms.get(confirm_id)
            if confirm is None:
                return False
            claim = self.confirm_claims.get(confirm_id, {})
            stamp = self.now()
            taken = _claimed_at(claim)
            if (
                confirm.state != PENDING
                and (taken is None or stamp - taken <= CLAIM_LEASE)
            ):
                return False
            self.confirms[confirm_id] = _updated(
                confirm, {"state": COMMITTING}
            )
            self.confirm_claims[confirm_id] = {
                "claimed_at": stamp,
                "claimed_by": owner or self.new_id(),
            }
            return True

    async def release_confirm(self, confirm_id: str) -> None:
        async with self._lock:
            confirm = self.confirms.get(confirm_id)
            if confirm is None:
                return
            self.confirms[confirm_id] = _updated(confirm, {"state": PENDING})
            self.confirm_claims.pop(confirm_id, None)

    async def save_link_token(self, token: LinkToken) -> LinkToken:
        async with self._lock:
            self.link_tokens[token.id] = _clone(token)
            return _clone(token)

    async def get_link_token(self, token_id: str) -> Optional[LinkToken]:
        async with self._lock:
            row = self.link_tokens.get(token_id)
            return _clone(row) if row is not None else None

    async def list_link_tokens(self, doctor_id: str) -> list[LinkToken]:
        async with self._lock:
            rows = [
                _clone(row)
                for row in self.link_tokens.values()
                if row.doctor_id == doctor_id
            ]
            return sorted(rows, key=lambda row: row.created_at)

    async def latest_link_token(self, doctor_id: str) -> Optional[LinkToken]:
        rows = await self.list_link_tokens(doctor_id)
        return rows[-1] if rows else None

    async def burn_link_token(self, token_id: str) -> None:
        async with self._lock:
            token = self.link_tokens.get(token_id)
            if token is None:
                raise self._missing("link token", token_id)
            self.link_tokens[token_id] = _updated(token, {"used": True})

    async def consume_link_token(self, token_id: str) -> Optional[LinkToken]:
        async with self._lock:
            token = self.link_tokens.get(token_id)
            if token is None or token.used or token.revoked:
                return None
            answer = _clone(token)
            self.link_tokens[token_id] = _updated(token, {"used": True})
            return answer

    async def revoke_link_tokens(self, doctor_id: str) -> int:
        async with self._lock:
            count = 0
            for ident, token in list(self.link_tokens.items()):
                if token.doctor_id != doctor_id or token.revoked:
                    continue
                self.link_tokens[ident] = _updated(token, {"revoked": True})
                count += 1
            return count

    async def save_relay(self, relay: Relay) -> Relay:
        async with self._lock:
            self.relays[relay.id] = _clone(relay)
            return _clone(relay)

    async def get_relay(self, relay_id: str) -> Optional[Relay]:
        async with self._lock:
            row = self.relays.get(relay_id)
            return _clone(row) if row is not None else None

    async def open_relays(self, doctor_id: str) -> list[Relay]:
        async with self._lock:
            rows = [
                _clone(row)
                for row in self.relays.values()
                if row.doctor_id == doctor_id and row.state == "open"
            ]
            return sorted(rows, key=lambda row: row.created_at)

    async def close_relay(self, relay_id: str) -> None:
        async with self._lock:
            relay = self.relays.get(relay_id)
            if relay is None:
                raise self._missing("relay", relay_id)
            self.relays[relay_id] = _updated(relay, {"state": "answered"})

    async def save_pending_start(self, start: PendingStart) -> PendingStart:
        async with self._lock:
            self.pending_starts[start.id] = _clone(start)
            return _clone(start)

    async def list_pending_starts(self) -> list[PendingStart]:
        async with self._lock:
            return sorted(
                (_clone(row) for row in self.pending_starts.values()),
                key=lambda row: row.created_at,
                reverse=True,
            )

    async def latest_pending_start(self) -> Optional[PendingStart]:
        rows = await self.list_pending_starts()
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Doctor-scoped reset
    # ------------------------------------------------------------------
    async def wipe_doctor(self, doctor_id: str) -> dict[str, int]:
        async with self._lock:
            doctor = self.doctors.get(doctor_id)
            patient_ids = {
                row.id for row in self.patients.values() if row.doctor_id == doctor_id
            }
            confirm_ids = {
                row.id for row in self.confirms.values() if row.doctor_id == doctor_id
            }
            deleted: dict[str, int] = {}

            def remove(mapping: dict[str, Any], belongs: Any) -> int:
                count = 0
                for ident, row in list(mapping.items()):
                    if belongs(row):
                        del mapping[ident]
                        count += 1
                return count

            deleted["patients"] = remove(
                self.patients, lambda row: row.doctor_id == doctor_id
            )
            deleted["loops"] = remove(
                self.loops, lambda row: row.doctor_id == doctor_id
            )
            deleted["events"] = remove(
                self.events, lambda row: row.doctor_id == doctor_id
            )
            deleted["pending_confirms"] = remove(
                self.confirms, lambda row: row.doctor_id == doctor_id
            )
            for ident in confirm_ids:
                self.confirm_claims.pop(ident, None)
            deleted["link_tokens"] = remove(
                self.link_tokens, lambda row: row.doctor_id == doctor_id
            )
            deleted["relays"] = remove(
                self.relays, lambda row: row.doctor_id == doctor_id
            )
            deleted["sends"] = remove(
                self.sends, lambda row: row.doctor_id == doctor_id
            )
            deleted["reports"] = remove(
                self.reports, lambda row: row.doctor_id == doctor_id
            )
            deleted["contacts"] = remove(
                self.contacts, lambda row: row.get("doctor_id") == doctor_id
            )
            deleted["card_actions"] = remove(
                self.card_actions, lambda row: row.get("doctor_id") == doctor_id
            )
            deleted["patient_turns"] = remove(
                self.patient_turns,
                lambda row: row.get("patient_id") in patient_ids,
            )
            deleted["photo_receipts"] = remove(
                self.photo_receipts,
                lambda row: row.get("patient_id") in patient_ids,
            )
            chat_id = doctor.telegram_chat_id if doctor is not None else None
            deleted["tg_pending_starts"] = (
                remove(
                    self.pending_starts,
                    lambda row: chat_id is not None and row.chat_id == chat_id,
                )
                if chat_id is not None
                else 0
            )
            return deleted


_FUNCTION_NAMES = (
    "db",
    "now",
    "new_id",
    "derived_id",
    "new_web_token",
    "doctor_by_name",
    "doctor_by_id",
    "doctor_by_telegram",
    "doctor_by_token",
    "create_doctor",
    "update_doctor",
    "create_patient",
    "get_patient",
    "list_patients",
    "patient_by_telegram",
    "patients_by_telegram",
    "update_patient",
    "claim_opt_out",
    "doctor_chat_bindings",
    "create_loop",
    "get_loop",
    "list_loops",
    "update_loop",
    "claim_patient_turn",
    "release_patient_turn",
    "photo_claim_id",
    "claim_photo",
    "complete_photo",
    "release_photo",
    "claim_send",
    "channels_done",
    "mark_channel_done",
    "mark_send",
    "send_state",
    "sends_for_patient",
    "release_send",
    "contact_id",
    "note_contact",
    "reserve_contact",
    "refund_day",
    "add_contact_kind",
    "contacted_on",
    "contact_days_for_patient",
    "add_contact",
    "refund_contact",
    "append_reading",
    "append_result",
    "add_evidence_request",
    "claim_delivery",
    "add_reluctance",
    "bump_generation",
    "bump_schedule_version",
    "claim_resume",
    "get_settings",
    "set_settings",
    "add_event",
    "list_events",
    "get_event",
    "update_event",
    "claim_card_action",
    "claim_action",
    "release_action",
    "claim_command_receipt",
    "get_command_receipt",
    "complete_command_receipt",
    "release_command_receipt",
    "reclaim_stale",
    "release_card_action",
    "save_report",
    "list_reports",
    "save_confirm",
    "get_confirm",
    "delete_confirm",
    "claim_confirm",
    "release_confirm",
    "save_link_token",
    "get_link_token",
    "list_link_tokens",
    "latest_link_token",
    "burn_link_token",
    "consume_link_token",
    "revoke_link_tokens",
    "save_relay",
    "get_relay",
    "open_relays",
    "close_relay",
    "save_pending_start",
    "list_pending_starts",
    "latest_pending_start",
    "wipe_doctor",
    "claim_expired",
)

_CONSTANTS = {
    "PROJECT": MemoryStore.PROJECT,
    "CLAIMED": CLAIMED,
    "ALREADY_SENT": ALREADY_SENT,
    "RESEND": RESEND,
    "MAX_RESENDS": MAX_RESENDS,
    "CLAIM_LEASE": CLAIM_LEASE,
    "PATIENT_TURN_LEASE": PATIENT_TURN_LEASE,
    "PENDING": PENDING,
    "COMMITTING": COMMITTING,
    "LADDER": LADDER,
    "COORDINATOR": COORDINATOR,
    "RELUCTANCE": RELUCTANCE,
    "INTENT": INTENT,
    "NO_DAY_LEFT": NO_DAY_LEFT,
    "NO_CONTACTS_LEFT": NO_CONTACTS_LEFT,
    "RESET_COLLECTIONS": RESET_COLLECTIONS,
}


@contextmanager
def patched_store(memory: Optional[MemoryStore] = None) -> Iterator[MemoryStore]:
    """Install ``memory`` on ``core.store`` and restore every binding on exit."""

    from core import store as store_module

    instance = memory or MemoryStore()
    replacements = {
        name: getattr(instance, name) for name in _FUNCTION_NAMES
    }
    replacements.update(_CONSTANTS)
    previous = {name: getattr(store_module, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(store_module, name, value)
        yield instance
    finally:
        for name, value in previous.items():
            setattr(store_module, name, value)


# Two readable aliases for callers that prefer a noun or an imperative.
memory_store = patched_store
install = patched_store


__all__ = [
    "DEFAULT_NOW",
    "DEFAULT_TICK",
    "MemoryStore",
    "MissingRecordError",
    "install",
    "memory_store",
    "patched_store",
]
