"""Owns the three checks a lab slip has to pass before it satisfies a contract.

A photograph of a result is not evidence yet. It is evidence when it is this
patient's result, taken after the doctor ordered it, and carrying everything he
asked for. Until S6 a slip that reached the extractor was attached on the
strength of its analytes alone, so somebody else's slip, or last year's, closed
a loop as neatly as the right one.

The three checks, all in code, none of them a model call:

  identity   the name printed on the slip is the name on the record, compared
             fuzzily in both scripts (core/names.same_person). A mismatch never
             attaches: it goes to the doctor as an escalation and the contract
             stays open. Two names written in two different alphabets are not a
             mismatch, they are "cannot compare", and that escalates too.
  date       the collection date printed on the slip is on or after the day the
             doctor ordered the test. A slip that predates the order is an old
             result, and an old result does not close a new contract.
  complete   every analyte the contract asked for is on the slip. A partial
             result keeps the contract open and names what is missing, which is
             what the Coordinator's request_missing_evidence then asks for.

An unreadable date and an unreadable name are not failures: the slip carries
what the lab printed, and labs do print slips with no name on them. Those
attach, so the doctor sees the values, and they are reported as "not printed" on
the card. What they never do is satisfy the contract. Until S11 they did: only
`before_order` was refused, so an unnamed or undated slip closed the evidence
side of an obligation on a check that was never made (reviews/
codex-troubleshoot-1.md item 3). A check that could not be done is not a check
that passed, so the loop stays open, the card names which check stood down, and
the Coordinator's request path handles it exactly as it handles a partial slip.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional, Sequence

from . import labs, names

ARABIC_FIRST, ARABIC_LAST = "؀", "ۿ"


def _has_arabic(text: str) -> bool:
    return any(ARABIC_FIRST <= ch <= ARABIC_LAST for ch in text or "")


# --------------------------------------------------------------------------- #
# The date printed on a slip
# --------------------------------------------------------------------------- #
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DMY = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})")
MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_TEXT_DATE = re.compile(
    r"(\d{1,2})\s*[- ]?\s*([A-Za-z]{3,9})\s*[- ]?\s*(\d{2,4})"
)


def parse_date(text: Optional[str]) -> Optional[date]:
    """A printed date -> a date, or None when it is not one.

    "2026-08-20", "20/08/2026", "20-8-26", "20 Aug 2026" and their Arabic-Indic
    digits are dates. Anything else is None, which the caller reports as "not
    printed" rather than treating as a pass.
    """
    if not text:
        return None
    cleaned = str(text).strip().translate(_DIGITS)
    match = _ISO.search(cleaned)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _safe(year, month, day)
    match = _TEXT_DATE.search(cleaned)
    if match:
        day, word, year = match.group(1), match.group(2)[:3].lower(), match.group(3)
        if word in MONTHS:
            return _safe(_year(year), MONTHS[word], int(day))
    match = _DMY.search(cleaned)
    if match:
        first, second, year = (int(g) for g in match.groups())
        # A day above twelve settles which of the two numbers is the month;
        # otherwise Egypt writes the day first and this reads it that way.
        day, month = (first, second) if first > 12 or second <= 12 else (second, first)
        return _safe(_year(str(year)), month, day)
    return None


def _year(raw: str) -> int:
    value = int(raw)
    return value if value > 100 else 2000 + value


def _safe(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# What the contract asked for
# --------------------------------------------------------------------------- #
def required_analytes(loop: Any) -> tuple[str, ...]:
    """The analytes this contract needs, from the doctor's own words.

    A doctor who dictated "potassium and creatinine" has named them; a doctor
    who said "kidney function tests" has named a panel, and core/labs.py knows
    which analytes a panel is ordinarily made of. When neither resolves to
    anything, the answer is empty, which means completeness cannot be judged and
    is reported as such rather than as a pass.
    """
    details = getattr(loop, "details", None) or {}
    named = details.get("analytes")
    if isinstance(named, (list, tuple)) and named:
        return tuple(str(a).strip() for a in named if str(a).strip())
    test_name = str(details.get("test_name") or getattr(loop, "title", "") or "")
    return labs.panel_analytes(test_name)


def missing_analytes(required: Sequence[str], printed: Sequence[str]) -> tuple[str, ...]:
    """Which of the required analytes are not on the slip, in the doctor's words."""
    on_slip = {labs.canonical(a) for a in printed if str(a).strip()}
    return tuple(
        want for want in required if labs.canonical(want) not in on_slip
    )


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    """What the three checks said. `satisfies` is the only thing that attaches."""

    identity: str = "match"        # match | mismatch | cannot_compare | not_printed
    identity_why: str = ""
    dated: str = "ok"             # ok | before_order | not_printed
    collected_on: Optional[date] = None
    missing: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    reasons: list[str] = field(default_factory=list)

    @property
    def identity_failed(self) -> bool:
        """A slip that is not this patient's, or cannot be shown to be his."""
        return self.identity in ("mismatch", "cannot_compare")

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def unverified(self) -> tuple[str, ...]:
        """The checks that could not be made on this slip, in the card's words.

        A slip with no printed name and a slip with no printed date each cost
        one check. This is what the doctor's card prints so he knows why a
        result he can see did not close the obligation behind it.
        """
        out: list[str] = []
        if self.identity == "not_printed":
            out.append("identity")
        if self.dated == "not_printed":
            out.append("date")
        return tuple(out)

    @property
    def attaches(self) -> bool:
        """May these values be written onto the patient's loop at all?

        Only an identity failure says no. A slip with no name printed on it is
        an ordinary slip from an ordinary Egyptian lab, and refusing those would
        mean refusing most real results; it attaches, and the card says the name
        could not be checked.
        """
        return not self.identity_failed

    @property
    def satisfies(self) -> bool:
        """Does this slip close the evidence side of the contract?

        All three checks have to PASS, which is not the same as "none of them
        failed". The name printed on the slip has to be this patient's, the
        collection date has to be readable and on or after the order, and every
        requested analyte has to be there. A check that could not be made
        (`not_printed` on either side) leaves the contract open exactly as a
        partial result does: the values attach, the doctor sees them, and the
        Coordinator's request path asks for what is still missing.
        """
        return (self.identity == "match" and self.dated == "ok"
                and self.complete)

    def as_meta(self) -> dict[str, Any]:
        return {
            "identity": self.identity, "identity_why": self.identity_why,
            "dated": self.dated, "unverified": list(self.unverified),
            "collected_on": self.collected_on.isoformat() if self.collected_on else "",
            "required": list(self.required), "missing": list(self.missing),
            "attaches": self.attaches, "satisfies": self.satisfies,
            "reasons": list(self.reasons),
        }

    def lines(self) -> list[str]:
        """What the doctor's card prints about the verification."""
        if self.required:
            present = len(self.required) - len(self.missing)
            head = (f"verified: identity {self.identity}, date {self.dated}, "
                    f"{present} of {len(self.required)} requested analytes present")
        else:
            head = (f"verified: identity {self.identity}, date {self.dated}, "
                    "the order named no analytes to check against")
        lines = [head, *self.reasons]
        if self.unverified:
            lines.append(
                "the " + " and the ".join(self.unverified)
                + " check could not be done on this slip, so the values are "
                "attached for your review and the obligation stays open"
            )
        return lines


def check(
    *,
    printed_name: str,
    printed_date: str,
    printed_analytes: Sequence[str],
    patient_name: str,
    ordered_on: Optional[datetime],
    required: Sequence[str] = (),
) -> Verdict:
    """The three checks, in one verdict. Nothing here writes anything."""
    verdict = Verdict(required=tuple(required))

    printed_name = (printed_name or "").strip()
    if not printed_name:
        verdict.identity = "not_printed"
        verdict.identity_why = "the slip prints no patient name"
        verdict.reasons.append(
            "identity: the slip prints no name, so it was attached for your "
            "review and it does not close the obligation on its own"
        )
    elif _has_arabic(printed_name) != _has_arabic(patient_name or ""):
        verdict.identity = "cannot_compare"
        verdict.identity_why = (
            f"the slip prints {printed_name!r} and the record says "
            f"{patient_name!r}, in two different alphabets"
        )
        verdict.reasons.append("identity: " + verdict.identity_why)
    else:
        same, why = names.same_person(printed_name, patient_name or "")
        verdict.identity = "match" if same else "mismatch"
        verdict.identity_why = why
        if not same:
            verdict.reasons.append(f"identity: {why}")

    collected = parse_date(printed_date)
    verdict.collected_on = collected
    if collected is None:
        verdict.dated = "not_printed"
        verdict.reasons.append("date: the slip prints no readable collection date")
    elif ordered_on is not None and collected < ordered_on.date():
        verdict.dated = "before_order"
        verdict.reasons.append(
            f"date: collected {collected.isoformat()}, before the order on "
            f"{ordered_on.date().isoformat()}"
        )

    verdict.missing = missing_analytes(verdict.required, printed_analytes)
    if verdict.missing:
        verdict.reasons.append("missing: " + ", ".join(verdict.missing))
    return verdict
