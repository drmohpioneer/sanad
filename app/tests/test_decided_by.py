"""Who decided this, counted on the screen, and the count that must stay zero.

rev 17 items 12, 14 and 15.

Item 12 puts a badge on every card's audit footer and a line at the top of the
Inbox: "N decided by code · N model choice, guards in code · 0 decided by a
model alone". The three words a judge is looking for appear once, with a zero in
front of them, and the zero is not a claim: it is a count over the `decided_by`
field every event already carries.

The count is only worth anything if it cannot quietly stop being zero. So the
same bucketing rule is asserted twice here: once in Python over every
`decided_by` string this codebase can write, and once against the rule the
dashboard applies to them. A new event whose label says a model decided
something alone fails the suite, which is the image build.

Item 14 asserts the other half: the calls a guard refused are on the record and
on the screen, in the shape the dashboard reads.

Item 15 asserts that a card sent to a doctor about a patient carries that
patient, which is what lets the Inbox offer "Open the patient".
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parents[1]
CORE = APP_ROOT / "core"
DASHBOARD = (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")
ADAPTERS = (CORE / "adapters.py").read_text(encoding="utf-8")
CHASER = (CORE / "chaser.py").read_text(encoding="utf-8")


def _trees() -> dict[str, ast.Module]:
    return {path.name: ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(CORE.glob("*.py"))}


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "..."` for every name that starts with DECIDED.

    rev 18. Half the labels moved out of the dict literals and into named
    constants, because one card is now reached by two different deciders and
    the label has to be chosen rather than typed twice. A rail that only read
    `"decided_by": "..."` literals would have gone quiet at exactly the moment
    there was more to check, so it reads both.
    """
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("DECIDED"):
                found[target.id] = value.value
    return found


def _returns_of(tree: ast.Module, name: str, constants: dict[str, str]
                ) -> list[str]:
    """The labels a helper like `decided_by_sentinel` can return."""
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            found = [constants[n.id] for n in ast.walk(node)
                     if isinstance(n, ast.Name) and n.id in constants]
            return found
    return []


def _resolve(node: ast.AST, tree: ast.Module, constants: dict[str, str],
             enclosing: Optional[ast.AST]) -> list[str]:
    """Every label one expression can produce, read without running anything.

    A card send may name its label four ways and all four are used: a string
    written where it is sent, a module constant, a helper that chooses between
    two module constants, and a parameter whose default is one of them (the
    Coordinator's one card function is reached by two different deciders). An
    expression this cannot read comes back empty, which fails the rail: an
    unreadable label is not a labelled card.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        if node.id in constants:
            return [constants[node.id]]
        if enclosing is not None:
            args = enclosing.args
            names = [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
            defaults = ([None] * (len(args.args) - len(args.defaults))
                        + list(args.defaults) + list(args.kw_defaults))
            for arg_name, default in zip(names, defaults):
                if arg_name == node.id and default is not None:
                    return _resolve(default, tree, constants, None)
        return []
    if isinstance(node, ast.Call):
        return _returns_of(tree, getattr(node.func, "id", ""), constants)
    return []


def labels() -> dict[str, list[str]]:
    """Every label core/ can write: the dict literals and the named constants."""
    found: dict[str, list[str]] = {}
    for name, tree in _trees().items():
        hits = list(_constants(tree).values())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "decided_by"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    hits.append(value.value)
        if hits:
            found[name] = hits
    return found


def bucket(label: str) -> str:
    """The dashboard's rule, in Python. The two must not drift."""
    by = (label or "").lower()
    if not by:
        return "none"
    model, code = "model" in by, "code" in by
    if model and code:
        return "model"
    if code:
        return "code"
    if model:
        return "alone"
    return "none"


def card_sends() -> list[tuple[str, int, list[str]]]:
    """Every doctor-bound card send in core/, with the labels it can carry.

    (file, line, labels). An empty list is a card with no readable
    `decided_by`, which is the "unlabelled" the Inbox header counts and which
    rev 18 item 3 requires to be zero.
    """
    out: list[tuple[str, int, list[str]]] = []
    for name, tree in _trees().items():
        # The channel seam bridges an already-decided direct edge card into a
        # shadow OutboundIntent. It is not a second product decision or a card
        # event, so counting its generic constructor would double-count every
        # caller and demand a label from transport code.
        if name == "adapters.py":
            continue
        constants = _constants(tree)
        functions = [n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "OutboundMessage"):
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            if "card" not in keywords:
                continue
            enclosing = None
            for function in functions:
                if (function.lineno <= node.lineno
                        and (enclosing is None
                             or function.lineno > enclosing.lineno)):
                    enclosing = function
            found: list[str] = []
            meta = keywords.get("meta")
            if isinstance(meta, ast.Dict):
                for key, value in zip(meta.keys, meta.values):
                    if isinstance(key, ast.Constant) and key.value == "decided_by":
                        found = _resolve(value, tree, constants, enclosing)
            out.append((name, node.lineno, found))
    return out


class NothingIsDecidedByAModelAlone(unittest.TestCase):
    def test_the_codebase_writes_labels_at_all(self) -> None:
        found = labels()
        self.assertGreaterEqual(sum(len(v) for v in found.values()), 15)
        for name in ("coordinator.py", "chaser.py", "concierge.py",
                     "extractor.py", "registrar.py"):
            with self.subTest(name=name):
                self.assertIn(name, found)

    def test_the_administrative_tier_labels_by_the_net_that_found_it(self
                                                                    ) -> None:
        """rev 18. core/intents.py writes no label of its own on purpose: a
        chore the pattern list matched had no model near the decision, one the
        add-only vote named did, and one function decides which of the two the
        event carries so the two callers cannot drift apart."""
        source = (CORE / "intents.py").read_text(encoding="utf-8")
        self.assertIn("coordinator.intent_decided_by(found)", source)
        chooser = (CORE / "coordinator.py").read_text(encoding="utf-8").split(
            "def intent_decided_by(", 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("DECIDED_BY_INTENT_VOTE", chooser)
        self.assertIn("DECIDED_BY_INTENT_CODE", chooser)
        constants = _constants(ast.parse(
            (CORE / "coordinator.py").read_text(encoding="utf-8")))
        self.assertEqual(bucket(constants["DECIDED_BY_INTENT_CODE"]), "code")
        self.assertEqual(bucket(constants["DECIDED_BY_INTENT_VOTE"]), "model")

    def test_not_one_of_them_says_a_model_decided_it_alone(self) -> None:
        for name, found in labels().items():
            for label in found:
                with self.subTest(name=name, label=label):
                    self.assertNotEqual(
                        bucket(label), "alone",
                        f"{name} writes a label with a model and no code in it")

    def test_none_of_them_is_empty_or_unreadable(self) -> None:
        """An unlabelled card is a gap in the audit trail, not a zero."""
        for name, found in labels().items():
            for label in found:
                with self.subTest(name=name, label=label):
                    self.assertNotEqual(bucket(label), "none")

    def test_the_agents_own_choices_are_labelled_as_choices(self) -> None:
        joined = " ".join(sum(labels().values(), []))
        self.assertIn("model choice, guards in code (core/policy.py)", joined)


class EveryCardSaysWhoDecidedIt(unittest.TestCase):
    """rev 18 item 3: "unlabelled" is a number on the board, so it is a test.

    Live on rev 17, thirteen of the card sends in this codebase carried
    no `decided_by` at all. The badge is drawn from that one field, so it was
    not drawn, and the Inbox header counted the one card on the board as
    unlabelled. The header still counts, and now it cannot count anything: a
    card send with no readable label fails here, which is the image build.
    """

    def test_every_card_send_is_counted_and_the_rail_can_read_them_all(self
                                                                       ) -> None:
        """Fifteen at rev 18, nineteen in wave C, twenty-one in round two.

        The nineteenth is the unreadable voice note (codex item 11): a
        transcription that hangs or throws used to be an HTTP 500 and is now a
        card. Round two adds the bare-name lookup and explicit opt-out cards.

        The number is not the point and the rail is: every card send in core/
        has to carry a `decided_by` this file can read without running it. The
        count is asserted so that a card added without a label is a failure
        here rather than an "unlabelled" the doctor reads on the board.
        """
        sends = card_sends()
        self.assertEqual(len(sends), 21)
        for name, line, found in sends:
            with self.subTest(where=f"{name}:{line}"):
                self.assertTrue(found, "this card send carries no decided_by")

    def test_not_one_card_send_is_unlabelled(self) -> None:
        """The zero the Inbox header prints, asserted over the source."""
        unlabelled = [f"{name}:{line}" for name, line, found in card_sends()
                      if not found]
        self.assertEqual(unlabelled, [])

    def test_every_card_label_is_code_or_model_and_never_a_model_alone(self
                                                                      ) -> None:
        for name, line, found in card_sends():
            for label in found:
                with self.subTest(where=f"{name}:{line}", label=label):
                    self.assertIn(bucket(label), ("code", "model"))

    def test_the_rail_walks_every_module_that_sends_a_card(self) -> None:
        """Not the four rev 17 happened to name: core/registrar.py was outside
        that list, which is why its Committed card was found live and not here."""
        walked = {name for name, _, _ in card_sends()}
        for name in ("chaser.py", "concierge.py", "coordinator.py",
                     "extractor.py", "registrar.py"):
            with self.subTest(name=name):
                self.assertIn(name, walked)

    def test_the_header_always_prints_the_unlabelled_count(self) -> None:
        """Including when it is zero: a count that only appears when it is
        broken cannot be read as a promise when it is not."""
        summary = DASHBOARD.split("function decidedSummary(", 1)[1].split(
            "function decidedByCode(", 1)[0]
        self.assertIn('c.none + " unlabelled"', summary)
        self.assertNotIn("if (c.none)", summary)

    def test_the_committed_card_opens_the_patient(self) -> None:
        """rev 18 item 4: it names the patient, so it carries the patient."""
        source = (CORE / "registrar.py").read_text(encoding="utf-8")
        commit = source.split("async def commit(", 1)[1]
        card = commit.split('"title": "Committed.",', 1)[0]
        self.assertIn("patient_id=patient.id", card)


class TheDashboardCountsThemTheSameWay(unittest.TestCase):
    def test_the_four_buckets_and_their_words(self) -> None:
        for label in ("CODE", "MODEL CHOICE · CODE GUARDS", "MODEL ALONE",
                      "UNLABELLED"):
            with self.subTest(label=label):
                self.assertIn(label, DASHBOARD)

    def test_a_missing_label_is_unlabelled_and_never_a_model_alone(self) -> None:
        rule = DASHBOARD.split("function decidedBucket(", 1)[1].split(
            "function decidedBadge(", 1)[0]
        self.assertIn('if (!by) return "none";', rule)
        self.assertIn('if (model && code) return "model";', rule)
        self.assertLess(rule.index('return "model"'), rule.index('return "alone"'))

    def test_the_header_line_is_computed_from_the_cards_it_shows(self) -> None:
        render = DASHBOARD.split("function renderInbox(", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertIn("decidedSummary(items)", render)
        summary = DASHBOARD.split("function decidedSummary(", 1)[1].split(
            "function decidedByCode(", 1)[0]
        self.assertIn("decided by a model alone", summary)
        self.assertIn("decided by code", summary)
        self.assertIn("model choice, guards in code", summary)

    def test_every_audit_footer_carries_the_badge(self) -> None:
        audit = DASHBOARD.split("function auditLine(", 1)[1].split(
            "\n/* Mirrors", 1)[0]
        self.assertIn("decidedBadge(meta)", audit)


class ARefusalIsShownAsARefusal(unittest.TestCase):
    """rev 17 item 14, and item 7's cheapest real implementation."""

    def test_the_withheld_nudge_says_who_refused_it(self) -> None:
        self.assertIn(
            'REFUSED_BY_CODE = "refused by code (core/policy.py)"', CHASER)
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertIn('line = f"{REFUSED_BY_CODE}: {allowed.why}"', fire)
        self.assertIn('"refused": [allowed.as_meta()]', fire)

    def test_the_ladder_never_claims_a_model_chose_for_it(self) -> None:
        """`Decision.audit()` ends in "decided_by: model choice"; no model ran."""
        fire = CHASER.split("async def fire(", 1)[1]
        withheld = fire.split("if not allowed.allowed:", 1)[1].split(
            "return {", 1)[0]
        self.assertNotIn("allowed.audit()", withheld)

    def test_the_feed_draws_the_refusal_before_the_accepted_row(self) -> None:
        feed = DASHBOARD.split("function renderFeed(", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertIn("refusedRows(e)", feed)
        self.assertIn("here.push(feedRow(e))", feed)
        rows = DASHBOARD.split("function refusedRows(", 1)[1].split(
            "function isPureRefusal(", 1)[0]
        self.assertIn('chip:"REFUSED"', rows)
        self.assertIn('"guard: "', rows)

    def test_one_seeded_loop_sits_at_the_policy_ceiling(self) -> None:
        """So the refusal can be reproduced, not staged (item 7)."""
        from core import background, policy

        rows = [c for person in background.PEOPLE for c in person.contracts
                if c.contacts >= policy.DEFAULT.max_contacts]
        self.assertEqual(len(rows), 1, "exactly one loop is at the ceiling")
        self.assertEqual(rows[0].title, "Glucose tolerance test")

    def test_force_due_can_drop_its_own_exemption(self) -> None:
        self.assertIn('STRICT = "strict"', CHASER)
        force = CHASER.split("async def force_due(", 1)[1].split(
            "async def fire(", 1)[0]
        self.assertIn('"force": not strict', force)


class ADoctorCardSaysWhichPatientItIsAbout(unittest.TestCase):
    """rev 17 item 15: the Inbox could not offer "Open the patient"."""

    def test_the_message_can_carry_a_patient(self) -> None:
        self.assertIn("patient_id: Optional[str] = None", ADAPTERS)

    def test_the_web_adapter_uses_it_only_when_the_ref_has_none(self) -> None:
        send = ADAPTERS.split("class WebAdapter:", 1)[1].split(
            "class TelegramAdapter:", 1)[0]
        self.assertIn("patient_id = patient_id or msg.patient_id", send)

    def test_every_card_about_a_patient_names_that_patient(self) -> None:
        """Each doctor-bound send whose text names a patient carries the id."""
        for name in ("coordinator.py", "chaser.py", "concierge.py",
                     "extractor.py"):
            source = (CORE / name).read_text(encoding="utf-8")
            for block in source.split("OutboundMessage(")[1:]:
                head = block.split("))", 1)[0]
                if "card=" not in head or "patient.name" not in head:
                    continue
                with self.subTest(name=name, head=head.split("\n")[0][:60]):
                    self.assertIn("patient_id=", head)


class HandledWhileYouSlept(unittest.TestCase):
    """rev 17 item 13, repaired by rev 18 item 2.

    Live, every receipt on this tile belonged to a different message and one
    row paired a scheduled wake-up with a message from six minutes earlier. The
    cause was one rule: the tile searched the feed for the answer, forward in
    time from the event that explains it, and the answer is always written
    BEFORE that event. So the pair is now written by id at the moment of
    sending, and the rule below is asserted twice, in Python over a fixture
    feed and against the dashboard's own source, so the two cannot drift.
    """

    # The rule the dashboard applies, in Python. Kept beside the source
    # assertions underneath it for the reason the bucket rule above is: a rule
    # nobody can run is a description.
    TRIGGERS = ("reply", "evidence", "intent")

    @staticmethod
    def pairs(events: list[dict]) -> list[dict]:
        by_id = {e["id"]: e for e in events}
        out = []
        for sys in events:
            meta = sys.get("meta") or {}
            if meta.get("answered") is not True or not sys.get("patient_id"):
                continue
            if meta.get("trigger") not in HandledWhileYouSlept.TRIGGERS:
                continue
            said = by_id.get(meta.get("said") or "")
            if said is None or said.get("kind") != "patient_in":
                continue
            if said.get("patient_id") != sys.get("patient_id"):
                continue
            if not str(said.get("text") or "").strip():
                continue
            sent = list(meta.get("sent") or [])
            out.append({"said": said["id"], "sys": sys["id"],
                        "receipt": sent[0] if sent else ""})
        return out

    @staticmethod
    def feed() -> list[dict]:
        """The live feed that produced the wrong receipts, in its own order.

        10:10:23 the patient writes; 10:10:31 the answer goes out; 10:10:31 the
        event that explains it is written, a moment after the answer; 10:16:19 a
        scheduled wake-up sends an unrelated nudge to the same patient and
        writes its own event. Six minutes apart, well inside the twenty minute
        window the old rule used.
        """
        return [
            {"id": "said-1", "kind": "patient_in", "patient_id": "p2",
             "text": "المعمل مقفول لحد الأحد", "meta": {}},
            {"id": "answer-1", "kind": "agent_out", "patient_id": "p2",
             "text": "تمام. هسأل عليك تاني يوم الثلاثاء.", "meta": {}},
            {"id": "sys-1", "kind": "system", "patient_id": "p2",
             "text": "coordinator: classify_barrier on Fasting blood sugar",
             "meta": {"answered": True, "trigger": "reply", "said": "said-1",
                      "sent": ["answer-1"],
                      "coordinator": {"tool": "classify_barrier", "allowed": True,
                                      "reason": "The lab is closed until Sunday"}}},
            {"id": "nudge-1", "kind": "agent_out", "patient_id": "p2",
             "text": "أهلاً 👋 فاكر إن Test Doctor طالب منك Follow-up visit؟",
             "meta": {}},
            {"id": "sys-2", "kind": "system", "patient_id": "p2",
             "text": "coordinator: schedule_next_contact on Follow-up visit",
             "meta": {"answered": True, "trigger": "wake", "said": "",
                      "sent": ["nudge-1"],
                      "coordinator": {"tool": "schedule_next_contact",
                                      "allowed": True, "reason": "the ladder step"}}},
        ]

    def test_the_receipt_is_the_answer_and_not_the_nudge_six_minutes_later(
            self) -> None:
        rows = self.pairs(self.feed())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["said"], "said-1")
        self.assertEqual(rows[0]["receipt"], "answer-1")
        self.assertNotEqual(rows[0]["receipt"], "nudge-1")

    def test_a_wake_up_never_appears_on_this_tile(self) -> None:
        """Even carrying `answered` true, which every wake-up turn does."""
        rows = self.pairs(self.feed())
        self.assertNotIn("sys-2", [row["sys"] for row in rows])

    def test_a_turn_that_answered_nothing_is_not_a_row(self) -> None:
        feed = self.feed()
        feed[2]["meta"]["answered"] = False
        self.assertEqual(self.pairs(feed), [])

    def test_a_turn_with_no_message_behind_it_is_not_a_row(self) -> None:
        feed = self.feed()
        feed[2]["meta"]["said"] = ""
        self.assertEqual(self.pairs(feed), [])

    def test_the_coordinator_writes_both_halves_of_the_pair_by_id(self) -> None:
        source = (CORE / "coordinator.py").read_text(encoding="utf-8")
        say = source.split("async def _say(", 1)[1].split("async def _card(", 1)[0]
        self.assertIn("sent = await fanout().send(", say)
        self.assertIn("turn.sent.append(sent)", say)
        execute = source.split("async def _execute(", 1)[1].split(
            "# The other half of a barrier", 1)[0]
        self.assertIn('"said": turn.said, "sent": list(turn.sent),', execute)
        intent = source.split("async def _execute_intent(", 1)[1]
        self.assertIn('"said": turn.said, "sent": list(turn.sent),', intent)

    def test_the_send_hands_back_the_receipt_it_wrote(self) -> None:
        """`meta.sent` can only be written if sending answers with an id."""
        web = ADAPTERS.split("class WebAdapter:", 1)[1].split(
            "class TelegramAdapter:", 1)[0]
        self.assertIn("event = await events.append_event(", web)
        self.assertIn("return event.id", web)
        out = ADAPTERS.split("class Fanout:", 1)[1]
        self.assertIn("receipt = receipt or written", out)

    def test_the_patient_message_carries_its_own_id_down_the_path(self) -> None:
        concierge = (CORE / "concierge.py").read_text(encoding="utf-8")
        self.assertIn("said_id = said.id", concierge)
        self.assertIn("said=said_id", concierge)
        intents = (CORE / "intents.py").read_text(encoding="utf-8")
        self.assertIn("said=said", intents)

    def test_the_tile_reads_the_ids_and_searches_for_nothing(self) -> None:
        pairs = DASHBOARD.split("function sleptPairs(", 1)[1].split(
            "\nfunction oneStop(", 1)[0]
        self.assertIn("m.said ? eventById(m.said)", pairs)
        self.assertIn("SLEPT_TRIGGERS[m.trigger]", pairs)
        self.assertIn("m.sent[0]", pairs)
        # The forward search that produced the wrong receipts, by its shape.
        self.assertNotIn('x.kind === "agent_out"', pairs)
        self.assertIn("const SLEPT_TRIGGERS = {reply:1, evidence:1, intent:1};",
                      DASHBOARD)

    def test_the_python_rule_and_the_dashboards_are_the_same_three_triggers(
            self) -> None:
        line = DASHBOARD.split("const SLEPT_TRIGGERS = {", 1)[1].split("}", 1)[0]
        self.assertEqual(sorted(part.split(":")[0].strip()
                                for part in line.split(",")),
                         sorted(self.TRIGGERS))

    def test_it_says_the_sentence_the_doctor_is_meant_to_read(self) -> None:
        render = DASHBOARD.split("function renderSlept(", 1)[1].split(
            "\n/* ------", 1)[0]
        self.assertIn("You were not woken.", render)
        self.assertIn("waiting for you above", render)
        self.assertIn("receipt ", render)

    def test_the_line_reads_as_one_sentence_with_one_full_stop(self) -> None:
        """rev 18 item b: "Sanad recorded a barrier: the lab is closed until
        Sunday.", and never "..", and never a capital after the colon."""
        did = DASHBOARD.split("function sleptDid(", 1)[1].split(
            "function renderSlept(", 1)[0]
        self.assertIn('": " + lowerFirst(why, who)', did)
        self.assertIn("oneStop(", did)
        stop = DASHBOARD.split("function oneStop(", 1)[1].split(
            "\n/*", 1)[0]
        self.assertIn('replace(/[\\s.]+$/, "")', stop)

    def test_the_agent_tile_shows_the_receipt_the_event_carries(self) -> None:
        """rev 18 item d: "not sent yet" for a message that had been sent."""
        steps = DASHBOARD.split("function agentSteps(", 1)[1].split(
            "\n/* ---", 1)[0]
        self.assertIn("hit.meta.sent", steps)
        self.assertIn("eventById(receipt)", steps)

    def test_the_send_now_reschedule_finds_its_ladder_nudge_by_key(self) -> None:
        """fix queue rev 18 item 8, which is defect 2 of the live run.

        `schedule_next_contact` with `days_from_now: 0` on a wake-up is the S3
        ladder step, and core/chaser.py sends it AFTER the Coordinator has
        written its own event, so `meta.sent` on that event is empty by
        construction and the fourth step read "not sent yet" beside a nudge that
        had gone out in the same second. Both records now carry the one wake-up
        key the Chaser claimed, and the tile pairs them by it.
        """
        chaser = (CORE / "chaser.py").read_text(encoding="utf-8")
        ladder = chaser.split("text = nudge_text(", 1)[1].split(
            "counted = loop.attempts", 1)[0]
        self.assertIn('"receipt": send.id', ladder)

        steps = DASHBOARD.split("function agentSteps(", 1)[1].split(
            "\n/* ---", 1)[0]
        self.assertIn("hit.meta.detail && hit.meta.detail.ladder", steps)
        self.assertIn("a.receipt === hit.meta.receipt", steps)
        self.assertIn("the ladder step is going out now", steps)

    def test_the_coordinator_still_writes_the_two_halves_of_that_key(
            self) -> None:
        """The pairing is only possible while both fields are on the event."""
        source = (CORE / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn('detail["ladder"] = True', source)
        self.assertIn('"detail": detail, "receipt": turn.receipt', source)

    def test_the_tile_is_on_the_board(self) -> None:
        self.assertIn('id="sleptBody"', DASHBOARD)
        self.assertIn("renderSlept();", DASHBOARD)
        self.assertIn(".t-slept{grid-column:1/5;grid-row:6/7}", DASHBOARD)

    def test_its_row_class_does_not_collide_with_the_screen_reader_one(
            self) -> None:
        """`.sr` is position:absolute;width:1px, and it swallowed this tile once."""
        self.assertIn(".slept .row{", DASHBOARD)
        self.assertNotIn(".slept .sr{", DASHBOARD)


class AReasonIsPrintedWithOneFullStop(unittest.TestCase):
    """rev 18 item a: "refuses to do the lipid panel.." on the live board."""

    def test_a_reason_never_arrives_with_its_own_full_stop(self) -> None:
        from core import policy

        self.assertEqual(policy.one_sentence("The lab is closed until Sunday."),
                         "The lab is closed until Sunday")
        self.assertEqual(
            policy.one_sentence("  refused today.  Scheduling tomorrow. "),
            "refused today. Scheduling tomorrow")
        self.assertEqual(policy.one_sentence(""), "")

    def test_the_guard_trims_it_where_every_decision_is_made(self) -> None:
        from datetime import datetime, timedelta, timezone

        from core import policy

        now = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
        facts = policy.LoopFacts(now=now, due_at=now + timedelta(days=10))
        decision = policy.check("escalate_barrier", {"barrier": "cost"}, facts,
                                reason="The patient says it is too expensive.")
        self.assertEqual(decision.reason,
                         "The patient says it is too expensive")
        self.assertNotIn("..", decision.audit())

    def test_the_card_line_that_printed_two_of_them(self) -> None:
        source = (CORE / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn('f"Coordinator\'s reason: {decision.reason or \'not stated\'}.",',
                      source)


if __name__ == "__main__":
    unittest.main()
