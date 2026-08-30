"""Reads a due date out of the doctor's own words when the model left it empty.

S17. `due_in_days` is a field of the Registrar's structured proposal and until
this file existed the model was its only source. When Gemini returned it empty
the loop was committed with no deadline, no ladder was queued for it, and the
confirm card said "no due date was dictated" about a sentence that dictated one.

Measured, not assumed: research/s16-live-results.md defect 2. On revision 26 the
runbook's own beat-1 dictation ("Lipid panel in 2 weeks. Blood pressure twice a
day for 7 days. Come back in 3 weeks.") came back with `due_in_days` empty on
the TEST and the MONITOR loop, on two consecutive dictations for two different
patients, so Confirm queued nine follow-up tasks instead of twelve and the lipid
ladder never ran at all. The VISIT loop got its 21 days both times, so the model
was reading relative dates, just not those two.

What this module does is read the plain relative phrases a doctor says, in
English, Egyptian Arabic and Franco-Arabic, and attach at most one of them to
one loop. What it will not do is guess. A phrase is used only when:

  - the clause it sits in also names that loop, by the loop's own test name,
    metric or title words, or by the vocabulary of that kind of obligation;
  - exactly one loop still needing a due date answers to that clause;
  - the whole dictation gives that loop one answer and not two.

A loop that fails any of those keeps no due date at all, and the confirm card
prints "Due date: not dictated, not filled in by Sanad" for it in the block that
already says what the doctor did not dictate. A `due_in_days` the model did
return is never touched.

The one date that is read from the record rather than from the sentence is a
MONITOR loop's own duration: "blood pressure twice a day for 7 days" fills the
`days` field, and a seven-day monitoring obligation is due on day seven. That is
the model's own number, restated, not a new one.

Pure code. Nothing here imports the cloud SDK, so it runs anywhere.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .sentinel import normalize

DAY, WEEK, MONTH = 1, 7, 30

# The three kinds of obligation that carry a deadline. MEDICATION is a standing
# instruction and TASK has no ladder, so neither is filled in and neither is
# reported as missing one.
FALLBACK_TYPES: tuple[str, ...] = ("TEST", "MONITOR", "VISIT")

# Where a filled date came from, for the caller's audit line.
FROM_DICTATION = "the doctor's own words"
FROM_MONITOR_DAYS = "the monitoring duration on the loop"

# Arabic-Indic digits are not compatibility characters, so NFKC leaves them
# alone and sentinel.normalize drops them as non-text. A dictation transcribed
# in Arabic can carry them, so they are folded to ASCII before anything is read.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# A sentence is cut into clauses at the marks a dictation actually carries, in
# both scripts, the comma included: a doctor who says "Lipid panel in 2 weeks,
# blood pressure twice a day for 7 days" in one breath has still named two
# obligations, and only the cut keeps each phrase with the one it belongs to.
# The colon is deliberately not a cut: "Lipid panel: in 2 weeks" is one clause
# and cutting it would separate the phrase from the loop it belongs to.
_CLAUSE = re.compile(r"[.,،؛؟!?;\n]+")

_NUMBERS_RAW: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
    "واحد": 1, "اتنين": 2, "اثنين": 2, "تلات": 3, "تلاته": 3, "ثلاثة": 3,
    "اربع": 4, "اربعه": 4, "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6,
    "سبع": 7, "سبعه": 7, "تمان": 8, "تمانيه": 8, "ثمانية": 8,
    "تسع": 9, "تسعه": 9, "عشر": 10, "عشره": 10,
    "wa7ed": 1, "etnen": 2, "itnen": 2, "talat": 3, "talata": 3,
    "arba3": 4, "arba3a": 4, "khams": 5, "khamsa": 5, "sett": 6, "setta": 6,
    "sab3a": 7, "tamanya": 8, "tes3a": 9, "3ashara": 10,
}

_UNITS_RAW: dict[str, int] = {
    "day": DAY, "days": DAY, "week": WEEK, "weeks": WEEK,
    "month": MONTH, "months": MONTH,
    "يوم": DAY, "ايام": DAY, "اسبوع": WEEK, "اسابيع": WEEK,
    "شهر": MONTH, "شهور": MONTH, "اشهر": MONTH,
    "yom": DAY, "youm": DAY, "ayam": DAY, "ayyam": DAY,
    "osbo3": WEEK, "esbo3": WEEK, "usbu3": WEEK, "asabi3": WEEK,
    "shahr": MONTH, "shohor": MONTH, "ashhor": MONTH,
}

# Arabic writes "two weeks" as one word, and that word carries the count as well
# as the unit, so it is never preceded by a number.
_DUALS_RAW: dict[str, int] = {
    "يومين": 2 * DAY, "اسبوعين": 2 * WEEK, "شهرين": 2 * MONTH,
    "yomen": 2 * DAY, "youmen": 2 * DAY,
    "osbo3en": 2 * WEEK, "esbo3en": 2 * WEEK, "osboo3en": 2 * WEEK,
    "shahren": 2 * MONTH,
}

# The word that opens a relative phrase: "in two weeks", "after 3 weeks",
# "بعد اسبوعين", "كمان اسبوع", "ba3d osbo3en", "kaman 2 weeks".
_LEADS_RAW: tuple[str, ...] = (
    "in", "after", "بعد", "كمان", "ba3d", "ba3ed", "kaman", "kman",
)

# "2 weeks from now", and the two ways the same tail is said in Egypt.
_TAILS_RAW: tuple[tuple[str, str], ...] = (
    ("from", "now"), ("من", "دلوقتي"), ("men", "delwa2ti"),
)

# "next week" and "next month", said as a phrase rather than as a count.
_NEXT_ONE = "next"
_NEXT_PHRASES_RAW: dict[str, int] = {
    "الاسبوع الجاي": WEEK, "الاسبوع القادم": WEEK, "الاسبوع الجى": WEEK,
    "الشهر الجاي": MONTH, "الشهر القادم": MONTH,
    "el osbo3 el gay": WEEK, "el shahr el gay": MONTH,
}

# What a clause about this kind of obligation sounds like when the doctor does
# not repeat the loop's own name. "Come back in 3 weeks" is the visit, and the
# loop the Registrar wrote for it is titled "Follow-up visit", which shares not
# one word with the sentence that ordered it.
_TYPE_WORDS_RAW: dict[str, tuple[str, ...]] = {
    "TEST": ("test", "tests", "lab", "labs", "analysis", "scan", "x ray",
             "تحليل", "التحليل", "تحاليل", "اشعه", "الاشعه", "فحص",
             "ta7lil", "ta7alil", "asheaa", "fa7s"),
    "MONITOR": ("measure", "measures", "measurement", "measurements", "monitor",
                "monitoring", "reading", "readings", "قياس", "القياس", "قيس",
                "يقيس", "تقيس", "متابعه", "2ees", "2yas", "moraqba"),
    "VISIT": ("come back", "comes back", "come again", "back to me",
              "follow up", "followup", "visit", "appointment", "see me",
              "see you", "review",
              "تعالي", "تعالى", "ترجع", "ارجع", "زياره", "الزياره", "معاد",
              "الميعاد", "موعد", "كشف", "الكشف", "شوفني",
              "ارجعلي", "تعالالي",
              "ta3ala", "ta3alali", "erga3", "erga3li", "yerga3", "ma3ad",
              "keshf", "ziara", "shofni"),
}

# Function words carry no identity, and a loop must never answer to a clause on
# the strength of one of them. Anything shorter than three characters is dropped
# as well, which is most of the rest of them in both scripts.
_STOP: frozenset[str] = frozenset({
    "the", "and", "for", "with", "his", "her", "him", "this", "that", "from",
    "every", "please", "and", "أو", "او", "الى", "علي", "على", "من", "في",
    "مع", "ده", "دي", "بعد", "كمان",
})


def _fold(text: Any) -> str:
    """Normalized, space padded, with Arabic-Indic digits folded to ASCII."""
    return normalize(str(text or "").translate(_ARABIC_DIGITS))


def _word(text: str) -> str:
    return _fold(text).strip()


def _table(raw: dict[str, int]) -> dict[str, int]:
    return {_word(key): value for key, value in raw.items() if _word(key)}


NUMBERS = _table(_NUMBERS_RAW)
UNITS = _table(_UNITS_RAW)
DUALS = _table(_DUALS_RAW)
LEADS = frozenset(_word(one) for one in _LEADS_RAW)
TAILS = tuple((_word(head), _word(tail)) for head, tail in _TAILS_RAW)
NEXT_PHRASES = _table(_NEXT_PHRASES_RAW)
TYPE_WORDS = {kind: tuple(f" {_word(one)} " for one in words)
              for kind, words in _TYPE_WORDS_RAW.items()}


# --------------------------------------------------------------------------- #
# The phrases
# --------------------------------------------------------------------------- #
def clauses(text: str) -> list[str]:
    """The dictation, cut where the doctor paused. Empty pieces are dropped."""
    return [piece for piece in _CLAUSE.split(text or "") if piece.strip()]


def _count(word: str) -> int:
    if word.isdigit():
        return int(word)
    return NUMBERS.get(word, 0)


def _quantity(words: Sequence[str], i: int) -> int:
    """"2 weeks" or "a month" or "اسبوعين", read forwards from index i."""
    if i >= len(words):
        return 0
    word = words[i]
    if word in DUALS:
        return DUALS[word]
    count = _count(word)
    if not count:
        # "كمان اسبوع" is one week with no number said at all.
        return UNITS.get(word, 0)
    if i + 1 < len(words) and words[i + 1] in UNITS:
        return count * UNITS[words[i + 1]]
    return 0


def _quantity_before(words: Sequence[str], i: int) -> int:
    """"2 weeks" read backwards from the word after it, for "... from now"."""
    if i < 1:
        return 0
    unit = words[i - 1]
    if unit in DUALS:
        return DUALS[unit]
    if unit not in UNITS:
        return 0
    count = _count(words[i - 2]) if i >= 2 else 0
    return (count or 1) * UNITS[unit]


def days_in(text: str) -> list[int]:
    """Every relative phrase in this text, as whole days, in the order said."""
    words = _fold(text).split()
    found: list[int] = []
    for i, word in enumerate(words):
        if word in LEADS:
            days = _quantity(words, i + 1)
            if days:
                found.append(days)
        elif word == _NEXT_ONE and i + 1 < len(words) and words[i + 1] in UNITS:
            found.append(UNITS[words[i + 1]])
    for head, tail in TAILS:
        for i in range(len(words) - 1):
            if words[i] == head and words[i + 1] == tail:
                days = _quantity_before(words, i)
                if days:
                    found.append(days)
    padded = _fold(text)
    for phrase, days in NEXT_PHRASES.items():
        if f" {phrase} " in padded:
            found.append(days)
    return found


# --------------------------------------------------------------------------- #
# Which loop a clause is about
# --------------------------------------------------------------------------- #
def loop_words(loop: Any) -> tuple[str, ...]:
    """The words this loop is recognised by: its own fields, not its type."""
    said: list[str] = []
    for field in ("test_name", "metric", "title"):
        for token in _fold(getattr(loop, field, "")).split():
            if len(token) >= 3 and token not in _STOP and token not in said:
                said.append(token)
    return tuple(said)


def answers_to(loop: Any, clause: str) -> bool:
    """Does this clause name this loop, by its own words or by its kind?"""
    padded = _fold(clause)
    if any(f" {token} " in padded for token in loop_words(loop)):
        return True
    return any(phrase in padded
               for phrase in TYPE_WORDS.get(getattr(loop, "type", ""), ()))


# --------------------------------------------------------------------------- #
# The fallback itself
# --------------------------------------------------------------------------- #
def derive(loops: Sequence[Any], dictation: str, *,
           cap: int = 365) -> dict[int, tuple[int, str]]:
    """{loop index: (days, why)} for the loops a date can honestly be read for.

    A loop absent from the answer is a loop with no due date, and the caller
    prints that absence rather than filling it.
    """
    wanted = [(i, loop) for i, loop in enumerate(loops)
              if getattr(loop, "type", "") in FALLBACK_TYPES
              and getattr(loop, "due_in_days", None) is None]
    if not wanted:
        return {}

    votes: dict[int, set[int]] = {i: set() for i, _ in wanted}
    for clause in clauses(dictation):
        said = {days for days in days_in(clause) if 0 < days <= cap}
        if len(said) != 1:
            # No phrase, or two different ones in one breath. Neither is an
            # answer, so neither is used.
            continue
        owners = [i for i, loop in wanted if answers_to(loop, clause)]
        if len(owners) == 1:
            votes[owners[0]].add(said.pop())

    found: dict[int, tuple[int, str]] = {}
    for i, loop in wanted:
        if len(votes[i]) == 1:
            found[i] = (votes[i].pop(), FROM_DICTATION)
            continue
        days = getattr(loop, "days", None)
        if (getattr(loop, "type", "") == "MONITOR" and isinstance(days, int)
                and 0 < days <= cap):
            found[i] = (days, FROM_MONITOR_DAYS)
    return found


def fill(record: Any, dictation: str, *,
         cap: int = 365) -> tuple[Any, dict[int, tuple[int, str]]]:
    """The record with the readable due dates filled in, and what was filled.

    The record is copied, never mutated, and a `due_in_days` the model returned
    is copied through untouched.
    """
    found = derive(record.loops, dictation, cap=cap)
    if not found:
        return record, {}
    loops = [loop.model_copy(update={"due_in_days": found[i][0]})
             if i in found else loop
             for i, loop in enumerate(record.loops)]
    return record.model_copy(update={"loops": loops}), found


__all__ = [
    "FALLBACK_TYPES", "FROM_DICTATION", "FROM_MONITOR_DAYS", "answers_to",
    "clauses", "days_in", "derive", "fill", "loop_words",
]
