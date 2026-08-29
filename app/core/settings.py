"""Owns the two demo knobs: which run this is, and how long a "day" lasts.

Both have an environment default set at deploy time and both can be changed at
run time through POST /admin/settings, because a rehearsal must not need a
redeploy between takes.

  run_id      every scheduled task carries the run id it was created under. A
              handler drops a task whose run id is not the current one, so a
              purged rehearsal can never fire into the next one.
  time_scale  how many real seconds make one Sanad day. 86400 is real time; a
              few seconds compresses the whole three-nudge ladder into the length
              of a demo, with no other change to the logic (see core/timing.py).

Read per request, never cached: the process holds no state, so a knob turned on
one instance is seen by all of them.
"""

from __future__ import annotations

import logging
import os

from . import store
from .timing import REAL_DAY_SECONDS

ENV_RUN_ID = os.environ.get("DEMO_RUN_ID", "dev").strip() or "dev"
ENV_TIME_SCALE = int(os.environ.get("TIME_SCALE", REAL_DAY_SECONDS) or REAL_DAY_SECONDS)

log = logging.getLogger("sanad.settings")


async def current() -> tuple[str, int]:
    """(run id, time scale) - Firestore first, the deployed defaults behind it.

    "Behind it" now includes a read that throws. These two knobs are read on
    the doctor's patient panel and inside the completion report, and a
    Firestore hiccup on this one document used to be a 500 on a page that has
    nothing to do with the knobs (codex item 11). The deployed defaults are the
    same values `--set-env-vars` put there at deploy time, so falling back to
    them is falling back to the configured answer, not to a guess.
    """
    try:
        saved = await store.get_settings()
    except Exception:
        log.warning("settings read failed, using the deployed defaults",
                    exc_info=True)
        saved = {}
    run_id = str(saved.get("run_id") or ENV_RUN_ID)
    try:
        scale = int(saved.get("time_scale") or ENV_TIME_SCALE)
    except (TypeError, ValueError):
        scale = ENV_TIME_SCALE
    return run_id, max(1, scale)


async def run_id() -> str:
    return (await current())[0]


async def time_scale() -> int:
    return (await current())[1]
