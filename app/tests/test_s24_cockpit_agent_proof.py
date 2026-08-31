"""The v2 cockpit prints what the agents decided, and who the count is about.

Two review findings, proved the same way: build one real snapshot with
``core.workspace.build_snapshot``, hand that exact JSON to the page's own
JavaScript in node against a small DOM double, and read what the doctor would
have on screen.  Nothing here asserts on a hand-written HTML fixture, so a
render path that silently stops reading a field fails the test rather than
passing a string match.

Finding 5: cockpit v2 received ``meta.auditor``, ``meta.steward``,
``meta.evidence_packet`` and ``meta.audit.line`` on every event and rendered
none of them.
Finding 4: the "Active patients" tile drilled into 21 bare patient ids.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.models import Doctor, Event, Loop, Patient
from core.workspace import build_snapshot
from core.workspace_records import WorkspaceRecords


APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (APP_ROOT / "web" / "dashboard_v2.html").read_text(encoding="utf-8")

NOW = datetime(2026, 8, 31, 9, 0, 0, tzinfo=timezone.utc)

# One hostile string, carried on a field the record owns rather than one the
# projection writes.  A proof line is patient-influenced text reaching
# innerHTML, so it has to arrive escaped or this feature is an injection.
HOSTILE = '<img src=x onerror="alert(1)">'

AUDITOR_META = {
    "held": True,
    "gap": "the evening reading on day 6",
    "note": "Sanad is completing the record first: the evening reading on day 6",
    "asked_the_model": True,
    "decided_by": (
        "auditor (gemini) + guard: model choice, guard in code "
        "(core/verify.py, core/policy.py)"
    ),
}

STEWARD_META = {
    "verdict": "hold_for_digest",
    "note": "the case steward is keeping this for the morning",
    "asked_the_model": True,
    "decided_by": (
        "steward (gemini) + policy in code (core/policy.py): "
        "model judgement, guards in code"
    ),
    "release_at": "2026-09-01T06:00:00+00:00",
    "alternatives": ["schedule_next_contact", "ask_the_patient"],
}

STEWARD_REVISED_META = {
    "verdict": "revise",
    "note": "the case steward chose another allowed step",
    "tool": "ask_the_patient",
    "asked_the_model": True,
    "decided_by": "steward (gemini) + policy in code (core/policy.py)",
}

EVIDENCE_META = {
    "kind": "lab_slip",
    "loop_id": "l1",
    "route": "attach_to_loop",
    "missing": ["HbA1c"],
    "reason": "the page carries the analytes this order asked for",
    "refused": [
        "code guard refused loop 'l9': it is not an open loop on this patient",
    ],
    "candidates": ["lab_slip", "prescription"],
    "offered": ["l1"],
    "code_route": {"kind": "lab_slip", "loop_id": "l1", "route": "attach_to_loop"},
    "agreed_with_code": True,
    "decided_by": (
        "evidence-orchestrator (gemini) + gates: model choice, "
        "guards in code (core/photos.py, core/verify.py, core/labs.py)"
    ),
}

AUDIT_META = {
    "tier": "coordinator",
    "line": (
        "coordinator: schedule_next_contact refused"
        + " " + HOSTILE
        + " · guard: 6 contacts already on this loop and the policy limit is 6"
    ),
}


def doctor() -> Doctor:
    return Doctor(
        id="d1",
        synthetic=True,
        name="Test Doctor",
        specialty="cardiology",
        web_token="token-d1",
        cockpit_v2_enabled=True,
        created_at=NOW - timedelta(days=30),
    )


def patient(ident: str, name: str) -> Patient:
    return Patient(
        id=ident,
        synthetic=True,
        doctor_id="d1",
        name=name,
        diagnosis="fixture diagnosis",
        status="active",
        plan_text="fixture plan",
        created_at=NOW - timedelta(days=4),
    )


def loop(ident: str, patient_id: str) -> Loop:
    return Loop(
        id=ident,
        synthetic=True,
        patient_id=patient_id,
        doctor_id="d1",
        type="TEST",
        title="HbA1c",
        details={"test_name": "HbA1c"},
        state="open",
        created_at=NOW - timedelta(days=3),
        updated_at=NOW,
    )


def event(
    ident: str,
    *,
    meta: dict,
    patient_id: str = "p1",
    kind: str = "system",
    text: str = "fixture event",
    minute: int = 0,
) -> Event:
    return Event(
        id=ident,
        synthetic=True,
        doctor_id="d1",
        patient_id=patient_id,
        kind=kind,
        text=text,
        meta=meta,
        ts=NOW + timedelta(minutes=minute),
        persisted_at=NOW + timedelta(minutes=minute),
    )


def danger_card_event(ident: str, *, meta: dict, minute: int = 0) -> Event:
    card = {
        "title": "Glucose tolerance test",
        "severity": "red",
        "lines": ["glucose 402 mg/dL CRITICAL"],
        "actions": [{"id": f"seen:{ident}", "label": "Seen"}],
    }
    return event(
        ident,
        meta={"card": card, "notification_class": "DANGER", **meta},
        kind="card",
        text="intent: did_test stood down on Glucose tolerance test",
        minute=minute,
    )


PATIENTS = (
    patient("p1", "Mahmoud Fahmy"),
    patient("p2", "Nour El Sayed"),
    patient("p3", "Hala Ibrahim"),
)


def snapshot(selected: str | None = "p1") -> dict:
    events = (
        danger_card_event("card-danger", meta={"steward": STEWARD_META}),
        event("ev-auditor", meta={"auditor": AUDITOR_META}, minute=1,
              text="close refused: the evening reading on day 6"),
        event("ev-evidence", meta={"evidence_packet": EVIDENCE_META},
              minute=2, text="lab slip read for HbA1c"),
        event("ev-audit", meta={"audit": AUDIT_META,
                                "decided_by": "model choice, guards in code"},
              minute=3, text="intent: did_test stood down"),
        event("ev-revise", meta={"steward": STEWARD_REVISED_META},
              patient_id="p2", minute=4, text="the plan was revised"),
        event("ev-plain", meta={}, minute=5, text="nothing was decided here"),
    )
    records = WorkspaceRecords(
        doctor=doctor(),
        patients=PATIENTS,
        loops=(loop("l1", "p1"),),
        events=events,
        reports=(),
        link_tokens=(),
        open_relays=(),
        settings={"run_id": "s24", "time_scale": 86400},
        read_at=NOW + timedelta(hours=1),
    )
    return build_snapshot(records, NOW, selected_patient_id=selected)


# --------------------------------------------------------------------------- #
# The server projection
# --------------------------------------------------------------------------- #
class TheDrillKnowsWhoItCounted(unittest.TestCase):
    """Finding 4: the count is right and the rows behind it are people."""

    def test_every_active_patient_row_carries_its_name(self) -> None:
        built = snapshot()
        row_ids = built["queues"]["active_patients"]["row_ids"]
        self.assertEqual(len(row_ids), len(PATIENTS))
        names = [built["rows"][row_id]["patient_name"] for row_id in row_ids]
        self.assertEqual(sorted(names), sorted(person.name for person in PATIENTS))
        for row_id in row_ids:
            with self.subTest(row=row_id):
                row = built["rows"][row_id]
                self.assertEqual(row["row_type"], "patient_ref")
                # The id stays: the name is what the doctor reads, the id is
                # what he can quote back at the record.
                self.assertTrue(row["source_id"])

    def test_the_metric_and_its_rows_still_agree(self) -> None:
        built = snapshot()
        self.assertEqual(
            built["metrics"]["active_patients_total"]["row_ids"],
            built["queues"]["active_patients"]["row_ids"],
        )
        self.assertEqual(
            built["metrics"]["active_patients_total"]["count"], len(PATIENTS)
        )

    def test_the_reference_row_still_withholds_the_clinical_summary(self) -> None:
        """A name is not a licence to ship the whole record on every row."""
        built = snapshot()
        row = built["rows"]["patient:p1"]
        for withheld in ("diagnosis", "plan", "loop_row_ids", "next_due"):
            with self.subTest(field=withheld):
                self.assertNotIn(withheld, row)

    def test_a_private_channel_id_in_a_name_is_still_redacted(self) -> None:
        """The name goes through the same scrub as every other row's name."""
        records = WorkspaceRecords(
            doctor=doctor().model_copy(update={"telegram_chat_id": 987654321}),
            patients=(patient("p1", "Chat 987654321"),),
            loops=(),
            events=(),
            reports=(),
            link_tokens=(),
            open_relays=(),
            settings={"run_id": "s24", "time_scale": 86400},
            read_at=NOW + timedelta(hours=1),
        )
        built = build_snapshot(records, NOW)
        self.assertNotIn("987654321", built["rows"]["patient:p1"]["patient_name"])
        self.assertIn("REDACTED", built["rows"]["patient:p1"]["patient_name"])


class TheSnapshotStillShipsTheAgentDecisions(unittest.TestCase):
    """Finding 5, server half: the data was always there to render."""

    def test_agent_meta_reaches_the_page_on_events_and_on_card_rows(self) -> None:
        built = snapshot()
        recent = {item["id"]: item for item in built["agent_events"]["recent"]}
        self.assertEqual(recent["ev-auditor"]["meta"]["auditor"], AUDITOR_META)
        self.assertEqual(recent["ev-evidence"]["meta"]["evidence_packet"],
                         EVIDENCE_META)
        self.assertEqual(recent["ev-audit"]["meta"]["audit"]["line"],
                         AUDIT_META["line"])
        self.assertEqual(built["rows"]["event:card-danger"]["meta"]["steward"],
                         STEWARD_META)


# --------------------------------------------------------------------------- #
# The page, run
# --------------------------------------------------------------------------- #
SCRIPT = SOURCE[SOURCE.rindex("<script>") + len("<script>"): SOURCE.rindex("</script>")]

# A DOM double, not a browser.  Every node the page writes to is recorded, so
# the assertions below read the page's own output instead of its source text.
SHIM = """
const __FS = require("fs");
const __SNAPSHOT = JSON.parse(__FS.readFileSync(process.argv[2], "utf8"));
const __NODES = Object.create(null);
function __Element(id){
  this.id = id; this.innerHTML = ""; this.textContent = "";
  this.disabled = false; this.attributes = Object.create(null);
  this.classes = Object.create(null);
  const self = this;
  this.classList = {
    add:function(name){ self.classes[name] = true; },
    remove:function(name){ delete self.classes[name]; },
    contains:function(name){ return Boolean(self.classes[name]); }
  };
}
__Element.prototype.setAttribute = function(name, value){ this.attributes[name] = value; };
__Element.prototype.scrollIntoView = function(){};
__Element.prototype.querySelectorAll = function(){ return []; };
__Element.prototype.focus = function(){};
function __node(id){
  if (!__NODES[id]) __NODES[id] = new __Element(id);
  return __NODES[id];
}
globalThis.location = {pathname:"/c/0123456789abcdef0123456789abcdef"};
globalThis.document = {
  getElementById:__node,
  querySelector:function(sel){ return __node("query:" + sel); },
  querySelectorAll:function(){ return []; }
};
globalThis.fetch = function(){ return new Promise(function(){}); };
globalThis.setInterval = function(){ return 0; };
globalThis.setTimeout = function(){ return 0; };
"""

TAIL = """
commitWorkspaceSnapshot(__SNAPSHOT);
render();
const __dump = {};
__dump.danger = __node("queueRows").innerHTML;
openQueue("active_patients");
__dump.activePatients = __node("queueRows").innerHTML;
__dump.events = __node("eventRows").innerHTML;
S.patientId = __SNAPSHOT.selected_patient.patient.id;
openPatientDetail();
__dump.drawer = __node("detailBody").innerHTML;
__dump.drawerOpen = __node("patientDrawer").classList.contains("open");
__dump.error = __node("errorBox").textContent;
process.stdout.write(JSON.stringify(__dump));
"""


def render_page(built: dict) -> dict:
    """Run the page's own JavaScript over one real snapshot and read the DOM."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the build image
        raise unittest.SkipTest("node is not available in this environment")
    with tempfile.TemporaryDirectory() as work:
        harness = Path(work) / "harness.js"
        payload = Path(work) / "snapshot.json"
        harness.write_text(SHIM + SCRIPT + TAIL, encoding="utf-8")
        payload.write_text(json.dumps(built), encoding="utf-8")
        finished = subprocess.run(
            [node, str(harness), str(payload)],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "NODE_OPTIONS": ""},
        )
    if finished.returncode != 0:
        raise AssertionError(
            "the cockpit script failed in node:\n" + finished.stderr[-4000:]
        )
    return json.loads(finished.stdout)


class RenderedCockpit(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.built = snapshot()
        cls.dom = render_page(cls.built)

    @staticmethod
    def text_of(markup: str) -> str:
        return html.unescape(re.sub(r"<[^>]*>", " ", markup))

    # ---------------------------------------------------------------- finding 5
    def test_the_case_steward_verdict_and_reason_are_on_the_card_row(self) -> None:
        danger = self.dom["danger"]
        self.assertIn("Case Steward", danger)
        self.assertIn("held", self.text_of(danger))
        self.assertIn(STEWARD_META["note"], self.text_of(danger))
        self.assertIn(STEWARD_META["decided_by"], self.text_of(danger))
        # The verdict is a chip, not a sentence the reader has to find.
        self.assertRegex(danger, r'<span class="verdict wait">held</span>')

    def test_the_closure_auditor_hold_and_its_gap_are_on_the_event(self) -> None:
        events = self.text_of(self.dom["events"])
        self.assertIn("Closure Auditor", events)
        self.assertIn("held this close", events)
        self.assertIn(AUDITOR_META["note"], events)
        self.assertIn(AUDITOR_META["decided_by"], events)

    def test_the_evidence_orchestrator_packet_and_its_refusal_are_rendered(self) -> None:
        events = self.text_of(self.dom["events"])
        self.assertIn("Evidence Orchestrator", events)
        self.assertIn("lab_slip to attach_to_loop", events)
        self.assertIn(EVIDENCE_META["reason"], events)
        self.assertIn("missing: HbA1c", events)
        self.assertIn(EVIDENCE_META["refused"][0], events)
        self.assertIn(EVIDENCE_META["decided_by"], events)
        self.assertIn('<span class="verdict stop">guard refused</span>',
                      self.dom["events"])

    def test_the_guard_clause_line_is_rendered_with_its_tier(self) -> None:
        events = self.text_of(self.dom["events"])
        self.assertIn("Guard clause", events)
        self.assertIn("coordinator", events)
        self.assertIn("6 contacts already on this loop and the policy limit is 6",
                      events)

    def test_a_revised_verdict_says_which_step_replaced_the_plan(self) -> None:
        events = self.text_of(self.dom["events"])
        self.assertIn("revised", events)
        self.assertIn("chose ask_the_patient instead", events)

    def test_an_event_with_no_agent_decision_prints_no_proof_block(self) -> None:
        blocks = self.dom["events"].count('<div class="proof">')
        # Six fixture events reach the panel and five of them carry a decision.
        # The sixth prints its headline and nothing else, so an empty meta can
        # never be dressed up as an agent having said something.
        self.assertEqual(blocks, 5)
        self.assertIn("nothing was decided here", self.text_of(self.dom["events"]))

    def test_the_drawer_shows_the_agent_decisions_for_that_patient_only(self) -> None:
        drawer = self.text_of(self.dom["drawer"])
        self.assertTrue(self.dom["drawerOpen"])
        self.assertIn("What the agents decided", drawer)
        self.assertIn("Closure Auditor", drawer)
        self.assertIn("Evidence Orchestrator", drawer)
        self.assertIn("Case Steward", drawer)
        # p2 is a different patient, so its revision is not in p1's drawer.
        self.assertNotIn("chose ask_the_patient instead", drawer)

    def test_record_text_reaching_a_proof_line_is_escaped(self) -> None:
        for surface in ("events", "drawer"):
            with self.subTest(surface=surface):
                self.assertNotIn(HOSTILE, self.dom[surface])
                self.assertIn("&lt;img src=x onerror=", self.dom[surface])
                self.assertIn(HOSTILE, self.text_of(self.dom[surface]))

    # ---------------------------------------------------------------- finding 4
    def test_the_active_patients_drill_reads_as_a_list_of_people(self) -> None:
        drill = self.dom["activePatients"]
        readable = self.text_of(drill)
        self.assertEqual(drill.count('<article class="row'), len(PATIENTS))
        for person in PATIENTS:
            with self.subTest(patient=person.name):
                self.assertIn("<h3>" + person.name + "</h3>", drill)
                self.assertIn(person.id, readable)
        # No row is titled by its id any more.
        for person in PATIENTS:
            self.assertNotIn("<h3>" + person.id + "</h3>", drill)

    def test_the_drill_is_not_broken_by_the_change_of_row_kind(self) -> None:
        self.assertIn("patient_ref", self.dom["activePatients"])
        self.assertIn('class="rowid"', self.dom["activePatients"])

    def test_the_page_rendered_without_falling_back_to_an_error(self) -> None:
        self.assertEqual(self.dom["error"], "")


if __name__ == "__main__":
    unittest.main()
