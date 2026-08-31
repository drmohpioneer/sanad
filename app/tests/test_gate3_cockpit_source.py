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
        # Danger leads the hero row: a safety product does not put its danger
        # count below four calmer numbers.
        self.assertEqual(
            re.findall(r'data-metric-queue="([^"]+)"', SOURCE),
            [
                "danger",
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


class EveryDecisionShowsItsEvidence(unittest.TestCase):
    """A card the doctor can act on has to print what it decided from."""

    def test_card_lines_are_rendered_escaped_in_every_queue_row(self) -> None:
        queue = function_body("renderQueue")
        self.assertIn("card.lines", queue)
        self.assertIn("Array.isArray(card.lines)", queue)
        self.assertIn('<div class="lines">', queue)
        # Card lines are patient-influenced text, so nothing reaches innerHTML
        # unescaped.
        self.assertRegex(queue, r'<div class="line[^>]*>\'\s*\+\s*escapeHtml\(')
        # The attribution line is evidence, not a headline.
        self.assertIn('decided_by:', queue)
        self.assertIn('" muted"', queue)
        rows = SOURCE[SOURCE.index("byId(\"queueRows\").innerHTML") :]
        self.assertLess(rows.index("class=\"text\""), rows.index("+ lines"))
        self.assertLess(rows.index("+ lines"), rows.index('class="actions"'))

    def test_rows_carrying_media_say_so(self) -> None:
        queue = function_body("renderQueue")
        self.assertIn("Array.isArray(row.media)", queue)
        self.assertIn('media-note', queue)
        self.assertIn("escapeHtml(media.length)", queue)

    def test_actions_without_their_evidence_are_offered_inert(self) -> None:
        queue = function_body("renderQueue")
        self.assertIn("const evidence = Boolean(cardLines.length || text)", queue)
        self.assertRegex(queue, r"if\s*\(!evidence\)[^;]*disabled")
        self.assertIn("evidence unavailable in this snapshot", queue)
        # An inert button carries no action id, so nothing can wire a click
        # onto it.
        inert = queue[queue.index("if (!evidence)") :]
        self.assertNotIn("data-action=", inert[: inert.index("const inputId")])

    def test_patient_drawer_prints_the_loop_evidence_not_only_its_title(self) -> None:
        detail = function_body("loopDetail")
        self.assertIn("item.readings", detail)
        self.assertIn("item.results", detail)
        self.assertIn("verifiedSummary(item.verified)", detail)
        self.assertIn("Readings", detail)
        self.assertIn("Results", detail)
        self.assertIn("Verified", detail)

        lines = function_body("detailLines")
        self.assertIn("escapeHtml(heading)", lines)
        self.assertIn("escapeHtml(value)", lines)

        opener = function_body("openPatientDetail")
        self.assertIn("loops.map(loopDetail)", opener)


class DangerLeadsTheCockpit(unittest.TestCase):
    def test_danger_is_the_first_and_strongest_hero_tile(self) -> None:
        hero = SOURCE[SOURCE.index('<section class="hero"') : SOURCE.index("</section>")]
        self.assertLess(
            hero.index('data-metric-queue="danger"'),
            hero.index('data-metric-queue="terminal_waiting_review"'),
        )
        self.assertIn('class="metric danger"', hero)
        self.assertIn('id="dangerMetric"', hero)
        self.assertIn(".metric.danger{", SOURCE)
        self.assertIn("border-top:5px solid var(--red)", SOURCE)

        render = function_body("renderHero")
        self.assertIn("metrics.danger_unacknowledged", render)
        self.assertIn('byId("dangerMetric").textContent', render)

    def test_unclassified_red_is_surfaced_beside_the_danger_count(self) -> None:
        render = function_body("renderHero")
        self.assertIn("queues.unclassified_red", render)
        self.assertIn("legacy red", render)
        self.assertIn('queueLink("unclassified_red"', render)

        link = function_body("queueLink")
        self.assertIn("data-open-queue=", link)
        self.assertIn("escapeHtml(queueName)", link)
        self.assertIn("escapeHtml(label)", link)

        opener = function_body("openQueue")
        self.assertIn("S.selectedQueue = name", opener)
        self.assertIn("renderQueue(S.selectedQueue)", opener)

        bind = function_body("bindQueueLinks")
        self.assertIn("[data-open-queue]", bind)
        self.assertIn("openQueue(link.dataset.openQueue)", bind)
        # Opening the legacy queue from inside the tile must not also fire the
        # tile's own click.
        self.assertIn("event.stopPropagation()", bind)

    def test_five_tiles_lay_out_at_desktop_and_stack_on_a_phone(self) -> None:
        self.assertIn("grid-template-columns:1.25fr repeat(4,minmax(0,1fr))", SOURCE)
        narrow = re.findall(r"@media\(max-width:(\d+)px\)\{([^\n]*)", SOURCE)
        self.assertTrue(narrow)
        for width, rules in narrow:
            with self.subTest(width=width):
                if ".hero{" in rules:
                    self.assertIn(".metric.danger{grid-column:1 / -1}", rules)


class NoReturnedEventGoesUnseen(unittest.TestCase):
    def test_cursor_page_and_recent_tail_are_rendered_as_one_union(self) -> None:
        events = function_body("renderEvents")
        # The old page read one or the other, so a whole cursor page could
        # advance past the doctor unseen.
        self.assertNotRegex(SOURCE, r"block\.recent\s*\|\|\s*block\.items")
        self.assertNotRegex(SOURCE, r"block\.items\s*\|\|\s*block\.recent")
        self.assertIn("Array.isArray(block.items)", events)
        self.assertIn("Array.isArray(block.recent)", events)
        self.assertIn("cursorPage.concat(tail)", events)
        self.assertIn("eventKey(event)", events)
        self.assertIn("events.sort(", events)
        self.assertIn("eventOrder(left) - eventOrder(right)", events)
        self.assertIn("slice(-20)", events)

        key = function_body("eventKey")
        self.assertIn("event.id", key)

        order = function_body("eventOrder")
        self.assertIn("Date.parse(", order)
        self.assertIn("ts_ms", order)


class TheDrawerNeverShowsAStaleSnapshot(unittest.TestCase):
    def test_render_refreshes_or_closes_an_open_patient_drawer(self) -> None:
        render = function_body("render")
        self.assertIn("refreshOpenDrawer(snapshot)", render)

        refresh = function_body("refreshOpenDrawer")
        self.assertIn("if (!drawerIsOpen()) return", refresh)
        self.assertIn("snapshot.selected_patient", refresh)
        self.assertIn("detail.patient.id === S.patientId", refresh)
        self.assertIn("openPatientDetail()", refresh)
        self.assertIn("closePatientDrawer()", refresh)
        self.assertIn("S.patientId = null", refresh)
        self.assertIn(
            'showError("Patient view refreshed; reopen from the list.", false)',
            refresh,
        )
        # Non-sticky: it must not be written into the action error slot.
        self.assertNotIn("true)", refresh[refresh.index("showError(") :])

        opened = function_body("drawerIsOpen")
        self.assertIn('classList.contains("open")', opened)


class OptionalServerFieldsAreGuardedByPresence(unittest.TestCase):
    def test_reviewed_unverified_is_validated_like_every_other_metric_pair(self) -> None:
        validator = function_body("validateWorkspaceSnapshot")
        self.assertIn("snapshot.metrics.reviewed_unverified !== undefined", validator)
        self.assertIn(
            'requireCountedRefs(snapshot.queues.reviewed_unverified, "queue reviewed_unverified", true)',
            validator,
        )
        self.assertIn(
            'requireCountedRefs(snapshot.metrics.reviewed_unverified, "metric reviewed_unverified", false)',
            validator,
        )
        self.assertIn("metric reviewed_unverified does not match its queue", validator)

    def test_reviewed_unverified_count_rides_the_closed_today_note(self) -> None:
        render = function_body("renderHero")
        self.assertIn("metrics.reviewed_unverified", render)
        self.assertIn("reviewed without verified evidence", render)
        self.assertIn('queueLink("reviewed_unverified"', render)
        # Presence-guarded: absent metric, no note, no thrown read.
        self.assertIn("metrics.reviewed_unverified || null", render)
        self.assertIn("reviewedUnverified && reviewedUnverified.count > 0", render)

    def test_reviewed_unverified_tab_appears_only_when_the_server_projects_it(self) -> None:
        self.assertIn('["reviewed_unverified","Reviewed · unverified"]', SOURCE)
        self.assertIn("const OPTIONAL_QUEUES = [", SOURCE)
        visible = function_body("visibleQueues")
        self.assertIn("OPTIONAL_QUEUES", visible)
        self.assertIn("if (queues[item[0]]) offered.push(item)", visible)
        self.assertIn("QUEUES.concat(offered)", visible)

        tabs = function_body("renderQueueTabs")
        self.assertIn("visibleQueues(snapshot)", tabs)


if __name__ == "__main__":
    unittest.main()
