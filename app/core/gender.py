"""Owns one question: which grammatical gender does Sanad write this person in.

Arabic conjugates the second person: a reminder that reads "فاكر" to a man reads
wrong to a woman, and Mohamed's first real phone test caught exactly that on a
female patient. English is milder but not free: "his result", "messaging him".

The rule is code, not a prompt. The patient record carries `sex`; this module
turns whatever was dictated ("male", "female", "أنثى", "F", nothing at all) into
one of three answers, and every template that mentions or addresses a patient
picks its wording from that answer:

    "m"  masculine forms
    "f"  feminine forms
    "u"  unknown -> wording that commits to neither, in both languages

Unknown is a real answer, not a fallback to masculine: a doctor who did not say
the sex gets neutral text rather than a guess with a two-in-three chance of
reading wrong.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal, Optional, TypeVar

Gender = Literal["m", "f", "u"]

T = TypeVar("T")

# What a doctor's dictation actually puts in the `sex` field, in either
# language. Matched on a normalized token, so "Male", "male." and "ذكر" all land.
MALE_WORDS: frozenset[str] = frozenset(
    {"m", "male", "man", "boy", "masculine", "ذكر", "رجل", "راجل", "ولد", "صبي"}
)
FEMALE_WORDS: frozenset[str] = frozenset(
    {"f", "female", "woman", "girl", "feminine", "انثي", "ست", "سيده", "بنت",
     "مرا", "امراه"}
)

_DIACRITICS = re.compile(r"[ً-ٰـ]")
_LETTERS = str.maketrans(
    {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و",
     "ئ": "ي", "ک": "ك", "ی": "ي"}
)


def _token(text: Optional[str]) -> str:
    """Whatever was dictated -> one comparable lowercase token."""
    s = unicodedata.normalize("NFKC", (text or "").strip().lower())
    s = _DIACRITICS.sub("", s).translate(_LETTERS)
    s = re.sub(r"[^0-9a-zء-ي ]+", " ", s)
    return " ".join(s.split())


def of(sex: Optional[str]) -> Gender:
    """A record's `sex` field -> the grammatical gender to write in."""
    token = _token(sex)
    if not token:
        return "u"
    if token in MALE_WORDS:
        return "m"
    if token in FEMALE_WORDS:
        return "f"
    # A multi-word value ("58 year old female") still decides, on its own words.
    words = set(token.split())
    if words & FEMALE_WORDS:
        return "f"
    if words & MALE_WORDS:
        return "m"
    return "u"


def of_patient(patient: object) -> Gender:
    """The same question asked of a Patient record. Anything unset is unknown."""
    return of(getattr(patient, "sex", None))


def pick(gender: Gender, masculine: T, feminine: T, neutral: T) -> T:
    """Choose one of three wordings. The only branch any template needs."""
    if gender == "m":
        return masculine
    if gender == "f":
        return feminine
    return neutral


# --------------------------------------------------------------------------- #
# English, third person: what the doctor's cards and reports say about a patient
# --------------------------------------------------------------------------- #
def possessive(gender: Gender) -> str:
    """his / her / their."""
    return pick(gender, "his", "her", "their")


def object_pronoun(gender: Gender) -> str:
    """him / her / them."""
    return pick(gender, "him", "her", "them")


def subject_pronoun(gender: Gender) -> str:
    """he / she / they. Prefer the patient's name; this is for when it must be
    a pronoun. "they" takes a plural verb, so callers write around it."""
    return pick(gender, "he", "she", "they")
