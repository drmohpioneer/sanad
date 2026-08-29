"""Owns one question: which patient did the doctor mean.

The doctor types "/report Ismail" or "/force_due Hend". The fragment is matched
against the names on his own board, in code, and the answer is one of three
things: exactly one patient, nobody, or more than one. The third is why this
file exists. Until S5 the first match simply won, so with "Hend Ismail" and
"Ismail Roshdy" both on the board, "/force_due Ismail" chased Hend and said
nothing about the other one. Nudging the wrong patient is a small harm, but
guessing which patient a doctor meant is exactly the kind of decision Sanad
does not make anywhere else, so here it asks instead.

One exception to "more than one is ambiguous": a fragment that is somebody's
whole name is that person, even when it is also part of a longer name. A doctor
who typed the full name has already been as specific as he can be.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .sentinel import normalize


@dataclass(frozen=True)
class Match:
    """What the board had to say about a fragment the doctor typed."""

    fragment: str
    names: tuple[str, ...]  # every name that matched, in board order

    @property
    def one(self) -> Optional[str]:
        """The single patient meant, or None when there is not exactly one."""
        return self.names[0] if len(self.names) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.names) > 1

    def warning(self) -> str:
        """What the doctor is told instead of a guess. Names every candidate."""
        listed = ", ".join(self.names)
        return (f"{self.fragment!r} matches {len(self.names)} of your patients: "
                f"{listed}. Say more of the name.")

    def nobody(self) -> str:
        return f"No patient of yours matches {self.fragment!r}."


def resolve(names: Sequence[str], fragment: str) -> Match:
    """A fragment plus the names on the board -> which patient was meant.

    Case-insensitive substring match, in board order, which is oldest first. An
    exact whole-name match collapses the result to that one patient; anything
    else that matches more than once is reported as ambiguous rather than
    resolved by position.
    """
    wanted = (fragment or "").strip().lower()
    if not wanted:
        return Match(fragment=fragment or "", names=())
    hits = tuple(name for name in names if wanted in name.lower())
    exact = tuple(name for name in hits if name.lower() == wanted)
    if len(exact) == 1:
        return Match(fragment=fragment, names=exact)
    return Match(fragment=fragment, names=hits)


# --------------------------------------------------------------------------- #
# The second question, added at S6: is the name printed on this slip the name
# on the record? (specs/S6-care-coordinator.md item B, the verifier)
# --------------------------------------------------------------------------- #
# A lab prints a name the way the receptionist typed it: with a title in front
# of it, with the father's name added or dropped, in Arabic when the record is
# in English. None of that makes it a different person, and a missing middle
# name must not send a real result back to the doctor as an identity mismatch.
# What does make it a different person is not sharing a name part at all.
#
# The comparison runs on core/sentinel.normalize, the same folding the emergency
# table uses: diacritics stripped, the several spellings of one Arabic letter
# unified, case dropped. Nothing here transliterates: an Arabic slip against an
# English record shares no tokens and is reported as "cannot compare", which
# escalates rather than attaches.
TITLES: frozenset[str] = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "prof", "patient", "name", "pt",
    "السيد", "السيده", "الاست", "الانسه", "المريض", "المريضه", "الاستاذ",
    "الاستاذه", "دكتور", "دكتوره", "مستر", "مدام", "اسم",
})
# Name parts that carry no identity on their own.
CONNECTORS: frozenset[str] = frozenset({
    "bin", "ben", "abd", "abdel", "abdul", "el", "al", "abu", "abo", "ابن",
    "بن", "عبد", "ابو", "ال",
})


def name_tokens(name: str) -> tuple[str, ...]:
    """A written name -> the parts that identify a person, in order."""
    parts = normalize(name).split()
    return tuple(p for p in parts if p not in TITLES and p not in CONNECTORS)


def same_person(printed: str, record: str) -> tuple[bool, str]:
    """(is this the same person, why). Fuzzy on purpose, in both scripts.

    Two name parts in common is a match, and so is a record name whose every
    part appears on the slip. One part in common, when one of the two names has
    only one part, is a match as well: a slip that prints "Ahmed" against a
    record that says "Ahmed" is not an identity mismatch, it is a short slip.

    Everything else is False, and the caller escalates instead of attaching.
    """
    left, right = name_tokens(printed), name_tokens(record)
    if not left or not right:
        return False, "no name printed on the slip" if not left else "no name on the record"
    shared = [t for t in left if t in right]
    if len(shared) >= 2:
        return True, f"{len(shared)} name parts match"
    if right and all(t in left for t in right):
        return True, "every part of the recorded name is printed on the slip"
    if shared and (len(left) == 1 or len(right) == 1):
        return True, "the whole of the shorter name matches"
    return False, (f"printed {' '.join(left)!r} against {' '.join(right)!r} "
                   "on the record")


# --------------------------------------------------------------------------- #
# The third question, added at rev 17: what does an Arabic sentence call him?
# (specs/S6-fix-queue-rev17.md item 11)
# --------------------------------------------------------------------------- #
# The doctor dictates on camera in English, so the record holds "Ahmed Ali" and
# every Arabic line Sanad sends greeted him as "يا Ahmed": Latin letters inside
# an Arabic sentence, which is the tell of a machine and the first thing a Cairo
# patient distrusts.
#
# The table below is strict on purpose and there is no fuzzy matching anywhere
# near it. A wrong transliteration is worse than a Latin name, and a name that
# is not on this list is DROPPED from the Arabic sentence rather than printed in
# Latin: "تمام. هسأل عليك تاني يوم الأحد" reads perfectly well with no name in
# it, and "تمام يا Ahmed" does not.
#
# This is display only. core/verify.py still matches the printed slip against
# the dictated record name through `same_person` above, never against anything
# rendered here, so nothing in this table can attach a result to the wrong
# person.
ARABIC_FIRST_NAMES: dict[str, str] = {
    # men
    "ahmed": "أحمد", "ahmad": "أحمد",
    "mohamed": "محمد", "mohammed": "محمد", "muhammad": "محمد",
    "mohammad": "محمد", "mohamad": "محمد", "mahmoud": "محمود",
    "mahmud": "محمود", "mostafa": "مصطفى", "mustafa": "مصطفى",
    "moustafa": "مصطفى", "ali": "علي", "omar": "عمر", "omer": "عمر",
    "amr": "عمرو", "hassan": "حسن", "hasan": "حسن", "hussein": "حسين",
    "hussain": "حسين", "hossam": "حسام", "hosam": "حسام", "khaled": "خالد",
    "khalid": "خالد", "tarek": "طارق", "tarik": "طارق", "tareq": "طارق",
    "youssef": "يوسف", "yousef": "يوسف", "yusuf": "يوسف",
    "ibrahim": "إبراهيم", "karim": "كريم", "kareem": "كريم",
    "sherif": "شريف", "sayed": "سيد", "mohsen": "محسن", "nabil": "نبيل",
    "sameh": "سامح", "tamer": "تامر", "wael": "وائل", "hany": "هاني",
    "hani": "هاني", "ashraf": "أشرف", "adel": "عادل", "magdy": "مجدي",
    "ayman": "أيمن", "osama": "أسامة", "usama": "أسامة", "ehab": "إيهاب",
    "ramy": "رامي", "rami": "رامي", "sami": "سامي", "saeed": "سعيد",
    "said": "سعيد", "salah": "صلاح", "nader": "نادر", "maged": "ماجد",
    "majed": "ماجد", "emad": "عماد", "alaa": "علاء", "gamal": "جمال",
    "hesham": "هشام", "hisham": "هشام", "islam": "إسلام", "kamal": "كمال",
    "mazen": "مازن", "mounir": "منير", "monir": "منير", "reda": "رضا",
    "samir": "سمير", "shady": "شادي", "shadi": "شادي", "waleed": "وليد",
    "walid": "وليد", "yasser": "ياسر", "ziad": "زياد", "zeyad": "زياد",
    "abdallah": "عبدالله", "abdullah": "عبدالله", "amir": "أمير",
    "anas": "أنس", "bassem": "باسم", "basem": "باسم", "fady": "فادي",
    "fadi": "فادي", "mina": "مينا", "george": "جورج", "peter": "بيتر",
    "bishoy": "بيشوي", "kirollos": "كيرلس",
    # women
    "fatma": "فاطمة", "fatima": "فاطمة", "mona": "منى", "sara": "سارة",
    "sarah": "سارة", "heba": "هبة", "hiba": "هبة", "nour": "نور",
    "noor": "نور", "mariam": "مريم", "maryam": "مريم", "salma": "سلمى",
    "aya": "آية", "dina": "دينا", "rania": "رانيا", "amira": "أميرة",
    "asmaa": "أسماء", "asma": "أسماء", "doaa": "دعاء", "eman": "إيمان",
    "iman": "إيمان", "esraa": "إسراء", "israa": "إسراء", "ghada": "غادة",
    "hala": "هالة", "hanan": "حنان", "hend": "هند", "hind": "هند",
    "hoda": "هدى", "huda": "هدى", "laila": "ليلى", "layla": "ليلى",
    "manal": "منال", "marwa": "مروة", "nadia": "نادية", "naglaa": "نجلاء",
    "noha": "نهى", "nourhan": "نورهان", "omnia": "أمنية", "rasha": "رشا",
    "reem": "ريم", "rehab": "رحاب", "safaa": "صفاء", "sahar": "سحر",
    "shaimaa": "شيماء", "shereen": "شيرين", "soha": "سها", "walaa": "ولاء",
    "yara": "يارا", "yasmin": "ياسمين", "yasmine": "ياسمين",
    "yasmeen": "ياسمين", "zeinab": "زينب", "zainab": "زينب", "amal": "أمل",
}

# The Arabic block. A record the doctor dictated in Arabic already holds the
# form the patient reads, so it is used as it is and never transliterated back.
_ARABIC = range(0x0600, 0x0700)


def is_arabic(text: str) -> bool:
    return any(ord(ch) in _ARABIC for ch in text or "")


def first_name(name: str) -> str:
    """The name a sentence greets someone by. Never raises on a blank one.

    A record with no name should not be possible (the Registrar refuses a
    dictation without one, placeholders included), but a template that raised
    here would take a whole wake-up down, so it does not.
    """
    parts = (name or "").split()
    return parts[0] if parts else ""


def in_arabic(name: str) -> str:
    """The Arabic form of a first name, or "" when none is known.

    Exact table lookup on the lowercased first word, plus the one rule that
    needs no table: a name already written in Arabic is already the answer.
    """
    first = first_name(name)
    if not first:
        return ""
    if is_arabic(first):
        return first
    return ARABIC_FIRST_NAMES.get(first.strip(".,").lower(), "")


def vocative(name: str, speak: str) -> str:
    """What a sentence in `speak` is allowed to call this person.

    Arabic: "يا أحمد", or nothing at all when no Arabic form is known. English:
    the first name as the doctor dictated it. The templates that carry this
    field leave the vocative particle inside it for exactly that reason, and
    `templates.render` tidies the spacing a dropped name leaves behind.
    """
    if speak == "ar":
        arabic = in_arabic(name)
        return f"يا {arabic}" if arabic else ""
    return first_name(name)
