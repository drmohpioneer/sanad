"""Owns one question: which language does this person get written to in.

Sanad answers a patient in the language he wrote in, which is a code decision,
not a model one. When Sanad speaks first - a nudge, a lab result, a reminder -
there is no message to read, so the choice falls back to the last thing the
patient ever sent, and then to Arabic, because the clinic is in Cairo.

Three callers share this: the Concierge (replies), the Chaser (nudges) and the
Lab-Extractor (what the patient hears about his own slip).
"""

from __future__ import annotations

from typing import Literal

from . import events
from .models import Patient

Lang = Literal["ar", "en"]

ARABIC_FIRST = "؀"
ARABIC_LAST = "ۿ"


def is_arabic(text: str) -> bool:
    """Any Arabic letter wins. Franco-Arabic reads as English, which is right."""
    return any(ARABIC_FIRST <= ch <= ARABIC_LAST for ch in text or "")


def of(text: str) -> Lang:
    return "ar" if is_arabic(text) else "en"


async def for_patient(patient: Patient, doctor_id: str) -> Lang:
    """The patient's language when Sanad speaks first. Defaults to Arabic."""
    history = await events.last_events(doctor_id, 0)
    for event in reversed(history):
        if event.patient_id == patient.id and event.kind == "patient_in" and event.text:
            return of(event.text)
    return "ar"
