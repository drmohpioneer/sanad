"""Gate 1 regressions for evidence closure, completeness, and attribution.

These tests state the safe behavior required by the S23 dossier before the
new evidence and closure seams are introduced.  Normal builds report the live
bugs as expected failures; ``SANAD_GATE1_STRICT=1`` exposes the raw failures.
Once a fix lands, an unexpected success forces removal of its marker.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import concierge, extractor, verify
from core.models import Doctor, Loop, Patient, PhotoReading, SlipAnalyte


NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


def gate1_live_bug(test):
    """Expect assertion failures only; fixture/runtime errors remain hard errors."""
    if os.environ.get("SANAD_GATE1_STRICT") == "1":
        return test

    def record(case: unittest.TestCase, error=None) -> None:
        outcome = case._outcome
        if outcome is None:
            raise RuntimeError("Gate 1 outcome is unavailable")
        if error is None:
            if outcome.success:
                case._addUnexpectedSuccess(outcome.result)
        else:
            case._addExpectedFailure(outcome.result, error)
        outcome.success = False

    if inspect.iscoroutinefunction(test):
        @functools.wraps(test)
        async def async_wrapper(case, *args, **kwargs):
            try:
                await test(case, *args, **kwargs)
            except case.failureException:
                record(case, sys.exc_info())
            else:
                record(case)
        return async_wrapper

    @functools.wraps(test)
    def wrapper(case, *args, **kwargs):
        try:
            test(case, *args, **kwargs)
        except case.failureException:
            record(case, sys.exc_info())
        else:
            record(case)
    return wrapper


def _doctor() -> Doctor:
    return Doctor(
        id="doctor-1",
        name="Test Doctor",
        specialty="cardiology",
        web_token="doctor-token",
        created_at=NOW,
    )


def _patient() -> Patient:
    return Patient(
        id="patient-1",
        doctor_id="doctor-1",
        name="Ahmed Ali",
        diagnosis="heart failure",
        created_at=NOW,
    )


def _loop(
    *,
    title: str = "Electrolytes panel",
    verified: dict | None = None,
    analytes: tuple[str, ...] = ("Potassium",),
) -> Loop:
    return Loop(
        id="loop-1",
        patient_id="patient-1",
        doctor_id="doctor-1",
        type="TEST",
        title=title,
        details={"test_name": title, "analytes": list(analytes)},
        state="open",
        results=[{"analyte": "Potassium", "value": "4.2"}],
        verified=verified or {},
        created_at=NOW,
        updated_at=NOW,
    )


class ReviewedCannotOverrideEvidence(unittest.IsolatedAsyncioTestCase):
    @gate1_live_bug
    async def test_reviewed_records_review_but_does_not_close_fail_or_unknown(self) -> None:
        """A doctor's acknowledgement is not proof that evidence passed.

        ``before_order`` is a positively failed date predicate.  ``not_printed``
        is an unknown date predicate.  Both are real verifier outputs that may
        still appear on a values card, and neither may be converted to ``done``
        by pressing Reviewed.
        """
        cases = {
            "FAIL": verify.check(
                printed_name="Ahmed Ali",
                printed_date="2026-08-01",
                printed_analytes=["Potassium"],
                patient_name="Ahmed Ali",
                ordered_on=NOW,
                required=("Potassium",),
            ),
            "UNKNOWN": verify.check(
                printed_name="Ahmed Ali",
                printed_date="not printed",
                printed_analytes=["Potassium"],
                patient_name="Ahmed Ali",
                ordered_on=NOW,
                required=("Potassium",),
            ),
        }
        self.assertEqual("before_order", cases["FAIL"].dated)
        self.assertEqual("not_printed", cases["UNKNOWN"].dated)

        closed: list[str] = []
        for status, verdict in cases.items():
            with self.subTest(status=status):
                self.assertFalse(verdict.satisfies)
                loop = _loop(verified=verdict.as_meta())
                update_loop = AsyncMock()
                out = SimpleNamespace(send=AsyncMock())
                with (
                    patch.object(
                        concierge.store,
                        "get_loop",
                        AsyncMock(return_value=loop),
                    ),
                    patch.object(concierge.store, "update_loop", update_loop),
                    patch.object(
                        concierge.store,
                        "get_patient",
                        AsyncMock(return_value=None),
                    ),
                    patch.object(
                        concierge.events,
                        "append_event",
                        AsyncMock(return_value=SimpleNamespace(id="event-1")),
                    ),
                    patch.object(concierge, "fanout", return_value=out),
                ):
                    await concierge.mark_reviewed(_doctor(), loop.id)

                update_loop.assert_awaited_once()
                written = update_loop.await_args.kwargs
                self.assertIs(
                    written.get("doctor_reviewed"),
                    True,
                    "Reviewed should record acknowledgement even when closure is refused",
                )
                resulting_state = written.get("state", loop.state)
                if resulting_state == "done":
                    closed.append(status)

        self.assertEqual(
            [],
            closed,
            "Reviewed closed evidence whose verifier outcome was "
            + ", ".join(closed),
        )


class UnknownPanelIsNotComplete(unittest.TestCase):
    @gate1_live_bug
    def test_empty_requirement_set_is_unknown_and_cannot_satisfy_the_order(self) -> None:
        """Set-theory truth for an empty list must not become clinical truth."""
        loop = _loop(title="ZXQ-999 bespoke laboratory panel", analytes=())
        requirements = verify.required_analytes(loop)
        self.assertEqual((), requirements, "the fixture must remain an unknown panel")

        verdict = verify.check(
            printed_name="Ahmed Ali",
            printed_date="2026-08-30",
            printed_analytes=["ZXQ-999 marker"],
            patient_name="Ahmed Ali",
            ordered_on=NOW - timedelta(days=1),
            required=requirements,
        )

        self.assertFalse(
            verdict.satisfies,
            "an empty requirement set means completeness is UNKNOWN, not PASS",
        )


class IdentityMismatchIsUnassigned(unittest.IsolatedAsyncioTestCase):
    @gate1_live_bug
    async def test_critical_mismatched_document_is_quarantined_not_patient_attached(self) -> None:
        """Danger remains visible without attributing another person's slip.

        The uploader is still given urgent, subject-neutral guidance and the
        clinician still sees the critical-looking document.  Evidence-bearing
        records and the clinician card, however, must be unassigned until the
        subject is proved; they may not carry the uploader's patient or loop ID.
        """
        doctor = _doctor()
        patient = _patient()
        loop = _loop()
        reading = PhotoReading(
            kind="lab_slip",
            text_orientation="upright",
            patient_name="Mohamed Sayed",
            taken_on="2026-08-30",
            analytes=[
                SlipAnalyte(
                    analyte="Potassium (K+)",
                    value="6.4",
                    unit="mmol/L",
                    ref_range="3.5 - 5.1",
                    flag="H",
                )
            ],
        )

        recorded: list[dict] = []

        async def append_event(doctor_id: str, kind: str, text: str = "", **fields):
            row = {
                "id": f"event-{len(recorded) + 1}",
                "doctor_id": doctor_id,
                "kind": kind,
                "text": text,
                **fields,
            }
            recorded.append(row)
            return SimpleNamespace(**row)

        out = SimpleNamespace(send=AsyncMock())
        append_result = AsyncMock()
        update_loop = AsyncMock()
        with (
            patch.object(extractor, "fanout", return_value=out),
            patch.object(
                extractor,
                "recent_words",
                AsyncMock(return_value=[]),
            ),
            patch.object(extractor.events, "append_event", side_effect=append_event),
            patch.object(extractor.store, "append_result", append_result),
            patch.object(extractor.store, "update_loop", update_loop),
            patch.object(
                extractor.store,
                "get_loop",
                AsyncMock(return_value=loop),
            ),
            patch.object(
                extractor.coordinator,
                "on_evidence",
                AsyncMock(),
            ),
        ):
            await extractor._handle_lab(
                patient,
                doctor,
                reading,
                note={"attempts": 1},
                image_path="gs://quarantine/candidate.png",
                speak="en",
                who="m",
                channel="web",
                loop=loop,
            )

        append_result.assert_not_awaited()
        update_loop.assert_not_awaited()

        evidence_records = [
            row for row in recorded if (row.get("meta") or {}).get("results")
        ]
        self.assertTrue(evidence_records, "the dangerous-looking values must stay visible")
        doctor_messages = [
            call.args[1]
            for call in out.send.await_args_list
            if call.args[0].startswith("doctor:")
        ]
        self.assertEqual(1, len(doctor_messages))
        clinician_message = doctor_messages[0]
        actions = clinician_message.card["actions"]
        unsafe_actions = [
            action.get("id", "")
            for action in actions
            if action.get("id", "").startswith(("attach:", "openloop:"))
        ]
        self.assertEqual(
            [],
            unsafe_actions,
            "an identity-mismatched document still offers a patient-attachment action",
        )

        clinician_context = " ".join(
            (
                clinician_message.text,
                repr(clinician_message.card),
                repr(clinician_message.meta),
            )
        ).lower()
        self.assertIn(
            "gs://quarantine/candidate.png",
            clinician_context,
            "the clinician did not receive the quarantined document",
        )
        self.assertTrue(
            any(
                marker in clinician_context
                for marker in ("identity", "mismatch", "unassigned", "subject unconfirmed")
            ),
            "the clinician received the document without its identity uncertainty",
        )

        patient_messages = [
            call.args[1]
            for call in out.send.await_args_list
            if call.args[0].startswith("patient:")
        ]
        self.assertEqual(1, len(patient_messages))
        patient_message = patient_messages[0]
        self.assertTrue(patient_message.text.strip(), "urgent guidance was empty")
        urgency_context = repr(patient_message.meta).lower()
        self.assertTrue(
            any(marker in urgency_context for marker in ("danger", "emergency", "urgent")),
            "the uploader did not receive guidance classified as urgent",
        )

        attribution_errors = [
            f"{row['id']} carries patient={row.get('patient_id')!r} "
            f"loop={row.get('loop_id')!r}"
            for row in evidence_records
            if row.get("patient_id") is not None or row.get("loop_id") is not None
        ]
        if clinician_message.patient_id is not None:
            attribution_errors.append(
                "clinician card carries patient="
                f"{clinician_message.patient_id!r}"
            )
        self.assertEqual(
            [],
            attribution_errors,
            "identity-mismatched critical evidence was not quarantined: "
            + "; ".join(attribution_errors),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
