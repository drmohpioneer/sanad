"""Owns where a patient's photo goes: a private bucket, referenced by path.

The rule is that image bytes never enter Firestore. A lab slip is a medical
document; it is written to gs://<bucket>/<run id>/<patient id>/<uuid>.<ext> in a
bucket created with uniform access and public access prevention, and everything
else in Sanad carries only the gs:// path.

Nothing here ever makes an object public and there is no signed-URL surface: the
doctor's card shows the path, and the bytes are reachable only by someone with
IAM on the bucket. The runtime service account gets roles/storage.objectAdmin on
that one bucket in deploy.sh, and on nothing else.

The Cloud Storage client is synchronous, so every call is handed to a worker
thread and the event loop keeps serving the console's two-second poll.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

log = logging.getLogger("sanad.storage")

BUCKET = os.environ.get("LABS_BUCKET", "").strip()

EXTENSIONS = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/heic": "heic",
}


def enabled() -> bool:
    """False when no bucket is configured; the slip is then read but not kept."""
    return bool(BUCKET)


def _put(path: str, data: bytes, content_type: str) -> None:
    from google.cloud import storage

    client = storage.Client()
    blob = client.bucket(BUCKET).blob(path)
    blob.upload_from_string(data, content_type=content_type)


async def put_image(
    data: bytes, *, run_id: str, patient_id: str, mime: str = "image/jpeg"
) -> str:
    """Store one photo, return its gs:// path. Empty string when disabled."""
    if not enabled() or not data:
        return ""
    ext = EXTENSIONS.get((mime or "").lower(), "bin")
    path = f"{run_id}/{patient_id}/{uuid.uuid4().hex}.{ext}"
    await asyncio.to_thread(_put, path, data, mime or "application/octet-stream")
    log.info("stored image gs://%s/%s bytes=%d", BUCKET, path, len(data))
    return f"gs://{BUCKET}/{path}"
