"""Owns one question: which obligation does this piece of evidence answer?

Until S24 that question was answered by a table. `core/photos.route` turned a
class ("lab slip") and a boolean ("is a TEST loop open") into a route, and
`core/photos.open_test_loop` picked the loop by counting shared words between
the slip's analytes and the doctor's own test name. Both are good tables and
neither is going anywhere: they are still what runs when this module cannot.

What a table cannot do is read the rest of the page. A slip photographed on top
of yesterday's slip, a prescription that also carries three analytes, a result
collected before the order that belongs to the visit before this one, a second
page of a panel whose first page already attached: each of those is a
disposition, not a classification, and the facts that settle it are on the
paper and on the board at the same time.

So the DECISION is re-homed here, under one bounded Gemini turn, and nothing
else moves:

  code   builds the facts. What the picture might be (the candidate kinds), which
         loops are open and what each one's contract asked for, the name and the
         date printed on the page, the analytes read off it, and - written down
         in the same block - the route the table would have chosen.
  model  chooses ONE disposition from that block: a kind out of the candidates,
         and either one of the offered loops or none at all.
  code   refuses anything that is not on the list, recomputes the route from the
         same `core/photos.route` table it always used, and computes what is
         missing with `core/verify.missing_analytes`. The refusal is printed on
         the packet, not swallowed.

Three rails hold whatever the model says:

  1. The Sentinel is upstream of this whole file (core/concierge.py gate 1). An
     emergency in the caption ends the turn before a photograph is ever read,
     and this module cannot be reached from that branch.
  2. `core/verify.check` runs DOWNSTREAM, in core/extractor.py, unchanged. The
     identity, date and completeness verdicts are code, they are made after
     this disposition, and they overrule it: a slip the orchestrator attached to
     a loop still detaches when the printed name is somebody else's, and the
     doctor still gets the identity-mismatch card with no attach button on it.
     This module cannot approve an attachment; it can only nominate one.
  3. A model that is down, slow, or that answers with something not on the list
     is not an outage of the photo path. `decide` returns None, and None means
     the caller does exactly what it did before this file existed, byte for
     byte: same route, same event, same label, and no packet.

The Evidence Packet is what the decision leaves behind. It is event metadata,
appended by the existing `events.append_event` on the routing event that was
already being written, and it exists only when the orchestrator actually
decided. A fail-open turn writes no packet, because there is nothing to
attribute.

No I/O of its own: nothing here reads or writes Firestore, sends a message, or
touches a loop. It is given facts and it returns a disposition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field

from . import bounded, photos, verify
from .models import EvidencePacket, Loop, PhotoKind, PhotoReading

log = logging.getLogger("sanad.evidence")

# Seconds. One turn of one structured decision, on the lane a patient is
# waiting on. It is the scale of the other decision turns (core/bounded.py
# TRIAGE and VOTE are both 12s) and it is a hang-stop, not a latency budget: a
# real turn on this workload lands in a second. It is declared here rather than
# in core/bounded.py's table only because that file is outside this change's
# allowlist; it belongs in the table beside the others.
DEADLINE = 12.0

# rev 18 item 3 taxonomy: a label carrying both "model" and "code" is bucketed
# MODEL CHOICE · CODE GUARDS, which is exactly what this is. The count that has
# to stay zero is "decided by a model alone", and no path here can produce one,
# because the route is recomputed by the table and every gate is downstream.
# core/extractor.py repeats this string as its own module constant so the label
# it writes is readable by the audit rail; tests/test_s24_evidence.py asserts
# the two cannot drift apart.
DECIDED_BY = ("evidence-orchestrator (gemini) + gates: model choice, "
              "guards in code (core/photos.py, core/verify.py, core/labs.py)")

# Which loop type each kind of picture can possibly answer. A lab slip cannot
# be filed on a blood-pressure chart and a monitor screen cannot close a lab
# order, whatever anybody proposes.
LOOP_TYPE_FOR: dict[str, str] = {"lab_slip": "TEST", "bp_monitor": "MONITOR"}

OPEN_STATES_FOR: dict[str, tuple[str, ...]] = {
    "TEST": photos.OPEN_TEST_STATES,
    "MONITOR": photos.OPEN_MONITOR_STATES,
}


# --------------------------------------------------------------------------- #
# The facts, built in code
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Offer:
    """One loop the orchestrator is allowed to choose, and its contract.

    The list of offers is the guard. It is built from this patient's own loops,
    filtered to the states that still accept evidence, so a closed loop, a loop
    on somebody else's record and an invented id are all the same refusal: not
    on the list.
    """

    id: str
    type: str
    title: str
    test_name: str
    state: str
    required: tuple[str, ...] = ()
    ordered_on: str = ""

    def as_line(self) -> str:
        bits = [f"id={self.id}", f"type={self.type}", f"title={self.title}"]
        if self.test_name and self.test_name != self.title:
            bits.append(f"test={self.test_name}")
        bits.append(f"state={self.state}")
        if self.required:
            bits.append("asked for=" + ", ".join(self.required))
        if self.ordered_on:
            bits.append(f"ordered_on={self.ordered_on}")
        return " | ".join(bits)


@dataclass(frozen=True)
class Facts:
    """Everything the turn is allowed to see, and the route code would take."""

    patient_name: str
    printed_name: str
    printed_date: str
    analytes: tuple[str, ...]
    kinds: tuple[str, ...]
    offers: tuple[Offer, ...]
    code_kind: str
    code_loop_id: str
    code_route: str
    reading_legible: bool = False


@dataclass(frozen=True)
class Disposition:
    """What this evidence answers, after the guards have had it."""

    kind: str
    loop_id: str
    route: str
    missing: tuple[str, ...] = ()
    reason: str = ""
    refusals: tuple[str, ...] = ()


class EvidenceProposal(BaseModel):
    """The only shape the model may return. It nominates; it never decides."""

    kind: PhotoKind = Field(
        description="What this photograph is, chosen from the candidate kinds "
        "you were given. Never a kind that is not on that list."
    )
    loop_id: str = Field(
        default="",
        description="The id of the open obligation this evidence answers, "
        "copied exactly from the list you were given. Empty when none of them "
        "is what this page is about.",
    )
    reason: str = Field(
        default="",
        description="One line, naming what on the page decided it: an analyte, "
        "a date, a test name. Never an opinion about a value.",
    )


PROMPT = """You are the evidence step of a clinical follow-up system. A patient
has sent a photograph. Another part of the system has already read the page and
transcribed it; a third part has already listed the obligations this doctor is
carrying for this patient, and what each one asked for.

Your only job is to say what this page is and which of those obligations it
answers.

Choose:
- kind: one of the candidate kinds you are given, and nothing else.
- loop_id: one of the listed ids, copied exactly, or empty when none of them is
  what this page is about. An empty answer is a normal answer: a result nobody
  ordered is a real result and the doctor is shown it either way.

What decides it is on the page and in the list: the analytes printed on it
against what each obligation asked for, the name printed on it, the collection
date against the date the obligation was made.

Rules you must not break:
- Never invent an id. Code refuses an id that is not on the list, keeps its own
  choice, and prints the refusal on the record.
- Never say whether a value is normal, high, low, dangerous or fine. A fixed
  table in code decides that, and it decides it whatever you answer.
- Never decide whether the page satisfies the obligation. Three checks in code
  (the printed name, the collection date, the analytes that were asked for) do
  that after you, and they overrule you.
- An old page is not the current one. If the collection date is before the date
  the obligation was made, that obligation is not what this page answers.
- You are choosing where this evidence goes, not what it means."""


# --------------------------------------------------------------------------- #
# Building the block the turn reads
# --------------------------------------------------------------------------- #
def candidate_kinds(reading: PhotoReading, *, legible_reading: bool) -> tuple[str, ...]:
    """What this picture could be, according to the page rather than the label.

    The classification the reading came back with is always first and is always
    a candidate. The others are added by code from what is actually on the
    page: rows of analytes make a lab slip possible whatever the picture was
    called, and two readable pressures make a monitor screen possible.

    "other" is the relay exit, and it is offered only when the vision reading
    itself found nothing on the page to read: no analyte rows and no legible
    pressures. The relay lane does not call core/labs.assess, does not consult
    the critical-value table and never reaches core/escalate, so a page that
    already parsed into values must not be movable onto it by any answer. A
    slip printing `Potassium 6.4 H` is a lab slip whatever the turn calls it.
    Every other direction stays open: a "prescription" carrying analyte rows
    can still be promoted to a lab slip, because that direction adds the value
    table rather than removing it.
    """
    found: list[str] = [reading.kind]
    if reading.analytes and "lab_slip" not in found:
        found.append("lab_slip")
    if legible_reading and "bp_monitor" not in found:
        found.append("bp_monitor")
    if (not reading.analytes and not legible_reading
            and "other" not in found):
        found.append("other")
    return tuple(found)


def offers(loops: Sequence[Loop]) -> tuple[Offer, ...]:
    """The loops that may still receive evidence, with their contracts."""
    out: list[Offer] = []
    for loop in loops:
        states = OPEN_STATES_FOR.get(loop.type or "")
        if states is None or loop.state not in states:
            continue
        out.append(Offer(
            id=loop.id,
            type=loop.type,
            title=loop.title,
            test_name=photos.test_name_of(loop),
            state=loop.state,
            required=tuple(verify.required_analytes(loop)),
            ordered_on=(loop.created_at.date().isoformat()
                        if loop.created_at is not None else ""),
        ))
    return tuple(out)


def facts_for(
    patient_name: str,
    reading: PhotoReading,
    loops: Sequence[Loop],
    *,
    code_kind: str,
    code_loop_id: str,
    code_route: str,
    legible_reading: bool = False,
) -> Facts:
    """The whole of what the turn sees, assembled by code from the record."""
    return Facts(
        patient_name=patient_name,
        printed_name=reading.patient_name or "",
        printed_date=reading.taken_on or "",
        analytes=tuple(a.analyte for a in reading.analytes if a.analyte),
        kinds=candidate_kinds(reading, legible_reading=legible_reading),
        offers=offers(loops),
        code_kind=code_kind,
        code_loop_id=code_loop_id or "",
        code_route=code_route,
        reading_legible=legible_reading,
    )


def block(facts: Facts) -> str:
    """The facts as the lines the model reads. One obligation per line."""
    lines = [
        "THE PAGE:",
        f"  candidate kinds: {', '.join(facts.kinds)}",
        f"  the reader classified it as: {facts.code_kind}",
        f"  name printed on it: {facts.printed_name or '(none printed)'}",
        f"  date printed on it: {facts.printed_date or '(none printed)'}",
        f"  analytes read off it: "
        f"{', '.join(facts.analytes) if facts.analytes else '(none)'}",
        "",
        f"THE PATIENT ON THE RECORD: {facts.patient_name}",
        "",
    ]
    if facts.offers:
        lines.append("THE OBLIGATIONS STILL OPEN FOR HIM:")
        lines += [f"  {offer.as_line()}" for offer in facts.offers]
    else:
        lines.append("HE HAS NO OPEN OBLIGATION. Nothing was ordered.")
    lines += [
        "",
        "WHAT THE ROUTING TABLE WOULD DO WITH THIS ON ITS OWN:",
        f"  kind={facts.code_kind} loop={facts.code_loop_id or '(none)'} "
        f"route={facts.code_route}",
        "",
        "Answer with the kind and the one obligation this page answers, or an "
        "empty loop_id when none of them is.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The turn
# --------------------------------------------------------------------------- #
def _hermetic() -> bool:
    """True inside the hermetic unittest process, where no model exists.

    `sanad_test_guard` replaces the GenAI client with an inert double whose
    refusal is a `BaseException`, on purpose: an unmocked model call in a test
    must not be swallowed by a production `except Exception`. So this file does
    not call it at all. In a test that has not scripted this seam the
    orchestrator is simply unavailable, which is the fail-open branch, and the
    frozen golden journey therefore replays the code routing byte for byte.
    """
    try:
        from . import media

        return getattr(media.client, "_sanad_hermetic", False) is True
    except Exception:  # noqa: BLE001 - an unreadable client is not a model
        return True


async def _ask(facts: Facts) -> Optional[EvidenceProposal]:
    """One bounded Gemini turn, structured output, nothing kept afterwards.

    None on anything at all: no client, a timeout, an error, an answer that
    does not validate. The caller reads None as "the table decides".
    """
    if _hermetic():
        return None
    try:
        from google.genai import types

        from . import media

        response = await bounded.within(
            DEADLINE,
            media.client.aio.models.generate_content(
                model=media.MODEL,
                contents=[types.Part(text=block(facts))],
                config=types.GenerateContentConfig(
                    system_instruction=PROMPT,
                    response_mime_type="application/json",
                    response_schema=EvidenceProposal,
                    temperature=0,
                ),
            ),
            what="the evidence orchestrator",
        )
        parsed = response.parsed
        if isinstance(parsed, EvidenceProposal):
            return parsed
        return EvidenceProposal.model_validate(parsed) if parsed else None
    except Exception:  # noqa: BLE001 - every failure is the same fallback
        log.warning("the evidence orchestrator did not answer", exc_info=True)
        return None


def guard(facts: Facts, proposal: EvidenceProposal) -> Disposition:
    """Everything the model said, checked against the lists code built.

    Nothing here trusts the answer. A kind that was not offered and a loop that
    was not offered are both replaced by the table's own choice, and the
    refusal is printed on the packet so the doctor's record says a proposal was
    made and refused rather than saying nothing happened.
    """
    refusals: list[str] = []

    kind = (proposal.kind or "").strip()
    if kind not in facts.kinds:
        refusals.append(
            f"code guard refused kind {kind or '(empty)'!r}: it is not one of "
            f"the candidate kinds ({', '.join(facts.kinds)}); "
            f"{facts.code_kind} stands"
        )
        kind = facts.code_kind

    wanted = LOOP_TYPE_FOR.get(kind)
    by_id = {offer.id: offer for offer in facts.offers}
    loop_id = (proposal.loop_id or "").strip()
    if wanted is None:
        # A kind with no loop type of its own - a prescription, anything else -
        # is relayed unread, and a relay never carries a loop however it was
        # proposed. This is checked before the list is consulted, because the
        # reason is the kind and not the loop.
        if loop_id:
            refusals.append(
                f"code guard refused loop {loop_id!r}: a {kind} is relayed "
                "unread and is never filed on an obligation"
            )
        loop_id = ""
    elif loop_id:
        offer = by_id.get(loop_id)
        if offer is None:
            refusals.append(
                f"code guard refused loop {loop_id!r}: it is not an open "
                f"obligation on this patient's record; "
                f"{facts.code_loop_id or 'no loop'} stands"
            )
            loop_id = facts.code_loop_id
        elif offer.type != wanted:
            refusals.append(
                f"code guard refused loop {loop_id!r}: a {kind} cannot be "
                f"filed on a {offer.type} obligation; "
                f"{facts.code_loop_id or 'no loop'} stands"
            )
            loop_id = facts.code_loop_id

    # A fallback to the table's own choice is only a fallback when the table's
    # choice is still valid for the kind that survived.
    fallen_back = by_id.get(loop_id)
    if loop_id and (fallen_back is None or fallen_back.type != wanted):
        loop_id = ""

    # The route is not the model's to choose. It comes back out of the same
    # table it always came out of, over the kind and the loop that survived.
    route = photos.route(
        kind,
        test_loop=bool(loop_id) and wanted == "TEST",
        monitor_loop=bool(loop_id) and wanted == "MONITOR",
    )
    offer = by_id.get(loop_id)
    missing = (verify.missing_analytes(offer.required, facts.analytes)
               if offer is not None and offer.required else ())
    return Disposition(
        kind=kind, loop_id=loop_id, route=route, missing=tuple(missing),
        reason=(proposal.reason or "").strip(), refusals=tuple(refusals),
    )


async def decide(facts: Facts) -> Optional[Disposition]:
    """One disposition for this page, or None when the table has to answer.

    None is the whole of the fallback contract. It is not a route, not a
    refusal and not an error the caller has to handle: it is "there was no
    turn", and core/extractor.py then does exactly what it did before this
    module existed.
    """
    try:
        proposal = await _ask(facts)
    except Exception:  # noqa: BLE001 - a turn that throws is a turn that did not
        log.warning("the evidence orchestrator failed", exc_info=True)
        proposal = None
    if proposal is None:
        log.info("the evidence orchestrator stood down; core/photos.py routes "
                 "(kind=%s route=%s)", facts.code_kind, facts.code_route)
        return None
    disposition = guard(facts, proposal)
    for refusal in disposition.refusals:
        log.warning("%s", refusal)
    log.info("evidence orchestrator: kind=%s loop=%s route=%s",
             disposition.kind, disposition.loop_id or "(none)",
             disposition.route)
    return disposition


# --------------------------------------------------------------------------- #
# The packet
# --------------------------------------------------------------------------- #
def packet(facts: Facts, disposition: Disposition) -> dict[str, Any]:
    """The typed Evidence Packet, as it goes onto the routing event's meta.

    It exists only when a turn happened. A fail-open route writes no packet:
    there is no decision to attribute, and the event is the one this system
    always wrote.
    """
    return EvidencePacket(
        kind=disposition.kind,
        loop_id=disposition.loop_id,
        route=disposition.route,
        missing=list(disposition.missing),
        reason=disposition.reason,
        refused=list(disposition.refusals),
        candidates=list(facts.kinds),
        offered=[offer.id for offer in facts.offers],
        code_route={"kind": facts.code_kind, "loop_id": facts.code_loop_id,
                    "route": facts.code_route},
        agreed_with_code=(disposition.kind == facts.code_kind
                          and disposition.loop_id == facts.code_loop_id
                          and disposition.route == facts.code_route),
        provenance={
            "proposed_by": "evidence-orchestrator (gemini)",
            "routed_by": "code (core/photos.py routing table)",
            "gated_by": ["code (core/verify.py identity, date, completeness)",
                         "code (core/labs.py value tables)"],
        },
        decided_by=DECIDED_BY,
    ).model_dump()


def chosen(loops: Sequence[Loop], disposition: Disposition) -> Optional[Loop]:
    """The loop object behind a guarded disposition, or None."""
    if not disposition.loop_id:
        return None
    for loop in loops:
        if loop.id == disposition.loop_id:
            return loop
    return None
