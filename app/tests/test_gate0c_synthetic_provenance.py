"""Gate 0C: synthetic provenance is explicit, strict, and fail closed.

The competition demo remains synthetic.  A provider-authenticated Telegram
message may still be distinguished from a web simulation, while evidence and
missions derived from an invented actor remain synthetic.
"""

from __future__ import annotations

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

import main as sanad_main
from core import (
    background,
    cards,
    concierge,
    contract,
    escalate,
    events,
    extractor,
    policy,
    provenance,
    registrar,
    sentinel,
    store,
    tg_router,
    views,
)
from core.adapters import InboundMessage
from core.models import (
    Doctor,
    Event,
    LinkToken,
    Loop,
    Patient,
    PendingConfirm,
    PhotoReading,
    ProposedLoop,
    ProposedPatient,
    ProposedRecord,
    SlipAnalyte,
)
from tests.gate0b.artifacts import digest as artifact_digest, legacy_projection


NOW = datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)
APP_ROOT = Path(__file__).resolve().parents[1]


def doctor(*, synthetic: bool = True) -> Doctor:
    return Doctor(
        id="d", synthetic=synthetic, name="Dr Test", specialty="cardiology",
        web_token="doctor-token", created_at=NOW,
    )


def patient(*, synthetic: bool = True) -> Patient:
    return Patient(
        id="p", synthetic=synthetic, doctor_id="d", name="Ahmed",
        diagnosis="hypertension", plan_text="Measure blood pressure.",
        created_at=NOW,
    )


def mission(*, synthetic: bool = True, kind: str = "MONITOR") -> Loop:
    details = {"metric": "BP"} if kind == "MONITOR" else {"test_name": "LDL"}
    return Loop(
        id="l", synthetic=synthetic, patient_id="p", doctor_id="d",
        type=kind, title="Blood pressure" if kind == "MONITOR" else "Lipid panel",
        details=details, created_at=NOW, updated_at=NOW,
    )


def event(*, synthetic: bool = True, kind: str = "patient_in",
          meta: dict | None = None) -> Event:
    return Event(
        id="e", synthetic=synthetic, doctor_id="d", patient_id="p",
        kind=kind, text="120/80", meta=meta or {}, ts=NOW,
    )


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send(self, target: str, message: object) -> str:
        self.sent.append((target, message))
        return f"out-{len(self.sent)}"


class RequestStub:
    def __init__(self, body: dict, secret: str = "secret") -> None:
        self._body = body
        self.headers = {"x-telegram-bot-api-secret-token": secret}
        self.base_url = "https://sanad.test/"
        self.query_params: dict[str, str] = {}

    async def json(self) -> dict:
        return self._body


class StrictTopLevelProvenance(unittest.TestCase):
    def builders(self):
        return {
            "Doctor": lambda **extra: Doctor(
                id="d", name="D", web_token="t", created_at=NOW, **extra),
            "Patient": lambda **extra: Patient(
                id="p", doctor_id="d", name="P", created_at=NOW, **extra),
            "Loop": lambda **extra: Loop(
                id="l", patient_id="p", doctor_id="d", type="TEST", title="T",
                created_at=NOW, updated_at=NOW, **extra),
            "Event": lambda **extra: Event(
                id="e", doctor_id="d", kind="patient_in", ts=NOW, **extra),
            "InboundMessage": lambda **extra: InboundMessage(
                channel="web", sender_ref="patient:p", **extra),
            "PendingConfirm": lambda **extra: PendingConfirm(
                id="c", doctor_id="d", proposed={}, expires_at=NOW, **extra),
        }

    def test_missing_legacy_fields_default_true_and_literals_survive(self) -> None:
        for name, build in self.builders().items():
            with self.subTest(model=name):
                self.assertIs(build().synthetic, True)
                self.assertIs(build(synthetic=True).synthetic, True)
                self.assertIs(build(synthetic=False).synthetic, False)

    def test_strings_integers_and_null_never_masquerade_as_booleans(self) -> None:
        for name, build in self.builders().items():
            for bad in (0, 1, "true", "false", None):
                with self.subTest(model=name, value=bad):
                    with self.assertRaises(ValidationError):
                        build(synthetic=bad)

    def test_persisted_models_write_a_real_boolean(self) -> None:
        for row in (doctor(synthetic=False), patient(synthetic=False),
                    mission(synthetic=False), event(synthetic=False)):
            body = store._write(row)
            self.assertIn("synthetic", body)
            self.assertIs(type(body["synthetic"]), bool)
            self.assertIs(body["synthetic"], False)

    def test_generic_patch_helpers_cannot_rewrite_provenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "immutable"):
            store._reject_synthetic_update({"synthetic": False})


class EmbeddedEvidenceIsExplicit(unittest.TestCase):
    def test_legacy_missing_or_malformed_evidence_fails_closed(self) -> None:
        rows = [
            {"value": "one"},
            {"value": "two", "synthetic": False},
            {"value": "three", "synthetic": "false"},
            {"value": "four", "synthetic": 0},
        ]
        loop = mission().model_copy(update={"results": rows, "readings": rows})
        loop = Loop.model_validate(loop.model_dump())
        self.assertEqual(
            [row["synthetic"] for row in loop.results],
            [True, False, True, True],
        )
        self.assertEqual(
            [row["synthetic"] for row in loop.readings],
            [True, False, True, True],
        )

        held = Patient(
            **patient().model_dump(exclude={"results"}),
            results=[{"lab": "x", "results": rows}],
        )
        self.assertIs(held.results[0]["synthetic"], True)
        self.assertEqual(
            [row["synthetic"] for row in held.results[0]["results"]],
            [True, False, True, True],
        )

        logged = Event(
            **event().model_dump(exclude={"media", "meta"}),
            media=rows,
            meta={"results": rows, "reading": rows[1],
                  "verify": {"identity": "match"}},
        )
        self.assertEqual(
            [row["synthetic"] for row in logged.media],
            [True, False, True, True],
        )
        self.assertIs(logged.meta["reading"]["synthetic"], False)
        self.assertIs(logged.meta["verify"]["synthetic"], True)

    def test_malformed_evidence_shapes_are_rejected_not_silently_erased(self) -> None:
        malformed = (
            lambda: Loop(**mission().model_dump(exclude={"results"}),
                         results={"analyte": "K"}),
            lambda: Loop(**mission().model_dump(exclude={"results"}),
                         results=["not-a-row"]),
            lambda: Patient(**patient().model_dump(exclude={"results"}),
                            results=["not-a-row"]),
            lambda: Patient(**patient().model_dump(exclude={"results"}),
                            results=[{"results": "not-a-list"}]),
            lambda: Event(**event().model_dump(exclude={"media"}),
                          media={"kind": "image"}),
            lambda: Event(**event().model_dump(exclude={"meta"}), meta="bad"),
            lambda: Event(**event().model_dump(exclude={"meta"}),
                          meta={"results": {"analyte": "K"}}),
            lambda: Event(**event().model_dump(exclude={"meta"}),
                          meta={"reading": "bad"}),
            lambda: Loop(**mission().model_dump(exclude={"verified"}),
                         verified="bad"),
        )
        for build in malformed:
            with self.subTest(build=build):
                with self.assertRaises(ValidationError):
                    build()

    def test_derived_data_is_real_only_when_every_origin_is_false(self) -> None:
        self.assertIs(provenance.derived(False, False), False)
        for origins in ((True, False), (False, True), (None, False),
                        ("false", False), ()):
            with self.subTest(origins=origins):
                self.assertIs(provenance.derived(*origins), True)

    def test_all_background_records_and_evidence_are_explicitly_synthetic(self) -> None:
        patients, loops, logged, _ = background.records("doctor", NOW)
        self.assertTrue(patients and loops and logged)
        self.assertTrue(all(row.synthetic is True for row in patients))
        self.assertTrue(all(row.synthetic is True for row in loops))
        self.assertTrue(all(row.synthetic is True for row in logged))
        evidence_rows = [
            evidence
            for loop in loops
            for evidence in [*loop.results, *loop.readings]
        ]
        self.assertTrue(evidence_rows)
        self.assertTrue(all(row["synthetic"] is True for row in evidence_rows))


class ProviderBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_webhook_never_reaches_the_router(self) -> None:
        routed = AsyncMock()
        with (
            patch.object(sanad_main.telegram, "verify_secret", return_value=False),
            patch.object(sanad_main.tg_router, "handle_provider_update", routed),
        ):
            with self.assertRaises(HTTPException) as raised:
                await sanad_main.telegram_webhook(RequestStub({"update_id": 1}))
        self.assertEqual(raised.exception.status_code, 404)
        routed.assert_not_awaited()

    async def test_verified_telegram_doctor_and_patient_messages_are_false(self) -> None:
        from core.command_bus import ReplayClaim

        class HermeticReplay:
            async def claim(self, _command):
                return ReplayClaim(state="CLAIMED")

            async def complete(self, _command, _result):
                return None

            async def release(self, _command):
                return None

        transport = sanad_main.transport_runtime()
        for update_id, role in enumerate(("doctor", "patient"), start=7):
            received: list[InboundMessage] = []

            async def handle(msg: InboundMessage) -> None:
                received.append(msg)

            found_doctor = doctor(synthetic=True) if role == "doctor" else None
            found_patient = patient(synthetic=True) if role == "patient" else None
            with (
                patch.object(sanad_main.telegram, "verify_secret", return_value=True),
                patch.object(tg_router.store, "doctor_by_telegram",
                             AsyncMock(return_value=found_doctor)),
                patch.object(tg_router.store, "patient_by_telegram",
                             AsyncMock(return_value=found_patient)),
                patch.object(tg_router.dispatch, "handle_inbound", handle),
                patch.object(transport.bus, "_replay", HermeticReplay()),
            ):
                await sanad_main.telegram_webhook(RequestStub({
                    "update_id": update_id,
                    "message": {"chat": {"id": 77}, "text": "hello"},
                }))
            with self.subTest(role=role):
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0].channel, "telegram")
                self.assertIs(received[0].synthetic, False)

    async def test_direct_router_call_and_channel_label_fail_safe_true(self) -> None:
        received: list[InboundMessage] = []

        async def handle(msg: InboundMessage) -> None:
            received.append(msg)

        with (
            patch.object(tg_router.store, "doctor_by_telegram",
                         AsyncMock(return_value=doctor())),
            patch.object(tg_router.dispatch, "handle_inbound", handle),
        ):
            await tg_router.handle_update(
                {"message": {"chat": {"id": 77}, "text": "hello"}},
                "https://sanad.test/",
            )
        self.assertIs(received[0].synthetic, True)
        self.assertIs(InboundMessage(
            channel="telegram", sender_ref="patient:p").synthetic, True)
        with self.assertRaises(TypeError):
            await tg_router.handle_update(
                {"message": {"chat": {"id": 77}, "text": "hello"}},
                "https://sanad.test/", synthetic=False,
            )
        with (
            patch.object(tg_router.telegram, "verify_secret", return_value=False),
            self.assertRaises(PermissionError),
        ):
            await tg_router.handle_provider_update(
                {"message": {"chat": {"id": 77}, "text": "hello"}},
                "https://sanad.test/", secret_token="wrong",
            )

    async def test_all_three_web_ingress_routes_force_true(self) -> None:
        received: list[InboundMessage] = []

        async def handle(msg: InboundMessage) -> None:
            received.append(msg)

        real_patient = patient(synthetic=False)
        real_doctor = doctor(synthetic=False)
        with (
            patch.object(sanad_main.dispatch, "handle_inbound", handle),
            patch.object(sanad_main, "patient_from_link",
                         AsyncMock(return_value=real_patient)),
            patch.object(sanad_main.store, "get_patient",
                         AsyncMock(return_value=real_patient)),
        ):
            await sanad_main.patient_send("link", text="one", file=None)
            await sanad_main.doctor_in(text="two", file=None, doctor=real_doctor)
            await sanad_main.patient_in(
                real_patient.id, text="three", file=None, doctor=real_doctor)
        self.assertEqual(len(received), 3)
        self.assertTrue(all(msg.channel == "web" for msg in received))
        self.assertTrue(all(msg.synthetic is True for msg in received))

    def test_only_the_verified_http_boundary_enters_the_provider_router(self) -> None:
        provider_calls: list[tuple[str, str]] = []
        literal_false: list[tuple[str, str]] = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "handle_provider_update"):
                    provider_calls.append((
                        path.relative_to(APP_ROOT).as_posix(), node.func.attr
                    ))
                for keyword in node.keywords:
                    if (keyword.arg == "synthetic"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is False):
                        function = ""
                        # The source location and called attribute are enough to
                        # pin the privilege without relying on formatting.
                        if isinstance(node.func, ast.Attribute):
                            function = node.func.attr
                        elif isinstance(node.func, ast.Name):
                            function = node.func.id
                        literal_false.append((
                            path.relative_to(APP_ROOT).as_posix(), function
                        ))
        self.assertEqual(provider_calls, [("main.py", "handle_provider_update")])
        self.assertEqual(literal_false, [("core/tg_router.py", "_handle_update")])


class ProvenanceSurvivesTheWork(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_event_and_typed_reading_keep_distinct_provenance(self) -> None:
        for actor_synthetic, expected_evidence in ((False, False), (True, True)):
            logged: list[Event] = []
            readings: list[dict] = []
            out = Outbox()
            who = patient(synthetic=actor_synthetic)
            loop = mission(synthetic=actor_synthetic)

            async def append_event(doctor_id, kind, text="", **fields):
                made = Event(
                    id=f"e{len(logged)}", doctor_id=doctor_id, kind=kind,
                    text=text, ts=NOW, **fields,
                )
                logged.append(made)
                return made

            async def append_reading(_loop_id, row):
                readings.append(row)

            with (
                patch.object(concierge.events, "append_event", append_event),
                patch.object(concierge.store, "list_loops",
                             AsyncMock(return_value=[loop])),
                patch.object(concierge.store, "append_reading", append_reading),
                patch.object(concierge.chaser, "note_patient_reply", AsyncMock()),
                patch.object(concierge.chaser, "revive_unreachable", AsyncMock()),
                patch.object(concierge, "fanout", return_value=out),
            ):
                await concierge.handle_patient_message(
                    who, doctor(), "120/80", channel="telegram", synthetic=False)

            with self.subTest(actor_synthetic=actor_synthetic):
                self.assertIs(logged[0].synthetic, False)
                self.assertEqual(logged[0].kind, "patient_in")
                self.assertEqual(len(readings), 1)
                self.assertIs(readings[0]["synthetic"], expected_evidence)

    async def test_real_provider_emergency_and_fail_closed_event_stay_false(self) -> None:
        logged: list[Event] = []
        out = Outbox()

        async def append_event(doctor_id, kind, text="", **fields):
            made = Event(
                id=f"urgent-{len(logged)}", doctor_id=doctor_id, kind=kind,
                text=text, ts=NOW, **fields,
            )
            logged.append(made)
            return made

        with (
            patch.object(concierge.events, "append_event", append_event),
            patch.object(concierge.chaser, "note_patient_reply", AsyncMock()),
            patch.object(concierge.chaser, "revive_unreachable", AsyncMock()),
            patch.object(concierge, "fanout", return_value=out),
        ):
            await concierge.handle_patient_message(
                patient(synthetic=False), doctor(synthetic=False),
                "severe chest pain", channel="telegram", synthetic=False,
                gate=sentinel.Sentinel(
                    fired=True, net="code", concept="chest pain",
                    checked=["code"],
                ),
            )

        self.assertEqual([row.kind for row in logged],
                         ["patient_in", "escalation"])
        self.assertTrue(all(row.synthetic is False for row in logged))

        async def broken_persist() -> None:
            raise RuntimeError("write failed")

        fallback = AsyncMock()
        with patch.object(escalate.events, "append_event", fallback):
            landed = await escalate.told_or_fail_closed(
                broken_persist, doctor_id="d", patient_id="p",
                synthetic=False,
            )
        self.assertIs(landed, False)
        self.assertIs(fallback.await_args.kwargs["synthetic"], False)

    async def test_lab_rows_media_and_event_keep_real_evidence_false(self) -> None:
        logged: list[Event] = []
        out = Outbox()
        reading = PhotoReading(
            kind="lab_slip", text_orientation="upright", patient_name="Ahmed",
            analytes=[SlipAnalyte(
                analyte="LDL", value="100", unit="mg/dL")],
        )

        async def append_event(doctor_id, kind, text="", **fields):
            made = Event(
                id=f"lab-{len(logged)}", doctor_id=doctor_id, kind=kind,
                text=text, ts=NOW, **fields,
            )
            logged.append(made)
            return made

        with (
            patch.object(extractor.events, "last_events",
                         AsyncMock(return_value=[])),
            patch.object(extractor.events, "append_event", append_event),
            patch.object(extractor, "fanout", return_value=out),
        ):
            await extractor._handle_lab(
                patient(synthetic=False), doctor(synthetic=False), reading, {},
                "gs://evidence", "en", "m", "telegram", None,
                synthetic=False,
            )
        evidence_event = next(row for row in logged if "lab slip read" in row.text)
        self.assertIs(evidence_event.synthetic, False)
        self.assertIs(evidence_event.media[0]["synthetic"], False)
        self.assertIs(evidence_event.meta["results"][0]["synthetic"], False)

    async def test_delayed_confirm_preserves_actor_and_mission_provenance(self) -> None:
        made_patients: list[Patient] = []
        made_loops: list[Loop] = []
        out = Outbox()
        proposal = ProposedRecord(
            patient=ProposedPatient(name="Ahmed", diagnosis="hypertension"),
            plan_text="Measure blood pressure.",
            loops=[ProposedLoop(
                type="MONITOR", title="Blood pressure", metric="BP")],
        )
        confirm = PendingConfirm(
            id="confirm", synthetic=False, doctor_id="d",
            proposed=proposal.model_dump(), expires_at=NOW + timedelta(hours=1),
        )

        async def create_patient(row: Patient) -> Patient:
            made_patients.append(row)
            return row

        async def create_loop(row: Loop) -> Loop:
            made_loops.append(row)
            return row

        token = LinkToken(
            id="link", doctor_id="d", patient_id="p", created_at=NOW)
        with (
            patch.object(registrar, "fanout", return_value=out),
            patch.object(registrar.store, "now", return_value=NOW),
            patch.object(registrar.store, "create_patient", create_patient),
            patch.object(registrar.store, "create_loop", create_loop),
            patch.object(registrar.store, "delete_confirm", AsyncMock()),
            patch.object(registrar.events, "append_event", AsyncMock()),
            patch.object(registrar.links, "mint", AsyncMock(return_value=token)),
            patch.object(registrar.links, "card_lines", AsyncMock(return_value=[])),
            patch.object(registrar.chaser, "schedule_patient",
                         AsyncMock(return_value=[])),
        ):
            await registrar._commit(doctor(synthetic=False), confirm)

        self.assertEqual(len(made_patients), 1)
        self.assertEqual(len(made_loops), 1)
        self.assertIs(made_patients[0].synthetic, False)
        self.assertIs(made_loops[0].synthetic, False)
        reread = PendingConfirm.model_validate(confirm.model_dump())
        self.assertIs(reread.synthetic, False)

    async def test_evidence_opened_loop_cannot_drop_copied_provenance(self) -> None:
        source = Event(
            id="result", synthetic=False, doctor_id="d", patient_id="p",
            kind="system", ts=NOW,
            meta={"results": [{"analyte": "LDL", "synthetic": False}]},
        )
        created: list[Loop] = []
        out = Outbox()

        async def create_loop(row: Loop) -> Loop:
            created.append(row)
            return row

        with (
            patch.object(extractor.store, "get_event",
                         AsyncMock(return_value=source)),
            patch.object(extractor.store, "get_patient",
                         AsyncMock(return_value=patient(synthetic=False))),
            patch.object(extractor.store, "create_loop", create_loop),
            patch.object(extractor.store, "now", return_value=NOW),
            patch.object(extractor.events, "append_event", AsyncMock()),
            patch.object(extractor, "fanout", return_value=out),
        ):
            await extractor.open_loop_for(doctor(synthetic=False), source.id)
        self.assertEqual(len(created), 1)
        self.assertIs(created[0].synthetic, False)
        self.assertIs(created[0].results[0]["synthetic"], False)


class ProvenanceIsInspectable(unittest.IsolatedAsyncioTestCase):
    async def test_feed_board_patient_cards_settings_and_contract_expose_it(self) -> None:
        doc = doctor(synthetic=False)
        who = patient(synthetic=False)
        loop = mission(synthetic=False, kind="TEST")
        incoming = event(synthetic=False)
        card_event = Event(
            id="card", synthetic=False, doctor_id="d", patient_id="p",
            kind="card", text="needs review",
            meta={"card": {"severity": "red", "actions": []}}, ts=NOW,
        )

        with patch.object(sanad_main.events, "last_events",
                          AsyncMock(return_value=[incoming])):
            feed = await sanad_main.feed(doctor=doc)
        self.assertIs(feed["events"][0]["synthetic"], False)

        with (
            patch.object(sanad_main.store, "list_events",
                         AsyncMock(return_value=[incoming])),
            patch.object(sanad_main.store, "list_link_tokens",
                         AsyncMock(return_value=[])),
            patch.object(sanad_main.store, "list_patients",
                         AsyncMock(return_value=[who])),
            patch.object(sanad_main.store, "list_loops",
                         AsyncMock(return_value=[loop])),
            patch.object(sanad_main.store, "latest_link_token",
                         AsyncMock(return_value=None)),
        ):
            board = await sanad_main.board_view(doctor=doc)
        self.assertIs(board["synthetic"], False)
        self.assertIs(board["patients"][0]["synthetic"], False)
        self.assertIs(board["patients"][0]["loops"][0]["synthetic"], False)

        with (
            patch.object(sanad_main.store, "get_patient",
                         AsyncMock(return_value=who)),
            patch.object(sanad_main.store, "list_loops",
                         AsyncMock(return_value=[loop])),
            patch.object(sanad_main.events, "last_events",
                         AsyncMock(return_value=[incoming])),
            patch.object(sanad_main.store, "list_link_tokens",
                         AsyncMock(return_value=[])),
            patch.object(sanad_main.settings, "current",
                         AsyncMock(return_value=("run", 86400))),
        ):
            detail = await sanad_main.patient_view(who.id, doctor=doc)
        self.assertIs(detail["patient"]["synthetic"], False)
        self.assertIs(detail["loops"][0]["synthetic"], False)
        self.assertIs(detail["timeline"][0]["synthetic"], False)
        self.assertIs(detail["contracts"][0]["synthetic"], False)

        self.assertIs(cards.row(card_event)["synthetic"], False)
        self.assertIs(views.settings_view(doc, policy.DEFAULT)["synthetic"], False)
        self.assertIs(
            contract.render(loop, policy.DEFAULT, doc.name, who.name)["synthetic"],
            False,
        )

    def test_gate0b_projection_allows_only_new_boolean_synthetic_fields(self) -> None:
        baseline = {"row": {"value": 1}, "meta": {"synthetic": True}}
        actual = {
            "row": {"value": 1, "synthetic": False},
            "meta": {"synthetic": True},
        }
        self.assertEqual(legacy_projection(actual, baseline), baseline)
        with self.assertRaises(AssertionError):
            legacy_projection({"row": {"value": 1, "other": 2},
                               "meta": {"synthetic": True}}, baseline)
        with self.assertRaises(AssertionError):
            legacy_projection({"row": {"value": 1, "synthetic": "false"},
                               "meta": {"synthetic": True}}, baseline)

    def test_gate0b_projection_rejects_a_corrupt_actual_subtree_seal(self) -> None:
        baseline = {
            "api": {"events": []},
            "api_sha256": artifact_digest({"events": []}),
        }
        actual_api = {"events": [{"synthetic": True}]}
        actual = {"api": actual_api, "api_sha256": "0" * 64}
        with self.assertRaisesRegex(AssertionError, "invalid api_sha256"):
            legacy_projection(actual, baseline)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
