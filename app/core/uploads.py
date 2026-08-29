"""Owns what may be uploaded: how big, what kind, and who decides.

Security audit M2. Both upload routes read the whole body into memory with
`await file.read()` and no cap at all, and then chose the audio lane or the
image lane from the client's own `content_type` header. Two things follow from
that, and only one of them is obvious:

  size    Cloud Run caps a request at 32 MiB and the instance has 1 GiB, so a
          handful of concurrent 30 MiB uploads is an out-of-memory kill. There
          was no check anywhere below that ceiling.
  type    the client said what its file was, and the server believed it. Calling
          a JPEG `audio/ogg` sent it to ffmpeg and then to a transcription
          model; calling an audio file `image/jpeg` sent it to Pillow. Neither
          is a security hole on its own, and both are a way to spend somebody
          else's compute on a shape the code was not written for.

So the bytes are read in chunks against a cap and the type is decided here, in
code, from the file's own first bytes. The client's header is used for nothing
except the error message. A file whose magic number is not in the table below is
refused: this is a clinic, and the only things a patient sends are a photograph
and a voice note.

A refusal is not an error. The patient gets one line in his own language, the
doctor gets nothing at all (a photo that was too big is not a clinical event),
an event is written so the board shows the attempt, and the route answers 200.
Nothing crashes and nothing is stored.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("sanad.uploads")

# 10 MiB. A phone photograph is 2 to 5 MB and a two-minute voice note is under
# 1 MB, so this is generous for both and a long way under the instance's memory.
MAX_BYTES = 10 * 1024 * 1024
CHUNK = 64 * 1024

# Magic numbers, in the order they are tried. `at` is the offset the marker
# starts at; `also` is a second marker that must appear at offset 8 (the RIFF
# container carries its real type there).
_SIGNATURES: tuple[tuple[bytes, int, Optional[bytes], str], ...] = (
    (b"\xff\xd8\xff", 0, None, "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, None, "image/png"),
    (b"RIFF", 0, b"WEBP", "image/webp"),
    (b"RIFF", 0, b"WAVE", "audio/wav"),
    (b"OggS", 0, None, "audio/ogg"),
    (b"ID3", 0, None, "audio/mpeg"),
    (b"\xff\xfb", 0, None, "audio/mpeg"),
    (b"\xff\xf3", 0, None, "audio/mpeg"),
    (b"\xff\xf2", 0, None, "audio/mpeg"),
)

# ISO base media (MP4 and its relatives) all start "....ftyp<brand>", and the
# brand is what separates a HEIC photograph from an M4A voice note.
_FTYP_IMAGE = ("heic", "heix", "hevc", "hevx", "heim", "heis", "mif1", "msf1")
_FTYP_AUDIO = ("M4A ", "M4B ", "mp42", "isom", "iso2", "mp41", "dash")

AUDIO = "audio"
IMAGE = "image"


class Rejected(Exception):
    """This upload is not going any further. `reason` is for the event log."""

    def __init__(self, reason: str, *, too_large: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.too_large = too_large


# The two lines a patient can hear about his own upload, in his language and
# grammatical gender. Fixed strings, like every other patient-facing block in
# this codebase: no model writes them.
TOO_LARGE = {
    "ar": {
        "m": "الملف ده كبير أوي. ابعت صورة أصغر، أو رسالة صوتية أقصر.",
        "f": "الملف ده كبير أوي. ابعتي صورة أصغر، أو رسالة صوتية أقصر.",
        "u": "الملف ده كبير أوي. المطلوب صورة أصغر، أو رسالة صوتية أقصر.",
    },
    "en": "That file is too large. Please send a smaller photo, or a shorter "
          "voice note.",
}

WRONG_KIND = {
    "ar": {
        "m": "أقدر أستقبل صورة أو رسالة صوتية بس. ابعت صورة للتحليل، "
             "أو اكتب اللي عايز تقوله.",
        "f": "أقدر أستقبل صورة أو رسالة صوتية بس. ابعتي صورة للتحليل، "
             "أو اكتبي اللي عايزة تقوليه.",
        "u": "أقدر أستقبل صورة أو رسالة صوتية بس. المطلوب صورة للتحليل، "
             "أو كتابة الرسالة.",
    },
    "en": "I can only take a photo or a voice note. Please send a photo of the "
          "result, or type what you want to say.",
}


def refusal_text(speak: str, who: str, *, too_large: bool) -> str:
    table = TOO_LARGE if too_large else WRONG_KIND
    if speak != "ar":
        return table["en"]
    return table["ar"].get(who, table["ar"]["u"])


def sniff(raw: bytes) -> str:
    """The file's real type from its own first bytes, or "" for unknown."""
    if len(raw) < 12:
        return ""
    for marker, at, also, mime in _SIGNATURES:
        if not raw.startswith(marker, at):
            continue
        if also is not None and not raw.startswith(also, 8):
            continue
        return mime
    if raw[4:8] == b"ftyp":
        brand = raw[8:12].decode("latin-1", "replace")
        if brand.lower() in _FTYP_IMAGE:
            return "image/heic"
        if brand in _FTYP_AUDIO or brand.lower() in (
                b.strip().lower() for b in _FTYP_AUDIO):
            return "audio/mp4"
    return ""


def classify(raw: bytes) -> tuple[str, str]:
    """(lane, mime) for these bytes. Raises `Rejected` for anything else.

    The lane is decided from the bytes and never from the client's header,
    which is the whole point: the header is a claim and the magic number is a
    fact. `core/photos.py` and `core/media.py` are both written for one shape
    of input and this is what guarantees they get it.
    """
    if not raw:
        raise Rejected("the upload was empty")
    if len(raw) > MAX_BYTES:
        raise Rejected(f"{len(raw)} bytes, over the {MAX_BYTES} byte limit",
                       too_large=True)
    mime = sniff(raw)
    if not mime:
        raise Rejected("the file is not a photo or a voice note")
    return (AUDIO if mime.startswith("audio") else IMAGE), mime


async def read(file: Any) -> bytes:
    """Read an UploadFile against the cap, in chunks.

    Never `await file.read()` with no argument: that is the line the audit
    found, and it puts the whole body in memory before anything can object to
    its size. This holds one chunk more than the cap and no more, and it stops
    reading the socket the moment the cap is passed.
    """
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise Rejected(f"over the {MAX_BYTES} byte limit",
                           too_large=True)
        parts.append(chunk)
    return b"".join(parts)


async def take(file: Any) -> tuple[bytes, str, str]:
    """(bytes, lane, mime) for one upload, or `Rejected`. The one door."""
    raw = await read(file)
    lane, mime = classify(raw)
    log.info("upload accepted bytes=%d lane=%s mime=%s", len(raw), lane, mime)
    return raw, lane, mime
