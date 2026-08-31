"""Gate 0B acceptance tests for the frozen nine-beat Sanad journey.

These assertions intentionally repeat the accepted numbers and artifact names
instead of importing the runner's expectations.  A mistaken edit to the runner
therefore cannot silently move both sides of the characterization contract.
"""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import unittest
from collections import defaultdict
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
GOLDENS = HERE / "gate0b" / "goldens"

# The image is not the repository: app/.gcloudignore's `*.json` rule keeps
# credentials out and takes these goldens with them. tests/_fixtures.py is
# the one place that asks whether a fixture family is here.
from tests._fixtures import (  # noqa: E402 - beside the path it guards
    HAS_GOLDEN_JOURNEY, HAS_JSON_GOLDENS)

BEATS = (
    "beat-01-contract",
    "beat-02-durable-future",
    "beat-03-cost-barrier",
    "beat-04-incomplete-evidence",
    "beat-05-complete-evidence",
    "beat-06-critical-potassium",
    "beat-07-contact-guard",
    "beat-08-doctor-review",
    "beat-09-end-of-day",
)
BEAT_FILES = ("00-initial", *BEATS)
VIEWPORTS = {"375x812": (375, 812), "1440x1000": (1440, 1000)}
FIXED_START = "2026-08-30T02:47:00+00:00"

INITIAL_COUNTS = {
    "carried": 31,
    "completed_with_evidence": 3,
    "progressing": 17,
    "needed_help": 6,
    "unreachable": 1,
    "questions": 1,
    "criticals": 2,
    "attention": 11,
    "closed_without_evidence": 1,
    "lost": 0,
    "duplicates": 0,
}
FINAL_COUNTS = {
    "carried": 35,
    "completed_with_evidence": 4,
    "progressing": 19,
    "needed_help": 7,
    "unreachable": 1,
    "questions": 2,
    "criticals": 3,
    "attention": 13,
    "closed_without_evidence": 1,
    "lost": 0,
    "duplicates": 0,
}
TRACE_COUNTS = {
    "scenario_trigger_http_requests": 23,
    "model_calls": 25,
    "logical_outbound_messages": 33,
    "tasks_enqueued": 13,
    "task_handlers_executed": 12,
    "tasks_pending": 1,
}
JSON_ARTIFACTS = {
    *(f"beats/{name}.json" for name in BEAT_FILES),
    "traces/counts.json",
    "traces/delivery.json",
    "traces/http.json",
    "traces/messages.json",
    "traces/models.json",
    "traces/tasks.json",
}
SCREENSHOTS = {
    f"screenshots/{viewport}/{beat}.png"
    for viewport in VIEWPORTS
    for beat in BEATS
}
TARGET_BEATS = {
    "beat-02-durable-future",
    "beat-03-cost-barrier",
    "beat-04-incomplete-evidence",
    "beat-05-complete-evidence",
    "beat-06-critical-potassium",
    "beat-07-contact-guard",
    "beat-08-doctor-review",
}
EXPECTED_SELECTIONS = {
    "beat-01-contract": {
        "view": "patient", "patient_name": "Ahmed Ali",
        "anchor_kind": "patient-heading", "anchor_text": "ahmed ali",
    },
    "beat-02-durable-future": {
        "view": "patient", "patient_name": "Ahmed Ali",
        "anchor_kind": "selected-evidence", "anchor_text": "unreachable",
    },
    "beat-03-cost-barrier": {
        "view": "patient", "patient_name": "Ahmed Ali",
        "anchor_kind": "selected-evidence", "anchor_text": "hospital lab is free",
    },
    "beat-04-incomplete-evidence": {
        "view": "patient", "patient_name": "Ahmed Ali",
        "anchor_kind": "selected-evidence",
        "anchor_text": "triglycerides, hdl is missing",
    },
    "beat-05-complete-evidence": {
        "view": "patient", "patient_name": "Ahmed Ali",
        "anchor_kind": "selected-evidence", "anchor_text": "needs your review",
    },
    "beat-06-critical-potassium": {
        "view": "inbox", "patient_name": None,
        "anchor_kind": "selected-evidence", "anchor_text": "critical lab",
    },
    "beat-07-contact-guard": {
        "view": "patient", "patient_name": "Amany Roushdy",
        "anchor_kind": "selected-evidence", "anchor_text": "barrier needs you",
    },
    "beat-08-doctor-review": {
        "view": "patient", "patient_name": "Ahmed Ali",
        "anchor_kind": "selected-evidence", "anchor_text": "lipid panel",
    },
    "beat-09-end-of-day": {
        "view": "board", "patient_name": None,
        "anchor_kind": "board-heading", "anchor_text": "exception line",
    },
}
ATTESTATION_WIDTH = 16
ATTESTATION_HEIGHT = 8
ATTESTATION_INSET = 2
DOSSIER_SHA256 = "2ab94a42b90ae16f4e7660e0c7bf92daa5824c7f501e0bb68699f7f1688d329b"
CLEAN_PUBLIC_COMMIT = "17520ab3ff6b4b2a978f9437c2f3dd417a8770a1"
HERMETIC_BASELINE_COMMIT = "f9743a2c72e0dddb012ddbac3cbbbc413b740a3d"
DEPLOYED_REVISION_AT_GATE0_FREEZE = "sanad-00029-g9f"
LAST_VERIFIED_NINE_BEAT_REVISION = "sanad-00028-zjm"
PRIVATE_SOURCE_REPOSITORY_COMMIT = "4d938e3101dbae3c04995b6d4c77a7ef5f30dd2d"
S18_LIVE_RESULTS_SHA256 = "f6d17a70ac77261479eb59f52ff8d151817c337aeb140153529d89f5e7c7fe0c"
EXPERIMENTAL_FREEZE_HASHES = {
    "manifest_sha256": "f81ab998177f299fc3b1066b697ce70d9a462a4c9d09a9916d2e27f62a3a073f",
    "active_tracked_patch_sha256": "1d8a3e66b3894c898072d9fb24944c32a7b812e3e823f0914c3d3711e609f922",
    "active_tree_snapshot_sha256": "a9041e10924a55e98356384819e7e332c9faa945746680d6ec42511d243d574c",
}
NON_CLAIMS = [
    "Gemini, ADK, transcription, OCR accuracy, or live model determinism",
    "Firestore contention or durability",
    "Cloud Tasks durability, Cloud Storage, Google Cloud, or latency",
    "Telegram/provider acceptance, delivery, notification, or read receipt",
    "WhatsApp or channel switching",
    "exactly-once external delivery",
    "clinical safety or readiness for real patients",
    "the future S23 Orchestra, Steward, Outcome Kernel, Evidence Orchestrator, or Closure Auditor",
]


def _read(relative: str) -> Any:
    return json.loads((GOLDENS / relative).read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha(value: Any) -> str:
    raw = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _compact_sha(value: Any) -> str:
    raw = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _attestation_tag(session_nonce: str, capture_id: str, beat: str) -> str:
    return hashlib.sha256(
        f"sanad-gate0b-attestation/v2\0{session_nonce}\0{capture_id}\0{beat}".encode()
    ).hexdigest()[:32]


def _decode_attestation(picture: Any) -> str:
    rgb = picture.convert("RGB")
    width, height = rgb.size
    left = width - ATTESTATION_INSET - ATTESTATION_WIDTH
    top = height - ATTESTATION_INSET - ATTESTATION_HEIGHT
    bits: list[int] = []
    for y in range(ATTESTATION_HEIGHT):
        for x in range(ATTESTATION_WIDTH):
            pixel = rgb.getpixel((left + x, top + y))
            if max(pixel) <= 32:
                bits.append(0)
            elif min(pixel) >= 223:
                bits.append(1)
            else:
                raise AssertionError(f"non-binary attestation pixel: {pixel!r}")
    return "".join(str(bit) for bit in bits)


def _core_counts(value: dict[str, Any]) -> dict[str, Any]:
    return {name: value.get(name) for name in FINAL_COUNTS}


def _fixture_patient_id(beat: str, name: str | None) -> str:
    if name is None:
        return ""
    patients = _read(f"beats/{beat}.json")["api"]["patients"]
    matches = [
        patient_id for patient_id, payload in patients.items()
        if payload.get("patient", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"{beat}: expected one patient named {name!r}: {matches!r}")
    return matches[0]


@HAS_JSON_GOLDENS
class CommittedCharacterizationIsCoherent(unittest.TestCase):
    """Static checks fail quickly before the more expensive replay runs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _read("manifest.json")
        cls.counts = _read("traces/counts.json")
        cls.http = _read("traces/http.json")
        cls.messages = _read("traces/messages.json")
        cls.delivery = _read("traces/delivery.json")
        cls.models = _read("traces/models.json")
        cls.tasks = _read("traces/tasks.json")

    def test_manifest_hashes_every_committed_artifact(self) -> None:
        recorded = self.manifest["artifact_sha256"]
        actual = {
            path.relative_to(GOLDENS).as_posix(): _sha(path)
            for path in sorted(GOLDENS.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        }
        self.assertEqual(set(recorded), set(actual))
        self.assertEqual(recorded, actual)
        expected_json = set(JSON_ARTIFACTS)
        for name in ("screenshot-receipts.json", "screenshot-provenance.json"):
            if (GOLDENS / name).is_file():
                expected_json.add(name)
        self.assertEqual(expected_json, {name for name in recorded if name.endswith(".json")})

    def test_ten_beat_fixtures_are_complete_and_self_hashed(self) -> None:
        found = {
            path.stem for path in (GOLDENS / "beats").glob("*.json")
        }
        self.assertEqual(set(BEAT_FILES), found)

        previous = {"http": -1, "outbound": -1, "delivery": -1, "models": -1, "tasks": -1}
        for name in BEAT_FILES:
            snapshot = _read(f"beats/{name}.json")
            self.assertEqual(name, snapshot["label"])
            self.assertIs(snapshot["synthetic"], True)
            self.assertEqual(snapshot["state_sha256"], _canonical_sha(snapshot["state"]))
            self.assertEqual(snapshot["api_sha256"], _canonical_sha(snapshot["api"]))
            self.assertEqual(
                {"board", "cards", "feed", "health", "patient_feed", "patients", "qr", "reports", "settings", "summary"},
                set(snapshot["api"]),
            )
            monitors = [
                patient for patient in snapshot["api"]["board"].get("patients", [])
                if any(
                    str(loop.get("type", "")).upper() == "MONITOR"
                    and loop.get("state") != "done"
                    for loop in patient.get("loops", [])
                )
            ]
            if monitors:
                self.assertIn(monitors[0]["id"], snapshot["api"]["patients"])
            qr_link = snapshot["api"]["board"].get("qr")
            qr = snapshot["api"]["qr"]
            if qr_link:
                self.assertEqual(qr_link["url"], qr["path"])
                raw = base64.b64decode(qr["base64"], validate=True)
                self.assertEqual(b"\x89PNG\r\n\x1a\n", raw[:8])
                self.assertEqual(len(raw), qr["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), qr["sha256"])
            else:
                self.assertIsNone(qr)
            for cursor, value in snapshot["ledger_cursors"].items():
                self.assertGreaterEqual(value, previous[cursor], f"{name}: {cursor} moved backwards")
                previous[cursor] = value

        initial = _read("beats/00-initial.json")["api"]["summary"]["counts"]
        final = _read("beats/beat-09-end-of-day.json")["api"]["summary"]["counts"]
        self.assertEqual(INITIAL_COUNTS, _core_counts(initial))
        self.assertEqual(FINAL_COUNTS, _core_counts(final))

    def test_historical_record_and_deterministic_replay_match_exactly(self) -> None:
        historical = self.manifest["historical_live_reference"]
        self.assertEqual(
            "private frozen S18 live results, recorded 2026-08-30",
            historical["source"],
        )
        self.assertEqual(
            "research/s18-live-results.md in the frozen private Sanad tree",
            historical["source_file"],
        )
        self.assertEqual(S18_LIVE_RESULTS_SHA256, historical["source_sha256"])
        self.assertEqual(
            "2026-08-30 05:47 to 06:15 Africa/Cairo",
            historical["observed_window"],
        )
        self.assertEqual(
            "moved out of quiet hours (22:00 to 09:00 Cairo)",
            historical["beat_3_quiet_hours_audit"],
        )
        self.assertEqual(FIXED_START, self.manifest["fixed_start"])
        self.assertEqual(LAST_VERIFIED_NINE_BEAT_REVISION, historical["serving_revision"])
        self.assertEqual(CLEAN_PUBLIC_COMMIT, historical["clean_public_commit"])
        self.assertEqual(FINAL_COUNTS, historical["counts"])
        self.assertIs(historical["used_as_replay_acceptance_oracle"], True)
        self.assertEqual(FINAL_COUNTS, _core_counts(self.manifest["replay_final_counts"]))
        self.assertEqual(FINAL_COUNTS, _core_counts(
            _read("beats/beat-09-end-of-day.json")["api"]["summary"]["counts"]
        ))
        beat_3 = _read("beats/beat-03-cost-barrier.json")
        audit_lines = [
            str(((event.get("meta") or {}).get("audit") or {}).get("line") or "")
            for event in beat_3["state"]["events"]
        ]
        self.assertEqual(
            1,
            sum(
                "moved out of quiet hours (22:00 to 09:00 Cairo)" in line
                for line in audit_lines
            ),
        )
        pending = [row for row in self.tasks["tasks"] if row["state"] == "pending"]
        self.assertEqual(1, len(pending))
        self.assertTrue(pending[0]["scheduled_at"].startswith("2026-08-31T06:00:00"))

    def test_gate0_source_and_non_claim_pins_cannot_move_with_regeneration(self) -> None:
        self.assertEqual(DOSSIER_SHA256, self.manifest["dossier_sha256"])
        self.assertEqual(HERMETIC_BASELINE_COMMIT, self.manifest["baseline_commit"])
        self.assertEqual(
            {
                "clean_public_commit": CLEAN_PUBLIC_COMMIT,
                "hermetic_baseline_commit": HERMETIC_BASELINE_COMMIT,
                "deployed_revision_at_gate0_freeze": DEPLOYED_REVISION_AT_GATE0_FREEZE,
                "last_verified_nine_beat_revision": LAST_VERIFIED_NINE_BEAT_REVISION,
                "private_source_repository_commit": PRIVATE_SOURCE_REPOSITORY_COMMIT,
            },
            self.manifest["source_baseline"],
        )
        freeze = self.manifest["experimental_tree_freeze"]
        self.assertEqual("sanad-s23-freeze-2026-08-30", freeze["archive_name"])
        self.assertIs(freeze["captured_before_s23"], True)
        for field, expected in EXPERIMENTAL_FREEZE_HASHES.items():
            self.assertEqual(expected, freeze[field])
        self.assertEqual(NON_CLAIMS, self.manifest["non_claims"])

    def test_exact_trace_totals_are_derived_from_the_ledgers(self) -> None:
        trigger_categories = {"scenario_mutation", "task_callback", "scenario_observation"}
        observed = {
            "scenario_trigger_http_requests": sum(
                row["category"] in trigger_categories for row in self.http
            ),
            "model_calls": len(self.models),
            "logical_outbound_messages": len(self.messages),
            "tasks_enqueued": len(self.tasks["tasks"]),
            "task_handlers_executed": sum(
                row["state"] == "completed" for row in self.tasks["tasks"]
            ),
            "tasks_pending": sum(
                row["state"] == "pending" for row in self.tasks["tasks"]
            ),
        }
        self.assertEqual(TRACE_COUNTS, observed)
        self.assertEqual(TRACE_COUNTS, self.counts["scenario_trigger_totals"])
        self.assertEqual(TRACE_COUNTS, self.manifest["replay_trace_counts"])
        self.assertEqual(25, self.counts["models"]["total"])
        self.assertEqual(33, self.counts["logical_outbound_total"])
        self.assertEqual(
            {"completed": 12, "pending": 1}, self.counts["tasks"]["states"]
        )

    def test_trace_proves_real_api_and_task_callback_paths_were_driven(self) -> None:
        self.assertEqual(list(range(1, len(self.http) + 1)), [row["sequence"] for row in self.http])
        self.assertTrue(all(row["status"] == 200 for row in self.http))

        callbacks = [row for row in self.http if row["category"] == "task_callback"]
        self.assertEqual(12, len(callbacks))
        self.assertTrue(all(
            (row["method"], row["path"], row["beat"])
            == ("POST", "/tasks/nudge", "beat-02-durable-future")
            for row in callbacks
        ))
        self.assertEqual(12, len(self.tasks["verifications"]))
        self.assertTrue(all(row["path"] == "/tasks/nudge" for row in self.tasks["tasks"]))

        mutations = [
            (row["method"], row["path"])
            for row in self.http
            if row["category"] == "scenario_mutation"
        ]
        self.assertTrue(any(method == "POST" and path.endswith("/doctor") for method, path in mutations))
        self.assertTrue(any(method == "POST" and path.endswith("/action") for method, path in mutations))
        self.assertTrue(any(method == "GET" and path.startswith("/p/") for method, path in mutations))
        self.assertEqual(5, sum(
            method == "POST" and "/patient/" in path for method, path in mutations
        ))
        observations = [row for row in self.http if row["category"] == "scenario_observation"]
        self.assertEqual(1, len(observations))
        self.assertEqual(("GET", "/summary"), (
            observations[0]["method"], observations[0]["path"][-8:]
        ))

        snapshot_suffixes = {"/board", "/cards", "/feed?since=0", "/reports", "/settings", "/summary"}
        snapshot_paths = {
            suffix
            for row in self.http
            if row["category"] == "snapshot_read"
            for suffix in snapshot_suffixes
            if row["path"].endswith(suffix)
        }
        self.assertEqual(snapshot_suffixes, snapshot_paths)
        self.assertTrue(any(
            row["category"] == "snapshot_read" and row["path"] == "/health"
            for row in self.http
        ))

    def test_delivery_ledger_makes_no_provider_delivery_claim(self) -> None:
        self.assertEqual(list(range(1, 34)), [row["sequence"] for row in self.messages])
        self.assertEqual({"intent_recorded"}, {row["outcome"] for row in self.messages})

        by_outbound: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self.delivery:
            by_outbound[row["outbound_sequence"]].append(row)
        self.assertEqual(set(range(1, 34)), set(by_outbound))
        for sequence, rows in by_outbound.items():
            self.assertEqual({"web", "telegram"}, {row["channel"] for row in rows})
            outcomes = {row["channel"]: row["outcome"] for row in rows}
            self.assertEqual("persisted_event", outcomes["web"])
            self.assertEqual("skipped_disabled_unbound", outcomes["telegram"])
            telegram_row = next(row for row in rows if row["channel"] == "telegram")
            self.assertIsNone(telegram_row["receipt"])
            message = self.messages[sequence - 1]
            web_row = next(row for row in rows if row["channel"] == "web")
            self.assertEqual(message["web_event_receipt"], web_row["receipt"])

        forbidden = {"delivered", "accepted", "notified", "read"}
        self.assertTrue(forbidden.isdisjoint({row["outcome"] for row in self.delivery}))
        self.assertEqual(
            {"telegram:skipped_disabled_unbound": 33, "web:persisted_event": 33},
            self.counts["delivery_by_channel_and_outcome"],
        )
        self.assertIn("disabled and unbound", self.manifest["telegram"])
        self.assertTrue(any(
            "Telegram/provider acceptance" in statement
            for statement in self.manifest["non_claims"]
        ))

    def test_screenshot_contract_and_exact_png_dimensions(self) -> None:
        contract = self.manifest["screenshot_contract"]
        self.assertEqual(set(VIEWPORTS), set(contract["viewports"]))
        self.assertEqual(1, contract["dpr"])
        self.assertEqual("light", contract["theme"])
        self.assertIs(contract["reduced_motion"], True)
        self.assertEqual(SCREENSHOTS, set(contract["files"]))
        self.assertEqual(18, len(contract["files"]))

        manifested = {
            name for name in self.manifest["artifact_sha256"]
            if name.endswith(".png")
        }
        on_disk = {
            path.relative_to(GOLDENS).as_posix()
            for path in GOLDENS.rglob("*.png")
        }
        if not manifested and not on_disk:
            self.skipTest("Gate 0B screenshot capture has not been committed yet")

        self.assertEqual(SCREENSHOTS, manifested)
        self.assertEqual(SCREENSHOTS, on_disk)
        from PIL import Image

        for relative in sorted(SCREENSHOTS):
            viewport = relative.split("/")[1]
            with Image.open(GOLDENS / relative) as picture:
                self.assertEqual("PNG", picture.format, relative)
                self.assertEqual(VIEWPORTS[viewport], picture.size, relative)

        receipts = _read(self.manifest["screenshot_receipts"])
        provenance = _read(self.manifest["screenshot_provenance"])
        self.assertEqual("sanad-gate0b-browser-receipts/v3", receipts["schema"])
        self.assertEqual("sanad-gate0b-screenshot-provenance/v3", provenance["schema"])
        self.assertEqual(18, len(receipts["ready"]))
        self.assertEqual({}, receipts["failures"])
        nonce = receipts["capture_session_nonce"]
        self.assertEqual(64, len(nonce))
        self.assertTrue(all(character in "0123456789abcdef" for character in nonce))
        self.assertEqual(
            self.manifest["screenshot_contract"]["dashboard_sha256"],
            receipts["dashboard_sha256"],
        )
        self.assertEqual(
            _sha(HERE.parent / "web" / "dashboard.html"),
            receipts["dashboard_sha256"],
        )
        self.assertEqual(
            _sha(HERE / "gate0b" / "replay.py"),
            receipts["replay_helper_sha256"],
        )
        self.assertEqual(receipts["dashboard_sha256"], provenance["dashboard"]["sha256"])
        self.assertEqual(receipts["replay_helper_sha256"], provenance["replay_helper_sha256"])
        self.assertEqual(
            _sha(GOLDENS / self.manifest["screenshot_receipts"]),
            provenance["readiness_receipts"]["sha256"],
        )
        self.assertEqual(18, provenance["readiness_receipts"]["count"])
        self.assertEqual(
            hashlib.sha256(nonce.encode()).hexdigest(),
            provenance["readiness_receipts"]["capture_session_nonce_sha256"],
        )
        self.assertTrue(provenance["browser"]["protocol_version"])
        identity_fields = {
            "capture_tool", "protocol_version", "product", "revision",
            "user_agent", "js_version",
        }
        provenance_identity = {
            field: provenance["browser"][field] for field in identity_fields
        }
        self.assertTrue(all(provenance_identity.values()))
        self.assertIn("not claimed portable", provenance["contract"]["font_policy"])
        for beat in BEATS:
            self.assertEqual(
                _sha(GOLDENS / f"beats/{beat}.json"),
                provenance["fixture_sha256"][f"beats/{beat}.json"],
            )
        for relative in sorted(SCREENSHOTS):
            row = provenance["screenshots"][relative]
            actual_png_sha256 = _sha(GOLDENS / relative)
            self.assertEqual(actual_png_sha256, row["sha256"])
            receipt = receipts["ready"][row["capture_id"]]
            beat = relative.rsplit("/", 1)[-1][:-4]
            viewport = relative.split("/")[1]
            width, height = VIEWPORTS[viewport]
            capture_id = f"chrome-{viewport}-{beat}"
            self.assertEqual(capture_id, row["capture_id"])
            self.assertEqual(beat, receipt["beat"])
            self.assertEqual([receipt["width"], receipt["height"]], [row["width"], row["height"]])
            expected_tag = _attestation_tag(nonce, capture_id, beat)
            self.assertEqual(expected_tag, receipt["attestation_tag"])
            self.assertEqual(expected_tag, row["attestation_tag"])
            self.assertEqual(actual_png_sha256, receipt["png_sha256"])
            self.assertEqual(provenance_identity, receipt["capture_identity"])
            self.assertEqual(_compact_sha(receipt), row["readiness_receipt_sha256"])
            self.assertEqual(
                {"expected": 1, "observed": 1, "decoded": 1, "failures": 0},
                receipt["images"],
            )
            selection = EXPECTED_SELECTIONS[beat]
            self.assertEqual(selection["view"], receipt["view"])
            proof = receipt["view_proof"]
            self.assertEqual(selection["view"], proof["observed_view"])
            self.assertEqual([selection["view"]], proof["visible_views"])
            self.assertEqual(selection["view"], proof["state_view"])
            self.assertIs(proof["nav_current"], True)
            self.assertEqual(
                _fixture_patient_id(beat, selection["patient_name"]),
                proof["patient_id"],
            )
            self.assertEqual(selection["patient_name"] or "", proof["patient_name"])
            self.assertIs(proof["patient_heading_matches"], True)
            self.assertIs(proof["anchor_ancestor_chain_visible"], True)
            self.assertEqual(selection["anchor_kind"], proof["anchor_kind"])
            self.assertIn(selection["anchor_text"], proof["anchor_text"])
            anchor = proof["anchor_geometry"]
            self.assertEqual(width, anchor["visual_width"])
            self.assertEqual(height, anchor["visual_height"])
            self.assertGreaterEqual(anchor["top"], anchor["occlusion_bottom"] + 1)
            self.assertLessEqual(anchor["bottom"], height)
            self.assertGreater(anchor["bottom"], anchor["top"])
            self.assertGreaterEqual(anchor["left"], 0)
            self.assertLessEqual(anchor["right"], width)
            self.assertGreater(anchor["right"], anchor["left"])
            self.assertIs(anchor["unobscured"], True)
            geometry = receipt["target_geometry"]
            self.assertEqual(width, geometry["visual_width"])
            self.assertEqual(height, geometry["visual_height"])
            self.assertIs(geometry["unobscured"], True)
            self.assertGreaterEqual(geometry["occlusion_bottom"], 0)
            self.assertLess(geometry["occlusion_bottom"], height)
            if beat in TARGET_BEATS:
                self.assertEqual("visible", receipt["target"])
                self.assertGreaterEqual(
                    geometry["top"], geometry["occlusion_bottom"] + 8
                )
                self.assertGreater(geometry["right"], geometry["left"])
                self.assertGreaterEqual(geometry["left"], 0)
                self.assertLessEqual(geometry["right"], width)
                self.assertGreaterEqual(
                    min(geometry["bottom"], height)
                    - max(geometry["top"], geometry["occlusion_bottom"]),
                    min(geometry["bottom"] - geometry["top"], 48),
                )
                self.assertGreaterEqual(
                    geometry["evidence_top"], geometry["occlusion_bottom"] + 1
                )
                self.assertLessEqual(geometry["evidence_bottom"], height)
                self.assertGreater(geometry["evidence_bottom"], geometry["evidence_top"])
                self.assertGreaterEqual(geometry["evidence_left"], 0)
                self.assertLessEqual(geometry["evidence_right"], width)
                self.assertEqual(
                    (
                        geometry["evidence_top"], geometry["evidence_bottom"],
                        geometry["evidence_left"], geometry["evidence_right"],
                    ),
                    (anchor["top"], anchor["bottom"], anchor["left"], anchor["right"]),
                )
            else:
                self.assertEqual("none", receipt["target"])
                for field in (
                    "top", "bottom", "left", "right",
                    "evidence_top", "evidence_bottom", "evidence_left", "evidence_right",
                ):
                    self.assertEqual(-1, geometry[field])
            self.assertEqual(
                {
                    "source": "Chrome DevTools Protocol",
                    "inner_width": width,
                    "inner_height": height,
                    "device_pixel_ratio": 1.0,
                    "visual_width": width,
                    "visual_height": height,
                    "layout_client_width": width,
                    "layout_client_height": height,
                    "cdp_visual_client_width": width,
                    "cdp_visual_client_height": height,
                    "cdp_visual_scale": 1.0,
                    "png_width": width,
                    "png_height": height,
                },
                receipt["capture_metrics"],
            )
            with Image.open(GOLDENS / relative) as picture:
                attestation_bits = _decode_attestation(picture)
            self.assertEqual(expected_tag, f"{int(attestation_bits, 2):032x}")

        current_tags = {
            receipt["attestation_tag"] for receipt in receipts["ready"].values()
        }
        self.assertEqual(18, len(current_tags))
        changed_nonce = "0" * 64 if nonce != "0" * 64 else "1" * 64
        changed_tags = {
            _attestation_tag(changed_nonce, capture_id, receipt["beat"])
            for capture_id, receipt in receipts["ready"].items()
        }
        self.assertTrue(current_tags.isdisjoint(changed_tags))


@HAS_JSON_GOLDENS
class ScreenshotContractRejectsFalseEvidence(unittest.TestCase):
    def _valid_receipt(self) -> tuple[str, str, str, dict[str, Any]]:
        nonce = "a" * 64
        capture_id = "chrome-375x812-beat-02-durable-future"
        beat = "beat-02-durable-future"
        patient_id = _fixture_patient_id(beat, "Ahmed Ali")
        capture_identity = {
            "capture_tool": "Chrome DevTools Protocol device emulation via tests.gate0b.replay",
            "protocol_version": "1.3",
            "product": "Chrome/151.0.0.0",
            "revision": "@gate0b-test-revision",
            "user_agent": "Gate0B Test Chrome",
            "js_version": "15.1.0",
        }
        receipt = {
            "beat": beat,
            "view": "patient",
            "view_proof": {
                "observed_view": "patient",
                "visible_views": ["patient"],
                "state_view": "patient",
                "nav_current": True,
                "patient_id": patient_id,
                "patient_name": "Ahmed Ali",
                "patient_heading_matches": True,
                "anchor_ancestor_chain_visible": True,
                "anchor_kind": "selected-evidence",
                "anchor_text": "unreachable",
                "anchor_geometry": {
                    "top": 140,
                    "bottom": 170,
                    "left": 30,
                    "right": 300,
                    "occlusion_bottom": 114,
                    "visual_width": 375,
                    "visual_height": 812,
                    "unobscured": True,
                },
            },
            "target": "visible",
            "target_geometry": {
                "top": 130,
                "bottom": 230,
                "left": 16,
                "right": 359,
                "evidence_top": 140,
                "evidence_bottom": 170,
                "evidence_left": 30,
                "evidence_right": 300,
                "occlusion_bottom": 114,
                "visual_width": 375,
                "visual_height": 812,
                "unobscured": True,
            },
            "images": {"expected": 1, "observed": 1, "decoded": 1, "failures": 0},
            "capture_metrics": {
                "source": "Chrome DevTools Protocol",
                "inner_width": 375,
                "inner_height": 812,
                "device_pixel_ratio": 1.0,
                "visual_width": 375,
                "visual_height": 812,
                "layout_client_width": 375,
                "layout_client_height": 812,
                "cdp_visual_client_width": 375,
                "cdp_visual_client_height": 812,
                "cdp_visual_scale": 1.0,
                "png_width": 375,
                "png_height": 812,
            },
            "png_sha256": "c" * 64,
            "capture_identity": capture_identity,
            "width": 375,
            "height": 812,
            "dpr": 1.0,
            "attestation_tag": _attestation_tag(nonce, capture_id, beat),
        }
        return nonce, capture_id, beat, receipt

    def test_validator_rejects_hidden_stale_broken_or_wrong_viewport_evidence(self) -> None:
        from tests.gate0b.replay import _assert_ready_receipt

        nonce, capture_id, beat, receipt = self._valid_receipt()
        expected_png_sha256 = receipt["png_sha256"]
        expected_capture_identity = dict(receipt["capture_identity"])
        _assert_ready_receipt(
            receipt,
            session_nonce=nonce,
            capture_id=capture_id,
            slug=beat,
            width=375,
            height=812,
            png_sha256=expected_png_sha256,
            capture_identity=expected_capture_identity,
        )
        mutations: dict[str, Any] = {
            "under sticky header": lambda row: row["target_geometry"].update(top=115),
            "evidence below viewport": lambda row: row["target_geometry"].update(evidence_bottom=813),
            "broken image": lambda row: row["images"].update(decoded=0, failures=1),
            "500px layout masquerading as mobile": lambda row: row["capture_metrics"].update(inner_width=500),
            "wrong observed view": lambda row: row["view_proof"].update(observed_view="board"),
            "multiple visible views": lambda row: row["view_proof"].update(visible_views=["patient", "board"]),
            "wrong state view": lambda row: row["view_proof"].update(state_view="board"),
            "wrong patient identity": lambda row: row["view_proof"].update(patient_name="Another Patient"),
            "hidden patient heading": lambda row: row["view_proof"].update(patient_heading_matches=False),
            "hidden evidence ancestor": lambda row: row["view_proof"].update(anchor_ancestor_chain_visible=False),
            "hidden view anchor": lambda row: row["view_proof"]["anchor_geometry"].update(unobscured=False),
            "wrong selected text": lambda row: row["view_proof"].update(anchor_text="different evidence"),
            "changed PNG bytes": lambda row: row.update(png_sha256="d" * 64),
            "different browser process": lambda row: row["capture_identity"].update(product="Chrome/other"),
            "stale session marker": lambda row: row.update(
                attestation_tag=_attestation_tag("b" * 64, capture_id, beat)
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(receipt))
                mutate(changed)
                with self.assertRaises(ValueError):
                    _assert_ready_receipt(
                        changed,
                        session_nonce=nonce,
                        capture_id=capture_id,
                        slug=beat,
                        width=375,
                        height=812,
                        png_sha256=expected_png_sha256,
                        capture_identity=expected_capture_identity,
                    )

    def test_one_changed_marker_bit_no_longer_matches_the_receipt(self) -> None:
        from PIL import Image

        nonce, capture_id, beat, _receipt = self._valid_receipt()
        tag = _attestation_tag(nonce, capture_id, beat)
        bits = f"{int(tag, 16):0128b}"
        picture = Image.new("RGB", (375, 812), "white")
        left = 375 - ATTESTATION_INSET - ATTESTATION_WIDTH
        top = 812 - ATTESTATION_INSET - ATTESTATION_HEIGHT
        for index, bit in enumerate(bits):
            picture.putpixel(
                (left + index % ATTESTATION_WIDTH, top + index // ATTESTATION_WIDTH),
                (255, 255, 255) if bit == "1" else (0, 0, 0),
            )
        self.assertEqual(tag, f"{int(_decode_attestation(picture), 2):032x}")
        first = picture.getpixel((left, top))
        picture.putpixel((left, top), (0, 0, 0) if first == (255, 255, 255) else (255, 255, 255))
        self.assertNotEqual(tag, f"{int(_decode_attestation(picture), 2):032x}")

    def test_same_marker_altered_png_is_rejected_by_receipt_hash(self) -> None:
        from PIL import Image
        from tests.gate0b.replay import _assert_ready_receipt

        nonce, capture_id, beat, receipt = self._valid_receipt()
        tag = _attestation_tag(nonce, capture_id, beat)
        bits = f"{int(tag, 16):0128b}"
        picture = Image.new("RGB", (375, 812), "white")
        left = 375 - ATTESTATION_INSET - ATTESTATION_WIDTH
        top = 812 - ATTESTATION_INSET - ATTESTATION_HEIGHT
        for index, bit in enumerate(bits):
            picture.putpixel(
                (left + index % ATTESTATION_WIDTH, top + index // ATTESTATION_WIDTH),
                (255, 255, 255) if bit == "1" else (0, 0, 0),
            )
        original = BytesIO()
        picture.save(original, format="PNG")
        original_sha256 = hashlib.sha256(original.getvalue()).hexdigest()
        receipt["png_sha256"] = original_sha256
        capture_identity = dict(receipt["capture_identity"])
        _assert_ready_receipt(
            receipt,
            session_nonce=nonce,
            capture_id=capture_id,
            slug=beat,
            width=375,
            height=812,
            png_sha256=original_sha256,
            capture_identity=capture_identity,
        )

        picture.putpixel((0, 0), (254, 254, 254))
        altered = BytesIO()
        picture.save(altered, format="PNG")
        altered_sha256 = hashlib.sha256(altered.getvalue()).hexdigest()
        self.assertNotEqual(original_sha256, altered_sha256)
        self.assertEqual(tag, f"{int(_decode_attestation(picture), 2):032x}")
        with self.assertRaises(ValueError):
            _assert_ready_receipt(
                receipt,
                session_nonce=nonce,
                capture_id=capture_id,
                slug=beat,
                width=375,
                height=812,
                png_sha256=altered_sha256,
                capture_identity=capture_identity,
            )


@HAS_GOLDEN_JOURNEY
class ReplayMatchesCommittedGoldens(unittest.IsolatedAsyncioTestCase):
    async def test_full_route_replay_is_byte_stable_against_every_json_golden(self) -> None:
        # Importing here keeps the static integrity checks independent of the
        # application and avoids initializing it when only hashes are audited.
        from tests.gate0b.artifacts import legacy_projection
        from tests.gate0b.scenario import GoldenJourney

        result = await GoldenJourney().run()
        payloads = result.artifact_payloads()
        self.assertEqual(JSON_ARTIFACTS, set(payloads))
        for relative in sorted(JSON_ARTIFACTS):
            baseline = _read(relative)
            replay_hash = _canonical_sha(
                legacy_projection(payloads[relative], baseline)
            )
            committed_hash = _sha(GOLDENS / relative)
            self.assertEqual(
                committed_hash,
                replay_hash,
                f"deterministic replay diverged at {relative}",
            )

        committed_manifest = dict(_read("manifest.json"))
        committed_manifest.pop("artifact_sha256")
        self.assertEqual(committed_manifest, result.manifest)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
