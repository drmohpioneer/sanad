"""Owns whether a card still needs the doctor, and who says so.

Until S6 block 2 that answer lived in the browser. A card is an event with a
`meta.card` on it, pressing Confirm or Reviewed produced new events but left the
original untouched, and the dashboard kept a session-local set of action ids it
had already sent. Reload the page and every finished card came back, because the
browser was the only thing that knew.

The flag is on the server now. `resolve()` writes `meta.card.resolved` onto the
card event that carried the button, and `is_open()` is the one rule that decides
what still needs a human. Both the feed and `GET /c/{token}/cards` read it, so
the page and the record cannot disagree.

Two notes on what this deliberately does not do:

- It is the only write in Sanad that touches a stored event, and it adds a field
  rather than changing one. Text, kind, timestamp and media stay write-once, so
  the history remains the append-only log a judge reads. `claim()` below adds
  one more field of the same kind, and for the same reason: which button is
  being carried out right now has to be a fact on the server, or two presses are
  two runs of the work.
- Resolving is not undoing. Nothing here cancels the work the action did; the
  action has already run by the time this is called, and this only records that
  the card behind it is finished.

Every rule here is a pure function, and `core.store` is imported inside the one
coroutine that writes, the way core/coordinator.py imports the SDK inside the
call that needs it. So what decides whether a card is open, and which cards a
button finishes, is testable with nothing installed.
"""

from __future__ import annotations

from typing import Any, Iterable

# The verb the "Seen" button on a red card sends. A red emergency card carries
# no buttons on purpose (there is nothing to do to it but read it), so it is
# resolved by its own event id rather than by an action the card declares.
SEEN = "seen"

# Buttons that do something without finishing the card they sit on.
#
# "Send a note" sits beside "Reviewed" on a lab-values card, and it sends the
# doctor's line to the patient, which is a side message: the card itself is his
# to-do, and the result behind it is still waiting for the review that closes
# the loop. Retiring the card on a note would take a result he has not reviewed
# out of the Inbox, which is exactly the thing the Inbox is for. He can send as
# many notes as he likes and the card stays where it is until he presses
# Reviewed.
#
# S24-C review. "Open the patient" is the other one, and it is here because of
# what a retiring button costs rather than because of what this one does. A
# retiring press takes an action key, and the key is the action id: every other
# action id in Sanad names one occasion (a confirmation, a relay, an event),
# but `openpatient:<patient id>` names a PATIENT and the same row appears on
# every lookup list that patient ever matches. The first press burned that key
# for good, so the same name on next week's list answered "already done" and
# that list could never be finished. A row that opens a record creates nothing,
# repeats nothing and needs no idempotency: it is navigation, and navigation
# does not retire the list it was read from.
SIDE_ACTIONS: tuple[str, ...] = ("note", "openpatient")


def card_of(event: Any) -> dict[str, Any]:
    """The card on an event, or an empty dict. Never raises on a plain event."""
    meta = getattr(event, "meta", None) or {}
    card = meta.get("card") if isinstance(meta, dict) else None
    return card if isinstance(card, dict) else {}


def actions_of(event: Any) -> list[dict[str, Any]]:
    rows = card_of(event).get("actions") or []
    return [a for a in rows if isinstance(a, dict)]


def is_resolved(event: Any) -> bool:
    return bool(card_of(event).get("resolved"))


def is_open(event: Any) -> bool:
    """True when this card still needs the doctor.

    Three rules, and they are the ones the dashboard was already applying in the
    browser: a resolved card is finished; a card with buttons on it is waiting
    for one of them; a red card with no buttons is waiting to be seen. Anything
    else is a card that was only ever a notice, and it is not an obligation.
    """
    card = card_of(event)
    if not card:
        return False
    if card.get("resolved"):
        return False
    if actions_of(event):
        return True
    return card.get("severity") == "red"


def open_cards(events: Iterable[Any]) -> list[Any]:
    """Only the cards that still need the doctor, newest first."""
    return sorted((e for e in events if is_open(e)), key=lambda e: e.ts, reverse=True)


def retires(action_id: str) -> bool:
    """Does pressing this button finish the card it sits on?

    Everything does except the side actions above. This is a property of the
    button and not of the card, so it is asked once, before any card is looked
    at (`plan`).
    """
    verb, _, _ = action_id.partition(":")
    return verb not in SIDE_ACTIONS


def carries(event: Any, action_id: str) -> bool:
    """Is this the card the button belongs to?

    `seen:<event id>` names its event directly. Every other action id is read
    off the card's own action list, which is why pressing either half of a
    decision (Confirm or Cancel, Attach or Open loop) retires the whole card:
    both halves live on one card and the flag is on the card. "Reviewed" and
    "Send a note" also share a card, and they are NOT a pair in that sense:
    only Reviewed finishes it (see SIDE_ACTIONS).
    """
    verb, _, ident = action_id.partition(":")
    if verb == SEEN:
        return bool(card_of(event)) and getattr(event, "id", "") == ident
    return any(str(a.get("id", "")) == action_id for a in actions_of(event))


def mark(card: dict[str, Any], action_id: str, at: Any) -> dict[str, Any]:
    """A card plus the action that finished it -> the resolved card.

    A copy, not a mutation, so a caller holding the old dict still sees what was
    on the wire before the button was pressed.
    """
    out = dict(card)
    out["resolved"] = True
    out["resolved_by"] = action_id
    out["resolved_at"] = at.isoformat()
    out["resolved_at_ms"] = int(at.timestamp() * 1000)
    return out


def plan(events: Iterable[Any], action_id: str, at: Any) -> list[tuple[str, dict]]:
    """Which events this press finishes, and the meta each one is left with.

    Every card carrying the action, not the newest one: `reviewed:<loop id>` can
    sit on more than one card for the same obligation (the values card, and the
    card the "Open a loop" button produced), and one press finishes them all.
    Unique ids (a confirm, a relay) match exactly one card and behave the same.

    A side action finishes nothing, so it returns an empty plan without reading
    a single card.

    Pure, so the decision is tested with nothing installed; `resolve()` is the
    two lines that carry it to Firestore.
    """
    out: list[tuple[str, dict]] = []
    if not retires(action_id):
        return out
    for event in events:
        if not is_open(event) or not carries(event, action_id):
            continue
        meta = dict(event.meta or {})
        meta["card"] = mark(card_of(event), action_id, at)
        out.append((event.id, meta))
    return out


async def claim(doctor_id: str, action_id: str) -> tuple[bool, str]:
    """Take this action before its work runs. (may I proceed, which card).

    codex item 17. The action route did the domain work and wrote the resolved
    flag afterwards, so a second press while the first was still working ran the
    work again: two Confirms are two patients, two Attaches are two sets of
    results on one loop. The flag is written first now, inside a transaction on
    the card event, and a press that cannot take it is answered "already done"
    instead of being carried out.

    Two answers are deliberately "yes". A side action (`SIDE_ACTIONS`) claims
    nothing, because "Send a note" is meant to be pressed as often as the doctor
    likes. And an action id no card carries claims nothing either: the claim is
    a property of the card, so a verb that arrives without one, from a script or
    from a card that has since been purged, behaves exactly as it did before.

    A finished card is stepped over rather than refused on, because one action
    id can live on two cards at two different moments. The S9 identification is
    exactly that: "This is a new patient" sits on the ask card, and pressing a
    name there retires the ask card and produces a confirm card carrying the
    same button. The open card is the one being pressed. When every card
    carrying the action is finished, the work is done and the press is refused.
    """
    from . import store  # imported here so the rules above need nothing

    if not retires(action_id):
        return True, ""
    at = store.now()
    carried = False
    for event in await store.list_events(doctor_id):
        if not card_of(event) or not carries(event, action_id):
            continue
        carried = True
        if is_resolved(event):
            continue
        return await store.claim_card_action(event.id, action_id, at), event.id
    return not carried, ""


async def release(event_id: str) -> None:
    """Give a claimed card back, because the work behind the button failed."""
    if event_id:
        from . import store

        await store.release_card_action(event_id)


async def resolve(doctor_id: str, action_id: str) -> list[str]:
    """Write the resolved flag onto every open card carrying this action.

    Returns the event ids it marked, which is what the action route reports.
    """
    from . import store  # imported here so the rules above need nothing

    marked: list[str] = []
    for event_id, meta in plan(await store.list_events(doctor_id), action_id,
                               store.now()):
        await store.update_event(event_id, meta=meta)
        marked.append(event_id)
    return marked


def row(event: Any) -> dict[str, Any]:
    """One open card as the dashboard reads it."""
    return {
        "id": event.id,
        "synthetic": getattr(event, "synthetic", True),
        "patient_id": event.patient_id,
        "kind": event.kind,
        "text": event.text,
        "media": event.media,
        "meta": event.meta,
        "ts_ms": int(event.ts.timestamp() * 1000),
    }
