"""Gate 3: one canonical, versioned doctor workspace projection.

These tests deliberately exercise the projection as a pure function over one
already-loaded record bundle.  Storage atomicity and HTTP authentication live
in the route test beside this file.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from core.models import Doctor, Event, LinkToken, Loop, Patient, Relay, Report
from core.workspace import InvalidCursor, InvalidWorkspace, build_snapshot
from core.workspace_records import WorkspaceRecords


NOW = datetime(2026, 8, 31, 9, 0, 0, 123456, tzinfo=timezone.utc)
LINK_SECRET_A = "0123456789abcdef0123456789abcdef"
LINK_SECRET_B = "abcdef0123456789abcdef0123456789"


def doctor(ident: str = "d1") -> Doctor:
    return Doctor(
        id=ident,
        synthetic=True,
        name="Test Doctor",
        specialty="cardiology",
        web_token=f"token-{ident}",
        cockpit_v2_enabled=True,
        created_at=NOW - timedelta(days=30),
    )


def patient(ident: str, *, status: str = "active", minutes: int = 0) -> Patient:
    return Patient(
        id=ident,
        synthetic=True,
        doctor_id="d1",
        name=f"Patient {ident}",
        diagnosis="fixture diagnosis",
        status=status,
        created_at=NOW - timedelta(days=4) + timedelta(minutes=minutes),
    )


def loop(
    ident: str,
    state: str,
    *,
    patient_id: str = "p1",
    barrier: str = "",
    paused: bool = False,
    closed_at: datetime | None = None,
    updated_at: datetime | None = None,
    title: str | None = None,
    metric: str | None = None,
    readings: list[dict] | None = None,
    verified: dict | None = None,
) -> Loop:
    details = {"test_name": ident}
    kind = "TEST"
    if metric is not None:
        kind = "MONITOR"
        details = {"metric": metric, "schedule": "twice a day", "days": 7}
    return Loop(
        id=ident,
        synthetic=True,
        patient_id=patient_id,
        doctor_id="d1",
        type=kind,
        title=title or ident,
        details=details,
        state=state,
        barrier=barrier,
        paused=paused,
        doctor_reviewed=state == "done",
        closed_at=closed_at,
        readings=readings or [],
        verified=verified or {},
        created_at=NOW - timedelta(days=3),
        updated_at=updated_at or NOW,
    )


def card_event(
    ident: str,
    *,
    notification_class: str | None,
    resolved: bool = False,
    patient_id: str = "p1",
    minute: int = 0,
    severity: str = "red",
    actions: list[dict] | None = None,
) -> Event:
    card = {
        "title": "opaque fixture card",
        "severity": severity,
        "lines": [],
        "actions": actions or [],
        **({"resolved": True, "resolved_by": f"seen:{ident}"} if resolved else {}),
    }
    meta = {"card": card}
    if notification_class is not None:
        meta["notification_class"] = notification_class
    return Event(
        id=ident,
        synthetic=True,
        doctor_id="d1",
        patient_id=patient_id,
        kind="card",
        text="fixture",
        meta=meta,
        ts=NOW + timedelta(minutes=minute),
    )


def records(
    *,
    patients: tuple[Patient, ...] | None = None,
    loops: tuple[Loop, ...] = (),
    events: tuple[Event, ...] = (),
    relays: tuple[Relay, ...] = (),
    link_tokens: tuple[LinkToken, ...] = (),
    reports: tuple[Report, ...] = (),
    read_at: datetime | None = NOW + timedelta(hours=1),
) -> WorkspaceRecords:
    persisted_events = tuple(
        event
        if event.persisted_at is not None
        else event.model_copy(update={"persisted_at": NOW})
        for event in events
    )
    return WorkspaceRecords(
        doctor=doctor(),
        patients=patients or (patient("p1"),),
        loops=loops,
        events=persisted_events,
        reports=reports,
        link_tokens=link_tokens,
        open_relays=relays,
        settings={"run_id": "gate3", "time_scale": 86400},
        read_at=read_at,
    )


def metric(snapshot: dict, name: str) -> tuple[int, list[str]]:
    row = snapshot["metrics"][name]
    return row["count"], row["row_ids"]


class LiteralMetricTruth(unittest.TestCase):
    def test_red_urgent_review_is_not_called_danger(self) -> None:
        danger = card_event("danger", notification_class="DANGER")
        urgent = card_event("urgent", notification_class="URGENT_SLA", minute=1)
        untyped = card_event("legacy-red", notification_class=None, minute=2)
        mistyped = card_event("mistyped-red", notification_class="TYPO", minute=3)
        resolved = card_event(
            "resolved-danger", notification_class="DANGER", resolved=True, minute=4
        )

        snapshot = build_snapshot(
            records(events=(danger, urgent, untyped, mistyped, resolved)), NOW
        )

        self.assertEqual(metric(snapshot, "danger_unacknowledged"), (1, ["event:danger"]))
        self.assertEqual(
            snapshot["rows"]["event:danger"]["card"]["actions"],
            [{"id": "seen:danger", "label": "Seen"}],
        )
        self.assertEqual(snapshot["queues"]["urgent_review"]["row_ids"], ["event:urgent"])
        self.assertEqual(
            snapshot["queues"]["unclassified_red"]["row_ids"],
            ["event:legacy-red", "event:mistyped-red"],
        )
        self.assertNotIn("event:resolved-danger", snapshot["queues"]["danger"]["row_ids"])

    def test_unresolved_typed_alert_cannot_be_hidden_by_a_non_actionable_card(self) -> None:
        malformed = card_event(
            "hidden-danger",
            notification_class="DANGER",
            severity="yellow",
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(events=(malformed,)), NOW)

        resolved = card_event(
            "historical-danger",
            notification_class="DANGER",
            severity="yellow",
            resolved=True,
        )
        snapshot = build_snapshot(records(events=(resolved,)), NOW)
        self.assertEqual(snapshot["queues"]["danger"]["row_ids"], [])

        fake_resolution = malformed.model_copy(
            update={
                "meta": {
                    "card": {
                        **malformed.meta["card"],
                        "resolved": "false",
                    },
                    "notification_class": "DANGER",
                }
            }
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(events=(fake_resolution,)), NOW)

    def test_historical_green_updated_today_is_not_closed_today(self) -> None:
        historical = loop("historical", "done", updated_at=NOW)
        explicit = loop(
            "explicit",
            "done",
            closed_at=NOW - timedelta(minutes=1),
            updated_at=NOW,
        )
        yesterday = loop(
            "yesterday",
            "done",
            closed_at=NOW - timedelta(days=1),
            updated_at=NOW,
        )

        snapshot = build_snapshot(records(loops=(historical, explicit, yesterday)), NOW)

        self.assertEqual(metric(snapshot, "closed_today"), (1, ["loop:explicit"]))
        self.assertEqual(
            snapshot["shadow"]["legacy"]["board"]["green_row_ids"],
            ["loop:explicit", "loop:historical", "loop:yesterday"],
        )

    def test_literal_loop_queues_and_metrics_are_built_from_one_id_set(self) -> None:
        all_loops = (
            loop("working", "open"),
            loop("blocked", "waiting_patient", barrier="cost", paused=True),
            loop("received", "received", verified={"satisfies": True}),
            loop("review", "pending_review", verified={"satisfies": True}),
            loop("deadline", "unreachable"),
            loop("closed", "done", closed_at=NOW),
        )

        snapshot = build_snapshot(records(loops=all_loops), NOW)

        self.assertEqual(metric(snapshot, "sanad_working"), (1, ["loop:working"]))
        self.assertEqual(
            snapshot["queues"]["blocked"]["row_ids"], ["loop:blocked"]
        )
        self.assertEqual(
            metric(snapshot, "review_ready"),
            (2, ["loop:received", "loop:review"]),
        )
        self.assertEqual(metric(snapshot, "deadline_outcomes"), (1, ["loop:deadline"]))
        self.assertEqual(
            metric(snapshot, "terminal_waiting_review"),
            (3, ["loop:deadline", "loop:received", "loop:review"]),
        )

        for name, value in snapshot["metrics"].items():
            with self.subTest(metric=name):
                self.assertEqual(value["count"], len(value["row_ids"]))
                self.assertEqual(len(value["row_ids"]), len(set(value["row_ids"])))
                self.assertTrue(all(row_id in snapshot["rows"] for row_id in value["row_ids"]))

        for name, value in snapshot["queues"].items():
            with self.subTest(queue=name):
                self.assertEqual(value["count"], len(value["row_ids"]))
                self.assertEqual(value["row_ids"], [row["id"] for row in value["rows"]])
                self.assertTrue(all(row_id in snapshot["rows"] for row_id in value["row_ids"]))

        for metric_name, queue_name in (
            ("terminal_waiting_review", "terminal_waiting_review"),
            ("sanad_working", "sanad_working"),
            ("closed_today", "closed_today"),
            ("active_patients_total", "active_patients"),
        ):
            with self.subTest(hero_metric=metric_name):
                self.assertEqual(
                    snapshot["metrics"][metric_name]["row_ids"],
                    snapshot["queues"][queue_name]["row_ids"],
                )

        parity = snapshot["shadow"]["checks"]
        self.assertEqual(
            parity["legacy_red_partitioned_by_verification"]["status"], "MATCH"
        )
        self.assertEqual(parity["legacy_white_equals_deadline"]["status"], "MATCH")
        self.assertEqual(parity["legacy_yellow_equals_working_plus_blocked"]["status"], "MATCH")
        self.assertEqual(parity["closed_today_is_subset_of_legacy_green"]["status"], "MATCH")

    def test_unknown_verification_does_not_become_review_ready(self) -> None:
        verified = loop(
            "verified", "pending_review", verified={"satisfies": True}
        )
        unknown = loop("unknown", "pending_review")

        snapshot = build_snapshot(records(loops=(verified, unknown)), NOW)

        self.assertEqual(metric(snapshot, "review_ready"), (1, ["loop:verified"]))
        self.assertEqual(
            snapshot["queues"]["verification_unknown"]["row_ids"],
            ["loop:unknown"],
        )
        self.assertEqual(
            metric(snapshot, "terminal_waiting_review"),
            (2, ["loop:unknown", "loop:verified"]),
        )

    def test_future_close_timestamp_is_not_closed_today(self) -> None:
        future = loop("future", "done", closed_at=NOW + timedelta(minutes=1))
        snapshot = build_snapshot(records(loops=(future,)), NOW)
        self.assertEqual(metric(snapshot, "closed_today"), (0, []))

    def test_every_open_action_card_has_a_canonical_queue(self) -> None:
        ordinary = card_event(
            "confirm-card",
            notification_class=None,
            severity="yellow",
            actions=[{"id": "confirm:proposal", "label": "Confirm"}],
        )
        snapshot = build_snapshot(records(events=(ordinary,)), NOW)
        self.assertEqual(
            snapshot["queues"]["doctor_actions"]["row_ids"],
            ["event:confirm-card"],
        )
        self.assertEqual(
            snapshot["shadow"]["checks"]["legacy_open_cards_covered"]["status"],
            "MATCH",
        )
        self.assertEqual(snapshot["shadow"]["status"], "MATCH")


class VersionAndPaginationTruth(unittest.TestCase):
    def test_snapshot_id_is_order_independent_but_record_sensitive(self) -> None:
        p1, p2 = patient("p1"), patient("p2", minutes=1)
        l1 = loop("l1", "open", patient_id="p1")
        l2 = loop("l2", "pending_review", patient_id="p2")
        e1 = card_event("e1", notification_class="DANGER", patient_id="p1")
        e2 = Event(
            id="e2",
            doctor_id="d1",
            patient_id="p2",
            kind="system",
            text="later",
            ts=NOW + timedelta(seconds=1),
        )
        first = records(patients=(p1, p2), loops=(l1, l2), events=(e1, e2))
        shuffled = records(patients=(p2, p1), loops=(l2, l1), events=(e2, e1))

        a = build_snapshot(first, NOW)
        b = build_snapshot(shuffled, NOW + timedelta(minutes=10))
        self.assertEqual(a["snapshot_id"], b["snapshot_id"])
        self.assertEqual(a["snapshot_id_kind"], "RECORD_VERSION")
        self.assertNotEqual(a["as_of"], b["as_of"])

        changed_l1 = l1.model_copy(update={"title": "changed title"})
        changed = build_snapshot(
            records(patients=(p1, p2), loops=(changed_l1, l2), events=(e1, e2)), NOW
        )
        self.assertNotEqual(a["snapshot_id"], changed["snapshot_id"])

    def test_patient_total_is_not_the_page_length(self) -> None:
        rows = tuple(patient(f"p{i}", minutes=i) for i in range(7))
        snapshot = build_snapshot(
            records(patients=rows), NOW, patient_offset=2, patient_limit=3
        )
        self.assertEqual(snapshot["patients"]["total"], 7)
        self.assertEqual(len(snapshot["patients"]["items"]), 3)
        self.assertEqual(snapshot["patients"]["offset"], 2)
        self.assertTrue(snapshot["patients"]["has_more"])

    def test_optional_read_boundary_handles_an_empty_event_bundle(self) -> None:
        snapshot = build_snapshot(records(read_at=None), NOW)

        self.assertEqual(snapshot["agent_events"]["items"], [])
        self.assertEqual(snapshot["agent_events"]["count"], 0)
        self.assertIsInstance(snapshot["event_cursor"], str)

    def test_composite_event_cursor_loses_neither_same_time_nor_large_burst_rows(self) -> None:
        burst = tuple(
            Event(
                id=f"{i:032x}",
                doctor_id="d1",
                patient_id="p1",
                kind="system",
                text=f"event {i}",
                ts=NOW,
            )
            for i in range(1701)
        )
        bundle = records(events=tuple(reversed(burst)))

        first = build_snapshot(bundle, NOW, event_limit=1000)
        second = build_snapshot(
            bundle,
            NOW,
            event_cursor=first["event_cursor"],
            event_limit=1000,
        )
        third = build_snapshot(
            bundle,
            NOW,
            event_cursor=second["event_cursor"],
            event_limit=1000,
        )
        first_ids = [row["id"] for row in first["agent_events"]["items"]]
        second_ids = [row["id"] for row in second["agent_events"]["items"]]

        self.assertTrue(first["agent_events"]["has_more"])
        self.assertFalse(second["agent_events"]["has_more"])
        self.assertEqual(
            first_ids + second_ids,
            [f"{i:032x}" for i in range(1701)],
        )
        self.assertEqual(len(set(first_ids + second_ids)), 1701)
        self.assertEqual(third["agent_events"]["items"], [])
        self.assertLess(len(second["event_cursor"]), 4096)

    def test_cursor_does_not_lose_a_later_commit_with_a_lower_id(self) -> None:
        first_event = Event(
            id="z",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            text="first persisted",
            ts=NOW,
            persisted_at=NOW,
        )
        first = build_snapshot(
            records(events=(first_event,), read_at=NOW),
            NOW,
        )
        later_same_time = Event(
            id="a",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            text="persisted later at the same clock value",
            ts=NOW - timedelta(days=1),
            persisted_at=NOW + timedelta(microseconds=1),
        )
        second = build_snapshot(
            records(
                events=(first_event, later_same_time),
                read_at=NOW + timedelta(microseconds=2),
            ),
            NOW,
            event_cursor=first["event_cursor"],
        )
        self.assertEqual(
            [row["id"] for row in second["agent_events"]["items"]], ["a"]
        )

    def test_cursor_finishes_a_closed_read_before_opening_the_next_boundary(self) -> None:
        first_batch = (
            Event(
                id="b",
                doctor_id="d1",
                patient_id="p1",
                kind="system",
                ts=NOW,
                persisted_at=NOW,
            ),
            Event(
                id="c",
                doctor_id="d1",
                patient_id="p1",
                kind="system",
                ts=NOW,
                persisted_at=NOW,
            ),
        )
        first = build_snapshot(
            records(events=first_batch, read_at=NOW),
            NOW,
            event_limit=1,
        )
        later = Event(
            id="a",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            ts=NOW - timedelta(days=1),
            persisted_at=NOW + timedelta(microseconds=1),
        )
        expanded = records(
            events=first_batch + (later,),
            read_at=NOW + timedelta(microseconds=1),
        )

        second = build_snapshot(
            expanded,
            NOW,
            event_cursor=first["event_cursor"],
            event_limit=1,
        )
        third = build_snapshot(
            expanded,
            NOW,
            event_cursor=second["event_cursor"],
            event_limit=1,
        )

        self.assertEqual(
            [event["id"] for event in first["agent_events"]["items"]],
            ["b"],
        )
        self.assertEqual(
            [event["id"] for event in second["agent_events"]["items"]],
            ["c"],
        )
        self.assertEqual(
            [event["id"] for event in third["agent_events"]["items"]],
            ["a"],
        )

    def test_persisted_cursor_preserves_firestore_nanosecond_order(self) -> None:
        first_stamp = DatetimeWithNanoseconds(
            2026,
            8,
            31,
            9,
            0,
            0,
            tzinfo=timezone.utc,
            nanosecond=123456789,
        )
        later_stamp = DatetimeWithNanoseconds(
            2026,
            8,
            31,
            9,
            0,
            0,
            tzinfo=timezone.utc,
            nanosecond=123456790,
        )
        first_event = Event(
            id="z",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            ts=NOW,
            persisted_at=first_stamp,
        )
        first = build_snapshot(
            records(events=(first_event,), read_at=first_stamp),
            NOW,
        )
        later_event = Event(
            id="a",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            ts=NOW,
            persisted_at=later_stamp,
        )
        second = build_snapshot(
            records(events=(first_event, later_event), read_at=later_stamp),
            NOW,
            event_cursor=first["event_cursor"],
        )
        self.assertEqual(
            [row["id"] for row in second["agent_events"]["items"]],
            ["a"],
        )

    def test_event_cursor_is_bound_to_the_authenticated_doctor(self) -> None:
        event = Event(
            id="e1",
            doctor_id="d1",
            kind="system",
            text="event",
            ts=NOW,
        )
        cursor = build_snapshot(records(events=(event,)), NOW)["event_cursor"]
        other = replace(records(events=()), doctor=doctor("d2"), patients=())
        with self.assertRaises(InvalidCursor):
            build_snapshot(other, NOW, event_cursor=cursor)


class ProjectionSpecificity(unittest.TestCase):
    def test_weight_monitor_cannot_become_the_blood_pressure_tile(self) -> None:
        weight = loop(
            "weight",
            "open",
            metric="weight",
            readings=[{"at": NOW.isoformat(), "number": 84.0, "value": "84"}],
        )
        bp = loop(
            "bp",
            "open",
            metric="BP",
            readings=[{"at": NOW.isoformat(), "number": 130.0, "value": "130/82"}],
        )
        snapshot = build_snapshot(records(loops=(weight, bp)), NOW)
        self.assertEqual(snapshot["bp_tile"]["loop"]["id"], "bp")

    def test_revoked_link_is_not_truthful_reachability_but_legacy_shape_is_preserved(self) -> None:
        token = LinkToken(
            id=LINK_SECRET_A,
            doctor_id="d1",
            patient_id="p1",
            revoked=True,
            created_at=NOW,
        )
        bundle = replace(records(), link_tokens=(token,))
        snapshot = build_snapshot(bundle, NOW)
        self.assertEqual(snapshot["patients"]["items"][0]["reachability"], "NONE")
        self.assertEqual(snapshot["legacy"]["board"]["patients"][0]["channel"], "web")

    def test_patient_pagination_does_not_return_every_full_summary_in_rows(self) -> None:
        patients = (
            patient("p1"),
            patient("p2", minutes=1),
            patient("p3", minutes=2),
        )
        snapshot = build_snapshot(
            records(patients=patients), NOW, patient_offset=0, patient_limit=1
        )
        self.assertEqual([row["source_id"] for row in snapshot["patients"]["items"]], ["p1"])
        self.assertNotIn("name", snapshot["rows"]["patient:p2"])
        self.assertNotIn("diagnosis", snapshot["rows"]["patient:p2"])
        self.assertEqual(
            [row["id"] for row in snapshot["legacy"]["board"]["patients"]],
            ["p1"],
        )


class WorkspaceSafety(unittest.TestCase):
    def test_cross_tenant_or_orphan_references_fail_closed(self) -> None:
        foreign_link = LinkToken(
            id=LINK_SECRET_A,
            doctor_id="d1",
            patient_id="foreign-patient",
            created_at=NOW,
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(link_tokens=(foreign_link,)), NOW)

        foreign_loop_event = Event(
            id="bad-event",
            doctor_id="d1",
            patient_id="p1",
            loop_id="foreign-loop",
            kind="system",
            text="bad reference",
            ts=NOW,
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(events=(foreign_loop_event,)), NOW)

    def test_card_actions_cannot_target_another_patients_loop_or_event(self) -> None:
        patients = (patient("p1"), patient("p2", minutes=1))
        p1_loop = loop("l1", "pending_review", patient_id="p1")
        p2_loop = loop("l2", "pending_review", patient_id="p2")
        cross_loop_card = card_event(
            "cross-loop-card",
            patient_id="p1",
            notification_class=None,
            severity="yellow",
            actions=[{"id": "reviewed:l2", "label": "Reviewed"}],
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(
                records(
                    patients=patients,
                    loops=(p1_loop, p2_loop),
                    events=(cross_loop_card,),
                ),
                NOW,
            )

        valid_loop_card = card_event(
            "valid-loop-card",
            patient_id="p1",
            notification_class=None,
            severity="yellow",
            actions=[{"id": "reviewed:l1", "label": "Reviewed"}],
        )
        valid = build_snapshot(
            records(
                patients=patients,
                loops=(p1_loop, p2_loop),
                events=(valid_loop_card,),
            ),
            NOW,
        )
        self.assertEqual(
            valid["rows"]["event:valid-loop-card"]["card"]["actions"][0]["id"],
            "reviewed:l1",
        )

        p2_evidence = Event(
            id="p2-evidence",
            doctor_id="d1",
            patient_id="p2",
            kind="system",
            ts=NOW,
        )
        cross_event_card = card_event(
            "cross-event-card",
            patient_id="p1",
            notification_class=None,
            severity="yellow",
            actions=[{"id": "attach:p2-evidence", "label": "Attach"}],
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(
                records(
                    patients=patients,
                    events=(p2_evidence, cross_event_card),
                ),
                NOW,
            )

    def test_seen_action_is_bound_to_the_canonical_card_event(self) -> None:
        evidence = Event(
            id="evidence-event",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            ts=NOW,
        )
        card = card_event(
            "identity-card",
            patient_id="p1",
            notification_class=None,
            severity="yellow",
            actions=[{"id": "seen:evidence-event", "label": "Seen"}],
        )

        snapshot = build_snapshot(records(events=(evidence, card)), NOW)

        self.assertEqual(
            snapshot["rows"]["event:identity-card"]["card"]["actions"],
            [{"id": "seen:identity-card", "label": "Seen"}],
        )

    def test_consumed_action_target_is_not_required_by_resolved_history(self) -> None:
        resolved = card_event(
            "resolved-reply-card",
            patient_id="p1",
            notification_class=None,
            resolved=True,
            severity="yellow",
            actions=[{"id": "reply:consumed-relay", "label": "Answer"}],
        )
        snapshot = build_snapshot(records(events=(resolved,)), NOW)
        self.assertNotIn(
            "event:resolved-reply-card",
            snapshot["queues"]["doctor_actions"]["row_ids"],
        )

        still_open = card_event(
            "open-reply-card",
            patient_id="p1",
            notification_class=None,
            severity="yellow",
            actions=[{"id": "reply:missing-relay", "label": "Answer"}],
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(events=(still_open,)), NOW)

    def test_malformed_actions_cannot_hide_an_unresolved_card(self) -> None:
        malformed = Event(
            id="malformed-card",
            doctor_id="d1",
            patient_id="p1",
            kind="card",
            meta={
                "card": {
                    "title": "Needs a doctor",
                    "severity": "yellow",
                    "actions": ["reply:relay-123"],
                }
            },
            ts=NOW,
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(events=(malformed,)), NOW)

        resolved = malformed.model_copy(
            update={
                "meta": {
                    "card": {
                        **malformed.meta["card"],
                        "resolved": True,
                        "resolved_by": "reply:relay-123",
                    }
                }
            }
        )
        snapshot = build_snapshot(records(events=(resolved,)), NOW)
        self.assertNotIn(
            "event:malformed-card",
            snapshot["queues"]["doctor_actions"]["row_ids"],
        )

    def test_card_event_requires_the_persisted_card_payload(self) -> None:
        for index, meta in enumerate(({}, {"card": "not-an-object"}, {"card": {}})):
            with self.subTest(meta=meta):
                malformed = Event(
                    id=f"missing-card-{index}",
                    doctor_id="d1",
                    patient_id="p1",
                    kind="card",
                    meta=meta,
                    ts=NOW,
                )
                with self.assertRaises(InvalidWorkspace):
                    build_snapshot(records(events=(malformed,)), NOW)

    def test_snapshot_wire_redacts_bearers_and_chat_ids(self) -> None:
        doctor_chat_id = 123456789
        patient_chat_id = 987654321
        token = LinkToken(
            id=LINK_SECRET_A,
            doctor_id="d1",
            patient_id="p1",
            created_at=NOW,
        )
        event = Event(
            id="credential-event",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            text=(
                f"provider routed from {doctor_chat_id} to {patient_chat_id}"
            ),
            meta={
                "chat_id": doctor_chat_id,
                "safe_numeric_field": patient_chat_id,
                "double_route_copy": float(doctor_chat_id),
                "safe_text_field": f"bound:{doctor_chat_id}",
                "token": "provider-secret-token",
                "safe": "kept",
            },
            ts=NOW,
        )
        bound_patient = patient("p1").model_copy(
            update={
                "channels": {
                    "web": True,
                    "telegram_chat_id": patient_chat_id,
                }
            }
        )
        bundle = replace(
            records(
                patients=(bound_patient,),
                events=(event,),
                link_tokens=(token,),
            ),
            doctor=doctor().model_copy(
                update={"telegram_chat_id": doctor_chat_id}
            ),
        )
        raw = json.dumps(
            build_snapshot(bundle, NOW),
            sort_keys=True,
        )
        self.assertNotIn(LINK_SECRET_A, raw)
        self.assertNotIn("provider-secret-token", raw)
        self.assertNotIn(str(doctor_chat_id), raw)
        self.assertNotIn(str(patient_chat_id), raw)
        self.assertIn("[REDACTED_CHAT_ID]", raw)
        self.assertIn('"safe": "kept"', raw)

    def test_private_chat_id_rotation_does_not_change_snapshot_identity(self) -> None:
        def bundle_for(doctor_chat_id: int, patient_chat_id: int) -> WorkspaceRecords:
            bound_patient = patient("p1").model_copy(
                update={
                    "channels": {
                        "web": True,
                        "telegram_chat_id": patient_chat_id,
                    }
                }
            )
            routed = Event(
                id="private-route-event",
                doctor_id="d1",
                patient_id="p1",
                kind="system",
                text=f"provider routed {doctor_chat_id} to {patient_chat_id}",
                meta={
                    "doctor_route": doctor_chat_id,
                    "patient_route": patient_chat_id,
                    "double_route_copy": float(doctor_chat_id),
                },
                ts=NOW,
            )
            return replace(
                records(patients=(bound_patient,), events=(routed,)),
                doctor=doctor().model_copy(
                    update={"telegram_chat_id": doctor_chat_id}
                ),
            )

        first = build_snapshot(bundle_for(123456789, 987654321), NOW)
        second = build_snapshot(bundle_for(111222333, 444555666), NOW)

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

    def test_weak_or_structurally_aliased_bearers_fail_closed(self) -> None:
        weak = LinkToken(
            id="short",
            doctor_id="d1",
            patient_id="p1",
            created_at=NOW,
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(link_tokens=(weak,)), NOW)

        semantic_key = LinkToken(
            id="severity",
            doctor_id="d1",
            patient_id="p1",
            created_at=NOW,
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(link_tokens=(semantic_key,)), NOW)

        secret = LINK_SECRET_A
        aliased = LinkToken(
            id=secret,
            doctor_id="d1",
            patient_id="p1",
            created_at=NOW,
        )
        event = Event(
            id=secret,
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            ts=NOW,
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(records(events=(event,), link_tokens=(aliased,)), NOW)

    def test_bearer_cannot_alias_a_synthesized_canonical_row_id(self) -> None:
        row_alias = replace(
            records(),
            doctor=doctor().model_copy(update={"web_token": "patient:p1"}),
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(row_alias, NOW)

        red = card_event("event-123", notification_class="DANGER")
        action_alias = replace(
            records(events=(red,)),
            doctor=doctor().model_copy(update={"web_token": "seen:event-123"}),
        )
        with self.assertRaises(InvalidWorkspace):
            build_snapshot(action_alias, NOW)

    def test_bearers_used_as_metadata_keys_are_redacted(self) -> None:
        secret = LINK_SECRET_A
        token = LinkToken(
            id=secret,
            doctor_id="d1",
            patient_id="p1",
            created_at=NOW,
        )
        event = Event(
            id="metadata-key-event",
            doctor_id="d1",
            patient_id="p1",
            kind="system",
            meta={secret: "sensitive key was present"},
            ts=NOW,
        )
        snapshot = build_snapshot(
            records(events=(event,), link_tokens=(token,)), NOW
        )
        raw = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn(secret, raw)
        self.assertIn("[REDACTED_CREDENTIAL]", raw)
        self.assertIn("sensitive key was present", raw)

    def test_bearers_used_as_contract_state_keys_are_redacted(self) -> None:
        token = LinkToken(
            id=LINK_SECRET_A,
            doctor_id="d1",
            patient_id="p1",
            created_at=NOW,
        )
        patient_loop = loop(
            "contract-loop",
            "received",
            verified={LINK_SECRET_A: "sensitive key was present"},
        )

        snapshot = build_snapshot(
            records(loops=(patient_loop,), link_tokens=(token,)),
            NOW,
            selected_patient_id="p1",
        )
        raw = json.dumps(snapshot["selected_patient"]["contracts"], sort_keys=True)

        self.assertNotIn(LINK_SECRET_A, raw)
        self.assertIn("[REDACTED_CREDENTIAL]", raw)
        self.assertIn("sensitive key was present", raw)

    def test_human_readable_bearers_fail_before_rewriting_schema_keys(self) -> None:
        for secret in (
            "snapshot_id",
            "closed_today",
            "terminal_waiting_review",
            "[REDACTED_CREDENTIAL]",
            "[REDACTED",
            "CREDENTIAL]",
        ):
            with self.subTest(secret=secret):
                unsafe = replace(
                    records(),
                    doctor=doctor().model_copy(update={"web_token": secret}),
                )
                with self.assertRaises(InvalidWorkspace):
                    build_snapshot(unsafe, NOW)

    def test_bearer_substring_cannot_corrupt_server_authored_cursor(self) -> None:
        baseline = build_snapshot(records(), NOW)
        body = baseline["event_cursor"].split(".", 1)[1]
        collision = next(
            body[index : index + 8]
            for index in range(len(body) - 7)
            if any(character.isdigit() for character in body[index : index + 8])
        )
        colliding_bundle = replace(
            records(),
            doctor=doctor().model_copy(update={"web_token": collision}),
        )

        snapshot = build_snapshot(colliding_bundle, NOW)

        self.assertEqual(snapshot["snapshot_id"], baseline["snapshot_id"])
        self.assertEqual(snapshot["event_cursor"], baseline["event_cursor"])
        self.assertNotIn("[REDACTED_CREDENTIAL]", snapshot["event_cursor"])
        build_snapshot(
            colliding_bundle,
            NOW,
            event_cursor=snapshot["event_cursor"],
        )

    def test_bearers_embedded_in_event_strings_are_redacted_and_not_hashed(self) -> None:
        def bundle_for(secret: str) -> WorkspaceRecords:
            token = LinkToken(
                id=secret,
                doctor_id="d1",
                patient_id="p1",
                created_at=NOW,
            )
            event = Event(
                id="credential-url-event",
                doctor_id="d1",
                patient_id="p1",
                kind="system",
                text="link minted",
                meta={
                    "card": {
                        "title": "Patient access",
                        "severity": "green",
                        "lines": [
                            f"https://t.me/SanadBot?start={secret}",
                            f"/p/{secret}",
                            f"/qr/{secret}.png",
                            "/c/token-d1/app",
                        ],
                        "actions": [],
                    }
                },
                ts=NOW,
            )
            return records(events=(event,), link_tokens=(token,))

        first_secret = LINK_SECRET_A
        second_secret = LINK_SECRET_B
        first = build_snapshot(bundle_for(first_secret), NOW)
        second = build_snapshot(bundle_for(second_secret), NOW)
        raw = json.dumps(first, sort_keys=True)

        self.assertNotIn(first_secret, raw)
        self.assertNotIn("token-d1", raw)
        self.assertIn("[REDACTED_CREDENTIAL]", raw)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
