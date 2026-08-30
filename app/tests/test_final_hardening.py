"""Focused regressions for the final independent audit hardening."""

from __future__ import annotations

import io
import unittest
import wave
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from core import media, report
from core.models import Doctor, Patient


def wav_with(sample: int) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(int(sample).to_bytes(2, "little", signed=True) * 160)
    return stream.getvalue()


class SilentVoiceNotes(unittest.IsolatedAsyncioTestCase):
    async def test_digital_silence_never_reaches_the_transcription_model(self) -> None:
        model = AsyncMock(return_value="Yes")
        with patch.object(media, "to_wav", return_value=wav_with(0)), \
                patch.object(media, "transcribe_wav_async", model):
            with self.assertRaises(media.SilentAudio):
                await media.transcribe_async(b"silent ogg")
        model.assert_not_awaited()

    async def test_audible_audio_still_reaches_the_transcription_model(self) -> None:
        model = AsyncMock(return_value="spoken words")
        with patch.object(media, "to_wav", return_value=wav_with(100)), \
                patch.object(media, "transcribe_wav_async", model):
            self.assertEqual(await media.transcribe_async(b"voice"), "spoken words")
        model.assert_awaited_once()


class CompletionReports(unittest.IsolatedAsyncioTestCase):
    async def test_no_unchecked_model_sentence_is_added_to_a_report(self) -> None:
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        doctor = Doctor(id="d", name="Dr M", web_token="token", created_at=now)
        patient = Patient(id="p", doctor_id="d", name="Patient", created_at=now)
        with patch.object(report.store, "list_loops", AsyncMock(return_value=[])), \
                patch.object(report.events, "last_events", AsyncMock(return_value=[])), \
                patch.object(report.settings, "current", AsyncMock(return_value=("run", 86400))), \
                patch.object(report.store, "now", return_value=now):
            body = await report.build(doctor, patient)
        self.assertNotIn("What I would flag", body)


if __name__ == "__main__":
    unittest.main()
