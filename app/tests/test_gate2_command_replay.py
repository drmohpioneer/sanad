"""Hermetic contracts for the durable Gate 2 command replay adapter."""

from __future__ import annotations

import asyncio
import copy
import re
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from core import channel_contracts, command_replay, store
from core.command_bus import CommandBus
from tests.gate0b.memory import MemoryStore, patched_store


NOW = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)


def _actor(kind: str, ident: str) -> channel_contracts.ActorRef:
    return channel_contracts.ActorRef(kind=kind, id=ident)


def _envelope(tenant_id: str) -> channel_contracts.InboundEnvelope:
    return channel_contracts.InboundEnvelope(
        provider="telegram",
        provider_account="sanad-bot",
        provider_message_id="provider-message-1",
        received_at=NOW,
        signature_verdict=channel_contracts.SignatureVerdict.VERIFIED,
        tenant_id=tenant_id,
        actor=_actor("patient", "patient-internal"),
        principal=_actor("doctor", tenant_id),
        endpoint_id="external-chat-7700",
        thread_id="external-thread-8800",
        text="review",
        raw_payload_ref="private:update:provider-message-1",
        synthetic=False,
        transient_payload={
            "provider_secret": "never-persist-provider-secret",
            "attachment_bytes": b"never-persist-attachment-bytes",
        },
    )


def _command(
    *,
    key: str = "provider-message-secret-key",
    kind: str = "LEGACY_ACTION",
    tenant_id: str = "doctor-internal",
    payload: dict[str, Any] | None = None,
    created_at: datetime = NOW,
) -> channel_contracts.Command:
    return channel_contracts.Command(
        id="command-request-1",
        idempotency_key=key,
        kind=kind,
        tenant_id=tenant_id,
        actor=_actor("patient", "patient-internal"),
        principal=_actor("doctor", tenant_id),
        source="telegram",
        endpoint_id="external-chat-7700",
        thread_id="external-thread-8800",
        payload=payload
        if payload is not None
        else {
            "raw_payload": {
                "provider_secret": "never-persist-provider-secret",
                "external_chat_id": "external-chat-7700",
            },
            "attachment_bytes": b"never-persist-attachment-bytes",
            "domain": {"patient_id": "patient-internal", "action": "review"},
        },
        created_at=created_at,
        synthetic=False,
        envelope=_envelope(tenant_id),
    )


async def _allow(_: channel_contracts.Command) -> None:
    return None


class DurableReplayMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.memory = MemoryStore(start=NOW)
        self.store_patch = patched_store(self.memory)
        self.store_patch.__enter__()
        self.addCleanup(self.store_patch.__exit__, None, None, None)
        self.ledger = command_replay.DurableReplayLedger()

    def test_receipt_id_and_fingerprint_are_deterministic_opaque_hashes(self) -> None:
        command = _command()
        reconstructed = command.model_copy(
            update={
                "id": "another-request-object",
                "created_at": NOW + timedelta(minutes=5),
            }
        )

        receipt_id = command_replay.command_receipt_id(command)
        fingerprint = command_replay.command_fingerprint(command)

        self.assertRegex(receipt_id, re.compile(r"^[0-9a-f]{32}$"))
        self.assertRegex(fingerprint, re.compile(r"^[0-9a-f]{64}$"))
        self.assertNotIn(command.idempotency_key, receipt_id)
        self.assertEqual(
            receipt_id,
            command_replay.command_receipt_id(reconstructed),
        )
        self.assertEqual(
            fingerprint,
            command_replay.command_fingerprint(reconstructed),
        )
        self.assertNotEqual(
            receipt_id,
            command_replay.command_receipt_id(
                _command(tenant_id="another-doctor")
            ),
        )
        self.assertNotEqual(
            receipt_id,
            command_replay.command_receipt_id(_command(kind="OTHER_ACTION")),
        )
        self.assertNotEqual(
            fingerprint,
            command_replay.command_fingerprint(
                _command(payload={"domain": {"action": "different"}})
            ),
        )

    async def test_first_claim_persists_only_fingerprint_state_and_timestamps(
        self,
    ) -> None:
        command = _command()

        claim = await self.ledger.claim(command)
        receipt_id = command_replay.command_receipt_id(command)
        row = await store.get_command_receipt(receipt_id)

        self.assertEqual("CLAIMED", claim.state)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(
            {"fingerprint", "state", "created_at", "updated_at"},
            set(row),
        )
        self.assertEqual("IN_FLIGHT", row["state"])
        self.assertEqual(command_replay.command_fingerprint(command), row["fingerprint"])
        rendered = repr(row)
        for forbidden in (
            command.idempotency_key,
            "never-persist-provider-secret",
            "external-chat-7700",
            "external-thread-8800",
            "never-persist-attachment-bytes",
            "raw_payload",
        ):
            self.assertNotIn(forbidden, rendered)

    async def test_same_key_with_different_content_is_a_typed_conflict(self) -> None:
        original = _command(payload={"domain": {"action": "review"}})
        mismatch = _command(payload={"domain": {"action": "cancel"}})
        await self.ledger.claim(original)

        claim = await self.ledger.claim(mismatch)

        self.assertEqual("IN_FLIGHT", claim.state)
        self.assertIsNotNone(claim.result)
        assert claim.result is not None
        self.assertEqual(channel_contracts.CommandStatus.CONFLICT, claim.result.status)
        self.assertEqual("idempotency_mismatch", claim.result.code)

    async def test_bus_surfaces_mismatch_without_reexecuting_completed_work(
        self,
    ) -> None:
        calls = 0

        async def handler(
            _: channel_contracts.Command,
        ) -> channel_contracts.CommandResult:
            nonlocal calls
            calls += 1
            return channel_contracts.CommandResult.accepted("done")

        bus = CommandBus(
            handlers={"LEGACY_ACTION": handler},
            authorizer=_allow,
            replay=self.ledger,
        )
        original = _command(payload={"domain": {"action": "review"}})
        mismatch = _command(payload={"domain": {"action": "cancel"}})

        accepted = await bus.execute(original)
        conflict = await bus.execute(mismatch)

        self.assertEqual(channel_contracts.CommandStatus.ACCEPTED, accepted.status)
        self.assertEqual(channel_contracts.CommandStatus.CONFLICT, conflict.status)
        self.assertEqual("idempotency_mismatch", conflict.code)
        self.assertEqual(1, calls)

    async def test_completed_duplicate_returns_the_stored_typed_result(self) -> None:
        calls = 0

        async def handler(
            _: channel_contracts.Command,
        ) -> channel_contracts.CommandResult:
            nonlocal calls
            calls += 1
            return channel_contracts.CommandResult.accepted(
                "reviewed", patient_id="patient-internal"
            )

        bus = CommandBus(
            handlers={"LEGACY_ACTION": handler},
            authorizer=_allow,
            replay=self.ledger,
        )
        command = _command(payload={"domain": {"action": "review"}})

        first = await bus.execute(command)
        duplicate = await bus.execute(
            command.model_copy(
                update={
                    "id": "reconstructed-request",
                    "created_at": NOW + timedelta(minutes=1),
                }
            )
        )

        self.assertEqual(first, duplicate)
        self.assertEqual("reviewed", duplicate.code)
        self.assertEqual(1, calls)
        row = await store.get_command_receipt(
            command_replay.command_receipt_id(command)
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("COMPLETED", row["state"])
        self.assertEqual(first.model_dump(mode="json"), row["result"])

    async def test_retryable_result_releases_then_allows_one_new_execution(self) -> None:
        calls = 0

        async def handler(
            _: channel_contracts.Command,
        ) -> channel_contracts.CommandResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return channel_contracts.CommandResult.retryable("try_again")
            return channel_contracts.CommandResult.accepted("done")

        bus = CommandBus(
            handlers={"LEGACY_ACTION": handler},
            authorizer=_allow,
            replay=self.ledger,
        )
        command = _command(payload={"domain": {"action": "review"}})
        receipt_id = command_replay.command_receipt_id(command)

        first = await bus.execute(command)
        self.assertEqual(channel_contracts.CommandStatus.RETRYABLE, first.status)
        self.assertIsNone(await store.get_command_receipt(receipt_id))

        second = await bus.execute(command)
        self.assertEqual(channel_contracts.CommandStatus.ACCEPTED, second.status)
        self.assertEqual(2, calls)
        self.assertEqual(
            "COMPLETED", (await store.get_command_receipt(receipt_id) or {})["state"]
        )

    async def test_ambiguous_handler_failure_remains_in_flight(self) -> None:
        calls = 0

        async def handler(
            _: channel_contracts.Command,
        ) -> channel_contracts.CommandResult:
            nonlocal calls
            calls += 1
            raise RuntimeError("effect outcome unknown")

        bus = CommandBus(
            handlers={"LEGACY_ACTION": handler},
            authorizer=_allow,
            replay=self.ledger,
        )
        command = _command(payload={"domain": {"action": "review"}})
        receipt_id = command_replay.command_receipt_id(command)

        with self.assertRaisesRegex(RuntimeError, "effect outcome unknown"):
            await bus.execute(command)
        duplicate = await bus.execute(command)

        self.assertEqual(channel_contracts.CommandStatus.CONFLICT, duplicate.status)
        self.assertEqual(1, calls)
        self.assertEqual(
            "IN_FLIGHT", (await store.get_command_receipt(receipt_id) or {})["state"]
        )

    async def test_transport_secret_in_result_is_refused_without_persisting_it(
        self,
    ) -> None:
        async def handler(
            _: channel_contracts.Command,
        ) -> channel_contracts.CommandResult:
            return channel_contracts.CommandResult.accepted(
                "unsafe", provider_secret="must-not-persist"
            )

        bus = CommandBus(
            handlers={"LEGACY_ACTION": handler},
            authorizer=_allow,
            replay=self.ledger,
        )
        command = _command(payload={"domain": {"action": "review"}})
        receipt_id = command_replay.command_receipt_id(command)

        with self.assertRaisesRegex(ValueError, "forbidden transport field"):
            await bus.execute(command)

        row = await store.get_command_receipt(receipt_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("IN_FLIGHT", row["state"])
        self.assertNotIn("must-not-persist", repr(row))


class _Snapshot:
    def __init__(self, body: dict[str, Any] | None) -> None:
        self.exists = body is not None
        self._body = copy.deepcopy(body)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._body or {})


class _Document:
    def __init__(self, database: "_Firestore", collection: str, ident: str) -> None:
        self.database = database
        self.collection = collection
        self.ident = ident

    async def get(self, transaction: Any = None) -> _Snapshot:
        body = self.database.tables.setdefault(self.collection, {}).get(self.ident)
        return _Snapshot(body)


class _Collection:
    def __init__(self, database: "_Firestore", name: str) -> None:
        self.database = database
        self.name = name

    def document(self, ident: str) -> _Document:
        return _Document(self.database, self.name, ident)


class _Transaction:
    def __init__(self, database: "_Firestore") -> None:
        self.database = database

    def set(self, ref: _Document, body: dict[str, Any]) -> None:
        self.database.tables.setdefault(ref.collection, {})[ref.ident] = copy.deepcopy(
            body
        )

    def update(self, ref: _Document, fields: dict[str, Any]) -> None:
        rows = self.database.tables.setdefault(ref.collection, {})
        if ref.ident not in rows:
            raise AssertionError(f"updated missing test document: {ref.ident}")
        rows[ref.ident].update(copy.deepcopy(fields))

    def delete(self, ref: _Document) -> None:
        self.database.tables.setdefault(ref.collection, {}).pop(ref.ident, None)


class _Firestore:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {}
        self.lock = asyncio.Lock()

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def transaction(self) -> _Transaction:
        return _Transaction(self)


def _transactional(function: Any) -> Any:
    async def run(transaction: _Transaction) -> Any:
        async with transaction.database.lock:
            return await function(transaction)

    return run


class CommandReceiptStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = _Firestore()
        self.enterContext(patch.object(store, "db", return_value=self.database))
        self.enterContext(
            patch.object(store.firestore, "async_transactional", _transactional)
        )

    async def test_atomic_claim_complete_read_and_retryable_release(self) -> None:
        receipt_id = store.derived_id(
            "command-receipt", "doctor-internal", "LEGACY_ACTION", "opaque-key"
        )
        fingerprint = "a" * 64
        result = {
            "status": "ACCEPTED",
            "code": "done",
            "detail": "",
            "value": {"patient_id": "patient-internal"},
        }

        first, duplicate = await asyncio.gather(
            store.claim_command_receipt(receipt_id, fingerprint, NOW),
            store.claim_command_receipt(receipt_id, fingerprint, NOW),
        )
        self.assertEqual({"CLAIMED", "IN_FLIGHT"}, {first["state"], duplicate["state"]})
        self.assertEqual(
            {
                "fingerprint": fingerprint,
                "state": "IN_FLIGHT",
                "created_at": NOW,
                "updated_at": NOW,
            },
            await store.get_command_receipt(receipt_id),
        )

        mismatch = await store.claim_command_receipt(receipt_id, "b" * 64, NOW)
        self.assertEqual({"state": "MISMATCH"}, mismatch)

        completed_at = NOW + timedelta(seconds=1)
        await store.complete_command_receipt(
            receipt_id, fingerprint, result, completed_at
        )
        completed = await store.claim_command_receipt(
            receipt_id, fingerprint, completed_at
        )
        self.assertEqual("COMPLETED", completed["state"])
        self.assertEqual(result, completed["result"])
        row = await store.get_command_receipt(receipt_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(
            {
                "fingerprint",
                "state",
                "result",
                "created_at",
                "completed_at",
                "updated_at",
            },
            set(row),
        )

        await store.release_command_receipt(receipt_id, fingerprint)
        self.assertEqual("COMPLETED", (await store.get_command_receipt(receipt_id) or {})["state"])

        retry_id = store.derived_id(
            "command-receipt", "doctor-internal", "RETRYABLE", "opaque-key"
        )
        self.assertEqual(
            "CLAIMED",
            (await store.claim_command_receipt(retry_id, fingerprint, NOW))["state"],
        )
        await store.release_command_receipt(retry_id, fingerprint)
        self.assertIsNone(await store.get_command_receipt(retry_id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
