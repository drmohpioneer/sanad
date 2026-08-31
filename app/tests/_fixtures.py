"""Where the suite's fixtures live, and one answer to "are they here?".

The image the Cloud Run service is built from is not the repository. Two rules
in app/.gcloudignore decide that, and both of them are deliberate:

  `*.json`   keeps every service-account key and every credential file out of
             the image, which is worth far more than the Gate 0B goldens it
             also strips. The JSON goldens (tests/gate0b/goldens/manifest.json,
             goldens/beats/*.json, goldens/traces/*.json) are collateral.
  the build
  context    is `app/`, so anything at the repository root is simply not there:
             `docs/` and its seeded lab slips, and the README.

`test-assets/` is excluded by name, on purpose, and has been since it was
added: the acceptance-run lab slips live in the repository and not in a
container.

None of that makes a test wrong. It makes a fixture absent, which is a
different thing, and the only honest thing a test can do about a fixture that
is not there is say so and stand down. So every test that reads one of those
four families is decorated with the matching flag below, the reason names the
family rather than the file, and the test runs at full strength everywhere the
repository is complete: on Mohamed's laptop, in CI over a checkout, and in the
gate that has to pass before a deploy is allowed at all.

The flags are `unittest.skipUnless` decorators, ready to apply to a class or a
method. They are computed once at import: nothing in a container creates
`docs/` halfway through a run.

The rule for adding one: a skip guard is for a fixture the BUILD strips, never
for one a test could have made itself, and never for anything about the code
under test. If a guard would ever hide a real failure, it is the wrong guard.

Three guards predate this file and are deliberately left where they are:
tests/test_architecture_diagram.HAS_DOCS, tests/test_background.HAS_RUNBOOK and
tests/test_intents.HAS_RUNBOOK. Each of them stats the exact document its own
rail reads (ARCHITECTURE.md and architecture.svg; RUNBOOK.md), which is a
tighter question than "is docs/ here", and rewriting them onto the looser flags
below would trade a real check for a tidier import.
"""

from __future__ import annotations

import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
APP_ROOT = TESTS.parent
REPO_ROOT = APP_ROOT.parent

# 1. The repository root, which is outside the build context entirely.
DOCS = REPO_ROOT / "docs"
RUNBOOK = DOCS / "RUNBOOK.md"
SEED = DOCS / "seed"
README = REPO_ROOT / "README.md"

# 2. The Gate 0B goldens. The screenshots are PNGs and survive; the record
#    itself is JSON and does not, so presence is asked about the JSON.
GOLDENS = TESTS / "gate0b" / "goldens"
MANIFEST = GOLDENS / "manifest.json"

# 3. The acceptance-run lab slips.
TEST_ASSETS = APP_ROOT / "test-assets"

OUTSIDE_IMAGE = "docs/ is outside the image"
NO_RUNBOOK = "docs/RUNBOOK.md is outside the image"
NO_SEED = ("the seeded lab slips live in docs/, outside the image build "
           "context; the golden journey runs where the repository is complete")
NO_GOLDENS = ("the Gate 0B JSON goldens are stripped from the image by the "
              "*.json rule that keeps credentials out; the golden rail runs "
              "where the repository is complete")
NO_TEST_ASSETS = "test-assets/ is excluded from the image on purpose"


def _has(*paths: Path) -> bool:
    return all(path.exists() for path in paths)


HAS_DOCS = unittest.skipUnless(_has(DOCS), OUTSIDE_IMAGE)
HAS_RUNBOOK = unittest.skipUnless(_has(RUNBOOK), NO_RUNBOOK)
HAS_README = unittest.skipUnless(_has(README), "README.md is outside the image")
HAS_SEED = unittest.skipUnless(_has(SEED), NO_SEED)
HAS_TEST_ASSETS = unittest.skipUnless(_has(TEST_ASSETS), NO_TEST_ASSETS)

# The manifest is the index every other JSON golden is hashed against, so its
# absence is the whole family's absence and one stat answers for all of them.
HAS_JSON_GOLDENS = unittest.skipUnless(_has(MANIFEST), NO_GOLDENS)

# The full Gate 0B replay needs both: the seeded slip it photographs and the
# goldens it is compared against.
HAS_GOLDEN_JOURNEY = unittest.skipUnless(
    _has(MANIFEST, SEED), f"{NO_GOLDENS}; {NO_SEED}")
