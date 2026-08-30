"""Deterministic inference seams for the Gate 0B golden journey.

This module deliberately patches only model boundaries.  The policy decision
returned by the Coordinator seam is still made by :meth:`Turn.propose`, so the
real code guards every scripted model choice.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from typing import Any, Iterator, Optional
from unittest.mock import patch

from core import (
    concierge,
    coordinator,
    extractor,
    identify,
    intents,
    registrar,
    sentinel,
    validator,
)
from core.models import (
    ConciergeAnswer,
    PhotoReading,
    ProposedLoop,
    ProposedPatient,
    ProposedRecord,
    SlipAnalyte,
)
from core.policy import Decision


DICTATION = (
    "Ahmed Ali, 58, male, heart failure and high LDL. Start atorvastatin 40 at "
    "night. Lipid panel in 2 weeks. Blood pressure twice a day for 7 days. "
    "Come back in 3 weeks."
)
COST_MESSAGE = "I'm not doing the test, it's too expensive."
AMANY_MESSAGE = "I did the glucose test"
AMANY_RELAY_REASON = (
    "Patient states they completed the test, but contacts are exhausted and "
    "evidence is not yet received"
)


AHMED_RECORD = ProposedRecord(
    patient=ProposedPatient(
        name="Ahmed Ali", age=58, sex="male",
        diagnosis="heart failure and high LDL",
    ),
    plan_text=(
        "Hi Ahmed, to help with your heart failure and high LDL, please start "
        "taking Atorvastatin 40 at night. You will need to get a lipid panel "
        "test in 2 weeks, and check your blood pressure twice a day for 7 days. "
        "Finally, please come back to see us in 3 weeks."
    ),
    loops=[
        ProposedLoop(
            type="MEDICATION", title="Start Atorvastatin",
            drug="atorvastatin", dose="40 at night", action="start",
        ),
        ProposedLoop(
            type="TEST", title="Lipid panel", due_in_days=14,
            test_name="Lipid panel",
        ),
        ProposedLoop(
            type="MONITOR", title="Blood pressure monitoring", due_in_days=7,
            metric="Blood pressure", schedule="twice a day", days=7,
        ),
        ProposedLoop(type="VISIT", title="Return visit", due_in_days=21),
    ],
)

NEW_AHMED = identify.Verdict(intent=identify.NEW_PATIENT)

PARTIAL_LIPID = PhotoReading(
    kind="lab_slip", text_orientation="upright",
    lab_name="Nile Specialized Medical Laboratory",
    patient_name="Ahmed Ali", taken_on="30/08/2026",
    analytes=[
        SlipAnalyte(
            analyte="LDL Cholesterol", value="160", unit="mg/dL",
            ref_range="<100", flag="H",
        ),
        SlipAnalyte(
            analyte="Total Cholesterol", value="240", unit="mg/dL",
            ref_range="<200", flag="H",
        ),
    ],
)

COMPLETE_LIPID = PhotoReading(
    kind="lab_slip", text_orientation="upright",
    lab_name="Nile Specialized Medical Laboratory",
    patient_name="Ahmed Ali", taken_on="30/08/2026",
    analytes=[
        SlipAnalyte(
            analyte="LDL Cholesterol", value="92", unit="mg/dL",
            ref_range="<100",
        ),
        SlipAnalyte(
            analyte="HDL Cholesterol", value="48", unit="mg/dL",
            ref_range="40-60",
        ),
        SlipAnalyte(
            analyte="Total Cholesterol", value="178", unit="mg/dL",
            ref_range="<200",
        ),
        SlipAnalyte(
            analyte="Triglycerides", value="130", unit="mg/dL",
            ref_range="<150",
        ),
    ],
)

CRITICAL_POTASSIUM = PhotoReading(
    kind="lab_slip", text_orientation="upright",
    lab_name="Nile Specialized Medical Laboratory",
    patient_name="Ahmed Ali", taken_on="28/08/2026",
    analytes=[
        SlipAnalyte(
            analyte="Potassium (K+)", value="6.4", unit="mmol/L",
            ref_range="3.5-5.1", flag="H",
        ),
    ],
)


class UnexpectedBoundaryCall(BaseException):
    """A scripted seam was called outside the frozen journey.

    This intentionally derives directly from ``BaseException``.  Production
    boundaries fail closed by catching ``Exception``; a test fixture violation
    must escape those fallbacks or a missing script would look like a valid
    safety stand-down.
    """


@dataclass(frozen=True)
class BoundaryCall:
    sequence: int
    beat: str
    boundary: str
    inputs: dict[str, Any]
    output: Any

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def _beat_number(label: str) -> int:
    digits = "".join(ch for ch in (label or "") if ch.isdigit())
    if not digits:
        raise ValueError(f"a Gate 0B beat label needs a number: {label!r}")
    number = int(digits)
    if not 1 <= number <= 9:
        raise ValueError(f"Gate 0B has beats 1 through 9, not {number}")
    return number


class ScriptedBoundaries:
    """The complete, finite model script for one nine-beat run."""

    EXPECTED_COUNTS = {
        (1, "registrar.propose"): 1,
        (1, "identify.identify"): 1,
        (2, "coordinator._choose"): 12,
        (3, "sentinel.model_net"): 1,
        (3, "validator._yes_no"): 1,
        (3, "intents.model_vote"): 1,
        (3, "coordinator._choose"): 1,
        (4, "extractor._ask"): 1,
        (4, "coordinator._choose"): 1,
        (5, "extractor._ask"): 1,
        (6, "extractor._ask"): 1,
        (7, "sentinel.model_net"): 1,
        (7, "validator._yes_no"): 1,
        (7, "coordinator._choose"): 1,
    }

    def __init__(self) -> None:
        self.current_beat: Optional[int] = None
        self.current_label = ""
        self.calls: list[BoundaryCall] = []
        self._counts: Counter[tuple[int, str]] = Counter()

    @contextmanager
    def beat(self, label: str) -> Iterator["ScriptedBoundaries"]:
        """Label every boundary call made while one beat is being driven."""
        number = _beat_number(label)
        previous = self.current_beat, self.current_label
        self.current_beat, self.current_label = number, label
        try:
            yield self
        finally:
            self.current_beat, self.current_label = previous

    def set_beat(self, label: str) -> None:
        """Imperative counterpart to :meth:`beat` for scenario runners."""
        self.current_beat = _beat_number(label)
        self.current_label = label

    def _enter(self, boundary: str, inputs: dict[str, Any]) -> tuple[int, str]:
        if self.current_beat is None:
            raise UnexpectedBoundaryCall(
                f"{boundary} was called without an active Gate 0B beat"
            )
        key = (self.current_beat, boundary)
        expected = self.EXPECTED_COUNTS.get(key)
        if expected is None:
            raise UnexpectedBoundaryCall(
                f"unexpected {boundary} call in {self.current_label or self.current_beat}: "
                f"{_jsonable(inputs)!r}"
            )
        if self._counts[key] >= expected:
            raise UnexpectedBoundaryCall(
                f"too many {boundary} calls in beat {self.current_beat}: "
                f"expected {expected}"
            )
        self._counts[key] += 1
        return key

    def _record(self, boundary: str, inputs: dict[str, Any], output: Any) -> Any:
        self.calls.append(BoundaryCall(
            sequence=len(self.calls) + 1,
            beat=self.current_label or f"beat-{self.current_beat:02d}",
            boundary=boundary,
            inputs=_jsonable(inputs),
            output=_jsonable(output),
        ))
        return output

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise UnexpectedBoundaryCall(message)

    async def _registrar_propose(self, text: str) -> ProposedRecord:
        inputs = {"text": text}
        self._enter("registrar.propose", inputs)
        self._require(_flat(text) == DICTATION, "registrar received the wrong dictation")
        # Return a copy so production validation is free to normalise it.
        answer = AHMED_RECORD.model_copy(deep=True)
        return self._record("registrar.propose", inputs, answer)

    async def _identify(
        self, text: str, rows: list[identify.BoardRow], extracted_name: str = ""
    ) -> identify.Verdict:
        inputs = {
            "text": text,
            "rows": [asdict(row) for row in rows],
            "extracted_name": extracted_name,
        }
        self._enter("identify.identify", inputs)
        self._require(_flat(text) == DICTATION, "identify received the wrong dictation")
        self._require(extracted_name == "Ahmed Ali", "identify did not receive Ahmed Ali")
        self._require(bool(rows), "the new-patient decision must run against the seeded board")
        self._require(
            all(_flat(row.name).casefold() != "ahmed ali" for row in rows),
            "the seeded board unexpectedly already contains Ahmed Ali",
        )
        answer = NEW_AHMED.model_copy(deep=True)
        return self._record("identify.identify", inputs, answer)

    async def _sentinel_model_net(self, text: str) -> bool:
        inputs = {"text": text}
        self._enter("sentinel.model_net", inputs)
        wanted = COST_MESSAGE if self.current_beat == 3 else AMANY_MESSAGE
        self._require(_flat(text) == wanted, "sentinel received an unrecorded message")
        return self._record("sentinel.model_net", inputs, False)

    async def _validator_yes_no(self, system_prompt: str, label: str, text: str) -> bool:
        inputs = {"system_prompt": system_prompt, "label": label, "text": text}
        self._enter("validator._yes_no", inputs)
        wanted = COST_MESSAGE if self.current_beat == 3 else AMANY_MESSAGE
        self._require(label == "PATIENT MESSAGE", "only the treatment-change vote is scripted")
        self._require(_flat(text) == wanted, "validator received an unrecorded message")
        return self._record("validator._yes_no", inputs, False)

    async def _intent_model_vote(self, text: str) -> str:
        inputs = {"text": text}
        self._enter("intents.model_vote", inputs)
        self._require(_flat(text) == COST_MESSAGE, "intent vote received an unrecorded message")
        return self._record("intents.model_vote", inputs, "")

    async def _coordinator_choose(self, turn: coordinator.Turn) -> Optional[Decision]:
        inputs = {
            "trigger": turn.trigger,
            "message": turn.message,
            "patient": turn.patient.model_dump(mode="json"),
            "loop": turn.loop.model_dump(mode="json"),
            "facts": _jsonable(turn.facts),
        }
        self._enter("coordinator._choose", inputs)

        if self.current_beat == 2:
            self._require(turn.trigger == coordinator.WAKE, "beat 2 must be a scheduled wake")
            proposed = turn.propose(
                "schedule_next_contact", {"days_from_now": 0},
                "The scheduled ladder reminder is due now.",
            )
            self._require(proposed.get("status") == "accepted", repr(proposed))
            answer = turn.decision
        elif self.current_beat == 3:
            self._require(turn.trigger == coordinator.REPLY, "beat 3 must be a patient reply")
            self._require(_flat(turn.message) == COST_MESSAGE, "wrong cost-barrier message")
            self._require(turn.loop.type == "TEST", "the cost barrier must land on the TEST loop")
            proposed = turn.propose(
                "classify_barrier", {"barrier": "cost", "resume_in_days": 0},
                "The patient says the lipid panel test is too expensive.",
            )
            self._require(proposed.get("status") == "accepted", repr(proposed))
            answer = turn.decision
        elif self.current_beat == 4:
            self._require(turn.trigger == coordinator.EVIDENCE, "beat 4 must be evidence")
            missing = list((turn.loop.verified or {}).get("missing") or [])
            self._require(missing == ["Triglycerides", "HDL"], f"wrong missing list: {missing!r}")
            proposed = turn.propose(
                "request_missing_evidence", {"analyte": "Triglycerides"},
                "The uploaded lipid panel is missing Triglycerides and HDL.",
            )
            self._require(proposed.get("status") == "accepted", repr(proposed))
            answer = turn.decision
        else:
            self._require(self.current_beat == 7, "unrecorded Coordinator turn")
            self._require(turn.trigger == coordinator.REPLY, "beat 7 must be a patient reply")
            self._require(_flat(turn.message) == AMANY_MESSAGE, "wrong Amany message")
            self._require(turn.loop.type == "TEST", "Amany's message must land on her TEST loop")
            self._require(
                turn.facts.contacts >= turn.policy.max_contacts,
                "the six-contact refusal did not precede the conservative stand-down",
            )
            # The administrative code tier already proposed a new contact and
            # was refused.  The frozen live run then took the Coordinator's
            # conservative escalation path: it recorded an unclear barrier and
            # opened a doctor relay instead of inventing another contact.
            proposed = turn.propose(
                "escalate_barrier", {"barrier": "unclear"},
                AMANY_RELAY_REASON,
            )
            self._require(proposed.get("status") == "accepted", repr(proposed))
            answer = turn.decision

        return self._record("coordinator._choose", inputs, answer)

    async def _concierge_answer(
        self, patient: Any, doctor: Any, text: str, history: list[str]
    ) -> ConciergeAnswer:
        inputs = {
            "patient": patient.model_dump(mode="json"),
            "doctor": doctor.model_dump(mode="json"),
            "text": text,
            "history": list(history),
        }
        self._enter("concierge.answer", inputs)
        self._require(_flat(text) == AMANY_MESSAGE, "Concierge received the wrong Beat 7 text")
        answer = ConciergeAnswer(
            tier="relay",
            reply=concierge.relay_line(doctor, text),
            relay_reason=AMANY_RELAY_REASON,
        )
        return self._record("concierge.answer", inputs, answer)

    async def _extractor_ask(self, image: bytes) -> PhotoReading:
        inputs = {"image": image}
        self._enter("extractor._ask", inputs)
        answer = {
            4: PARTIAL_LIPID,
            5: COMPLETE_LIPID,
            6: CRITICAL_POTASSIUM,
        }.get(self.current_beat)
        self._require(answer is not None, "photo read outside beats 4 through 6")
        return self._record("extractor._ask", inputs, answer.model_copy(deep=True))

    @contextmanager
    def patch(self) -> Iterator["ScriptedBoundaries"]:
        """Install all eight seams and restore them even if the scenario fails."""
        with ExitStack() as stack:
            stack.enter_context(patch.object(registrar, "propose", self._registrar_propose))
            stack.enter_context(patch.object(identify, "identify", self._identify))
            stack.enter_context(patch.object(sentinel, "model_net", self._sentinel_model_net))
            stack.enter_context(patch.object(validator, "_yes_no", self._validator_yes_no))
            stack.enter_context(patch.object(intents, "model_vote", self._intent_model_vote))
            stack.enter_context(patch.object(coordinator, "_choose", self._coordinator_choose))
            stack.enter_context(patch.object(concierge, "answer", self._concierge_answer))
            stack.enter_context(patch.object(extractor, "_ask", self._extractor_ask))
            yield self

    def count_summary(self) -> dict[str, Any]:
        by_boundary = Counter(call.boundary for call in self.calls)
        by_beat = Counter(call.beat for call in self.calls)
        return {
            "total": len(self.calls),
            "by_boundary": dict(sorted(by_boundary.items())),
            "by_beat": dict(sorted(by_beat.items())),
        }

    def trace_as_dicts(self) -> list[dict[str, Any]]:
        return [call.as_dict() for call in self.calls]

    def assert_complete(self) -> None:
        """Prove the finite script was consumed exactly, not merely without excess."""
        missing = {
            f"beat-{beat:02d}:{boundary}": expected - self._counts[(beat, boundary)]
            for (beat, boundary), expected in self.EXPECTED_COUNTS.items()
            if self._counts[(beat, boundary)] != expected
        }
        if missing:
            raise AssertionError(f"Gate 0B inference script was not consumed: {missing}")
