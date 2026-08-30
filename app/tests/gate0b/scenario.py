"""Executable nine-beat Gate 0B legacy characterization.

The runner talks only to the real FastAPI routes.  Persistence, provider I/O
and model inference are finite test adapters; routing, policy, validation,
scheduling, evidence handling, cards, summaries and WebAdapter persistence are
the production implementations.  Nothing in this package is imported by the
application.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from contextlib import AsyncExitStack, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

import main as sanad_main
from core import media, storage, tasks, telegram

from .artifacts import GOLDENS, canonical, digest, write_json
from .boundaries import (
    AMANY_MESSAGE,
    COST_MESSAGE,
    DICTATION,
    ScriptedBoundaries,
)
from .memory import MemoryStore
from .traces import JourneyTrace, instrument_delivery
from .virtual_queue import VIRTUAL_TASK_AUTHORIZATION, VirtualTaskQueue


APP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = APP_ROOT.parent
SEED = REPO_ROOT / "docs" / "seed"
ADMIN_SECRET = "gate0b-local-admin"
RUN_ID = "gate0b-run-001"
BASE_URL = "http://sanad.test"
DOCTOR_NAME = "Test Doctor"
DOCTOR_SPECIALTY = "cardiology"
# The accepted S18 journey ran from 05:47 Cairo and its Beat 3 resumption
# therefore exercised the real-scale quiet-hours adjustment.  Starting at noon
# would preserve the final counts while silently characterizing a different
# policy branch.
FIXED_START = "2026-08-30T02:47:00+00:00"
DOSSIER_SHA256 = "2ab94a42b90ae16f4e7660e0c7bf92daa5824c7f501e0bb68699f7f1688d329b"
CLEAN_PUBLIC_COMMIT = "17520ab3ff6b4b2a978f9437c2f3dd417a8770a1"
HERMETIC_BASELINE_COMMIT = "f9743a2c72e0dddb012ddbac3cbbbc413b740a3d"
DEPLOYED_REVISION_AT_GATE0_FREEZE = "sanad-00029-g9f"
LAST_VERIFIED_NINE_BEAT_REVISION = "sanad-00028-zjm"
PRIVATE_SOURCE_REPOSITORY_COMMIT = "4d938e3101dbae3c04995b6d4c77a7ef5f30dd2d"
EXPERIMENTAL_TREE_FREEZE = {
    "archive_name": "sanad-s23-freeze-2026-08-30",
    "captured_before_s23": True,
    "manifest_sha256": "f81ab998177f299fc3b1066b697ce70d9a462a4c9d09a9916d2e27f62a3a073f",
    "active_tracked_patch_sha256": "1d8a3e66b3894c898072d9fb24944c32a7b812e3e823f0914c3d3711e609f922",
    "active_tree_snapshot_sha256": "a9041e10924a55e98356384819e7e332c9faa945746680d6ec42511d243d574c",
    "contents": (
        "external manifest, full-index binary-capable tracked patch, and complete "
        "subtree archive including untracked files; not merged into the clean baseline"
    ),
}


BEATS: tuple[tuple[str, str], ...] = (
    ("beat-01-contract", "Contract"),
    ("beat-02-durable-future", "Durable future"),
    ("beat-03-cost-barrier", "Cost barrier"),
    ("beat-04-incomplete-evidence", "Incomplete evidence"),
    ("beat-05-complete-evidence", "Complete evidence"),
    ("beat-06-critical-potassium", "Critical potassium"),
    ("beat-07-contact-guard", "Contact guard"),
    ("beat-08-doctor-review", "Doctor review"),
    ("beat-09-end-of-day", "End of day"),
)

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

# A separate, historical observation.  The deterministic replay does not copy
# these numbers into state; it derives its result through the real routes and
# must match this independently recorded acceptance oracle exactly.
HISTORICAL_LIVE_FINAL = {
    "source": "private frozen S18 live results, recorded 2026-08-30",
    "source_file": "research/s18-live-results.md in the frozen private Sanad tree",
    "source_sha256": "f6d17a70ac77261479eb59f52ff8d151817c337aeb140153529d89f5e7c7fe0c",
    "observed_window": "2026-08-30 05:47 to 06:15 Africa/Cairo",
    "beat_3_quiet_hours_audit": (
        "moved out of quiet hours (22:00 to 09:00 Cairo)"
    ),
    "serving_revision": LAST_VERIFIED_NINE_BEAT_REVISION,
    "clean_public_commit": CLEAN_PUBLIC_COMMIT,
    "counts": {
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
    },
    "used_as_replay_acceptance_oracle": True,
}
EXPECTED_FINAL_COUNTS = dict(HISTORICAL_LIVE_FINAL["counts"])
EXPECTED_TRACE_COUNTS = {
    "scenario_trigger_http_requests": 23,
    "model_calls": 25,
    "logical_outbound_messages": 33,
    "tasks_enqueued": 13,
    "task_handlers_executed": 12,
    "tasks_pending": 1,
}


class ScenarioViolation(AssertionError):
    pass


def _one_action(cards: dict[str, Any], prefix: str, *, title: str = "") -> str:
    matches: list[str] = []
    for row in cards.get("cards", []):
        card = (row.get("meta") or {}).get("card") or {}
        if title and title not in str(card.get("title") or ""):
            continue
        for action in card.get("actions", []):
            ident = str(action.get("id") or "")
            if ident.startswith(prefix):
                matches.append(ident)
    unique = list(dict.fromkeys(matches))
    if len(unique) != 1:
        raise ScenarioViolation(
            f"expected one {prefix!r} action{f' on {title!r}' if title else ''}, "
            f"found {unique!r}"
        )
    return unique[0]


def _model_rows(mapping: dict[str, Any]) -> list[Any]:
    return [mapping[key] for key in sorted(mapping)]


def _dashboard_monitor_patient_id(board: dict[str, Any]) -> str | None:
    """Mirror dashboard.html refreshMonitor's deterministic patient choice."""
    patients = board.get("patients") or []
    candidates = [
        patient for patient in patients
        if any(
            str(loop.get("type") or "").upper() == "MONITOR"
            and loop.get("state") != "done"
            for loop in patient.get("loops") or []
        )
    ]
    pick = candidates[0] if candidates else next(
        (
            patient for patient in patients
            if any(
                str(loop.get("type") or "").upper() == "MONITOR"
                for loop in patient.get("loops") or []
            )
        ),
        None,
    )
    return str(pick.get("id")) if pick and pick.get("id") else None


def state_snapshot(memory: MemoryStore) -> dict[str, Any]:
    """Full normalized state, including claims the public API does not expose."""
    return canonical({
        "clock": memory.peek_now(),
        "settings": memory.settings,
        "doctors": _model_rows(memory.doctors),
        "patients": _model_rows(memory.patients),
        "loops": _model_rows(memory.loops),
        "events": _model_rows(memory.events),
        "reports": _model_rows(memory.reports),
        "confirms": _model_rows(memory.confirms),
        "link_tokens": _model_rows(memory.link_tokens),
        "relays": _model_rows(memory.relays),
        "sends": _model_rows(memory.sends),
        "contacts": memory.contacts,
        "card_actions": memory.card_actions,
        "patient_turns": memory.patient_turns,
        "photo_receipts": memory.photo_receipts,
        "confirm_claims": memory.confirm_claims,
    })


@contextmanager
def provider_boundaries() -> Iterator[None]:
    """Replace only provider effects and make every missed seam loud."""
    async def put_image(raw: bytes, *, run_id: str, patient_id: str,
                        mime: str = "image/jpeg") -> str:
        suffix = "png" if mime == "image/png" else "jpg"
        sha = hashlib.sha256(raw).hexdigest()[:16]
        return f"gs://synthetic-gate0b/{run_id}/{patient_id}/{sha}.{suffix}"

    async def deep_link(token: str) -> str:
        return f"https://t.me/SanadSyntheticBot?start={token}"

    async def no_telegram(*_: Any, **__: Any) -> None:
        raise ScenarioViolation("Gate 0B attempted a Telegram provider send")

    async def ffmpeg_version() -> str:
        return "not-invoked-in-gate0b-text-and-image-replay"

    with ExitStack() as stack:
        stack.enter_context(patch.object(storage, "put_image", put_image))
        stack.enter_context(patch.object(storage, "enabled", lambda: False))
        stack.enter_context(patch.object(telegram, "enabled", lambda: False))
        stack.enter_context(patch.object(telegram, "deep_link", deep_link))
        stack.enter_context(patch.object(telegram, "send_card", no_telegram))
        stack.enter_context(patch.object(media, "ffmpeg_version_async", ffmpeg_version))
        stack.enter_context(patch.object(tasks, "engine", lambda: "virtual-gate0b"))
        stack.enter_context(patch.object(sanad_main, "MODEL", "scripted-boundary"))
        yield


@dataclass
class ScenarioResult:
    initial: dict[str, Any]
    beats: dict[str, dict[str, Any]]
    traces: dict[str, Any]
    manifest: dict[str, Any]

    def artifact_payloads(self) -> dict[str, Any]:
        payloads: dict[str, Any] = {"beats/00-initial.json": self.initial}
        payloads.update(
            {f"beats/{name}.json": value for name, value in self.beats.items()}
        )
        payloads.update(
            {f"traces/{name}.json": value for name, value in self.traces.items()}
        )
        return payloads


class GoldenJourney:
    def __init__(self) -> None:
        self.memory = MemoryStore(start=datetime.fromisoformat(FIXED_START))
        self.boundaries = ScriptedBoundaries()
        self.queue = VirtualTaskQueue(self.memory.peek_now)
        self.trace = JourneyTrace()
        self.beats: dict[str, dict[str, Any]] = {}
        self.initial: dict[str, Any] = {}
        self.client: Optional[httpx.AsyncClient] = None
        self.token = ""
        self.doctor_id = ""
        self.ahmed_id = ""
        self.ahmed_link = ""
        self.amany_id = ""

    async def request(
        self,
        category: str,
        method: str,
        path: str,
        *,
        expect: int = 200,
        data: Optional[dict[str, Any]] = None,
        json: Any = None,
        files: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> tuple[httpx.Response, Any]:
        if self.client is None:
            raise RuntimeError("Gate 0B client is not open")
        response = await self.client.request(
            method, path, data=data, json=json, files=files, headers=headers
        )
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            body: Any = response.json()
        else:
            body = {
                "content_type": content_type.split(";", 1)[0],
                "bytes": len(response.content),
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
        file_note = None
        if files:
            file_note = {}
            for field, given in files.items():
                filename, raw, mime = given
                file_note[field] = {
                    "filename": filename,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "content_type": mime,
                }
        self.trace.add_http(
            category=category, method=method, path=path,
            status=response.status_code,
            request={"data": data, "json": json, "files": file_note},
            response=body,
        )
        if response.status_code != expect:
            raise ScenarioViolation(
                f"{method} {path}: expected {expect}, got {response.status_code}: "
                f"{body!r}"
            )
        return response, body

    async def admin(
        self, method: str, path: str, *, category: str = "setup", **kwargs: Any
    ) -> Any:
        headers = {sanad_main.ADMIN_HEADER: ADMIN_SECRET}
        _, body = await self.request(
            category, method, path, headers=headers, **kwargs
        )
        return body

    async def _setup(self) -> None:
        os.environ["ADMIN_SECRET"] = ADMIN_SECRET
        seeded = await self.admin(
            "POST", f"/admin/seed?name={DOCTOR_NAME.replace(' ', '%20')}"
            f"&specialty={DOCTOR_SPECIALTY}"
        )
        self.token = seeded["console_url"].rsplit("/", 1)[-1]
        self.doctor_id = seeded["doctor_id"]
        await self.admin(
            "POST", f"/admin/reset?name={DOCTOR_NAME.replace(' ', '%20')}"
        )
        await self.admin(
            "POST", f"/admin/settings?run_id={RUN_ID}&time_scale=3"
        )
        background = await self.admin(
            "POST", f"/admin/seed-background?name={DOCTOR_NAME.replace(' ', '%20')}"
        )
        if background.get("synthetic") is not True:
            raise ScenarioViolation("background seeder did not mark its records synthetic")
        self.amany_id = next(
            patient.id for patient in self.memory.patients.values()
            if patient.name == "Amany Roushdy"
        )
        initial = await self.capture("00-initial", include_patient_ids=[self.amany_id])
        observed = initial["api"]["summary"]["counts"]
        if {key: observed.get(key) for key in INITIAL_COUNTS} != INITIAL_COUNTS:
            raise ScenarioViolation(
                f"wrong seeded summary: {observed!r}"
            )
        self.initial = initial

    async def capture(
        self,
        label: str,
        *,
        include_patient_ids: list[str],
        summary_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        base = f"/c/{self.token}"
        api: dict[str, Any] = {}
        _, api["health"] = await self.request("snapshot_read", "GET", "/health")
        for name, path in (
            ("board", f"{base}/board"),
            ("cards", f"{base}/cards"),
            ("feed", f"{base}/feed?since=0"),
            ("reports", f"{base}/reports"),
            ("settings", f"{base}/settings"),
        ):
            _, api[name] = await self.request("snapshot_read", "GET", path)
        if summary_body is None:
            _, api["summary"] = await self.request(
                "snapshot_read", "GET", f"{base}/summary"
            )
        else:
            api["summary"] = summary_body
        monitor_patient_id = _dashboard_monitor_patient_id(api["board"])
        patient_ids = set(include_patient_ids)
        if monitor_patient_id:
            patient_ids.add(monitor_patient_id)
        patients: dict[str, Any] = {}
        for patient_id in sorted(patient_ids):
            _, patients[patient_id] = await self.request(
                "snapshot_read", "GET", f"{base}/patient/{patient_id}"
            )
        api["patients"] = patients
        qr = api["board"].get("qr")
        if qr and qr.get("url"):
            response, _ = await self.request(
                "snapshot_read", "GET", str(qr["url"])
            )
            raw = response.content
            api["qr"] = {
                "path": str(qr["url"]),
                "content_type": response.headers.get("content-type", "").split(";", 1)[0],
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "base64": base64.b64encode(raw).decode("ascii"),
            }
        else:
            api["qr"] = None
        if self.ahmed_link:
            _, api["patient_feed"] = await self.request(
                "snapshot_read", "GET", f"/p/{self.ahmed_link}/feed?since=0"
            )
        else:
            api["patient_feed"] = None
        snapshot = {
            "label": label,
            "synthetic": True,
            "captured_at": self.memory.peek_now(),
            "api": api,
            "state": state_snapshot(self.memory),
            "ledger_cursors": {
                **self.trace.cursors(),
                "models": len(self.boundaries.calls),
                "tasks": len(self.queue.tasks),
            },
        }
        snapshot["state_sha256"] = digest(snapshot["state"])
        snapshot["api_sha256"] = digest(snapshot["api"])
        return canonical(snapshot)

    async def _select_cards(self) -> dict[str, Any]:
        _, body = await self.request(
            "control_read", "GET", f"/c/{self.token}/cards"
        )
        return body

    async def _beat_1(self) -> None:
        label = BEATS[0][0]
        self.trace.set_beat(label)
        self.boundaries.set_beat(label)
        await self.request(
            "scenario_mutation", "POST", f"/c/{self.token}/doctor",
            data={"text": DICTATION},
        )
        action = _one_action(await self._select_cards(), "confirm:")
        await self.request(
            "scenario_mutation", "POST", f"/c/{self.token}/action",
            json={"action_id": action, "text": ""},
        )
        ahmed = [p for p in self.memory.patients.values() if p.name == "Ahmed Ali"]
        if len(ahmed) != 1:
            raise ScenarioViolation(f"expected one Ahmed Ali, found {len(ahmed)}")
        self.ahmed_id = ahmed[0].id
        tokens = [
            token for token in self.memory.link_tokens.values()
            if token.patient_id == self.ahmed_id
        ]
        if len(tokens) != 1:
            raise ScenarioViolation(f"expected one Ahmed link, found {len(tokens)}")
        self.ahmed_link = tokens[0].id
        await self.request(
            "scenario_mutation", "GET", f"/p/{self.ahmed_link}"
        )
        loops = [l for l in self.memory.loops.values() if l.patient_id == self.ahmed_id]
        if [l.type for l in loops] != ["MEDICATION", "TEST", "MONITOR", "VISIT"]:
            raise ScenarioViolation(f"wrong Ahmed contracts: {[l.type for l in loops]!r}")
        if len(self.queue.tasks) != 12:
            raise ScenarioViolation(f"Beat 1 queued {len(self.queue.tasks)}, not 12 tasks")
        self.beats[label] = await self.capture(
            label, include_patient_ids=[self.ahmed_id, self.amany_id]
        )

    def _advance_to(self, moment: Any) -> None:
        delta = moment - self.memory.peek_now()
        if delta > timedelta(0):
            self.memory.advance(delta)

    async def _beat_2(self) -> None:
        label = BEATS[1][0]
        self.trace.set_beat(label)
        self.boundaries.set_beat(label)
        initial = self.queue.initial_ladder(12)
        for task in initial:
            self._advance_to(task.scheduled_at)
            self.queue.dispatch(task, at=self.memory.peek_now())
            response, _ = await self.request(
                "task_callback", "POST", task.path,
                json=task.payload,
                headers={"Authorization": VIRTUAL_TASK_AUTHORIZATION},
            )
            self.queue.complete(task, response.status_code)
        ahmed_loops = [
            loop for loop in self.memory.loops.values()
            if loop.patient_id == self.ahmed_id
        ]
        states = {loop.type: loop.state for loop in ahmed_loops}
        if states.get("TEST") != "unreachable" or states.get("VISIT") != "unreachable":
            raise ScenarioViolation(f"Beat 2 did not finish both ladders: {states!r}")
        await self.admin(
            "POST", "/admin/settings?time_scale=86400",
            category="scenario_control",
        )
        self.beats[label] = await self.capture(
            label, include_patient_ids=[self.ahmed_id, self.amany_id]
        )

    async def _beat_3(self) -> None:
        label = BEATS[2][0]
        self.trace.set_beat(label)
        self.boundaries.set_beat(label)
        await self.request(
            "scenario_mutation", "POST",
            f"/c/{self.token}/patient/{self.ahmed_id}",
            data={"text": COST_MESSAGE},
        )
        action = _one_action(
            await self._select_cards(), "reply:", title="Barrier needs you"
        )
        await self.request(
            "scenario_mutation", "POST", f"/c/{self.token}/action",
            json={
                "action_id": action,
                "text": "The hospital lab is free, go there.",
            },
        )
        if len(self.queue.tasks) != 13:
            raise ScenarioViolation(f"Beat 3 total queue is {len(self.queue.tasks)}, not 13")
        if len(self.queue.pending()) != 1:
            raise ScenarioViolation("Beat 3 resumption task is not the sole pending task")
        quiet_hours_note = "moved out of quiet hours (22:00 to 09:00 Cairo)"
        ahmed_audit_lines = [
            str(((event.meta or {}).get("audit") or {}).get("line") or "")
            for event in self.memory.events.values()
            if event.patient_id == self.ahmed_id
        ]
        if not any(quiet_hours_note in line for line in ahmed_audit_lines):
            raise ScenarioViolation(
                "Beat 3 did not reproduce S18's quiet-hours scheduling branch"
            )
        pending = self.queue.pending()[0]
        cairo_due = pending.scheduled_at.astimezone(ZoneInfo("Africa/Cairo"))
        if (cairo_due.hour, cairo_due.minute) != (9, 0):
            raise ScenarioViolation(
                f"Beat 3 quiet-hours task is due at {cairo_due.isoformat()}, not 09:00 Cairo"
            )
        self.beats[label] = await self.capture(
            label, include_patient_ids=[self.ahmed_id, self.amany_id]
        )

    async def _photo(self, index: int, filename: str) -> None:
        label = BEATS[index - 1][0]
        self.trace.set_beat(label)
        self.boundaries.set_beat(label)
        raw = (SEED / filename).read_bytes()
        await self.request(
            "scenario_mutation", "POST",
            f"/c/{self.token}/patient/{self.ahmed_id}",
            data={"text": ""},
            files={"file": (filename, raw, "image/png")},
        )
        self.beats[label] = await self.capture(
            label, include_patient_ids=[self.ahmed_id, self.amany_id]
        )

    async def _beat_7(self) -> None:
        label = BEATS[6][0]
        self.trace.set_beat(label)
        self.boundaries.set_beat(label)
        before = {
            loop.id: (loop.state, loop.contacts, loop.schedule_version)
            for loop in self.memory.loops.values()
            if loop.patient_id == self.amany_id
        }
        await self.request(
            "scenario_mutation", "POST",
            f"/c/{self.token}/patient/{self.amany_id}",
            data={"text": AMANY_MESSAGE},
        )
        amany_test = next(
            loop for loop in self.memory.loops.values()
            if loop.patient_id == self.amany_id and loop.type == "TEST"
        )
        after = {
            loop.id: (loop.state, loop.contacts, loop.schedule_version)
            for loop in self.memory.loops.values()
            if loop.patient_id == self.amany_id
        }
        expected_after = dict(before)
        old_state, old_contacts, old_version = expected_after[amany_test.id]
        expected_after[amany_test.id] = (
            old_state, old_contacts + 1, old_version,
        )
        if after != expected_after:
            raise ScenarioViolation(
                "Beat 7 did not preserve schedules/states and count only the "
                "fixed escalation reply: "
                f"before={before!r}, after={after!r}"
            )
        if amany_test.barrier != "unclear":
            raise ScenarioViolation(
                "Beat 7 did not reproduce the live conservative barrier after "
                "the contact guard refused another message"
            )
        if len(self.queue.tasks) != 13:
            raise ScenarioViolation("Beat 7 queued work despite the six-contact guard")
        lines = [
            str(((event.meta or {}).get("audit") or {}).get("line") or "")
            for event in self.memory.events.values()
            if event.patient_id == self.amany_id
        ]
        if not any("6 contacts already on this loop and the policy limit is 6" in line
                   for line in lines):
            raise ScenarioViolation("Beat 7 exact six-contact guard line is absent")
        self.beats[label] = await self.capture(
            label, include_patient_ids=[self.ahmed_id, self.amany_id]
        )

    async def _beat_8(self) -> None:
        label = BEATS[7][0]
        self.trace.set_beat(label)
        self.boundaries.set_beat(label)
        lipid = next(
            loop for loop in self.memory.loops.values()
            if loop.patient_id == self.ahmed_id and loop.type == "TEST"
        )
        if lipid.state != "pending_review":
            raise ScenarioViolation(f"lipid loop is {lipid.state}, not pending_review")
        await self.request(
            "scenario_mutation", "POST", f"/c/{self.token}/action",
            json={"action_id": f"reviewed:{lipid.id}", "text": ""},
        )
        closed = self.memory.loops[lipid.id]
        if closed.state != "done" or not closed.doctor_reviewed:
            raise ScenarioViolation("doctor review did not close the verified lipid loop")
        self.beats[label] = await self.capture(
            label, include_patient_ids=[self.ahmed_id, self.amany_id]
        )

    async def _beat_9(self) -> None:
        label = BEATS[8][0]
        self.trace.set_beat(label)
        self.boundaries.set_beat(label)
        _, summary = await self.request(
            "scenario_observation", "GET", f"/c/{self.token}/summary"
        )
        if summary["counts"].get("lost") != 0 or summary["counts"].get("duplicates") != 0:
            raise ScenarioViolation(f"final loss/duplicate invariant failed: {summary!r}")
        self.beats[label] = await self.capture(
            label, include_patient_ids=[self.ahmed_id, self.amany_id],
            summary_body=summary,
        )

    async def run(self) -> ScenarioResult:
        previous_admin = os.environ.get("ADMIN_SECRET")
        os.environ["ADMIN_SECRET"] = ADMIN_SECRET
        try:
            with (
                self.memory.patched(),
                self.boundaries.patch(),
                self.queue.patch(),
                provider_boundaries(),
                instrument_delivery(self.trace),
            ):
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(
                        sanad_main.app.router.lifespan_context(sanad_main.app)
                    )
                    self.client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=sanad_main.app),
                            base_url=BASE_URL,
                            follow_redirects=False,
                        )
                    )
                    await self._setup()
                    await self._beat_1()
                    await self._beat_2()
                    await self._beat_3()
                    await self._photo(4, "lab-slip-7-lipid-partial-0830.png")
                    await self._photo(5, "lab-slip-8-lipid-complete-0830.png")
                    await self._photo(6, "lab-slip-2.png")
                    await self._beat_7()
                    await self._beat_8()
                    await self._beat_9()
        finally:
            self.client = None
            if previous_admin is None:
                os.environ.pop("ADMIN_SECRET", None)
            else:
                os.environ["ADMIN_SECRET"] = previous_admin

        self.boundaries.assert_complete()
        final_counts = self.beats[BEATS[-1][0]]["api"]["summary"]["counts"]
        traces = {
            "http": self.trace.http,
            "messages": self.trace.outbound,
            "delivery": self.trace.delivery,
            "models": self.boundaries.trace_as_dicts(),
            "tasks": self.queue.ledger_as_dicts(),
            "counts": {
                **self.trace.counts(),
                "models": self.boundaries.count_summary(),
                "tasks": self.queue.count_summary(),
            },
        }
        final_core = {
            key: final_counts.get(key) for key in EXPECTED_FINAL_COUNTS
        }
        if final_core != EXPECTED_FINAL_COUNTS:
            raise ScenarioViolation(
                f"replay diverged from the frozen live baseline: {final_core!r}"
            )
        counts = traces["counts"]
        scenario_http = sum(
            int(counts["http_by_category"].get(name, 0))
            for name in ("scenario_mutation", "task_callback", "scenario_observation")
        )
        observed_trace_counts = {
            "scenario_trigger_http_requests": scenario_http,
            "model_calls": counts["models"]["total"],
            "logical_outbound_messages": counts["logical_outbound_total"],
            "tasks_enqueued": counts["tasks"]["enqueued"],
            "task_handlers_executed": counts["tasks"]["states"].get("completed", 0),
            "tasks_pending": counts["tasks"]["states"].get("pending", 0),
        }
        if observed_trace_counts != EXPECTED_TRACE_COUNTS:
            raise ScenarioViolation(
                f"replay trace counts moved: {observed_trace_counts!r}"
            )
        counts["scenario_trigger_totals"] = observed_trace_counts
        manifest = {
            "schema": "sanad-gate0b-characterization/v1",
            "synthetic": True,
            "baseline_commit": HERMETIC_BASELINE_COMMIT,
            "dossier_sha256": DOSSIER_SHA256,
            "source_baseline": {
                "clean_public_commit": CLEAN_PUBLIC_COMMIT,
                "hermetic_baseline_commit": HERMETIC_BASELINE_COMMIT,
                "deployed_revision_at_gate0_freeze": DEPLOYED_REVISION_AT_GATE0_FREEZE,
                "last_verified_nine_beat_revision": LAST_VERIFIED_NINE_BEAT_REVISION,
                "private_source_repository_commit": PRIVATE_SOURCE_REPOSITORY_COMMIT,
            },
            "experimental_tree_freeze": EXPERIMENTAL_TREE_FREEZE,
            "fixed_start": FIXED_START,
            "timezone": "Africa/Cairo",
            "run_id": RUN_ID,
            "doctor": DOCTOR_NAME,
            "inference": "strict scripted outputs; no Gemini/ADK provider call",
            "persistence": "deterministic in-memory store; no Firestore",
            "tasks": "virtual queue; callbacks use the real /tasks/nudge route",
            "storage": "synthetic URI; PNG decoding and evidence logic remain real",
            "telegram": "disabled and unbound; provider outcomes are skipped",
            "historical_live_reference": HISTORICAL_LIVE_FINAL,
            "replay_final_counts": final_counts,
            "replay_trace_counts": observed_trace_counts,
            "screenshot_receipts": "screenshot-receipts.json",
            "screenshot_provenance": "screenshot-provenance.json",
            "screenshot_contract": {
                "viewports": ["375x812", "1440x1000"],
                "dpr": 1,
                "theme": "light",
                "reduced_motion": True,
                "timezone": "Africa/Cairo",
                "locale": "en-US",
                "dashboard_sha256": hashlib.sha256(
                    (APP_ROOT / "web" / "dashboard.html").read_bytes()
                ).hexdigest(),
                "font_policy": (
                    "external fonts blocked; platform-local system fallback; "
                    "no cross-machine pixel-determinism claim"
                ),
                "network_policy": "dashboard reads restricted to the loopback replay origin",
                "readiness": (
                    "per-capture callback after routes and images succeed, exact CSS/CDP "
                    "viewport metrics match, exactly one expected DOM view plus patient "
                    "identity are observed, and the selected evidence anchor's full "
                    "ancestor chain is visibly unobscured; the final receipt binds the "
                    "captured PNG SHA-256 and Chrome/CDP identity"
                ),
                "provenance_file": "screenshot-provenance.json",
                "files": [
                    f"screenshots/{viewport}/{name}.png"
                    for viewport in ("375x812", "1440x1000")
                    for name, _ in BEATS
                ],
            },
            "non_claims": [
                "Gemini, ADK, transcription, OCR accuracy, or live model determinism",
                "Firestore contention or durability",
                "Cloud Tasks durability, Cloud Storage, Google Cloud, or latency",
                "Telegram/provider acceptance, delivery, notification, or read receipt",
                "WhatsApp or channel switching",
                "exactly-once external delivery",
                "clinical safety or readiness for real patients",
                "the future S23 Orchestra, Steward, Outcome Kernel, Evidence Orchestrator, or Closure Auditor",
            ],
        }
        return ScenarioResult(
            initial=self.initial, beats=self.beats, traces=traces, manifest=manifest
        )


def write_result(result: ScenarioResult, root: Path = GOLDENS) -> dict[str, Any]:
    """Regenerate canonical JSON artifacts. Screenshot files are preserved."""
    hashes: dict[str, str] = {}
    for relative, payload in result.artifact_payloads().items():
        hashes[relative] = write_json(root / relative, payload)
    # Existing screenshot hashes are added only after capture; missing files
    # remain visibly missing rather than being represented by placeholders.
    for path in sorted((root / "screenshots").rglob("*.png")):
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("screenshot-receipts.json", "screenshot-provenance.json"):
        path = root / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {**result.manifest, "artifact_sha256": dict(sorted(hashes.items()))}
    write_json(root / "manifest.json", manifest)
    return manifest


async def regenerate(root: Path = GOLDENS) -> dict[str, Any]:
    return write_result(await GoldenJourney().run(), root)


def main() -> None:
    manifest = asyncio.run(regenerate())
    print(
        "Gate 0B JSON regenerated: "
        f"{len(manifest['artifact_sha256'])} artifacts, final "
        f"{manifest['replay_final_counts']}"
    )


if __name__ == "__main__":  # pragma: no cover - explicit regeneration CLI
    main()
