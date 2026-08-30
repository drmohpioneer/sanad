"""Hermetic Gate 2 tests for the durable shadow-outbox store boundary.

The in-memory double below implements only Firestore-shaped collection,
document, query, and atomic ``create`` behavior.  Idempotency and conflict
decisions remain in ``core.store`` so these tests cannot pass by teaching the
double the production policy they are meant to verify.
"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from unittest.mock import patch

from core import channel_contracts, store


NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


class _AlreadyExists(Exception):
    """The only Firestore error this persistence boundary needs."""


class _Snapshot:
    def __init__(
        self,
        database: "_MemoryFirestore",
        collection: str,
        ident: str,
        body: dict[str, Any] | None,
    ) -> None:
        self.id = ident
        self.exists = body is not None
        self._body = copy.deepcopy(body)
        self.reference = _Document(database, collection, ident)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._body or {})


class _Document:
    def __init__(
        self, database: "_MemoryFirestore", collection: str, ident: str
    ) -> None:
        self.database = database
        self.collection = collection
        self.ident = ident

    async def create(self, body: dict[str, Any]) -> None:
        self.database.create_calls.append((self.collection, self.ident))
        rows = self.database.tables.setdefault(self.collection, {})
        if self.ident in rows:
            raise _AlreadyExists(self.ident)
        rows[self.ident] = copy.deepcopy(body)

    async def get(self) -> _Snapshot:
        body = self.database.tables.setdefault(self.collection, {}).get(self.ident)
        return _Snapshot(self.database, self.collection, self.ident, body)

    async def delete(self) -> None:
        self.database.delete_calls.append((self.collection, self.ident))
        self.database.tables.setdefault(self.collection, {}).pop(self.ident, None)


def _field(body: dict[str, Any], path: str) -> Any:
    value: Any = body
    for segment in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(segment)
    return value


class _Query:
    def __init__(
        self,
        database: "_MemoryFirestore",
        collection: str,
        field_path: str,
        operator: str,
        expected: Any,
    ) -> None:
        if operator != "==":
            raise AssertionError(f"unsupported test query operator: {operator}")
        self.database = database
        self.collection = collection
        self.field_path = field_path
        self.expected = expected

    async def stream(self) -> AsyncIterator[_Snapshot]:
        rows = self.database.tables.setdefault(self.collection, {})
        for ident, body in list(rows.items()):
            if _field(body, self.field_path) == self.expected:
                yield _Snapshot(self.database, self.collection, ident, body)


class _Collection:
    def __init__(self, database: "_MemoryFirestore", name: str) -> None:
        self.database = database
        self.name = name

    def document(self, ident: str) -> _Document:
        return _Document(self.database, self.name, ident)

    def where(self, *, filter: Any) -> _Query:
        return _Query(
            self.database,
            self.name,
            filter.field_path,
            filter.op_string,
            filter.value,
        )

    async def stream(self) -> AsyncIterator[_Snapshot]:
        rows = self.database.tables.setdefault(self.name, {})
        for ident, body in list(rows.items()):
            yield _Snapshot(self.database, self.name, ident, body)


class _MemoryFirestore:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {}
        self.create_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def seed(self, collection: str, ident: str, body: dict[str, Any]) -> None:
        self.tables.setdefault(collection, {})[ident] = copy.deepcopy(body)


def _record(
    ident: str,
    *,
    doctor_id: str = "doctor-a",
    created_at: datetime = NOW,
    text: str = "legacy message",
) -> channel_contracts.OutboxRecord:
    intent = channel_contracts.OutboundIntent(
        id=ident,
        synthetic=True,
        doctor_id=doctor_id,
        recipient_type="doctor",
        recipient_id=doctor_id,
        notification_class=channel_contracts.NotificationClass.LEGACY_UNCLASSIFIED,
        text=text,
        idempotency_key=f"legacy:{ident}",
        stable_idempotency=True,
        created_at=created_at,
    )
    return channel_contracts.OutboxRecord(
        id=ident,
        doctor_id=doctor_id,
        intent=intent,
        content_hash=channel_contracts.outbound_content_hash(intent),
        created_at=created_at,
        updated_at=created_at,
    )


class OutboundIntentStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = _MemoryFirestore()
        self.enterContext(patch.object(store, "db", return_value=self.database))
        self.enterContext(
            patch.object(store.gexc, "AlreadyExists", _AlreadyExists)
        )

    def test_record_rejects_an_outer_id_that_does_not_match_the_intent(self) -> None:
        body = _record("intent-owned").model_dump(mode="python")
        body["id"] = "different-outer-id"

        with self.assertRaisesRegex(
            ValueError, "outbox record id must equal intent id"
        ):
            channel_contracts.OutboxRecord(**body)

    def test_record_rejects_an_outer_doctor_that_does_not_match_the_intent(
        self,
    ) -> None:
        body = _record("intent-owned", doctor_id="doctor-a").model_dump(
            mode="python"
        )
        body["doctor_id"] = "doctor-b"

        with self.assertRaisesRegex(
            ValueError, "outbox record doctor_id must equal intent doctor_id"
        ):
            channel_contracts.OutboxRecord(**body)

    def test_record_rejects_a_hash_that_does_not_match_the_intent(self) -> None:
        body = _record("intent-owned").model_dump(mode="python")
        body["content_hash"] = "0" * 64

        with self.assertRaisesRegex(
            ValueError, "outbox content_hash does not match intent content"
        ):
            channel_contracts.OutboxRecord(**body)

    async def test_create_is_atomic_create_if_absent(self) -> None:
        record = _record("intent-new")

        created = await store.create_outbound_intent(record)

        self.assertTrue(created)
        self.assertEqual(
            self.database.create_calls,
            [("outbound_intents", "intent-new")],
        )
        expected = record.model_dump(mode="python")
        expected.pop("id")
        self.assertEqual(
            self.database.tables["outbound_intents"]["intent-new"], expected
        )

    async def test_identical_stable_replay_is_a_noop(self) -> None:
        first = _record("intent-stable")
        later = NOW + timedelta(minutes=5)
        replay = first.model_copy(
            update={
                "intent": first.intent.model_copy(update={"created_at": later}),
                "created_at": later,
                "updated_at": later,
            }
        )

        self.assertTrue(await store.create_outbound_intent(first))
        before = copy.deepcopy(self.database.tables["outbound_intents"])
        self.assertFalse(await store.create_outbound_intent(replay))

        self.assertEqual(self.database.tables["outbound_intents"], before)
        self.assertEqual(
            self.database.create_calls,
            [
                ("outbound_intents", "intent-stable"),
                ("outbound_intents", "intent-stable"),
            ],
        )

    async def test_same_id_with_a_different_hash_fails_closed(self) -> None:
        first = _record("intent-conflict", text="first payload")
        conflict = _record(
            "intent-conflict",
            text="different payload",
        )
        await store.create_outbound_intent(first)
        before = copy.deepcopy(self.database.tables["outbound_intents"])

        with self.assertRaisesRegex(
            ValueError, "outbound intent idempotency conflict: intent-conflict"
        ):
            await store.create_outbound_intent(conflict)

        self.assertEqual(self.database.tables["outbound_intents"], before)

    async def test_listing_is_doctor_scoped_then_sorted_by_time_and_id(self) -> None:
        records = (
            _record("late", created_at=NOW + timedelta(minutes=1)),
            _record("same-z", created_at=NOW),
            _record("foreign", doctor_id="doctor-b", created_at=NOW - timedelta(days=1)),
            _record("same-a", created_at=NOW),
        )
        for record in records:
            await store.create_outbound_intent(record)

        listed = await store.list_outbound_intents("doctor-a")

        self.assertEqual([record.id for record in listed], ["same-a", "same-z", "late"])
        self.assertTrue(all(record.doctor_id == "doctor-a" for record in listed))
        self.assertTrue(
            all(isinstance(record, channel_contracts.OutboxRecord) for record in listed)
        )

    async def test_doctor_wipe_preserves_the_additive_outbound_ledger(self) -> None:
        own_intent = _record("audit-a", doctor_id="doctor-a")
        other_intent = _record("audit-b", doctor_id="doctor-b")
        await store.create_outbound_intent(own_intent)
        await store.create_outbound_intent(other_intent)
        before = copy.deepcopy(self.database.tables["outbound_intents"])
        self.database.seed("events", "event-a", {"doctor_id": "doctor-a"})
        self.database.seed("events", "event-b", {"doctor_id": "doctor-b"})

        deleted = await store.wipe_doctor("doctor-a")

        self.assertEqual(deleted["events"], 1, "the wipe itself must not be a no-op")
        self.assertNotIn("event-a", self.database.tables["events"])
        self.assertIn("event-b", self.database.tables["events"])
        self.assertEqual(self.database.tables["outbound_intents"], before)
        self.assertFalse(
            any(name == "outbound_intents" for name, _ in self.database.delete_calls),
            "an admin rehearsal reset must not erase the additive audit ledger",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
