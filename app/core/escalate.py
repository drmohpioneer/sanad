"""Owns one promise: the patient hears "your doctor knows" only after he does.

"Your doctor has just been alerted" is the strongest sentence Sanad ever says to
a patient. It tells him to stop waiting here and go, and it is the reason he
stops typing. Until now it was said FIRST on every escalating path: the
reassurance went out, and only then were the escalation event, the relay and the
doctor's card written (reviews/codex-troubleshoot-1.md item 10). A Firestore
timeout, a cold instance or a crash in between left the worst state this system
can be in: a patient who has been told to stop waiting, and a doctor who was
never told anything at all.

So the order is inverted on every path, and it is inverted in ONE function, so
that no branch can be given the promise without the fallback that goes with it.
`told_or_fail_closed` runs the persistence first and answers whether it landed.
A caller that gets True says the reassuring sentence it always said. A caller
that gets False says the fail-closed line below instead: a sentence that never
claims the doctor knows, tells the patient what to do himself, and is followed
by an error event so the failure is on the board rather than only in a log.

Two fail-closed lines, because the two situations are not the same:

  EMERGENCY   the finding still stands. The patient is still sent to the
              nearest emergency room, because that instruction never depended
              on the doctor hearing anything; only the sentence about the
              doctor is withdrawn.
  RELAY       nothing about the patient is urgent by our own reading. He is
              told plainly that the message did not get through and asked to
              send it again.

Both are fixed code strings in the patient's language and grammatical gender,
like every other escalation block in this codebase. No model writes them and
none of them is in core/templates.py, because that table is the Coordinator's
and is frozen by a test that names its whole key set.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from . import events

log = logging.getLogger("sanad.escalate")

EMERGENCY_AR: dict[str, str] = {
    "m": "🚨 الكلام ده ممكن يكون خطر.\n"
         "روح أقرب مستشفى أو قسم طوارئ حالاً، أو اتصل بالإسعاف 123.\n"
         "سند مش قادر يوصل لدكتورك دلوقتي، فكلمه بنفسك كمان.",
    "f": "🚨 الكلام ده ممكن يكون خطر.\n"
         "روحي أقرب مستشفى أو قسم طوارئ حالاً، أو اتصلي بالإسعاف 123.\n"
         "سند مش قادر يوصل لدكتورك دلوقتي، فكلميه بنفسك كمان.",
    "u": "🚨 الكلام ده ممكن يكون خطر.\n"
         "المطلوب دلوقتي أقرب مستشفى أو قسم طوارئ حالاً، أو الاتصال بالإسعاف 123.\n"
         "سند مش قادر يوصل للدكتور دلوقتي، فلازم التواصل معاه بشكل مباشر كمان.",
}

EMERGENCY_EN = (
    "🚨 This could be an emergency.\n"
    "Go to the nearest emergency room now, or call 123 (ambulance).\n"
    "Sanad could not reach your doctor just now, so contact him yourself as well."
)

RELAY_AR: dict[str, str] = {
    "m": "سند مش قادر يوصل لدكتورك دلوقتي. ابعت رسالتك تاني بعد شوية، "
         "ولو الموضوع مستعجل كلم دكتورك بنفسك.",
    "f": "سند مش قادر يوصل لدكتورك دلوقتي. ابعتي رسالتك تاني بعد شوية، "
         "ولو الموضوع مستعجل كلمي دكتورك بنفسك.",
    "u": "سند مش قادر يوصل للدكتور دلوقتي. المطلوب إعادة إرسال الرسالة بعد "
         "شوية، ولو الموضوع مستعجل التواصل مع الدكتور بشكل مباشر.",
}

RELAY_EN = (
    "Sanad could not reach your doctor just now. Please send this again in a "
    "few minutes, and contact your doctor yourself if it cannot wait."
)

# What the audit line on the fail-closed message says it is.
FAIL_CLOSED = "escalation could not be persisted"


def fail_closed_text(speak: str, who: str = "u", *, emergency: bool) -> str:
    """The line a patient gets when the doctor could not be told."""
    if emergency:
        return EMERGENCY_EN if speak != "ar" else EMERGENCY_AR.get(
            who, EMERGENCY_AR["u"])
    return RELAY_EN if speak != "ar" else RELAY_AR.get(who, RELAY_AR["u"])


async def told_or_fail_closed(
    persist: Callable[[], Awaitable[Any]],
    *,
    doctor_id: str,
    patient_id: Optional[str] = None,
    what: str = "escalation",
    loop_id: Optional[str] = None,
    channel: str = "web",
    synthetic: bool = True,
) -> bool:
    """Run the persistence. True means the patient may now be told.

    `persist` writes the escalation event, opens the relay where the path has
    one, and puts the card in front of the doctor. It is passed in rather than
    described, because what has to be durable differs by path and the ordering
    guarantee does not.

    A failure is caught here and never raised at the caller, so an escalation
    that could not be written is still an answered turn and never a 500 on the
    patient's page (codex item 11). The error event is best effort by
    definition: if the event log is the thing that is down, the log line in
    Cloud Logging is all there is, and that is said out loud rather than
    pretended away.
    """
    try:
        await persist()
        return True
    except Exception:
        log.exception("could not persist %s for doctor=%s patient=%s",
                      what, doctor_id, patient_id)
        try:
            await events.append_event(
                doctor_id, "escalation",
                f"FAILED to record {what}: the patient was told Sanad could not "
                "reach the doctor",
                patient_id=patient_id, loop_id=loop_id, channel=channel,
                meta={"error": FAIL_CLOSED, "what": what,
                      "decided_by": "code (core/escalate.py fail closed)"},
                synthetic=synthetic,
            )
        except Exception:
            log.exception("could not write the error event either")
        return False
