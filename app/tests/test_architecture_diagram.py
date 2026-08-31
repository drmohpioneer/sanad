"""The picture on Devpost and the picture in the repository are one picture.

`docs/ARCHITECTURE.md` says the SVG is the rendered version of the mermaid block
below it, "for use on Devpost where an embedded mermaid block will not render".
So the SVG is what a judge looks at first, and until rev 17 it was the S4
diagram: no Care Coordinator, no `core/policy.py`, no administrative tier, no
blood-pressure gate. The code had moved twice and the image had not, which made
the picture carrying thirty percent of the architecture score smaller and less
agentic than the system it was drawing.

A document is not a rail. This is: every node declared in the mermaid has a
group with `id="n-<ID>"` in the SVG, and every such group has a node in the
mermaid. Adding a node to one and not the other fails the suite, which is the
Dockerfile's build step.

The image's build context is `app/` alone, so `docs/` is not copied into it and
these skip there, exactly as the runbook tests in tests/test_background.py do.
They run on the laptop and in any checkout of the whole tree, which is where the
diagram actually gets edited.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
ROOT = DOCS.parent
ARCHITECTURE = DOCS / "ARCHITECTURE.md"
SVG = DOCS / "architecture.svg"

# The README is the landing page of the public repository, so it sits at the
# root there; in the working tree it still sits in docs/ next to its siblings.
# Either place is fine and the rail reads whichever one is present. Neither is
# the failure the tests below are there to catch.
README = next(
    (path for path in (ROOT / "README.md", DOCS / "README.md") if path.exists()),
    None,
)

HAS_DOCS = unittest.skipUnless(
    ARCHITECTURE.exists() and SVG.exists(), "docs/ is outside the image"
)

# `ID["label"]`, `ID[["label"]]` and `ID[("label")]` are all node declarations.
NODE = re.compile(r"\b([A-Z][A-Z0-9]*)\[")


def mermaid_block() -> str:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    return text.split("```mermaid", 1)[1].split("```", 1)[0]


def mermaid_nodes() -> set[str]:
    return set(NODE.findall(mermaid_block()))


def svg_nodes() -> set[str]:
    found = re.findall(r'id="n-([A-Z0-9]+)"', SVG.read_text(encoding="utf-8"))
    return set(found)


@HAS_DOCS
class TheSvgMatchesTheMermaidNodeForNode(unittest.TestCase):
    def test_every_mermaid_node_is_drawn(self) -> None:
        missing = sorted(mermaid_nodes() - svg_nodes())
        self.assertEqual(missing, [], f"not in docs/architecture.svg: {missing}")

    def test_the_svg_invents_nothing(self) -> None:
        extra = sorted(svg_nodes() - mermaid_nodes())
        self.assertEqual(extra, [], f"not in the mermaid block: {extra}")

    def test_the_svg_is_well_formed_xml_and_is_an_svg(self) -> None:
        import xml.etree.ElementTree as ET

        root = ET.parse(SVG).getroot()
        self.assertTrue(root.tag.endswith("svg"))
        self.assertTrue(root.get("viewBox"))


@HAS_DOCS
class TheDiagramNamesWhatTheSubmissionClaims(unittest.TestCase):
    """The prose check the rev 17 queue asks for, run as a test."""

    def setUp(self) -> None:
        self.mermaid = mermaid_block()
        self.svg = SVG.read_text(encoding="utf-8")

    def test_all_seven_agents_are_named_in_both(self) -> None:
        """The headline says seven, so the picture has to draw seven.

        Until rev 33 this asserted three, and the picture drew three, while
        `docs/ARCHITECTURE.md` and DEVPOST both said seven: the diagram a judge
        looks at first contradicted the sentence he read first. The four newer
        ones are listed after the original three, in the order they shipped.
        """
        for agent in ("Registrar", "Care Coordinator", "Concierge",
                      "Resolver", "Evidence Orchestrator", "Closure Auditor",
                      "Case Steward"):
            with self.subTest(agent=agent):
                self.assertIn(agent, self.mermaid)
                self.assertIn(agent, self.svg)

    def test_the_title_counts_the_same_agents_the_picture_draws(self) -> None:
        self.assertIn("Sanad: seven agents, one safety kernel", self.svg)

    def test_the_guard_file_is_named_beside_the_agent_that_it_rules(self) -> None:
        for name in ("core/policy.py", "core/sentinel.py", "core/vitals.py",
                     "core/intents.py", "core/verify.py", "core/resolver.py",
                     "core/evidence.py", "core/auditor.py", "core/steward.py"):
            with self.subTest(name=name):
                self.assertIn(name, self.mermaid)
                self.assertIn(name, self.svg)

    def test_the_kernel_gates_are_drawn_in_the_order_code_runs_them(self) -> None:
        """The order is the guarantee, so the order is what is asserted.

        The Coordinator is numbered 6 and drawn in the chase path, because it
        is woken by a Cloud Task as well as by a reply; its number is checked
        below rather than its position in the patient-path column.
        """
        gates = ["1. Blood-pressure table", "2. Sentinel net 1",
                 "3. Sentinel net 2", "4. Treatment-change gate",
                 "5. Administrative tier", "7. Concierge",
                 "8. Output validator", "9. Reassurance vote"]
        for text in (self.mermaid, self.svg):
            places = [text.find(gate) for gate in gates]
            self.assertNotIn(-1, places, f"a gate is missing: {gates}")
            self.assertEqual(places, sorted(places))

    def test_the_coordinator_carries_its_place_in_that_order(self) -> None:
        for text in (self.mermaid, self.svg):
            self.assertIn("6. Care Coordinator", text)

    def test_the_google_cloud_products_are_all_on_the_picture(self) -> None:
        for product in ("Cloud Run", "Firestore", "Cloud Tasks", "Cloud Storage",
                        "Secret Manager", "Vertex AI"):
            with self.subTest(product=product):
                self.assertIn(product, self.mermaid)
                self.assertIn(product, self.svg)

    def test_the_coordinator_is_consulted_before_the_concierge_generates(self) -> None:
        """The old mermaid had the arrow the other way round.

        In code the Coordinator sits after the change gate and before any
        generation (core/concierge.handle_patient_message), so the drawn order
        is the administrative tier into the Coordinator, and the Coordinator
        standing down into the Concierge.
        """
        self.assertIn("ADMIN -->|a chore, or a reply about an open loop| COORD",
                      self.mermaid)
        self.assertIn("COORD -.stands down, or it was a question.-> CONC",
                      self.mermaid)
        self.assertNotIn("CONC -.reply about an open loop.-> COORD", self.mermaid)


@HAS_DOCS
class TheRotatedEdgeLabelsDoNotStack(unittest.TestCase):
    """rev 18 item 5: six of them overlapped into an unreadable column.

    Three long labels were drawn rotated in the narrow channel between the
    patient-path column and the chase-path column, in lanes twenty pixels
    apart, and their text ran the whole height of the channel, so at 1440 wide
    they read as one grey stack. Two more did the same in the left margin.

    This is geometry, so it is checked as geometry rather than looked at. A
    rotated label occupies a band as wide as its type and as long as its text,
    and two labels in lanes closer than a band apart may not both be over the
    same stretch of the picture. The character width below is an estimate for
    Helvetica at this size and it is deliberately generous: the rail is "these
    cannot be touching", not "these are exactly this wide".
    """

    # Helvetica lowercase averages about half the font size; 0.55 leaves room.
    CHAR = 0.55
    # Ascent, descent and the white halo either side of the stroke.
    BAND = 20.0

    def labels(self) -> list[tuple[float, float, float, str]]:
        """(lane x, first y, last y, text) for every rotated label."""
        found = []
        pattern = re.compile(
            r'<text x="([\d.]+)" y="([\d.]+)" font-size="([\d.]+)"[^>]*'
            r'transform="rotate\(-90 [^)]*\)"[^>]*>([^<]*)</text>')
        for x, y, size, text in pattern.findall(
                SVG.read_text(encoding="utf-8")):
            length = len(text) * float(size) * self.CHAR
            found.append((float(x), float(y) - length / 2,
                          float(y) + length / 2, text))
        return found

    def test_the_six_of_them_are_still_there(self) -> None:
        self.assertEqual(len(self.labels()), 6)

    def test_no_two_of_them_are_over_the_same_stretch_of_the_picture(self
                                                                    ) -> None:
        labels = self.labels()
        for i, (x1, top1, bottom1, text1) in enumerate(labels):
            for x2, top2, bottom2, text2 in labels[i + 1:]:
                if abs(x1 - x2) >= self.BAND:
                    continue
                with self.subTest(one=text1, two=text2):
                    self.assertTrue(
                        bottom1 <= top2 or bottom2 <= top1,
                        f"{text1!r} and {text2!r} are in neighbouring lanes and "
                        f"over the same stretch of the drawing")

    def test_none_of_them_is_long_enough_to_run_the_whole_channel(self) -> None:
        for _, top, bottom, text in self.labels():
            with self.subTest(text=text):
                self.assertLessEqual(bottom - top, 160.0,
                                     f"{text!r} is too long to sit beside its edge")

    def test_each_one_still_names_the_edge_it_is_on(self) -> None:
        """Shortened, not emptied. Every one keeps the word that identifies it."""
        texts = [text for _, _, _, text in self.labels()]
        for word in ("relay line", "adapter", "evidence", "chore",
                     "stands down", "/force_due"):
            with self.subTest(word=word):
                self.assertTrue(any(word in text for text in texts),
                                f"no label names {word}")

    def test_the_mermaid_still_carries_the_long_form(self) -> None:
        """The mermaid lays its own labels out and has no crowding to fix, so
        it keeps the full sentence and the SVG carries the short one."""
        block = mermaid_block()
        self.assertIn("a chore, or a reply about an open loop", block)
        self.assertIn("stands down, or it was a question", block)


@HAS_DOCS
class TheDocumentsCarryNoDashesEither(unittest.TestCase):
    """tests/test_dashboard_routes.py sweeps app/; this sweeps docs/.

    Same rule, and it is a rule because an em dash is the one punctuation mark
    that reads as machine-written to the person whose name is on this.
    """

    def test_no_em_dash_and_no_en_dash_in_any_shipped_document(self) -> None:
        for path in sorted(DOCS.rglob("*")):
            if path.suffix not in (".md", ".svg"):
                continue
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1):
                if "\u2014" in line or "\u2013" in line:
                    self.fail(f"{path.name}:{number} carries an em or en dash: "
                              f"{line.strip()[:90]}")


@HAS_DOCS
class TheRunbookSaysWhatToDoAfterTheTake(unittest.TestCase):
    """rev 17 items 7, 8 and 16, as lines a person can follow at 2 a.m."""

    def setUp(self) -> None:
        self.runbook = (DOCS / "RUNBOOK.md").read_text(encoding="utf-8")

    def test_it_says_to_rotate_the_console_token_before_the_upload(self) -> None:
        self.assertIn("/admin/rotate-token", self.runbook)
        self.assertIn("BEFORE you upload", self.runbook)
        rotate = self.runbook.split("Rotate the console token", 1)[1]
        self.assertIn("bearer credential", rotate)

    def test_it_says_to_set_the_budget_alert_before_submission(self) -> None:
        self.assertIn("Set the budget alert BEFORE submission", self.runbook)
        budget = self.runbook.split("Set the budget alert BEFORE submission", 1)[1]
        self.assertIn("Budgets and alerts", budget)

    def test_the_rotate_route_it_names_actually_exists(self) -> None:
        main = (DOCS.parent / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/admin/rotate-token")', main)
        self.assertIn("store.new_web_token()", main)

    def test_it_says_reset_clears_policy(self) -> None:
        self.assertIn("Reset clears policy", self.runbook)

    def test_it_carries_the_refusal_procedure_word_for_word(self) -> None:
        self.assertIn("Showing a guard refuse, on camera", self.runbook)
        self.assertIn("/force_due Amany glucose strict", self.runbook)
        self.assertIn("refused by code (core/policy.py)", self.runbook)

    def test_the_refusal_it_promises_is_the_one_the_code_produces(self) -> None:
        """The sentence in the runbook is the sentence core/policy.py writes."""
        try:  # core.chaser reaches the cloud SDK at import, as test_chaser.py notes
            from core import chaser, policy
        except ImportError as exc:  # pragma: no cover - the image always has it
            self.skipTest(f"cloud SDK not installed: {exc}")

        facts = policy.LoopFacts(
            now=__import__("datetime").datetime(2026, 8, 29, 9, 0,
                                                tzinfo=__import__("datetime").timezone.utc),
            wake=True, contacts=6,
        )
        refused = policy.check("schedule_next_contact", {"days_from_now": 0},
                               facts, policy.DEFAULT,
                               reason="the ladder step that is due now")
        self.assertFalse(refused.allowed)
        line = f"{chaser.REFUSED_BY_CODE}: {refused.why}"
        self.assertIn(line.split(": ", 1)[1], self.runbook.replace("\n", " "))
        self.assertIn(chaser.REFUSED_BY_CODE, self.runbook)

    def test_it_says_how_to_get_the_console_url_back(self) -> None:
        """rev 18 item 6. A rotation whose answer scrolled away is not a lost
        board: seeding an existing doctor returns his current console URL."""
        self.assertIn("If you lose the console URL after rotating", self.runbook)
        recovery = self.runbook.split(
            "If you lose the console URL after rotating", 1)[1]
        self.assertIn('-H "X-Sanad-Admin: $S" "$U/admin/seed"', recovery)
        self.assertIn("name=Test%20Doctor", recovery)
        self.assertIn('"created": false', recovery)

    def test_the_seed_route_really_does_hand_the_current_url_back(self) -> None:
        main = (DOCS.parent / "app" / "main.py").read_text(encoding="utf-8")
        seed = main.split('@app.post("/admin/seed")', 1)[1].split(
            '@app.post("/admin/reset")', 1)[0]
        self.assertIn('"console_url"', seed)
        self.assertIn("doctor.web_token", seed)
        self.assertIn('"created": created', seed)

    def test_it_says_what_a_pinned_restart_costs_the_next_deploy(self) -> None:
        """rev 18 item 1. The restart trick pins traffic to a named revision and
        the pin survives every later deploy, which is what made rev 17's deploy
        a no-op with a success message."""
        self.assertIn("If you restarted the service by pinning traffic",
                      self.runbook)
        restart = self.runbook.split(
            "If you restarted the service by pinning traffic", 1)[1]
        self.assertIn("--to-revisions", restart)
        self.assertIn("Traffic is pinned after this", restart)
        self.assertIn("update-traffic --to-latest", restart)


class TheDeployScriptProvesWhatItDeployed(unittest.TestCase):
    """rev 18 item 1, and it is not in the @HAS_DOCS group on purpose.

    `app/deploy.sh` is inside the image's build context, so this rail runs in
    the Docker build as well as here. It is the one that matters most: on rev 17
    the script exited 0, printed "is serving 100 percent of traffic", and served
    the previous revision, because the service's traffic was pinned to a
    revision name and the new one was retired for having no allocation. A deploy
    that cannot prove what it deployed is worse than a deploy that fails.
    """

    def setUp(self) -> None:
        self.deploy = (Path(__file__).resolve().parents[1]
                       / "deploy.sh").read_text(encoding="utf-8")

    def test_it_sends_the_traffic_to_what_it_just_built(self) -> None:
        self.assertIn("update-traffic", self.deploy)
        self.assertIn("--to-latest", self.deploy)

    def test_that_step_runs_after_the_deploy_and_before_the_check(self) -> None:
        deploy_at = self.deploy.index("gcloud run deploy")
        traffic_at = self.deploy.index("update-traffic")
        health_at = self.deploy.index("/health")
        self.assertLess(deploy_at, traffic_at)
        self.assertLess(traffic_at, health_at)

    def test_it_reads_the_serving_revision_out_of_health(self) -> None:
        self.assertIn("latestCreatedRevisionName", self.deploy)
        self.assertIn('"${URL}/health"', self.deploy)
        self.assertIn('"revision"', self.deploy)

    def test_it_fails_loudly_when_the_two_do_not_match(self) -> None:
        tail = self.deploy.split("SERVING=", 1)[1]
        self.assertIn('if [ "$SERVING" != "$BUILT" ]; then', tail)
        self.assertIn("DEPLOY FAILED THE SERVING CHECK.", tail)
        self.assertIn("exit 1", tail)
        # Both names are printed, because "it did not match" is not a report.
        self.assertIn("built:", tail)
        self.assertIn("serving:", tail)

    def test_it_names_the_pin_that_caused_it(self) -> None:
        self.assertIn("latestRevision: true", self.deploy)
        self.assertIn("yaml(spec.traffic)", self.deploy)

    def test_it_still_prints_the_service_url_last(self) -> None:
        """The runbook's `U=` line is copied off the end of this script."""
        self.assertTrue(self.deploy.rstrip().endswith('echo "$URL"'))


@HAS_DOCS
class TheThreeAgentsClaimCarriesItsCaveat(unittest.TestCase):
    """rev 17 item 5: one output type, two code paths, and the docs say so.

    `registrar.propose` is an ADK agent turn. `registrar.propose_from_image`
    calls the same model with the same schema through google.genai directly,
    because an ADK agent carrying an output schema takes a text turn. Both
    produce a `ProposedRecord` and both go through the same code validation.
    If that ever stops being true in either direction, this fails.
    """

    def setUp(self) -> None:
        app = DOCS.parent / "app"
        self.registrar = (app / "core" / "registrar.py").read_text(encoding="utf-8")
        self.architecture = ARCHITECTURE.read_text(encoding="utf-8")
        self.assertIsNotNone(
            README, "README.md is at neither the repository root nor docs/")
        self.readme = README.read_text(encoding="utf-8")

    def test_the_non_adk_path_still_exists(self) -> None:
        """The caveat is only honest while the thing it warns about is there."""
        image = self.registrar.split("async def propose_from_image(", 1)[1]
        self.assertIn("media.client.aio.models.generate_content", image)
        self.assertIn("response_schema=ProposedRecord", image)

    def test_the_adk_path_is_the_one_a_dictation_takes(self) -> None:
        text_turn = self.registrar.split("async def propose(", 1)[1].split(
            "async def propose_from_image(", 1)[0]
        self.assertIn("Agent(", text_turn)
        self.assertIn("output_schema=ProposedRecord", text_turn)

    def test_the_code_itself_says_it_is_not_an_adk_turn(self) -> None:
        image = self.registrar.split("async def propose_from_image(", 1)[1]
        self.assertIn("NOT an ADK turn", image)

    def test_the_architecture_document_states_the_caveat(self) -> None:
        self.assertIn("three agents", self.architecture)
        caveat = self.architecture.split("One caveat", 1)[1][:1400]
        self.assertIn("propose_from_image", caveat)
        self.assertIn("google.genai", caveat)
        self.assertIn("ProposedRecord", caveat)

    def test_the_readme_does_not_claim_more_than_that(self) -> None:
        self.assertIn("One caveat", self.readme)
        self.assertIn("google.genai", self.readme)

    def test_the_model_call_table_separates_the_two(self) -> None:
        table = self.architecture.split("## The honest model-call count", 1)[1]
        self.assertIn("A doctor dictation, typed", table)
        self.assertIn("A photographed prescription", table)
        self.assertIn("as an ADK agent turn", table)


if __name__ == "__main__":
    unittest.main()
