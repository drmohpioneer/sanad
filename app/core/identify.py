"""Owns one question the Registrar could not ask before S9: who is this about?

Until S9 every dictation made a new patient. A doctor who said "follow up with
Ahmed about his potassium in a week" got a second Ahmed on the board with one
loop on him and nothing else, and the real Ahmed, the one with the diagnosis and
the plan and the open lab, never heard about his potassium at all.

Two things decide it, in this order, and they are deliberately different kinds
of thing:

  1. `core/names.resolve`, the same code matcher `/report` and `/force_due` use.
     It answers on the written name only: nobody, exactly one, or more than one.
  2. one Gemini read of the doctor's words against a compact list of his own
     board. It exists because a doctor does not always say a name. He says "the
     father of my friend Tarek", "the old lady I saw last week with the swollen
     legs", "this is a new patient", "look for a patient called Ahmed". A name
     matcher has nothing to say about any of those.

Then code, and only code, decides what the doctor is shown:

  - an explicit "this is a new patient", in Arabic or English, always wins as
    new, whatever the model returned;
  - the auto-selected "Existing patient" card needs BOTH: exactly one name match
    in code and exactly one model candidate above the confidence threshold, and
    they have to be the same patient. Anything else asks;
  - a description-only match, a confidence below the threshold, or two
    candidates always asks, with one button per candidate and the reason the
    model quoted printed beside it;
  - "unclear" asks for the name;
  - "lookup" lists and writes nothing;
  - a model error or a malformed verdict falls back to the code matcher and to
    the ask card. Never to a silent guess.

Nothing here writes anything anywhere. `decide` is a pure function over a
verdict and a board, `identify` is the one model call and it returns None rather
than raising, and the Registrar is what turns an outcome into a card the doctor
taps. This module imports the SDK inside the call that needs it, the way
core/intents.py does, so every rule below is testable with nothing installed.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, Field

from . import names, sentinel

log = logging.getLogger("sanad.identify")

# How much board the model is allowed to see. Fifty rows of five short fields is
# a small prompt and it is also the honest limit: a doctor with three hundred
# patients on one board is not a demo, and a prompt that grew without a ceiling
# would be the thing that broke first.
BOARD_LIMIT = 50

# Below this the model's opinion is a suggestion, not an answer, and the card
# asks. It is a threshold and not a tuning knob: the only thing it can do is
# turn an auto-selected card into a question.
CONFIDENCE = 0.7

NEW_PATIENT = "new_patient"
EXISTING_PATIENT = "existing_patient"
LOOKUP = "lookup"
UNCLEAR = "unclear"

# What `decide` hands back to the Registrar.
NEW = "new"
EXISTING = "existing"
ASK = "ask"
LIST = "lookup"


# --------------------------------------------------------------------------- #
# Net one and a half: the two things the doctor can say that code must obey
# --------------------------------------------------------------------------- #
# Matched on core/sentinel.normalize, the same folding the emergency table uses,
# so diacritics, the Arabic definite article and spelling variants cannot make a
# phrase miss. Both lists are short on purpose: a phrase here overrides a model,
# so a loose pattern would be a bug that is hard to see.
NEW_PHRASES: tuple[str, ...] = (
    "new patient", "a new case", "new case", "first visit", "first time here",
    "never seen him before", "never seen her before",
    "مريض جديد", "مريضه جديده", "مريضة جديدة", "حاله جديده", "حالة جديدة",
    "كيس جديد", "اول مره", "اول زياره", "جديد عندي", "جديده عندي",
    "مش شايفه قبل كده", "مش شايفها قبل كده",
)

LOOKUP_PHRASES: tuple[str, ...] = (
    "look for", "look up", "search for", "find me", "find the patient",
    "do i have a patient", "who is the patient", "show me the patient",
    "which of my patients", "list the patients",
    "دور على", "دورلي على", "دور لي على", "ابحث عن", "هات لي", "هاتلي",
    "عندي مريض اسمه", "عندي مريضه اسمها", "مين المريض", "مين المريضه",
    "وريني المريض", "ابحثلي عن",
)


def _flat(text: str) -> str:
    """One padded, folded line, so a phrase never matches half a word."""
    return " " + sentinel.normalize(text or "").strip() + " "


def _has(text: str, phrases: Sequence[str]) -> bool:
    flat = _flat(text)
    return any(sentinel.normalize(p).strip() in flat for p in phrases)


# --------------------------------------------------------------------------- #
# The identification note, checked in code before it is stored
# --------------------------------------------------------------------------- #
# codex re-audit 2. The note is the one free-text field a model writes onto a
# patient record: "father of Dr Tarek", "the lady from Tanta with the swollen
# legs". Every rule about it lived in the prompt, which is a request and not a
# guard, so a model having an off turn could have written a dose onto a record
# and the doctor would have confirmed a card that never showed it to him.
#
# Four rules, all in code, and a note that fails any of them is dropped whole
# rather than trimmed: half a description is worse than none.
NOTE_MAX_WORDS = 12


def _words(text: str) -> list[str]:
    """The comparison words of a piece of text, folded the way the Sentinel folds."""
    return sentinel.normalize(text).split()


def clinical_words(record: Any) -> frozenset[str]:
    """Every drug and test name the proposed plan carries, as folded words.

    Read off the loops rather than off the prose: a loop names its test, its
    metric and its drug in its own fields, so this is the record saying what
    the clinical vocabulary of this dictation is instead of a word list
    guessing at it. Words shorter than three letters are left out, because they
    are the joining words of both languages and never a drug name.
    """
    out: set[str] = set()
    for loop in getattr(record, "loops", None) or []:
        for field in ("title", "test_name", "metric", "drug", "dose"):
            for word in _words(str(getattr(loop, field, "") or "")):
                if len(word) >= 3:
                    out.add(word)
    return frozenset(out)


def clean_note(note: str, dictation: str,
               clinical: frozenset[str] = frozenset()) -> tuple[str, str]:
    """(the note that may be stored, why it was dropped). An empty reason kept it.

    A note may only ever be the doctor's own words, said back: at most twelve
    of them, every one of them present in the dictation, no digit anywhere in
    it, and none of the drug or test names this dictation carries. That last
    rule is what stops a description from becoming a second, unvalidated copy
    of the plan on a field nothing else checks.
    """
    said = (note or "").strip()
    if not said:
        return "", ""
    words = _words(said)
    if len(words) > NOTE_MAX_WORDS:
        return "", f"{len(words)} words, and the limit is {NOTE_MAX_WORDS}"
    flat = unicodedata.normalize("NFKC", said)
    if any(ch.isdigit() for ch in flat):
        return "", "it carries a number, and a note never carries one"
    heard = set(_words(dictation))
    stray = [w for w in words if w not in heard]
    if stray:
        return "", f"it uses words the doctor did not say: {', '.join(stray)}"
    named = [w for w in words if w in clinical]
    if named:
        return "", f"it names something clinical: {', '.join(named)}"
    return said, ""


def says_new(text: str) -> bool:
    """The doctor said out loud that this is a new patient. This always wins."""
    return _has(text, NEW_PHRASES)


def asks_lookup(text: str) -> bool:
    """The doctor is looking somebody up, so nothing may be created at all."""
    return _has(text, LOOKUP_PHRASES)


def is_bare_name(text: str, extracted_name: str) -> bool:
    """True when the doctor supplied a name and no instruction whatsoever."""
    said = sentinel.normalize(text).strip()
    name = sentinel.normalize(extracted_name).strip()
    if not said or not name or said != name:
        return False
    words = said.split()
    return 1 <= len(words) <= 5 and not any(ch.isdigit() for ch in text)


# --------------------------------------------------------------------------- #
# The board, as the model is allowed to see it
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BoardRow:
    """One patient, in the five fields an identification is allowed to use.

    `notes` is the doctor's own free text about this person: "father of Dr
    Tarek", "lives in Zagazig". It is written at confirm time by the Registrar
    and it is doctor text, never patient text, which is why nothing a patient
    ever sends can reach this list and change who a later dictation matches.
    """

    id: str
    name: str
    age: Optional[int] = None
    sex: str = ""
    diagnosis: str = ""
    notes: tuple[str, ...] = ()
    last_seen: str = ""

    def label(self) -> str:
        """"Ahmed Ali, 58, heart failure". The button the doctor taps."""
        bits: list[str] = [self.name]
        if self.age is not None:
            bits.append(str(self.age))
        if (self.diagnosis or "").strip():
            bits.append(self.diagnosis.strip())
        return ", ".join(bits)

    def as_context(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "age": self.age,
                "sex": self.sex, "diagnosis": self.diagnosis,
                "notes": list(self.notes), "last_seen": self.last_seen}


def board(patients: Sequence[Any], last_seen: Optional[dict[str, datetime]] = None,
          limit: int = BOARD_LIMIT) -> list[BoardRow]:
    """The doctor's patients as rows, the most recent `limit` of them.

    Recency is the last event on that patient, and the record's own creation
    date when nothing has happened to him yet. The rows come back in board
    order (oldest first), which is the order `core/names.resolve` reports its
    hits in, so the two halves of an identification always list the same
    patients in the same order.
    """
    seen = last_seen or {}

    def when(patient: Any) -> datetime:
        return seen.get(patient.id) or patient.created_at

    keep = sorted(patients, key=when, reverse=True)[:limit]
    order = {p.id: i for i, p in enumerate(patients)}
    rows: list[BoardRow] = []
    for patient in sorted(keep, key=lambda p: order.get(p.id, 0)):
        moment = when(patient)
        rows.append(BoardRow(
            id=patient.id,
            name=patient.name,
            age=patient.age,
            sex=patient.sex or "",
            diagnosis=patient.diagnosis or "",
            notes=tuple(note_lines(getattr(patient, "notes", None))),
            last_seen=moment.strftime("%Y-%m-%d") if moment else "",
        ))
    return rows


def note_lines(notes: Any) -> list[str]:
    """The text of every note on a record, oldest first. Never raises.

    A note is stored as {"text", "at"}; a record written before S9 has no notes
    field at all, and a hand-edited one might hold plain strings. All three read
    the same way here, because a board row that raised would take the whole
    dictation down with it.
    """
    out: list[str] = []
    for note in notes or []:
        if isinstance(note, dict):
            text = str(note.get("text") or "").strip()
            when = str(note.get("at") or "").strip()
            if text:
                out.append(f"{text} ({when})" if when else text)
        elif isinstance(note, str) and note.strip():
            out.append(note.strip())
    return out


# --------------------------------------------------------------------------- #
# The verdict: what the model is allowed to say
# --------------------------------------------------------------------------- #
class Candidate(BaseModel):
    patient_id: str = Field(description="The id, copied from the board list.")
    confidence: float = Field(
        default=0.0, description="0 to 1. How sure you are this is the patient."
    )
    reason: str = Field(
        default="",
        description="One line, quoting the doctor's own words that matched.",
    )


class Verdict(BaseModel):
    intent: Literal["new_patient", "existing_patient", "lookup", "unclear"] = Field(
        description="What the doctor is doing with this dictation."
    )
    candidates: list[Candidate] = Field(
        default_factory=list,
        description="Patients from the board this could be about. Empty for a "
        "new patient.",
    )
    note: str = Field(
        default="",
        description="Any relationship or description worth remembering about "
        "this patient, in the doctor's own words, at most twelve words. Empty "
        "when the dictation carries none.",
    )


IDENTIFY_PROMPT = """You are the identification step of a clinical follow-up
system. A doctor has just dictated something. You are given his own board: the
patients he already follows, with their ages, diagnoses and any notes he has
made about who they are.

Decide what he is doing:
- new_patient: this is somebody who is not on the board.
- existing_patient: this dictation is about somebody who is on the board. Name
  them in candidates, with a confidence and a one line reason that quotes the
  words of his that matched.
- lookup: he is asking you to find or list a patient, not to record anything.
- unclear: you cannot tell.

Rules you must not break:
- A candidate id must be copied from the board list. Never invent one.
- List every patient it could plausibly be, not just the best one. Two
  candidates is a normal answer and the doctor will be asked to choose.
- Confidence is your own honesty about the match, not a score to maximise. A
  first name shared by two patients is not a confident match.
- The reason quotes the doctor. "father of your friend Tarek" matched the note
  on Salah Mahmoud. Never explain your reasoning in general terms.
- note is for a relationship or a description worth remembering about this
  person for next time: "father of Dr Tarek", "lives in Zagazig", "the one with
  the swollen legs". Leave it empty when the dictation carries none. Never put
  clinical instructions in it. Code checks it before it is stored and drops it
  whole if it breaks any of these: at most twelve words, every word one the
  doctor actually said, no digit anywhere in it, and no drug or test name.
  A note you are not sure about is better left empty than dropped.
- You are not deciding anything. Code checks your answer against a name matcher
  and the doctor taps a button. Say what you actually see."""


def context_block(rows: Sequence[BoardRow]) -> str:
    """The board, as the lines the model reads. One patient per line."""
    if not rows:
        return "THE BOARD IS EMPTY. This doctor has no patients yet."
    lines = ["THE DOCTOR'S BOARD:"]
    for row in rows:
        bits = [f"id={row.id}", f"name={row.name}"]
        if row.age is not None:
            bits.append(f"age={row.age}")
        if row.sex:
            bits.append(f"sex={row.sex}")
        if row.diagnosis:
            bits.append(f"diagnosis={row.diagnosis}")
        if row.notes:
            bits.append("notes=" + "; ".join(row.notes))
        if row.last_seen:
            bits.append(f"last_seen={row.last_seen}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


async def identify(text: str, rows: Sequence[BoardRow],
                   extracted_name: str = "") -> Optional[Verdict]:
    """One Gemini read of the dictation against the board. None on any failure.

    None is not "new patient" and it is not "unclear": it is "the model did not
    answer", and `decide` reads it as the instruction to fall back to the code
    name matcher and ask. That is the fail-closed direction, because the
    expensive mistake here is attaching a dictation to the wrong person's
    record without being asked.
    """
    if not (text or "").strip() and not (extracted_name or "").strip():
        return None
    # An empty board is not a question. There is nobody to be about, so no call
    # is made: it saves the call, and it removes the one way the first dictation
    # a doctor ever gives could come back as "which patient is this?" because a
    # model answered "unclear" about a board with nothing on it.
    if not rows:
        return None
    try:
        from google.genai import types

        from .media import MODEL, client

        said = (text or "").strip()
        asked = f"THE DICTATION:\n{said}"
        if extracted_name.strip():
            asked += f"\n\nTHE NAME THE EXTRACTION READ OUT OF IT: {extracted_name}"
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=[types.Part(text=context_block(rows) + "\n\n" + asked)],
            config=types.GenerateContentConfig(
                system_instruction=IDENTIFY_PROMPT,
                response_mime_type="application/json",
                response_schema=Verdict,
                temperature=0,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, Verdict):
            return parsed
        return Verdict.model_validate(parsed) if parsed else None
    except Exception:  # noqa: BLE001 - the code matcher and the ask card answer
        log.exception("identification failed; the code name matcher answers")
        return None


# --------------------------------------------------------------------------- #
# The rules, in code, over whatever the model said
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Outcome:
    """What the Registrar builds a card from. Nothing here is stored."""

    kind: str
    patient_id: str = ""
    candidates: tuple[tuple[str, str], ...] = ()  # (patient id, the reason)
    note: str = ""
    why: str = ""
    needs_name: bool = False

    def ids(self) -> tuple[str, ...]:
        return tuple(pid for pid, _ in self.candidates)


def code_matches(rows: Sequence[BoardRow], fragment: str) -> list[BoardRow]:
    """The name matcher's answer, as rows rather than as strings.

    `core/names.resolve` answers in names, and two patients can carry the same
    written name, so the names it reports are consumed one for one against the
    board in order. Nothing here re-implements the matching itself.
    """
    match = names.resolve([row.name for row in rows], fragment)
    wanted = list(match.names)
    out: list[BoardRow] = []
    for row in rows:
        if row.name in wanted:
            wanted.remove(row.name)
            out.append(row)
    return out


def _reason(candidate: Candidate) -> str:
    said = (candidate.reason or "").strip()
    return said or "the model matched this record"


def decide(text: str, extracted_name: str, rows: Sequence[BoardRow],
           verdict: Optional[Verdict]) -> Outcome:
    """The whole rule, and it is the only thing that says what the doctor sees."""
    by_id = {row.id: row for row in rows}
    coded = code_matches(rows, extracted_name)
    note = (verdict.note or "").strip() if verdict is not None else ""

    # 1. He said it out loud. Nothing else is consulted.
    if says_new(text):
        return Outcome(kind=NEW, note=note,
                       why="the doctor said this is a new patient")

    # 2. A lookup writes nothing, so it is decided before anything else could.
    listed = ([c for c in verdict.candidates if c.patient_id in by_id]
              if verdict is not None else [])
    if asks_lookup(text) or (verdict is not None and verdict.intent == LOOKUP):
        found = tuple((c.patient_id, _reason(c)) for c in listed) or tuple(
            (row.id, "the name matches this record") for row in coded)
        return Outcome(kind=LIST, candidates=found,
                       why="the doctor asked to look a patient up, so nothing "
                           "was created")

    # 3. The model did not answer. The code matcher does, and it always asks.
    if verdict is None:
        if not rows:
            return Outcome(kind=NEW,
                           why="this doctor has no patients on the board yet")
        if coded:
            return Outcome(
                kind=ASK,
                candidates=tuple((row.id, "the dictated name matches this "
                                          "record") for row in coded),
                why="the identification model was unavailable, so the code name "
                    "matcher asked instead of choosing")
        # codex re-audit 1. This used to be NEW, and that is the one branch
        # where "no name matched" proves nothing: the model is the half that
        # reads "the father of my friend Tarek", and it is the half that is
        # missing. A board with patients on it plus a doctor who did not say
        # "new patient" is a question, so it is asked. Registering a second
        # copy of a patient who is already followed is the expensive mistake
        # and asking costs one tap.
        return Outcome(kind=ASK, needs_name=True,
                       why="the identification model was unavailable and no "
                           "name on the board matches, so nothing was created")

    # 4. The one path to an auto-selected record: both halves agree, and each of
    #    them names exactly one patient.
    strong = [c for c in listed if c.confidence >= CONFIDENCE]
    if (verdict.intent == EXISTING_PATIENT and len(coded) == 1
            and len(listed) == 1 and len(strong) == 1
            and strong[0].patient_id == coded[0].id):
        return Outcome(kind=EXISTING, patient_id=coded[0].id, note=note,
                       why=f"the dictated name matches one record and the "
                           f"model agrees: {_reason(strong[0])}")

    # 5. Anything else with a candidate on it asks, with the reasons printed.
    candidates: list[tuple[str, str]] = [
        (row.id, "the dictated name matches this record") for row in coded
    ]
    for candidate in listed:
        if candidate.patient_id not in [pid for pid, _ in candidates]:
            candidates.append((candidate.patient_id, _reason(candidate)))
    if candidates:
        return Outcome(kind=ASK, candidates=tuple(candidates), note=note,
                       why="more than one reading of who this is, so nothing "
                           "was chosen")

    # 6. Nothing to ask about. "unclear" still asks, for the name.
    if verdict.intent == UNCLEAR:
        return Outcome(kind=ASK, note=note, needs_name=True,
                       why="the dictation did not say clearly who it is about")
    return Outcome(kind=NEW, note=note,
                   why="no patient on the board matches this dictation")
