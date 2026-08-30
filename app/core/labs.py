"""Owns the critical-lab table (specs/sentinel-list.md v2 3) and every comparison.

This is the determinism rule of S3 in one file. The model reads a slip into a
schema - analyte, value, unit, the slip's own printed reference range, the
slip's own printed flag - and stops there. Every judgement after that happens
here, in code, against three sources in this order:

  1. the critical-value table below, which never moves;
  2. the doctor's target for this patient, if he set one;
  3. the doctor's recorded baseline for this patient, if he recorded one.

Two analytes are deliberately unjudgeable without the slip: troponin and
D-dimer have per-lab cutoffs, so they are read against the slip's own printed
reference or its printed flag, and returned as "cannot judge, pending doctor
review" when the slip shows neither. Nothing here ever invents a cutoff.

An analyte the model returns without a numeric value is "cannot judge" as well,
and so is an analyte this table has never heard of. Both directions of that
error are safe: they put the value in front of the doctor instead of grading it.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Optional, Sequence, Union

from . import sentinel

Level = Literal["critical", "above_target", "below_target", "normal",
                "cannot_judge", "urgent_review"]

CANNOT_JUDGE_NOTE = "cannot judge, pending doctor review"
# An analyte with no row in the table is not a failure to judge, it is a value
# the table has no opinion about. The doctor sees it either way; the wording is
# the difference between "I could not decide" and "this is not mine to decide".
NOT_IN_TABLE_NOTE = "not in the critical-value table, for your review"
# The third thing a value can be, added at S5. Not "normal", not "critical", and
# not the ordinary "I have no opinion": a value the table would have judged if
# it could read it. A haemoglobin printed in an unconvertible unit, a number the
# parser could not read on a row the lab itself flagged HH, an analyte nobody
# has a row for that the lab called critical. Each of those is a value that may
# be an emergency and cannot be shown as anything else, so it goes in front of
# the doctor as urgent rather than sitting in the ordinary yellow pile.
URGENT_REVIEW_NOTE = "cannot be judged in code, URGENT doctor review"


@dataclass(frozen=True)
class LabRule:
    analyte: str
    unit: str
    low: Optional[float] = None      # critical below this
    high: Optional[float] = None     # critical above this
    needs_slip_cutoff: bool = False  # cutoff is printed on the slip, not here
    baseline_multiple: Optional[float] = None  # critical at N x the patient's own
    # A row the table escalates only when a second fact is present as well. The
    # pregnancy row is the only one: docs/SAFETY.md writes it as "positive with
    # abdominal pain reported in the same conversation (ectopic rule; needs
    # both)", and a positive test on its own is a result, not an emergency.
    two_factor: bool = False
    note: str = ""


CRITICAL_LABS: tuple[LabRule, ...] = (
    LabRule("K", "mmol/L", low=2.5, high=6.0),
    LabRule("Na", "mmol/L", low=120, high=160),
    LabRule("Glucose", "mg/dL", low=50, high=500),
    LabRule("Creatinine", "mg/dL", high=4, baseline_multiple=2,
            note="or >=2x the patient's baseline"),
    LabRule("Hb", "g/dL", low=7),
    LabRule("Troponin", "", needs_slip_cutoff=True, note="any positive/high flag"),
    LabRule("INR", "", high=5),
    LabRule("Platelets", "x10^3/uL", low=50),
    LabRule("D-dimer", "", needs_slip_cutoff=True, note="cutoff varies per lab"),
    LabRule("Calcium", "mg/dL", low=6.5, high=13),
    LabRule("WBC", "x10^3/uL", low=1.0, high=50),
    LabRule("Bilirubin (neonate)", "mg/dL", needs_slip_cutoff=True,
            note="any value the slip flags high"),
    LabRule("pH", "", low=7.2, high=7.6),
    LabRule("HCO3", "mmol/L", low=10),
    LabRule("Culture (blood/CSF)", "", needs_slip_cutoff=True,
            note="any growth the lab flags"),
    LabRule("Pregnancy test", "", needs_slip_cutoff=True, two_factor=True,
            note="positive AND abdominal pain reported = ectopic rule"),
    # LDL never escalates: it is a yellow "above target" card only.
    LabRule("LDL", "mg/dL", note="never escalates; above-target card only"),
)

# What a lab prints instead of the table's name. Matched after normalisation, so
# "Serum Potassium", "potassium (K+)" and "K +" all land on the same rule.
ALIASES: dict[str, str] = {
    "potassium": "K", "k+": "K", "kplus": "K", "potass": "K", "pot": "K",
    "sodium": "Na", "na+": "Na", "sod": "Na",
    "glucose fasting": "Glucose", "fasting glucose": "Glucose",
    "blood glucose": "Glucose", "blood sugar": "Glucose", "fbs": "Glucose",
    "rbs": "Glucose", "random blood sugar": "Glucose", "sugar": "Glucose",
    "creat": "Creatinine", "cr": "Creatinine", "creatinin": "Creatinine",
    "hemoglobin": "Hb", "haemoglobin": "Hb", "hgb": "Hb", "hb%": "Hb",
    "haemoglobin hb": "Hb", "hemoglobin hgb": "Hb",
    "troponin i": "Troponin", "troponin t": "Troponin", "hs troponin": "Troponin",
    "high sensitivity troponin": "Troponin", "trop": "Troponin",
    "hs tnt": "Troponin", "hs tni": "Troponin", "hstnt": "Troponin",
    "hstni": "Troponin", "troponin hs": "Troponin",
    "d dimer": "D-dimer", "ddimer": "D-dimer", "d-dimer quantitative": "D-dimer",
    "d dimer quantitative": "D-dimer",
    "plt": "Platelets", "platelet": "Platelets", "platelet count": "Platelets",
    "ca": "Calcium", "ca2+": "Calcium",
    "wbc count": "WBC", "white blood cells": "WBC", "white cell count": "WBC",
    "leukocytes": "WBC", "tlc": "WBC",
    "ldl c": "LDL", "ldl cholesterol": "LDL", "ldlc": "LDL",
    "bicarbonate": "HCO3", "hco3-": "HCO3", "co2": "HCO3",
    "ph": "pH",
    "bilirubin neonate": "Bilirubin (neonate)", "neonatal bilirubin":
        "Bilirubin (neonate)",
    "blood culture": "Culture (blood/CSF)", "csf culture": "Culture (blood/CSF)",
    "culture": "Culture (blood/CSF)",
    "pregnancy": "Pregnancy test", "bhcg": "Pregnancy test", "hcg": "Pregnancy test",
    "beta hcg": "Pregnancy test", "b hcg": "Pregnancy test",
    "beta hcg qualitative": "Pregnancy test",
}

# Words a lab prints in its own flag column when a value is out of range. A flag
# is only ever read for the cutoff-relative analytes; everywhere else the table
# above decides and the slip's opinion is recorded but not obeyed.
HIGH_FLAGS: tuple[str, ...] = (
    "h", "hh", "high", "elevated", "positive", "pos", "reactive", "abnormal",
    "critical", "panic", "detected", "مرتفع", "عالي", "ايجابي", "موجب",
)
LOW_FLAGS: tuple[str, ...] = ("l", "ll", "low", "منخفض", "قليل")

# S15 defect 2, found live and reproduced. The flag column is transcribed, and a
# transcription drops characters: three renders of the same beta hCG slip came
# back as "POSITIVE" once and "POSITIV" twice, and the truncated one fell out of
# the exact-membership list above and took the escalation with it. So a flag
# that is the BEGINNING of one of these words is read as that word, from this
# many characters on and no fewer. Five, because a three-letter head is a
# different word as often as it is a truncation, and because the words that are
# short enough to be swallowed that way ("h", "low", "pos") are already printed
# flags in their own right in the tables above: "pos" is high because the table
# says so, not because it starts "positive", and "posi", "hig" and "cri" decide
# nothing at all. The rule reads the tables, so it adds no word to them and
# nothing here is a new decision the doctor has not already seen frozen.
FLAG_PREFIX_MIN = 5

# The flags that mean "this row is out of range" no matter what the table can or
# cannot do with it. A row the parser could not read, or an analyte the table
# has never heard of, carrying one of these, is not filed quietly: it goes to
# urgent review (S5 red team, "parsing can hide the value").
URGENT_FLAGS: tuple[str, ...] = (
    "h", "hh", "l", "ll", "critical", "panic", "high", "low", "abnormal",
    "مرتفع", "منخفض",
)
# Two of those mean the lab itself called the row extreme. An analyte with no
# row in the table is escalated on these alone (spec item 13).
EXTREME_FLAGS: tuple[str, ...] = ("hh", "ll", "critical", "panic")

# --------------------------------------------------------------------------- #
# The second half of the ectopic rule
# --------------------------------------------------------------------------- #
# docs/SAFETY.md has always written the pregnancy row as two facts: a positive
# test AND abdominal pain reported in the same conversation. Until S11 the code
# read the first half only, so any positive test sent the emergency block to a
# patient whose only news was that she is pregnant. This is the second half, in
# code, on the same normalisation the sentinel uses (sentinel.normalize:
# diacritics stripped, Arabic letter variants unified, Franco spellings folded),
# so "بَطني بتوجعني", "batni bt wga3ny" and "lower abdominal cramps" are one
# concept. It is deliberately a token rule and not a phrase list, and the
# sentinel's own table is untouched by it: this concept escalates a lab row, it
# does not wake the doctor on its own.
#
# Tense is not read here. The sentinel stands its concept rules down on a
# resolved marker ("embare7", "went away") because a finished chest pain is not
# an emergency; abdominal pain in the last 48 hours next to a positive test is
# the ectopic question whether or not it has eased, so the marker is ignored and
# this rule fires on it.
ABDOMEN_WORDS: tuple[str, ...] = (
    "abdomen", "abdominal", "belly", "tummy", "stomach", "pelvic", "pelvis",
    "بطن", "بطني", "البطن", "بطنها", "الحوض", "حوضي", "جنبي",
    "معده", "معدتي", "المعده", "مصارين", "مصاريني", "السره", "سرتي",
    "batn", "batni", "beten", "batny", "7od",
    "ma3da", "ma3deti", "ma3dety", "masareen", "masarini",
)
PAIN_WORDS: tuple[str, ...] = (
    "pain", "ache*", "hurt*", "cramp*", "colic", "sore", "tender", "spasm*",
    "stabbing", "stabbed", "sharp", "burning",
    "وجع*", "واجع*", "بتوجع*", "بيوجع*", "الم", "الام", "مغص", "تقلص*",
    "بتقطع*", "بيقطع*", "قطع*", "طلق", "حرقان",
    "wag3*", "wga3*", "waga3*", "alam", "maghs", "mogs", "mag9",
    "bt2ata3*", "bt2ta3*", "bit2ata3*", "tal2",
)
# Phrases that name the concept on their own, so no second token is needed.
ABDOMINAL_PAIN_PHRASES: tuple[str, ...] = (
    "abdominal pain", "belly pain", "stomach pain", "stomach ache",
    "stomach cramps", "pelvic pain", "ectopic", "lower abdominal",
    "مغص", "حمل خارج الرحم", "وجع في بطني", "وجع بطن", "الم في البطن",
    "تحت السره", "maghs", "7aml barra el ra7m",
)
# A patient saying she does NOT have the pain must never complete the rule. The
# first pass matched an abdomen word and a pain word anywhere in the haystack,
# so "no abdominal pain" and "مفيش وجع في بطني" both fired and sent the
# emergency block to a woman who had just said the opposite. A negation within
# NEGATION_REACH tokens IN FRONT of a match stands that match down.
NEGATION_WORDS: tuple[str, ...] = (
    "no", "not", "never", "without", "none", "denies", "denied", "free",
    "مفيش", "مافيش", "ماعنديش", "معنديش", "مش", "لا", "بدون", "ولا",
    "mafish", "mafeesh", "mesh", "ma3andish", "ma3andesh", "bidoun", "bedoun",
)
# How far in front of a match a negation still reaches, and how far apart the
# abdomen word and the pain word may stand and still be one report. Both are
# counted in tokens, not characters, so they read the same in three scripts.
NEGATION_REACH = 3
PROXIMITY_TOKENS = 6


def _stem_forms(words: tuple[str, ...]) -> tuple[tuple[str, bool], ...]:
    """Rule tokens -> (comparison form, is it a stem?), normalized."""
    out: list[tuple[str, bool]] = []
    for word in words:
        stem = word.endswith("*")
        body = sentinel.normalize(word[:-1] if stem else word).strip()
        if body:
            out.append((body, stem))
    return tuple(out)


_ABDOMEN = _stem_forms(ABDOMEN_WORDS)
_PAIN = _stem_forms(PAIN_WORDS)
_NEGATIONS = frozenset(
    form for form in (sentinel.normalize(w).strip() for w in NEGATION_WORDS) if form
)
# Phrases are compared token by token, so a negation in front of a phrase is
# read the same way as a negation in front of a single word.
_ABDOMINAL_PHRASES = tuple(
    tuple(form.split())
    for form in (sentinel.normalize(p).strip() for p in ABDOMINAL_PAIN_PHRASES)
    if form
)

# The two things the doctor's card can say when a positive test did not complete
# the rule, and they are not the same sentence. Reporting a search that was never
# made is the error Codex item 3 was about, and it does not get to come back on a
# different card: if no context reached this module, the card says so.
PREGNANCY_NOT_CHECKED_NOTE = (
    "positive test, abdominal pain: not checked (no patient messages were "
    "searched): the ectopic rule needs both, doctor review"
)
PREGNANCY_NONE_FOUND_NOTE = (
    "positive test, abdominal pain: none found in the last 48 hours: "
    "the ectopic rule needs both, doctor review"
)


def _matches(token: str, forms: tuple[tuple[str, bool], ...]) -> bool:
    """Does this token carry one of these rule forms?

    A stem matches at the START of a token and never inside it, so "headache" is
    not an "ache" and "stable" is not a "stab". Egyptian Arabic glues the
    conjunction on the front of a word, so a leading "و" is allowed to stand in
    front of any form: "وبطني" is "بطني".
    """
    for body, stem in forms:
        for candidate in (token, token[1:] if token[:1] == "و" else ""):
            if not candidate:
                continue
            if candidate.startswith(body) if stem else candidate == body:
                return True
    return False


def _negated(tokens: list[str], index: int) -> bool:
    """Is there a negation within reach in front of this position?"""
    return any(tokens[j] in _NEGATIONS
               for j in range(max(0, index - NEGATION_REACH), index))


def abdominal_pain(context: Optional[Union[str, Iterable[str]]]) -> bool:
    """Does anything in this context report abdominal pain?

    `context` is the patient's own words: the caption under the slip and the
    messages from the last 48 hours, in any order, in any of the three ways an
    Egyptian patient writes. One string or several, it is all one haystack.

    Three things have to line up before this is True: a phrase that names the
    concept on its own, or an abdomen word and a pain word within
    PROXIMITY_TOKENS of each other; and no negation standing in front of the
    match. Tense is still not read, and that is deliberate: pain in the last 48
    hours next to a positive test is the ectopic question whether or not it has
    eased. A negation is not a tense, it is the patient saying the fact is not
    there at all, and that is read.
    """
    if context is None:
        return False
    parts = [context] if isinstance(context, str) else [str(c) for c in context]
    tokens = sentinel.normalize(" ".join(p for p in parts if p)).split()
    if not tokens:
        return False

    for phrase in _ABDOMINAL_PHRASES:
        span = len(phrase)
        for start in range(0, len(tokens) - span + 1):
            if tuple(tokens[start:start + span]) != phrase:
                continue
            if not _negated(tokens, start):
                return True

    abdomen = [i for i, t in enumerate(tokens)
               if _matches(t, _ABDOMEN) and not _negated(tokens, i)]
    if not abdomen:
        return False
    pain = [i for i, t in enumerate(tokens)
            if _matches(t, _PAIN) and not _negated(tokens, i)]
    return any(abs(a - b) <= PROXIMITY_TOKENS for a in abdomen for b in pain)


def context_searched(context: Optional[Union[str, Iterable[str]]]) -> bool:
    """Was there anything to search, or did nobody hand this module a context?

    None means the caller never looked. An empty string or an empty list means
    it looked and the patient had said nothing in the window. The doctor's card
    tells those two apart, because "not checked" and "none found" are different
    facts about his patient.
    """
    return context is not None


# Prefixes a lab puts in front of an analyte name that carry no meaning for us.
_STRIP_PREFIXES = ("serum ", "plasma ", "blood ", "total ", "s ", "p ")


def _key(name: str) -> str:
    """Analyte name as written anywhere -> a comparable key.

    Everything a lab decorates a name with goes: case, the unit in brackets, the
    "Serum"/"Plasma" prefix, punctuation. "Serum Potassium (K+)" and "potassium"
    end up as the same key, which is how the table finds its row.
    """
    text = (name or "").strip().lower()
    text = re.sub(r"\(.*?\)", " ", text)          # drop "(mg/dL)", "(fasting)"
    text = re.sub(r"[^a-z0-9؀-ۿ+%]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for prefix in _STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


BY_ANALYTE: dict[str, LabRule] = {_key(rule.analyte): rule for rule in CRITICAL_LABS}


# --------------------------------------------------------------------------- #
# Which panel an analyte belongs to
# --------------------------------------------------------------------------- #
# S4 review, carry-over 1. A doctor names a test loop in his own words ("Kidney
# function tests"); a slip names analytes ("Potassium (K+)"). With two tests
# open, a slip used to attach to whichever loop was opened first, so a
# potassium result landed on a lipid panel. These are the words a panel is
# ordinarily called by, seen from one of its analytes, and core/photos.py uses
# them to pick the loop a slip actually answers.
#
# This is a matching aid and nothing more: it never affects whether a value is
# critical, only which open loop the result is filed against. Nothing here can
# change what the table above decides.
PANEL_WORDS: dict[str, tuple[str, ...]] = {
    "K": ("electrolyte", "electrolytes", "kidney", "renal", "kft", "salts"),
    "Na": ("electrolyte", "electrolytes", "kidney", "renal", "kft", "salts"),
    "Creatinine": ("kidney", "renal", "kft", "chemistry"),
    "Glucose": ("sugar", "diabetes", "diabetic", "fbs", "chemistry"),
    "Hb": ("cbc", "haemoglobin", "hemoglobin", "anaemia", "anemia", "count"),
    "Platelets": ("cbc", "platelet", "count"),
    "WBC": ("cbc", "white", "leukocytes", "infection", "count"),
    "LDL": ("lipid", "lipids", "cholesterol"),
    "Troponin": ("cardiac", "enzymes", "heart"),
    "D-dimer": ("dimer", "clot", "thrombosis"),
    "INR": ("coagulation", "clotting", "warfarin", "pt"),
    "Calcium": ("bone", "chemistry"),
    "HCO3": ("bicarbonate", "gas", "gases", "abg"),
    "pH": ("gas", "gases", "abg"),
}

# The analytes with no row in the critical table still belong to a panel, and a
# slip is mostly made of them. Keyed by a word in the analyte's own name.
EXTRA_PANEL_WORDS: dict[str, tuple[str, ...]] = {
    "cholesterol": ("lipid", "lipids"),
    "triglycerides": ("lipid", "lipids"),
    "hdl": ("lipid", "lipids", "cholesterol"),
    "vldl": ("lipid", "lipids", "cholesterol"),
    "urea": ("kidney", "renal", "kft"),
    "bun": ("kidney", "renal", "kft"),
    "uric": ("kidney", "renal", "gout"),
    "egfr": ("kidney", "renal", "kft"),
    "tsh": ("thyroid",),
    "t3": ("thyroid",),
    "t4": ("thyroid",),
    "hba1c": ("sugar", "diabetes", "diabetic", "glycated"),
    "alt": ("liver", "lft"),
    "ast": ("liver", "lft"),
    "sgpt": ("liver", "lft"),
    "sgot": ("liver", "lft"),
    "albumin": ("liver", "lft", "protein"),
    "esr": ("inflammation",),
    "crp": ("inflammation", "infection"),
    "chloride": ("electrolyte", "electrolytes", "salts"),
    "magnesium": ("electrolyte", "electrolytes", "salts"),
    "phosphorus": ("bone", "chemistry"),
}


def panel_words(analyte: str) -> set[str]:
    """Every word this analyte could be looked for under, its own name included.

    "Potassium (K+)" -> {"potassium", "electrolyte", "electrolytes", "kidney",
    "renal", "kft", "salts"}. Used only to choose between open loops.
    """
    words = set(_key(analyte).split())
    rule = rule_for(analyte)
    if rule is not None:
        words |= set(PANEL_WORDS.get(rule.analyte, ()))
    for word in list(words):
        words |= set(EXTRA_PANEL_WORDS.get(word, ()))
    return words


# What a panel is ordinarily made of, seen from the doctor's own word for it.
# The inverse of PANEL_WORDS above, and the reason it exists separately: a slip
# is matched to a loop analyte by analyte since S6, not word by word. The old
# rule counted how many words the two names shared, which meant "Kidney
# function tests" and a potassium row overlapped through the word "kidney" and
# a lab that printed nothing but "K" and "Na" overlapped nothing at all.
#
# This is a matching and completeness aid only. Nothing here can change what
# the critical-value table decides about a value.
PANEL_ANALYTES: dict[str, tuple[str, ...]] = {
    "lipid": ("Total cholesterol", "Triglycerides", "HDL", "LDL"),
    "lipids": ("Total cholesterol", "Triglycerides", "HDL", "LDL"),
    "cholesterol": ("Total cholesterol", "Triglycerides", "HDL", "LDL"),
    "kidney": ("Urea", "Creatinine", "Sodium", "Potassium"),
    "renal": ("Urea", "Creatinine", "Sodium", "Potassium"),
    "kft": ("Urea", "Creatinine", "Sodium", "Potassium"),
    "electrolyte": ("Sodium", "Potassium", "Chloride", "Bicarbonate"),
    "electrolytes": ("Sodium", "Potassium", "Chloride", "Bicarbonate"),
    "salts": ("Sodium", "Potassium", "Chloride", "Bicarbonate"),
    "cbc": ("Hb", "WBC", "Platelets"),
    "count": ("Hb", "WBC", "Platelets"),
    "thyroid": ("TSH", "T3", "T4"),
    "liver": ("ALT", "AST", "Albumin", "Bilirubin"),
    "lft": ("ALT", "AST", "Albumin", "Bilirubin"),
    "sugar": ("Glucose", "HbA1c"),
    "diabetes": ("Glucose", "HbA1c"),
    "diabetic": ("Glucose", "HbA1c"),
    "glycated": ("HbA1c",),
    "fbs": ("Glucose",),
    "coagulation": ("INR",),
    "clotting": ("INR",),
    "warfarin": ("INR",),
    "gas": ("pH", "HCO3"),
    "gases": ("pH", "HCO3"),
    "abg": ("pH", "HCO3"),
    "cardiac": ("Troponin",),
    "enzymes": ("Troponin",),
    "dimer": ("D-dimer",),
    "bone": ("Calcium", "Phosphorus"),
    "inflammation": ("CRP", "ESR"),
}

# How an analyte with no table row is written back to a doctor or a patient.
DISPLAY: dict[str, str] = {
    "hba1c": "HbA1c", "ldl": "LDL", "hdl": "HDL", "vldl": "VLDL", "tsh": "TSH",
    "t3": "T3", "t4": "T4", "alt": "ALT", "ast": "AST", "sgpt": "SGPT",
    "sgot": "SGOT", "crp": "CRP", "esr": "ESR", "inr": "INR", "wbc": "WBC",
    "hb": "Hb", "egfr": "eGFR", "bun": "BUN", "ph": "pH", "hco3": "HCO3",
    "uric": "Uric acid",
}


def canonical(analyte: str) -> str:
    """One analyte name -> the one string two spellings of it both become.

    "Serum Potassium (K+)" and "K" are both "K"; "HDL Cholesterol" and "HDL" are
    both "hdl". Used to compare a slip with what a contract asked for, never to
    decide anything clinical.
    """
    rule = rule_for(analyte)
    if rule is not None:
        return rule.analyte
    key = _key(analyte)
    if key in EXTRA_PANEL_WORDS:
        return key
    for word in key.split():
        if word in EXTRA_PANEL_WORDS:
            return word
    return key


# What separates two analytes in one string. A comma, a semicolon, a slash, an
# Arabic comma, or the word "and" with spaces around it. Deliberately NOT the
# plus sign or the ampersand: a slip prints potassium as "K+" and sodium as
# "Na+", and splitting those in half would turn one analyte into two.
_ANALYTE_SPLIT = re.compile(r"\s*(?:,|;|/|،|\band\b|\+\s*and\b)\s*", re.IGNORECASE)


def named_analytes(value: str) -> list[str]:
    """How many analytes one string names, and which.

    "Triglycerides" is one. "Triglycerides, HDL" is two, and that is the point:
    the model called request_missing_evidence with both on rev sanad-00015-p6x,
    the guard let it through because something was named, and the patient got
    "I have your result but Triglycerides, HDL is missing", which agrees in
    neither language. The guard asks this function now (core/policy.py).
    """
    return [part.strip() for part in _ANALYTE_SPLIT.split(value or "")
            if part.strip()]


# The table is written in the short names a slip prints. A patient being asked
# for a missing part is not, so a handful of them are spelled out.
READABLE: dict[str, str] = {
    "K": "Potassium", "Na": "Sodium", "Hb": "Haemoglobin", "HCO3": "Bicarbonate",
    "WBC": "White cell count", "Platelets": "Platelets",
}


def display(analyte: str) -> str:
    """The analyte's name as it should be written to a person."""
    rule = rule_for(analyte)
    if rule is not None:
        return READABLE.get(rule.analyte, rule.analyte)
    key = _key(analyte)
    return DISPLAY.get(key, analyte.strip() or key)


def panel_analytes(test_name: str) -> tuple[str, ...]:
    """The doctor's own words for a test -> the analytes it ought to contain.

    "Lipid panel" is four analytes; "potassium and sodium" is two; "Vitamin D"
    is nothing this table knows, and nothing is what it returns, because a panel
    it has never heard of has no missing part it could name.
    """
    out: list[str] = []
    for word in _key(test_name).split():
        for analyte in PANEL_ANALYTES.get(word, ()):
            if analyte not in out:
                out.append(analyte)
        if word in PANEL_ANALYTES:
            continue
        if rule_for(word) is not None or word in EXTRA_PANEL_WORDS:
            named = display(word) if (word in DISPLAY or rule_for(word)) else word.title()
            if named not in out:
                out.append(named)
    return tuple(out)


ARABIC_TEST_NAMES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("glucose", "tolerance"), "اختبار تحمل الجلوكوز"),
    (("glycated",), "تحليل السكر التراكمي"),
    (("hba1c",), "تحليل السكر التراكمي"),
    (("lipid",), "تحليل الدهون"),
    (("cholesterol",), "تحليل الدهون"),
    (("kidney",), "تحاليل وظائف الكلى"),
    (("renal",), "تحاليل وظائف الكلى"),
    (("kft",), "تحاليل وظائف الكلى"),
    (("liver",), "تحاليل وظائف الكبد"),
    (("lft",), "تحاليل وظائف الكبد"),
    (("thyroid",), "تحاليل وظائف الغدة الدرقية"),
    (("cbc",), "صورة دم كاملة"),
    (("blood", "count"), "صورة دم كاملة"),
    (("pregnancy",), "تحليل الحمل بالدم"),
    (("beta", "hcg"), "تحليل الحمل بالدم"),
    (("potassium",), "تحليل البوتاسيوم"),
    (("sodium",), "تحليل الصوديوم"),
    (("creatinine",), "تحليل الكرياتينين"),
    (("inr",), "تحليل سيولة الدم"),
)


def arabic_test_name(test_name: str) -> str:
    """A patient-facing Arabic label for a known test, never a translation model."""
    words = set(_key(test_name).split())
    for required, translated in ARABIC_TEST_NAMES:
        if set(required).issubset(words):
            return translated
    return "التحليل المطلوب"


def panel_overlap(test_name: str, analytes: Sequence[str]) -> int:
    """How many of a slip's analytes belong to the panel the doctor ordered.

    Analyte level since S6: the slip's rows are canonicalised and counted
    against the analytes that panel is made of. Zero means this slip says
    nothing about that loop. The number itself is only ever compared with
    another loop's, never with a threshold.
    """
    wanted = {canonical(a) for a in panel_analytes(test_name)}
    if not wanted:
        return 0
    return sum(1 for a in analytes if str(a).strip() and canonical(a) in wanted)


def rule_for(analyte: str) -> Optional[LabRule]:
    """The table row for an analyte, through its aliases. None means unknown."""
    key = _key(analyte)
    if key in BY_ANALYTE:
        return BY_ANALYTE[key]
    canonical = ALIASES.get(key)
    if canonical:
        return BY_ANALYTE[_key(canonical)]
    tight = key.replace(" ", "")
    for candidate, rule in BY_ANALYTE.items():
        if candidate.replace(" ", "") == tight:
            return rule
    return None


_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_VALUE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def parse_value(text: str | float | None) -> Optional[float]:
    """A printed value -> a number, or None when it is not one.

    "6.4", "6,4", "<0.01", "160 mg/dL", "6.0E1", "12,500" and "٦٫٤" are all
    numbers. "positive", "see comment" and "" are not, and an analyte with no
    number is never graded.

    The exponent is the S5 red team's third lab bypass: a slip printing "6.0E1"
    was read as 6.0, so a white count of sixty thousand was filed as normal.
    Scientific notation is now read as what it says, and a "<" or ">" prefix is
    dropped rather than turning the value into a negative number.
    """
    if isinstance(text, (int, float)):
        return float(text)
    if not text:
        return None
    cleaned = str(text).strip().translate(_ARABIC_INDIC)
    cleaned = cleaned.replace("٫", ".").replace("−", "-")
    cleaned = re.sub(r"^[<>≤≥]\s*", "", cleaned)          # "<0.01" is 0.01
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(\D|$))", "", cleaned)  # 12,500
    cleaned = cleaned.replace(",", ".")
    match = _VALUE.search(cleaned)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Units: the table judges in its own unit and nothing else
# --------------------------------------------------------------------------- #
# The red team sent a haemoglobin of 60 g/L. In g/dL, the unit the table is
# written in, that is 6.0, which is below the critical floor of 7; read as a
# bare number it is 60, which is above every cutoff there is, so the slip came
# back "in range". A value is now converted into the table's unit before it is
# compared, and a unit that cannot be converted is not guessed at: it is
# urgent review.
#
# Keyed by (the table's own analyte name, the printed unit as written). The
# factor multiplies the printed value to give the table's unit.
UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    ("Hb", "g/l"): 0.1,                 # 60 g/L  = 6.0 g/dL
    ("Hb", "gm/l"): 0.1,
    ("Hb", "mmol/l"): 1.611,            # SI haemoglobin, x16.11 g/L
    ("Glucose", "mmol/l"): 18.0182,     # 3 mmol/L = 54 mg/dL
    ("Creatinine", "umol/l"): 0.011312,  # 88.4 umol/L = 1.0 mg/dL
    ("Creatinine", "µmol/l"): 0.011312,
    ("Calcium", "mmol/l"): 4.008,       # 2.5 mmol/L = 10 mg/dL
    ("K", "meq/l"): 1.0,                # mEq/L and mmol/L are the same number
    ("Na", "meq/l"): 1.0,
    ("HCO3", "meq/l"): 1.0,
    ("WBC", "x10^9/l"): 1.0,            # 10^9/L and 10^3/uL are the same number
    ("WBC", "10^9/l"): 1.0,
    ("WBC", "/ul"): 0.001,
    ("WBC", "cells/ul"): 0.001,
    ("Platelets", "x10^9/l"): 1.0,
    ("Platelets", "10^9/l"): 1.0,
    ("Platelets", "/ul"): 0.001,
    ("Platelets", "x10^3/l"): 1.0,
}


def unit_key(unit: str | None) -> str:
    """A printed unit -> the form the conversion table is keyed by."""
    text = (unit or "").strip().lower().replace(" ", "")
    text = text.replace("µ", "u").replace("μ", "u")
    text = text.replace("litre", "l").replace("liter", "l")
    text = text.replace("×", "x").replace("**", "^")
    return text


def in_table_units(rule: LabRule, value: Optional[float], unit: str | None
                   ) -> tuple[Optional[float], bool]:
    """(value in the table's unit, was it convertible?).

    An empty unit is taken as the table's own, which is how every slip that
    prints no unit column has always been read. A unit that matches the table's
    is used as printed. Anything else has to be in UNIT_CONVERSIONS or the
    answer is "cannot convert", and the caller escalates instead of comparing.
    """
    if value is None:
        return None, True
    printed, canonical = unit_key(unit), unit_key(rule.unit)
    if not printed or printed == canonical:
        return value, True
    factor = UNIT_CONVERSIONS.get((rule.analyte, printed))
    if factor is None:
        return None, False
    return value * factor, True


def parse_range(text: str | None) -> tuple[Optional[float], Optional[float]]:
    """The slip's printed reference range -> (low, high).

    Handles "3.5 - 5.1", "70-100 mg/dL", "< 0.04", "up to 500", "> 12".
    Anything it cannot read returns (None, None), which means cannot judge.
    """
    if not text:
        return (None, None)
    cleaned = str(text).replace(",", ".").replace("–", "-").replace("—", "-")
    pair = re.search(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", cleaned)
    if pair:
        return (float(pair.group(1)), float(pair.group(2)))
    upper = re.search(r"(?:<|<=|less than|up to|upto)\s*(-?\d+(?:\.\d+)?)", cleaned,
                      re.IGNORECASE)
    if upper:
        return (None, float(upper.group(1)))
    lower = re.search(r"(?:>|>=|more than|over)\s*(-?\d+(?:\.\d+)?)", cleaned,
                      re.IGNORECASE)
    if lower:
        return (float(lower.group(1)), None)
    return (None, None)


def _flag_says(token: str, words: tuple[str, ...]) -> bool:
    """Is this folded flag token one of these words, whole or truncated?

    Whole first: the flag IS the word, or the flag starts with the word and a
    space ("H (high)"). Then the truncation rule above, which is the other
    direction: the flag is the start of the word, and long enough to be sure of
    it. Nothing else is read.
    """
    if not token:
        return False
    if any(token == word or token.startswith(word + " ") for word in words):
        return True
    return len(token) >= FLAG_PREFIX_MIN and any(
        word.startswith(token) for word in words
    )


def flag_is_high(flag: str | None) -> bool:
    """True when the lab itself printed a high/positive marker."""
    return _flag_says(_key(flag or ""), HIGH_FLAGS)


def flag_is_low(flag: str | None) -> bool:
    return _flag_says(_key(flag or ""), LOW_FLAGS)


def flag_in(flag: str | None, words: tuple[str, ...]) -> bool:
    """Is the slip's own flag one of these? Matched on the whole flag token."""
    token = _key(flag or "")
    return bool(token) and any(
        token == word or token.startswith(word + " ") for word in words
    )


def judge(
    analyte: str,
    value: Optional[float],
    target: Optional[float] = None,
    *,
    baseline: Optional[float] = None,
    ref_range: Optional[str] = None,
    flag: Optional[str] = None,
    unit: Optional[str] = None,
    context: Optional[Union[str, Iterable[str]]] = None,
) -> Level:
    """Analyte + value + unit -> level. Nothing here ever guesses.

    `ref_range` and `flag` are the slip's own printing, used only where the table
    says the cutoff belongs to the lab (troponin, D-dimer, neonatal bilirubin,
    cultures, pregnancy tests) and, since S5, to decide that a value the table
    could not read is urgent rather than quiet.

    `unit` is the unit the slip printed. The table is written in one unit per
    analyte, so the value is converted into it before any comparison; a unit
    with no conversion is "urgent_review", never a comparison against the wrong
    scale.

    `context` is the patient's own words around the slip: the caption and the
    messages from the last 48 hours. It is read by exactly one row, the
    two-factor pregnancy row, and only ever to add the second half of a rule
    docs/SAFETY.md has always written as needing both halves.
    """
    rule = rule_for(analyte)
    if rule is None:
        # No row for this analyte. If the lab itself called the row extreme, the
        # absence of a row is not a reason to stay quiet about it.
        return "urgent_review" if flag_in(flag, EXTREME_FLAGS) else "cannot_judge"

    if rule.needs_slip_cutoff:
        # The slip's word first: a lab that printed "positive" or "H" has already
        # compared the value with its own cutoff, which is the only one there is.
        _, high = parse_range(ref_range)
        positive = flag_is_high(flag) or (
            value is not None and high is not None and value > high
        )
        if not positive:
            return "cannot_judge"
        if rule.two_factor:
            # The ectopic rule, both halves. A positive test on its own is a
            # result the doctor has to read tonight (urgent_review: doctor card,
            # no emergency block to the patient). It becomes an emergency only
            # when the patient's own words in the same conversation report
            # abdominal pain.
            return "critical" if abdominal_pain(context) else "urgent_review"
        return "critical"

    if value is None:
        # A row whose number could not be read is ordinarily "cannot judge". If
        # the lab flagged that same row, the number that could not be read was
        # an abnormal one, and that goes in front of the doctor as urgent.
        return "urgent_review" if flag_in(flag, URGENT_FLAGS) else "cannot_judge"

    value, convertible = in_table_units(rule, value, unit)
    if not convertible or value is None:
        return "urgent_review"

    if rule.analyte == "LDL":
        if target is None:
            return "normal"
        return "above_target" if value > target else "normal"

    if rule.low is not None and value < rule.low:
        return "critical"
    if rule.high is not None and value > rule.high:
        return "critical"
    if (
        rule.baseline_multiple is not None
        and baseline is not None
        and baseline > 0
        and value >= rule.baseline_multiple * baseline
    ):
        return "critical"
    if target is not None:
        if value > target:
            return "above_target"
        if value < target:
            return "below_target"
    return "normal"


# --------------------------------------------------------------------------- #
# The comparison the doctor's card is built from
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    """One analyte, judged. `line` is the sentence the doctor's card prints."""

    analyte: str
    printed: str          # the value exactly as the slip printed it
    value: Optional[float]
    unit: str
    ref_range: str
    flag: str
    level: Level
    target: Optional[float]
    baseline: Optional[float]
    line: str

    @property
    def critical(self) -> bool:
        return self.level == "critical"

    @property
    def urgent(self) -> bool:
        """A value the table could not judge and will not leave in the pile."""
        return self.level == "urgent_review"


def _lookup(book: dict[str, str], analyte: str) -> Optional[float]:
    """A doctor's target/baseline dict is keyed by whatever he dictated."""
    key, rule = _key(analyte), rule_for(analyte)
    for name, raw in (book or {}).items():
        if _key(name) == key or (rule is not None and rule_for(name) is rule):
            return parse_value(raw)
    return None


def _line(analyte: str, printed: str, unit: str, level: Level,
          target: Optional[float], baseline: Optional[float], rule: Optional[LabRule],
          ref_range: str, flag: str = "", searched: bool = False) -> str:
    head = f"{analyte} {printed}{(' ' + unit) if unit else ''}".strip()
    if level == "critical":
        bound = ""
        if rule is not None and not rule.needs_slip_cutoff:
            if rule.low is not None and rule.high is not None:
                bound = f" (critical outside {rule.low}-{rule.high})"
            elif rule.high is not None:
                bound = f" (critical above {rule.high})"
            elif rule.low is not None:
                bound = f" (critical below {rule.low})"
        elif ref_range:
            bound = f" (lab reference {ref_range})"
        return f"{head} · CRITICAL{bound}"
    if level in ("above_target", "below_target") and target is not None:
        return f"{head}, target {_short(target)}, {level.replace('_', ' ')}"
    if level == "urgent_review":
        if rule is not None and rule.two_factor:
            note = (PREGNANCY_NONE_FOUND_NOTE if searched
                    else PREGNANCY_NOT_CHECKED_NOTE)
            return f"{head} · {note}"
        why = (f"unit {unit!r} cannot be converted to {rule.unit}"
               if rule is not None and unit and rule.unit else "flagged by the lab")
        return f"{head} · {URGENT_REVIEW_NOTE} ({why})"
    if level == "cannot_judge":
        return f"{head} · {CANNOT_JUDGE_NOTE if rule else NOT_IN_TABLE_NOTE}"
    if baseline is not None:
        return f"{head}, baseline {_short(baseline)}"
    value = parse_value(printed)
    low, high = parse_range(ref_range)
    if flag_is_high(flag):
        return (f"{head}, above the lab's reference ({ref_range})" if ref_range
                else f"{head}, flagged {flag.strip()} by the lab")
    if flag_is_low(flag):
        return (f"{head}, below the lab's reference ({ref_range})" if ref_range
                else f"{head}, flagged {flag.strip()} by the lab")
    if value is not None and (low is not None or high is not None):
        if low is not None and value < low:
            return f"{head}, below the lab's reference ({ref_range})"
        if high is not None and value > high:
            return f"{head}, above the lab's reference ({ref_range})"
        return f"{head}, in range (lab reference {ref_range})"
    return f"{head}, no printed range to compare"


def _short(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def assess(
    analytes: list[dict],
    targets: Optional[dict[str, str]] = None,
    baseline: Optional[dict[str, str]] = None,
    *,
    context: Optional[Union[str, Iterable[str]]] = None,
) -> list[Finding]:
    """The whole slip, judged in code. The only place a lab value is graded.

    `analytes` is what the extractor got out of the photo: dicts carrying
    analyte, value, unit, ref_range and flag, all as strings, all as printed.

    `context` is the patient's own words around this slip: the caption and the
    messages of the last 48 hours. Only the two-factor pregnancy row reads it,
    and only to complete a rule that needs two facts. Passing nothing is safe by
    construction: the missing half means urgent review, never a quiet pass and
    never an emergency block on one fact. Passing nothing is also SAID: the card
    reads "abdominal pain: not checked" when no context arrived and "none found
    in the last 48 hours" when one did, because a search that was not made is
    not a search that came back empty.
    """
    findings: list[Finding] = []
    searched = context_searched(context)
    for row in analytes:
        analyte = (row.get("analyte") or "").strip()
        if not analyte:
            continue
        printed = (row.get("value") or "").strip()
        unit = (row.get("unit") or "").strip()
        ref_range = (row.get("ref_range") or "").strip()
        flag = (row.get("flag") or "").strip()
        value = parse_value(printed)
        target = _lookup(targets or {}, analyte)
        base = _lookup(baseline or {}, analyte)
        level = judge(analyte, value, target, baseline=base,
                      ref_range=ref_range, flag=flag, unit=unit, context=context)
        findings.append(
            Finding(
                analyte=analyte,
                printed=printed or "(no value)",
                value=value,
                unit=unit,
                ref_range=ref_range,
                flag=flag,
                level=level,
                target=target,
                baseline=base,
                line=_line(analyte, printed or "(no value)", unit, level, target,
                           base, rule_for(analyte), ref_range, flag, searched),
            )
        )
    return findings


def criticals(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.critical]


def urgents(findings: list[Finding]) -> list[Finding]:
    """The rows the table could not judge and the doctor has to see tonight."""
    return [f for f in findings if f.urgent]
