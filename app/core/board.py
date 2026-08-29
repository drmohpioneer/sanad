"""Owns the four numbers at the top of the doctor's console.

"3 need you, 17 handled" is the product claim in six words - the doctor is the
exception handler, not the project manager - so it is computed here, in code,
from the board itself. It is never typed into the console and never generated.

One table maps a loop's state to its colour, and both the emoji on a card and
the count in the header read from it, so the two can never disagree.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

from typing import Any, Iterable

COLOUR_FOR: dict[str, str] = {
    "pending_review": "red",      # a result is in and only the doctor can close it
    "received": "red",
    "unreachable": "white",       # Sanad stopped chasing; it is his call now
    "open": "yellow",             # scheduled, Sanad is carrying it
    "waiting_patient": "yellow",  # chased, waiting on the patient
    "done": "green",
}
# The two colours that mean a human has to do something. Everything else is a
# loop Sanad is still carrying on its own.
NEEDS_DOCTOR: tuple[str, ...] = ("red", "white")
UNKNOWN_COLOUR = "yellow"


def colour(state: str) -> str:
    """A loop state -> its colour. An unknown state counts as in flight."""
    return COLOUR_FOR.get(state, UNKNOWN_COLOUR)


def tally(states: Iterable[str]) -> dict[str, Any]:
    """Every loop state on the board -> the four counts and the header line."""
    counts = {"red": 0, "yellow": 0, "green": 0, "white": 0}
    total = 0
    for state in states:
        counts[colour(state)] += 1
        total += 1
    need_you = sum(counts[c] for c in NEEDS_DOCTOR)
    return {
        **counts,
        "total": total,
        "need_you": need_you,
        "handled": total - need_you,
        "line": f"{need_you} need you, {total - need_you} handled",
    }
