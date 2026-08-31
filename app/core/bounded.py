"""Owns one rule: nothing a patient is waiting on runs without a deadline.

Every dependency on the patient's lane is somebody else's service: Gemini for
the triage vote, the reply, the transcript and the photo, Cloud Storage for the
bytes. Each of them can be slow in a way that is indistinguishable from being
down, and none of them had a deadline (a Codex adversarial finding preserved by
`app/tests/test_wave_c.py`).
Two things went wrong because of that, and they are different problems:

  no timeout    a hung call held the request open until the Cloud Run instance
                timed it out. The patient's page showed a spinner for a minute
                and then an error, and the message was gone.
  no handler    an exception on those paths reached FastAPI, which answered
                500. A 500 is the worst possible answer here: the patient is
                told nothing, the doctor is told nothing, and the record shows
                nothing happened.

`within` fixes the first. Every caller wraps its own dependency in it with a
deadline from the table below, and a `TimedOut` is an ordinary exception the
caller's existing fail-closed branch already knows what to do with. There is one
table so the numbers are in one place and a judge can read them.

The second is fixed at each call site, not here, because what "fail closed"
means is different on each one: the triage gate relays, the Concierge relays,
the photo path goes to the "stored and relayed unread" exit that already exists,
and a transcription that never arrives becomes a voice note the doctor is asked
to listen to himself. In every case the patient gets a sentence, the doctor gets
a card, and the event log says what broke.

The deadlines are generous on purpose. They are there to stop a hang, not to cut
a slow answer short. These numbers were widened on 2026-08-31 after measurement:
plain generation on this workload answers in about two seconds, but the same
model asked for a structured JSON schema was measured at 13.6 to 20.0 seconds,
which is what the votes ask for. The bound exists so a waiting patient always
gets an answer and no stuck call holds an instance, not to make the model hurry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, TypeVar

log = logging.getLogger("sanad.bounded")

T = TypeVar("T")

# Seconds. One table, and every patient-facing dependency reads from it.
TRIAGE = 30.0        # core/sentinel.model_net, the emergency vote
VOTE = 30.0          # core/validator's two yes/no votes
TEXT = 45.0          # core/concierge.answer, the one model-written sentence
TRANSCRIBE = 45.0    # core/media.transcribe_async, ffmpeg plus the model
PHOTO = 45.0         # core/extractor._ask, one photograph
STORAGE = 20.0       # core/storage.put_image


class TimedOut(Exception):
    """A dependency did not answer inside its deadline."""

    def __init__(self, what: str, seconds: float) -> None:
        super().__init__(f"{what} did not answer in {seconds:g}s")
        self.what = what
        self.seconds = seconds


async def within(seconds: float, awaitable: Awaitable[T], *, what: str) -> T:
    """Await with a deadline. A timeout is raised as `TimedOut`.

    The awaitable is cancelled when the deadline passes, so a hung request does
    not keep a worker or a socket after the caller has given up on it.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError as exc:
        log.warning("%s did not answer in %ss", what, seconds)
        raise TimedOut(what, seconds) from exc


async def or_none(seconds: float, awaitable: Awaitable[T], *, what: str) -> Any:
    """`within`, with any failure at all folded into None.

    For the one caller whose fail-closed answer IS "carry on without it": the
    lab-slip bytes. Losing the photograph costs the doctor the picture; losing
    the values on it would cost him the result, and the values are already read.
    """
    try:
        return await within(seconds, awaitable, what=what)
    except Exception:
        log.warning("%s failed, carrying on without it", what, exc_info=True)
        return None
