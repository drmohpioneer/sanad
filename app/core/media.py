"""Owns audio in and text out.

Telegram-shaped voice notes arrive as OGG/Opus; Gemini wants 16 kHz mono WAV.
Proven end to end in S0, Arabic transcript verbatim (research/s0-results.md).
The spike route that proved it was deleted at rev 17, because an unauthenticated
transcription endpoint on a public service is somebody else's free service; the
Registrar and the patient path go through this one code path and always did.

Blocking and non-blocking versions live side by side. The product path always
uses the async ones: ffmpeg goes to a worker thread and the model call is the
SDK's own async client, so a 16-second voice note never freezes the console's
two-second poll (S1 review, carry-over 1).
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import tempfile
import wave

from google import genai
from google.genai import types

MODEL = "gemini-3.5-flash"
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "sanad-506914")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

# Vertex mode is also driven by GOOGLE_GENAI_USE_VERTEXAI, but we pass it
# explicitly so a misconfigured deploy fails loudly instead of silently
# falling back to the API-key path. No API keys anywhere in Sanad.
client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

TRANSCRIBE_PROMPT = "Transcribe this audio verbatim in its original language."


class SilentAudio(ValueError):
    """The decoded note contains no signal that is safe to interpret as speech."""


def require_audible_wav(wav_bytes: bytes) -> None:
    """Reject digital silence before a model can hallucinate a transcript.

    ffmpeg always gives this module mono 16-bit PCM.  The deliberately tiny
    threshold catches zero-filled and near-zero decoder output without trying
    to decide whether a genuinely quiet human voice is loud enough.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as source:
            if source.getsampwidth() != 2:
                raise SilentAudio("unsupported decoded sample width")
            frames = source.readframes(source.getnframes())
    except (EOFError, wave.Error) as exc:
        raise SilentAudio("decoded voice note is not readable") from exc
    if not frames:
        raise SilentAudio("decoded voice note is empty")
    samples = memoryview(frames).cast("h")
    if not samples or max(abs(sample) for sample in samples) <= 8:
        raise SilentAudio("decoded voice note is silent")


def ffmpeg_version() -> str:
    out = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()[0]


# Security audit M2. ffmpeg runs on bytes a stranger uploaded, so it runs with
# a clock on it and a fence around it.
FFMPEG_TIMEOUT = 30      # real seconds before the process is killed
MAX_AUDIO_SECONDS = 120  # of output; a longer note is transcribed to here


def to_wav(raw: bytes) -> bytes:
    """Any ffmpeg-readable audio -> 16 kHz mono WAV.

    Three guards, all from the security audit's M2:

      -nostdin        ffmpeg never waits on a terminal it does not have;
      timeout         a crafted container can keep a decoder busy for ever, and
                      this is a worker thread on a 1 GiB instance. Thirty
                      seconds is many times what a two-minute voice note needs;
      -protocol_whitelist / -t
                      the input is a local file and the output is bounded. The
                      whitelist is ffmpeg's own default for a file input, so it
                      changes nothing today; it is written down so that a future
                      edit that adds a URL input has to argue with it first.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.audio")
        dst = os.path.join(tmp, "out.wav")
        with open(src, "wb") as fh:
            fh.write(raw)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-protocol_whitelist", "file,crypto,data",
             "-y", "-i", src, "-t", str(MAX_AUDIO_SECONDS),
             "-ac", "1", "-ar", "16000", dst],
            capture_output=True,
            check=True,
            timeout=FFMPEG_TIMEOUT,
        )
        with open(dst, "rb") as fh:
            return fh.read()


def transcribe_wav(wav: bytes) -> str:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=wav, mime_type="audio/wav"),
            types.Part(text=TRANSCRIBE_PROMPT),
        ],
    )
    return (resp.text or "").strip()


def transcribe(raw: bytes) -> str:
    """Voice note bytes -> transcript, in whatever language was spoken."""
    wav = to_wav(raw)
    require_audible_wav(wav)
    return transcribe_wav(wav)


# --------------------------------------------------------------------------- #
# Non-blocking versions - the only ones the product path uses
# --------------------------------------------------------------------------- #
async def ffmpeg_version_async() -> str:
    return await asyncio.to_thread(ffmpeg_version)


async def transcribe_wav_async(wav: bytes) -> str:
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=wav, mime_type="audio/wav"),
            types.Part(text=TRANSCRIBE_PROMPT),
        ],
    )
    return (resp.text or "").strip()


async def transcribe_async(raw: bytes) -> str:
    """Voice note bytes -> transcript, without blocking the event loop."""
    wav = await asyncio.to_thread(to_wav, raw)  # ffmpeg is a blocking subprocess
    await asyncio.to_thread(require_audible_wav, wav)
    return await transcribe_wav_async(wav)
