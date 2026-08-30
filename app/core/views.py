"""Owns the read shapes the doctor's dashboard asks for.

Five screens in `design-system/MASTER.md` used to guess at facts the server had
but never returned: which channel a patient is actually on, where his link is,
when anything last happened to him, what the doctor's own settings are. The page
derived each one from whatever it could see (the deployment's Telegram flag, the
last two hundred feed events, the single latest link token) and was wrong at the
edges every time.

Every function here is pure: records in, JSON-ready dicts out. No I/O, no cloud
SDK, no model, so this module and its tests run anywhere. `app/main.py` loads the
records and calls these; nothing here decides what to load.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# A patient's channel is a fact about the patient, never about the deployment.
# "telegram" means this patient's own chat is bound. "web" means he has a link
# and can open the `/p/<link>` page on any phone. "none" means no link has been
# minted for him yet, so there is no way to reach him at all.
TELEGRAM = "telegram"
WEB = "web"
NONE = "none"


# --------------------------------------------------------------------------- #
# The board row
# --------------------------------------------------------------------------- #
def next_due(loops: Iterable[Any]) -> Optional[str]:
    """The earliest deadline still live on this patient, as an ISO string.

    A closed loop has no deadline left to miss, so `done` and `unreachable` are
    both out: one finished and the other is the doctor's call now.
    """
    live = [
        l.due_at for l in loops
        if getattr(l, "due_at", None) and l.state not in ("done", "unreachable")
    ]
    return min(live).isoformat() if live else None


def last_event(events: Iterable[Any], patient_id: str) -> dict[str, Any]:
    """When something last happened to this patient, and what it was.

    Read from the whole record rather than from the feed window. The feed hands
    the page the last two hundred events for the entire board, so a quiet
    patient on a busy board used to fall off the end of it and show no last
    event at all, which reads as "nothing ever happened here".
    """
    newest = None
    for event in events:
        if getattr(event, "patient_id", None) != patient_id:
            continue
        if newest is None or event.ts > newest.ts:
            newest = event
    if newest is None:
        return {"last_event_ms": None, "last_event_kind": None}
    return {
        "last_event_ms": int(newest.ts.timestamp() * 1000),
        "last_event_kind": newest.kind,
    }


# --------------------------------------------------------------------------- #
# The patient's channel and link
# --------------------------------------------------------------------------- #
def channel_of(patient: Any, token: Optional[Any]) -> str:
    """Which channel this patient is really on."""
    channels = getattr(patient, "channels", None) or {}
    if isinstance(channels, dict) and channels.get("telegram_chat_id") is not None:
        return TELEGRAM
    return WEB if token is not None else NONE


def link_for(token: Optional[Any]) -> Optional[dict[str, str]]:
    """The QR path and the page path behind one patient's link token."""
    if token is None:
        return None
    return {"qr": f"/qr/{token.id}.png", "page": f"/p/{token.id}"}


def links_by_patient(tokens: Iterable[Any]) -> dict[str, Any]:
    """Every patient's newest link token, keyed by patient.

    Newest wins: a re-issued link is the one that still opens a chat, and the
    older token for the same patient has usually been burned already.
    """
    newest: dict[str, Any] = {}
    for token in tokens:
        seen = newest.get(token.patient_id)
        if seen is None or token.created_at > seen.created_at:
            newest[token.patient_id] = token
    return newest


def reach(patient: Any, token: Optional[Any]) -> dict[str, Any]:
    """The two fields every patient shape carries about being reachable."""
    return {"channel": channel_of(patient, token), "link": link_for(token)}


# --------------------------------------------------------------------------- #
# Settings, and reports
# --------------------------------------------------------------------------- #
def settings_view(doctor: Any, pol: Any) -> dict[str, Any]:
    """The doctor's own record, read only.

    `telegram_chat_id_present` says the binding exists; the chat id itself is
    never returned, because a console token is not an admin credential and the
    number identifies a real phone.
    """
    bound = getattr(doctor, "telegram_chat_id", None) is not None
    return {
        "name": doctor.name,
        "synthetic": getattr(doctor, "synthetic", True),
        "specialty": getattr(doctor, "specialty", ""),
        "language": getattr(doctor, "lang", ""),
        "telegram_bound": bound,
        "telegram_chat_id_present": bound,
        "policy": {**pol.as_meta(), "followup_reason": pol.followup_reason},
    }


def report_row(report: Any) -> dict[str, Any]:
    return {
        "id": report.id,
        "kind": report.kind,
        "patient_id": report.patient_id,
        "title": report.title,
        "body": report.body,
        "ts_ms": int(report.created_at.timestamp() * 1000),
    }
