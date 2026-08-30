# Gate 0B legacy characterization

This test-only package freezes the current nine-beat Sanad journey before the
S23 reshape. It is a characterization baseline, not a new implementation.

The clean public source is pinned at
`17520ab3ff6b4b2a978f9437c2f3dd417a8770a1`; the accepted hermetic baseline is
`f9743a2c72e0dddb012ddbac3cbbbc413b740a3d`; and the deployment observed at the
Gate 0 freeze was `sanad-00029-g9f`. The last independently recorded live
nine-beat run used `sanad-00028-zjm`. The active private tree was preserved
outside this clean repository in `sanad-s23-freeze-2026-08-30` as a manifest,
full-index binary patch, and complete subtree archive. Their exact SHA-256
values are pinned independently in the manifest and acceptance test.

## Run it

From the repository root:

```sh
SANAD_TEST_MODE=1 PYTHONPATH=app .venv/bin/python -m unittest \
  tests.test_gate0b_characterization
```

Regenerate only the canonical JSON after an explicitly reviewed baseline
change:

```sh
SANAD_TEST_MODE=1 PYTHONPATH=app .venv/bin/python -m tests.gate0b.scenario
```

Regeneration is not an acceptance step by itself. Review the JSON diff, rerun
the tests, and independently audit any changed expectation.

Serve the real, unchanged dashboard against those JSON fixtures:

```sh
PYTHONPATH=app .venv/bin/python -m tests.gate0b.replay serve --port 8765
```

Each beat is available at
`http://127.0.0.1:8765/c/gate0b--<beat-name>/app`; for example,
`.../c/gate0b--beat-06-critical-potassium/app`. The replay selects the useful
board, inbox, or patient record itself. Inspect only after the document has
`data-gate0b-ready="true"`; that marker is withheld if an API/QR request fails,
the selected evidence is missing, or the same browser session has not completed
its readiness callback. `?mock=1` is not part of this baseline.

The `serve` command is useful for read-only inspection, but an ad hoc browser
screenshot is not accepted as evidence. Inspection is non-persistent by
default, so it cannot overwrite the committed receipt ledger; pass an explicit
`--receipt-file` only for a disposable diagnostic ledger. The authoritative
capture uses one fresh-profile headless Chrome process, an isolated browser
context per image,
and CDP device emulation:

```sh
PYTHONPATH=app .venv/bin/python -m tests.gate0b.replay capture
```

It captures all 18 files into a temporary staging directory and promotes
nothing until every route, decoded image, CSS and CDP viewport metric, visible
evidence target, observed DOM view, patient identity/heading, PNG dimension,
and marker check passes. The exact evidence node and every ancestor through the
document must remain rendered; exactly one expected `#view-*` section must be
visible, and the dashboard state plus active navigation must agree. One random
256-bit server nonce derives a unique 128-bit black/white 16×8 marker for each
capture-ID and beat. After CDP returns the screenshot, the server seals its
receipt with that PNG's SHA-256 and the actual Chrome/CDP process identity. The
receipt and provenance independently verify all three bindings, so a stale or
same-marker replacement image cannot be re-provenanced.

The order is JSON regeneration, reviewed JSON diff, authoritative screenshot
capture, automatic provenance/manifest finalization, tests, then an independent
visual and code audit. Re-running JSON regeneration after screenshots requires
re-running capture because the JSON generator intentionally rebuilds the
manifest from source artifacts.

## Boundary

The runner drives the real FastAPI application through `httpx.ASGITransport`.
The real routes, policy guards, validation, scheduling, evidence handling,
cards, summaries, fanout, and WebAdapter execute unchanged. The following
test-only seams make the run finite and hermetic:

- `MemoryStore` replaces Firestore with deterministic, copy-on-read/write state.
- `ScriptedBoundaries` supplies a strict, exhaustible model transcript.
- `VirtualTaskQueue` records work and invokes callbacks through the real
  `POST /tasks/nudge` route.
- Provider adapters disable Telegram and external storage; real PNG decoding
  and evidence logic still execute.

Nothing under `app/tests/gate0b/` is imported by product code.

## Frozen result

The initial seed is `31 carried / 3 completed / 17 progressing / 6 help / 1
unreachable / 1 question / 2 critical / 11 attention / 1
closed_without_evidence / 0 lost / 0 duplicates`.

The private historical S18 observation and this deterministic replay both end
at:

| Measure | Historical | Replay |
| --- | ---: | ---: |
| Carried | 35 | 35 |
| Completed with evidence | 4 | 4 |
| Progressing | 19 | 19 |
| Needed help | 7 | 7 |
| Unreachable | 1 | 1 |
| Questions | 2 | 2 |
| Criticals | 3 | 3 |
| Attention | 13 | 13 |
| Closed without evidence | 1 | 1 |
| Lost | 0 | 0 |
| Duplicates | 0 | 0 |

The route-level replay records exactly 23 scenario-trigger HTTP requests, 25
model-boundary calls, 33 logical outbound intents, 13 enqueued tasks, 12
executed task handlers, and one pending task.

The deterministic clock begins at `2026-08-30T02:47:00Z` (05:47 Cairo), matching
the frozen S18 run window. Beat 3 therefore preserves the historical
`moved out of quiet hours (22:00 to 09:00 Cairo)` policy branch, and the one
remaining task is parked at 09:00 Cairo rather than characterizing a daytime
shortcut with the same final counts.

## Artifacts and screenshots

`goldens/beats/` contains the initial snapshot plus all nine post-beat
snapshots. `goldens/traces/` contains complete HTTP, model, message, delivery,
task, and count ledgers. `goldens/manifest.json` hashes every committed
artifact and records the synthetic boundary and non-claims.

The screenshot contract is nine dashboard states at each of `375x812` and
`1440x1000` (18 PNGs, DPR 1, light theme, reduced motion). Screenshot checks
remain skipped only while neither files nor manifest hashes exist; once capture
starts, the suite requires the complete set and exact pixel dimensions.

Replay pins each fixture's clock in Cairo and locale `en-US`, blocks external
fonts with CSP, permits dashboard reads only from the loopback origin, and uses
platform-local system-font fallbacks. Pixels are not claimed portable across
machines; the capture platform and engine are recorded. The standalone Chrome
command additionally maps all non-loopback DNS to `NOTFOUND`.
`screenshot-provenance.json` records the browser/CDP version, dashboard and
replay hashes, fixture hashes, selected views, font/network/readiness policies,
receipt and 128-bit marker binding, observed DOM and patient anchors, dimensions,
and every PNG hash. The capture pipeline rejects a same-size, same-marker, or
stale PNG set without all 18 clean same-session receipts, exact CDP geometry,
matching browser identity, and receipt-bound PNG bytes.

## Non-claims

This is synthetic, local evidence. It does not establish live-model or OCR
determinism, Firestore or Cloud Tasks durability, Cloud Storage behavior,
Google Cloud latency, Telegram acceptance or delivery, channel switching,
exactly-once provider delivery, clinical safety, or readiness for real
patients. A web event receipt means only that an event was persisted locally;
Telegram outcomes in this run are explicitly `skipped_disabled_unbound`.
