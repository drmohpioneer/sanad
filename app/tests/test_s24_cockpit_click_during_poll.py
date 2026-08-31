"""A click that lands during a background poll is never silently dropped.

S24 finding 1, measured on the live cockpit: ``poll()`` opened with
``if (S.polling) return false;`` while a background poll ran every three
seconds, so roughly one Next-click in three did nothing at all -- no error, no
retry, and ``S.patientOffset`` was never updated, so the state did not catch up
on the following tick.  The same function carries the patient-row open and the
post-action refresh, so the same collision could swallow the drawer.

Proved the way the agent-proof tests prove things: the page's own JavaScript is
run in node against a DOM double, with ``fetch`` and ``setInterval`` under the
test's control so the collision can be staged exactly rather than raced.  The
snapshots handed back are real ``core.workspace.build_snapshot`` projections.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.models import Doctor, Patient
from core.workspace import build_snapshot
from core.workspace_records import WorkspaceRecords


APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (APP_ROOT / "web" / "dashboard_v2.html").read_text(encoding="utf-8")

NOW = datetime(2026, 8, 31, 9, 0, 0, tzinfo=timezone.utc)

# The live board the finding was measured on: 23 patients, 20 to a page, so
# the pager reads "1-20 of 23" and Next must take it to "21-23 of 23".
PATIENT_COUNT = 23


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


def patient(index: int) -> Patient:
    return Patient(
        id=f"p{index:02d}",
        synthetic=True,
        doctor_id="d1",
        name=f"Patient {index:02d}",
        diagnosis="fixture diagnosis",
        status="active",
        plan_text="fixture plan",
        created_at=NOW - timedelta(days=4, minutes=index),
    )


PATIENTS = tuple(patient(index) for index in range(1, PATIENT_COUNT + 1))


def records() -> WorkspaceRecords:
    return WorkspaceRecords(
        doctor=doctor(),
        patients=PATIENTS,
        loops=(),
        events=(),
        reports=(),
        link_tokens=(),
        open_relays=(),
        settings={"run_id": "s24", "time_scale": 86400},
        read_at=NOW + timedelta(hours=1),
    )


def page(offset: int, *, selected: str | None = None) -> dict:
    return build_snapshot(
        records(),
        NOW,
        patient_offset=offset,
        patient_limit=20,
        selected_patient_id=selected,
    )


# --------------------------------------------------------------------------- #
# The page, run, with the network and the clock in the test's hands
# --------------------------------------------------------------------------- #
SCRIPT = SOURCE[SOURCE.rindex("<script>") + len("<script>"): SOURCE.rindex("</script>")]

SHIM = """
const __FS = require("fs");
const __PAGES = JSON.parse(__FS.readFileSync(process.argv[2], "utf8"));
const __realTimeout = globalThis.setTimeout;
const __NODES = Object.create(null);
function __Element(id){
  this.id = id; this.innerHTML = ""; this.textContent = "";
  this.disabled = false; this.attributes = Object.create(null);
  this.classes = Object.create(null); this.dataset = Object.create(null);
  this._q = Object.create(null);
  const self = this;
  this.classList = {
    add:function(name){ self.classes[name] = true; },
    remove:function(name){ delete self.classes[name]; },
    contains:function(name){ return Boolean(self.classes[name]); }
  };
}
__Element.prototype.setAttribute = function(name, value){ this.attributes[name] = value; };
__Element.prototype.scrollIntoView = function(){};
__Element.prototype.focus = function(){};
// Enough of a query engine to hand back the delegated child buttons the page
// wires its handlers onto, so a click in this test runs the page's own
// handler rather than a re-implementation of it.  The results are cached
// against the markup they were read from, so the page and the test address
// the same objects for as long as the markup stands.
__Element.prototype.querySelectorAll = function(sel){
  const match = /^\\[data-([a-z-]+)\\]$/.exec(sel || "");
  if (!match) return [];
  const key = sel + "||" + this.innerHTML;
  if (this._q[key]) return this._q[key];
  const camel = match[1].replace(/-([a-z])/g, function(_, c){ return c.toUpperCase(); });
  const finder = new RegExp('data-' + match[1] + '="([^"]*)"', "g");
  const found = [];
  let hit;
  while ((hit = finder.exec(this.innerHTML)) !== null){
    const child = new __Element(this.id + "/" + match[1] + "/" + hit[1]);
    child.dataset[camel] = hit[1]
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
    found.push(child);
  }
  this._q[key] = found;
  return found;
};
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

// Every snapshot request is parked until the test releases it, so "a poll is
// in flight" is a fact of the test rather than a race it hopes to win.
const __REQUESTS = [];
globalThis.fetch = function(url){
  return new Promise(function(resolve){
    __REQUESTS.push({
      url:String(url),
      settled:false,
      settle:function(body){
        this.settled = true;
        resolve({ok:true, status:200, json:function(){ return Promise.resolve(body); }});
      }
    });
  });
};
let __TICK = null;
globalThis.setInterval = function(fn){ __TICK = fn; return 0; };
globalThis.setTimeout = function(){ return 0; };

function __drain(){
  let chain = Promise.resolve();
  for (let turn = 0; turn < 6; turn += 1){
    chain = chain.then(function(){
      return new Promise(function(resolve){ __realTimeout(resolve, 0); });
    });
  }
  return chain;
}
function __open(){ return __REQUESTS.filter(function(item){ return !item.settled; }); }
function __release(body){
  const pending = __open();
  if (!pending.length) throw new Error("no request was in flight to release");
  pending[0].settle(body);
  return __drain();
}
"""

TAIL = """
(async function(){
  const dump = {urls:[]};
  const fail = function(message){
    dump.harnessError = message;
    process.stdout.write(JSON.stringify(dump));
    process.exit(0);
  };

  // ---- boot ------------------------------------------------------------
  await __drain();
  if (__open().length !== 1) fail("boot did not issue exactly one request");
  await __release(__PAGES.page1);
  if (!__TICK) fail("the background interval was never registered");
  dump.bootRange = __node("patientRange").textContent;

  // ---- the 3-second tick is now in flight ------------------------------
  __TICK();
  await __drain();
  dump.pollingWhileTickInFlight = S.polling;
  const tickRequests = __open().length;

  // A second tick landing on top of the first still gives up: the queue is
  // for the doctor's clicks, not for a backlog of interval work.
  __TICK();
  await __drain();
  dump.tickDidNotQueue = (__open().length === tickRequests);

  // ---- the doctor clicks Next while that tick is unresolved -------------
  const nextClick = __node("nextPatients").onclick();
  await __drain();
  dump.clickWasNotAnsweredBeforeTheTick = (__open().length === tickRequests);

  // The tick comes back with the page the doctor was already on.
  await __release(__PAGES.page1);
  // The click must now be on the wire, carrying the offset it was clicked for.
  const queued = __open();
  if (queued.length !== 1) fail("the queued click did not reach the network");
  dump.clickUrl = queued[0].url;
  await __release(__PAGES.page2);

  await nextClick;
  dump.rangeAfterClick = __node("patientRange").textContent;
  dump.offsetAfterClick = S.patientOffset;
  dump.errorAfterClick = __node("errorBox").textContent;

  // ---- a patient row opened during a tick still opens the drawer --------
  if (__open().length) fail("a request was left in flight before the row click");
  const rows = __node("patientRows").querySelectorAll("[data-patient]");
  if (!rows.length) fail("no patient rows were rendered");
  __TICK();
  await __drain();
  dump.pollingWhenRowClicked = S.polling;
  const rowClick = rows[0].onclick();
  await __drain();
  await __release(__PAGES.page2);        // the tick settles
  const rowQueued = __open();
  if (rowQueued.length !== 1) fail("the queued row click did not reach the network");
  dump.rowUrl = rowQueued[0].url;
  await __release(__PAGES.selected);
  await rowClick;
  dump.drawerOpen = __node("patientDrawer").classList.contains("open");
  dump.drawerBody = __node("detailBody").innerHTML;
  dump.selectedPatientId = S.patientId;

  // ---- the bare refresh a card action fires ----------------------------
  __TICK();
  await __drain();
  dump.pollingWhenRefreshCalled = S.polling;
  // The drawer is open, so every request from here carries the selected
  // patient and the server answers with the snapshot that has him on it.
  const bare = poll();
  await __drain();
  await __release(__PAGES.selected);     // the tick settles
  const bareQueued = __open();
  dump.bareRefreshReachedTheNetwork = (bareQueued.length === 1);
  if (bareQueued.length === 1) await __release(__PAGES.selected);
  dump.bareRefreshResult = await bare;

  dump.errorAtEnd = __node("errorBox").textContent;
  dump.urls = __REQUESTS.map(function(item){ return item.url; });
  process.stdout.write(JSON.stringify(dump));
})();
"""


def drive() -> dict:
    """Run the cockpit's own script in node and return what the DOM ended up as."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the build image
        raise unittest.SkipTest("node is not available in this environment")
    page1 = page(0)
    page2 = page(20)
    items = page2["patients"]["items"]
    selected_id = items[0].get("source_id") or items[0]["id"]
    payload = {
        "page1": page1,
        "page2": page2,
        "selected": page(20, selected=selected_id),
        "selected_id": selected_id,
    }
    with tempfile.TemporaryDirectory() as work:
        harness = Path(work) / "harness.js"
        pages = Path(work) / "pages.json"
        harness.write_text(SHIM + SCRIPT + TAIL, encoding="utf-8")
        pages.write_text(json.dumps(payload), encoding="utf-8")
        finished = subprocess.run(
            [node, str(harness), str(pages)],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "NODE_OPTIONS": ""},
        )
    if finished.returncode != 0:
        raise AssertionError(
            "the cockpit script failed in node:\n" + finished.stderr[-4000:]
        )
    result = json.loads(finished.stdout)
    result["selected_id"] = selected_id
    return result


class AClickDuringAPollStillLands(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dom = drive()
        if "harnessError" in cls.dom:
            raise AssertionError("the drive did not complete: " + cls.dom["harnessError"])

    # ------------------------------------------------------------------ setup
    def test_the_collision_this_test_stages_is_a_real_one(self) -> None:
        """Without a poll actually in flight the rest of this file proves nothing."""
        self.assertEqual(self.dom["bootRange"], "1-20 of 23")
        self.assertTrue(self.dom["pollingWhileTickInFlight"])
        self.assertTrue(self.dom["pollingWhenRowClicked"])
        self.assertTrue(self.dom["pollingWhenRefreshCalled"])
        # The click really did collide: it could not be answered until the
        # tick ahead of it came back.
        self.assertTrue(self.dom["clickWasNotAnsweredBeforeTheTick"])

    def test_the_background_tick_still_gives_up_when_a_poll_is_in_flight(self) -> None:
        """The queue is for the doctor's clicks; interval work never stacks."""
        self.assertTrue(self.dom["tickDidNotQueue"])

    # ------------------------------------------------------------------ pager
    def test_next_clicked_during_a_poll_moves_the_pager(self) -> None:
        self.assertEqual(self.dom["rangeAfterClick"], "21-23 of 23")
        self.assertEqual(self.dom["offsetAfterClick"], 20)

    def test_the_queued_click_asks_the_server_for_the_page_it_was_clicked_for(self) -> None:
        self.assertIn("patient_offset=20", self.dom["clickUrl"])

    def test_no_error_is_flashed_for_a_click_that_had_to_wait(self) -> None:
        self.assertEqual(self.dom["errorAfterClick"], "")
        self.assertEqual(self.dom["errorAtEnd"], "")

    # ----------------------------------------------------------------- drawer
    def test_a_patient_row_opened_during_a_poll_still_opens_the_drawer(self) -> None:
        self.assertTrue(self.dom["drawerOpen"])
        self.assertEqual(self.dom["selectedPatientId"], self.dom["selected_id"])
        self.assertIn("patient_id=" + self.dom["selected_id"], self.dom["rowUrl"])
        self.assertTrue(self.dom["drawerBody"])

    # ---------------------------------------------------------------- refresh
    def test_the_post_action_refresh_during_a_poll_still_refreshes(self) -> None:
        """`await poll()` after a card action is the third caller of this path."""
        self.assertTrue(self.dom["bareRefreshReachedTheNetwork"])
        self.assertTrue(self.dom["bareRefreshResult"])


class TheSourceKeepsTheQueueRatherThanTheDrop(unittest.TestCase):
    """A guard against the early return coming back as a "simplification"."""

    def test_the_bare_early_return_is_gone(self) -> None:
        self.assertNotIn("if (S.polling) return false;", SOURCE)

    def test_only_the_background_tick_may_give_up(self) -> None:
        self.assertIn("if (options.background && S.polling)", SOURCE)
        self.assertIn("poll({background:true})", SOURCE)


if __name__ == "__main__":
    unittest.main()
