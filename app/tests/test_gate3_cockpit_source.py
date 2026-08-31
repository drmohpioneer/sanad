"""The flagged v2 cockpit consumes one server projection and no legacy joins."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (APP_ROOT / "web" / "dashboard_v2.html").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(
        rf"\b(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        SOURCE,
    )
    if match is None:
        raise AssertionError(f"missing JavaScript function {name}")
    depth = 1
    at = match.end()
    quote = ""
    escaped = False
    while at < len(SOURCE) and depth:
        char = SOURCE[at]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        at += 1
    if depth:
        raise AssertionError(f"unterminated JavaScript function {name}")
    return SOURCE[match.end() : at - 1]


class OneSnapshotRead(unittest.TestCase):
    def test_page_has_one_inline_script(self) -> None:
        self.assertEqual(len(re.findall(r"<script(?:\s[^>]*)?>", SOURCE, re.I)), 1)

    def test_poll_loads_validates_and_atomically_commits_one_snapshot(self) -> None:
        poll = function_body("poll")
        self.assertEqual(poll.count("loadWorkspaceSnapshot("), 1)
        self.assertEqual(poll.count("validateWorkspaceSnapshot("), 1)
        self.assertEqual(poll.count("commitWorkspaceSnapshot("), 1)
        self.assertLess(poll.index("loadWorkspaceSnapshot("), poll.index("validateWorkspaceSnapshot("))
        self.assertLess(poll.index("validateWorkspaceSnapshot("), poll.index("commitWorkspaceSnapshot("))

        loader = function_body("loadWorkspaceSnapshot")
        self.assertEqual(loader.count("/api/v2/workspace-snapshot"), 1)
        self.assertIn("Authorization", loader)
        self.assertIn("Bearer ", loader)

        validator = function_body("validateWorkspaceSnapshot")
        for field in ("snapshot_id", "snapshot_id_kind", "schema_version", "as_of"):
            with self.subTest(field=field):
                self.assertRegex(validator, rf"if\s*\([^)]*{field}")
                self.assertIn("throw", validator)
        self.assertIn('snapshot.snapshot_id_kind !== "RECORD_VERSION"', validator)
        for block in (
            "snapshot.doctor",
            "snapshot.metrics",
            "snapshot.queues",
            "snapshot.rows",
            "snapshot.patients",
            "snapshot.agent_events",
            "snapshot.delivery",
            "snapshot.system",
            "snapshot.health",
            "snapshot.bp_tile",
            "snapshot.selected_patient",
        ):
            with self.subTest(block=block):
                self.assertIn(block, validator)
        self.assertIn("block.count !== block.row_ids.length", validator)
        self.assertIn("!snapshot.rows[rowId]", validator)
        self.assertIn("metric.count !== queue.count", validator)
        self.assertIn("events.cursor !== snapshot.event_cursor", validator)

        commit = function_body("commitWorkspaceSnapshot")
        self.assertEqual(
            len(re.findall(r"\bS\s*\.\s*workspaceSnapshot\s*=", commit)), 1
        )

    def test_v2_source_contains_no_legacy_workspace_gets(self) -> None:
        forbidden = (
            '"/health"', '"/board"', '"/cards"', '"/feed"',
            '"/reports"', '"/settings"', '"/summary"', '"/patient/"',
        )
        self.assertEqual([value for value in forbidden if value in SOURCE], [])

    def test_failed_poll_keeps_the_complete_previous_snapshot(self) -> None:
        poll = function_body("poll")
        catch = poll[poll.find("catch") :]
        self.assertNotRegex(catch, r"\bS\s*\.\s*workspaceSnapshot\s*=")


class ServerOwnsMeaning(unittest.TestCase):
    def test_browser_has_no_loop_state_or_colour_counting_table(self) -> None:
        self.assertNotIn("COLOUR_FOR", SOURCE)
        self.assertNotRegex(
            SOURCE,
            r"pending_review\s*:\s*[\"']red|waiting_patient\s*:\s*[\"']yellow",
        )

    def test_closure_and_bp_tiles_read_only_literal_server_fields(self) -> None:
        hero = function_body("renderHero")
        bp = function_body("renderBP")
        self.assertIn("closed_today", hero)
        self.assertNotRegex(hero, r"\.green\b|\bgreen\s*\]")
        self.assertIn("bp_tile", bp)
        self.assertNotRegex(bp, r"\.monitor\b|weight|glucose")

    def test_rendered_queue_counts_use_supplied_row_ids(self) -> None:
        queue = function_body("renderQueue")
        self.assertIn("row_ids", queue)
        self.assertNotRegex(queue, r"\.filter\s*\(")
        self.assertNotIn("row.legacy", queue)

    def test_all_action_and_uncertain_queues_are_reachable(self) -> None:
        self.assertIn('["doctor_actions","Other doctor actions"]', SOURCE)
        self.assertIn('["verification_unknown","Verification unknown"]', SOURCE)
        self.assertIn('["unclassified_red","Legacy red · unclassified"]', SOURCE)

    def test_metric_tiles_open_their_exact_server_queue(self) -> None:
        self.assertEqual(
            re.findall(r'data-metric-queue="([^"]+)"', SOURCE),
            [
                "terminal_waiting_review",
                "sanad_working",
                "closed_today",
                "active_patients",
            ],
        )
        self.assertIn("button.dataset.metricQueue", SOURCE)
        self.assertIn("renderQueue(S.selectedQueue)", SOURCE)

    def test_actions_reload_truth_and_do_not_optimistically_change_snapshot(self) -> None:
        submit = function_body("submitAction")
        self.assertIn("await poll()", submit)
        self.assertNotRegex(submit, r"workspaceSnapshot[^;=]*=")

    def test_blank_reply_or_note_is_refused_before_the_command(self) -> None:
        submit = function_body("submitAction")
        guard = submit.index('verb === "reply" || verb === "note"')
        command = submit.index("await postJson(")
        self.assertLess(guard, command)
        self.assertIn("String(input.value || \"\").trim()", submit)
        self.assertIn('showError("Type the answer first.", true)', submit)
        self.assertIn('typeof input.focus === "function"', submit)
        self.assertLess(submit.index("return;", guard), command)

    def test_openpatient_action_reads_and_validates_detail_before_opening(self) -> None:
        submit = function_body("submitAction")
        command = submit.index("await postJson(")
        navigation = submit.index('actionId.indexOf("openpatient:")')
        self.assertLess(command, navigation)
        self.assertIn('actionId.slice("openpatient:".length)', submit)
        self.assertIn(
            "if (await poll({patientId:patientId})) openPatientDetail()",
            submit,
        )

    def test_ok_false_is_a_failed_command_before_input_is_cleared(self) -> None:
        post = function_body("postJson")
        self.assertRegex(post, r"if\s*\([^)]*ok\s*===\s*false[^)]*\)\s*throw")
        self.assertLess(post.index("response.json()"), post.index("!response.ok"))
        submit = function_body("submitAction")
        self.assertLess(submit.index("await postJson("), submit.index("input.value = \"\""))

    def test_rejected_action_draft_and_reason_survive_snapshot_rerenders(self) -> None:
        queue = function_body("renderQueue")
        self.assertIn("S.drafts[draftKey]", queue)
        self.assertIn("data-draft-input", queue)
        self.assertIn("S.drafts[input.dataset.draftInput] = input.value", queue)
        self.assertIn("S.renderedQueueKey = queueRenderKey(", queue)

        render = function_body("render")
        self.assertIn("if (S.renderedQueueKey !== queueRenderKey(", render)

        submit = function_body("submitAction")
        catch = submit[submit.index("catch") :]
        self.assertIn("showError(", catch)
        self.assertIn("true", catch)

        paint = function_body("paintError")
        self.assertIn("S.actionError", paint)
        self.assertIn("S.pollError", paint)
        self.assertIn('classList.add("show")', paint)

        clear = function_body("clearError")
        self.assertIn('S.pollError = ""', clear)
        self.assertNotIn('S.actionError = ""', clear)

    def test_offline_refresh_truth_is_not_masked_by_an_action_error(self) -> None:
        show = function_body("showError")
        self.assertIn("S.actionError = message", show)
        self.assertIn("S.pollError = message", show)
        self.assertNotIn("if (!sticky && S.actionError) return", show)

        poll = function_body("poll")
        self.assertIn("The previous complete snapshot is still shown", poll)
        self.assertIn("No snapshot is being shown", poll)

    def test_failed_patient_or_page_request_cannot_drift_committed_ui_state(self) -> None:
        poll = function_body("poll")
        self.assertIn("return true", poll)
        self.assertIn("return false", poll)
        self.assertLess(
            poll.index("commitWorkspaceSnapshot(candidate)"),
            poll.index("S.patientOffset = requestedOffset"),
        )
        self.assertLess(
            poll.index("commitWorkspaceSnapshot(candidate)"),
            poll.index("S.patientId = requestedPatientId"),
        )
        self.assertIn("selected patient does not match the request", poll)
        self.assertNotIn("S.patientOffset += 20", SOURCE)
        self.assertIn("if (await poll({patientId:patientId})) openPatientDetail()", SOURCE)


if __name__ == "__main__":
    unittest.main()
