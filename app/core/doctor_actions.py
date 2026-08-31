"""Owns one thing: what happens when the doctor presses a button, anywhere.

S24-C. Until now there were two corridors into the same records. The web
console went through `main._legacy_action`: claim the card, take the action
key, do the work, retire the card. Telegram callbacks went through
`core/tg_router._callback`, which took the action key for five verbs, called
the domain functions itself, and never touched `cards.claim` or
`cards.resolve` at all. The same tap therefore meant two different things
depending on which door it came through, and the records drifted: a relay
answered from the phone left its card open for ever, because nothing on that
path ever resolved one.

There is one ritual now and it lives here, so both doors run it letter for
letter:

    claim the card -> take the action key -> do the work -> retire the card

The release rules are the ones the web route already had, and they are the
reason the order is what it is:

- The claim is IN FRONT of the work. A second press while the first is still
  running is answered instead of carried out.
- Work that fails gives back both the card claim and the action key, so a
  doctor can press again after a real failure.
- Bookkeeping that fails after the work SUCCEEDED gives back nothing. The
  claim stands, the next press is answered "already done", and the work is
  never repeated. He sees a card that is still open, which is the honest
  picture of a card whose flag could not be written.

This module is domain code, not an edge: it never speaks Telegram, never
raises an HTTPException, and never knows which surface called it. The verb it
cannot name is a `UnknownAction`, and the route that has an HTTP status to
give turns that into one.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import cards, concierge, extractor, registrar, store
from .models import Doctor

# The body a refused press gets back. Every surface reads the same three keys,
# and the golden replay reads this dict byte for byte, so it is built in one
# place rather than typed at each return.
ALREADY_DETAIL = "already done"


class UnknownAction(ValueError):
    """A verb nothing here carries out. The HTTP route answers it with a 400."""


def already(action_id: str) -> dict[str, Any]:
    """The unchanged legacy body for a press that was refused."""
    return {"ok": False, "already": True, "action_id": action_id,
            "detail": ALREADY_DETAIL}


async def carry_out(
    doctor: Doctor, action_id: str, text: str = "", *, base_url: str = ""
) -> dict[str, Any]:
    """One claimed action, carried out. The caller retires the card after it."""
    verb, _, ident = action_id.partition(":")

    if verb == "confirm":
        await registrar.commit(doctor, ident, base_url)
    elif verb == "cancel":
        await registrar.cancel(doctor, ident)
    elif verb == "reply":
        await concierge.doctor_reply(doctor, ident, text)
    elif verb == "reviewed":
        await concierge.mark_reviewed(doctor, ident)
    elif verb == "note":
        await concierge.note_to_patient(doctor, ident, text)
    elif verb == "attach":
        await extractor.attach_results(doctor, ident)
    elif verb == "openloop":
        await extractor.open_loop_for(doctor, ident)
    elif verb == "existing":
        # "existing:<patient id>:<proposal id>". The proposal id is last so the
        # verb and the patient still read left to right, and so the split is the
        # same one every other action id uses.
        patient_id, _, confirm_id = ident.partition(":")
        await registrar.choose_existing(doctor, patient_id, confirm_id)
    elif verb == "newpatient":
        await registrar.choose_new(doctor, ident)
    elif verb == "openpatient":
        # A row on a lookup list. There is nothing to do on the server: the
        # list created nothing and this button only opens a record that already
        # exists. The dashboard opens the patient panel; pressing it here
        # retires the list, which is what a list that has been used is.
        pass
    elif verb == cards.SEEN:
        # A red emergency card has nothing to do to it but read it, so "Seen"
        # does no work of its own. It exists so that acknowledging one is a
        # fact on the server rather than a tab that is still open.
        pass
    else:
        raise UnknownAction(verb)

    return {"ok": True, "resolved": []}


async def perform(
    doctor: Doctor,
    action_id: str,
    text: str = "",
    *,
    base_url: str = "",
    on_released: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """The whole ritual, once, for one press. Returns the legacy route body.

    `on_released` is called when the work threw AND both the card claim and
    the action key went back, which is the one case where pressing again is
    safe. The Telegram edge uses it to decide whether a failed callback may be
    retried by the provider; the web route does not need it, because a raised
    exception is already the answer there.
    """
    # codex item 17. The claim is in front of the domain work, not behind it: a
    # second press while the first is still running is answered instead of
    # carried out. A press that fails gives the card back below, so a real
    # failure is still something the doctor can retry.
    may, claimed = await cards.claim(doctor.id, action_id)
    if not may:
        return already(action_id)

    # codex re-audit 17, the second half of it. The card claim is a fact on the
    # CARD, and it is handed back when the work behind the button fails, which
    # is what lets a doctor press again after a real failure. It could not
    # answer the other case: the work SUCCEEDED and the write that retires the
    # card threw. The claim went back, the doctor pressed again, and Confirm
    # made a second patient.
    #
    # So the domain work carries its own key, and the key is the action id he
    # pressed. It is written before the work and released only when the work
    # itself throws, never when the bookkeeping after it does. A verb no card
    # carries (a purged card, a script) is covered by this and by nothing else,
    # because `cards.claim` has nothing to claim for one.
    retiring = cards.retires(action_id)
    if retiring:
        if not await store.claim_action(doctor.id, action_id):
            await cards.release(claimed)
            return already(action_id)

    try:
        answer = await carry_out(doctor, action_id, text, base_url=base_url)
    except Exception:
        await cards.release(claimed)
        if retiring:
            # If THIS throws it propagates as itself. The key is still held, so
            # the press is not retryable and must not be labelled as one.
            await store.release_action(doctor.id, action_id)
            if on_released is not None:
                on_released()
        raise

    # The work is done; now the card behind the button is finished. This is
    # OUTSIDE the block above on purpose. Retiring the card is bookkeeping about
    # a thing that has already happened, so a failure here must not give the
    # action back: the claim stands, the doctor's next press is answered
    # "already done", and the work is not repeated. He sees a card that is still
    # open, which is the honest picture of a card whose flag could not be
    # written.
    answer["resolved"] = await cards.resolve(doctor.id, action_id)
    return answer


# --------------------------------------------------------------------------- #
# The two-step verbs: "Answer" and "Send a note" on a phone
# --------------------------------------------------------------------------- #
# Tapping either of those on Telegram cannot carry the text with it, so the tap
# only opens a window and the doctor's next message is the payload. The window
# is one write on the doctor record and it belongs here rather than in the
# router, because what a button means is domain knowledge and the router is an
# edge. `core/dispatch.py` closes the window by calling `perform` with the same
# action id the card carried, which is what finally retires the card.
async def open_answer_window(
    doctor: Doctor, action_id: str, *, channel: str
) -> None:
    """Consume the doctor's next message as the answer to this card."""
    verb, _, ident = action_id.partition(":")
    if verb not in ("reply", "note"):
        raise UnknownAction(verb)
    await store.update_doctor(
        doctor.id,
        awaiting_relay_id=ident if verb == "reply" else None,
        awaiting_note_loop_id=ident if verb == "note" else None,
        awaiting_since=store.now(),
        awaiting_channel=channel,
    )
