"""Owns the Telegram side: the Bot API calls, and nothing about what to say.

The bot token and the webhook secret arrive as mounted Secret Manager values and
never leave this module. Every inbound update is rejected unless Telegram sends
back the secret token we registered with setWebhook, so the public /tg URL is
useless to anyone else.

One bot serves both roles. Which role a chat has is a Firestore lookup, never a
guess: a doctor is bound once through the admin endpoint, a patient is bound by
tapping the one-time deep link on his commit card.
"""

from __future__ import annotations

import os
import secrets as pysecrets
from typing import Any, Optional

import httpx

from . import store

TOKEN = os.environ.get("SANAD_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "").strip()
API = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(20.0)

# The bot's own @username, fetched once per process. It is public, immutable and
# not per-request state, which is why this is the one thing Sanad caches.
_username: Optional[str] = None


def enabled() -> bool:
    """False until the bot token exists; every send then becomes a no-op."""
    return bool(TOKEN)


def verify_secret(header_value: Optional[str]) -> bool:
    """Constant-time check of Telegram's X-Telegram-Bot-Api-Secret-Token."""
    if not WEBHOOK_SECRET:
        return False
    return pysecrets.compare_digest(header_value or "", WEBHOOK_SECRET)


async def api(method: str, payload: Optional[dict] = None, **kw: Any) -> dict:
    """One Bot API call. Raises nothing: a dead bot must not break a reply."""
    if not enabled():
        return {"ok": False, "description": "no bot token"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        resp = await http.post(f"{API}/bot{TOKEN}/{method}", json=payload or {}, **kw)
        try:
            return resp.json()
        except ValueError:
            return {"ok": False, "status": resp.status_code}


async def bot_username() -> str:
    global _username
    if _username is None:
        me = await api("getMe")
        _username = (me.get("result") or {}).get("username", "")
    return _username or ""


async def deep_link(link_token: str) -> str:
    """t.me/<bot>?start=<one-time token>. Empty when the bot is not configured."""
    name = await bot_username()
    return f"https://t.me/{name}?start={link_token}" if name else ""


async def set_webhook(url: str) -> dict:
    return await api("setWebhook", {
        "url": url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    })


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def keyboard(card: Optional[dict]) -> Optional[dict]:
    """Card actions -> inline keyboard. Same action ids as the console buttons."""
    actions = (card or {}).get("actions") or []
    if not actions:
        return None
    return {"inline_keyboard": [[{"text": a["label"], "callback_data": a["id"]}]
                                for a in actions]}


def render(text: str, card: Optional[dict]) -> str:
    if not card:
        return text
    lines = [card.get("title", ""), *card.get("lines", [])]
    return "\n".join([text, "", *[l for l in lines if l]]).strip()


async def send_card(chat_id: int, text: str, card: Optional[dict] = None) -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": render(text, card)}
    markup = keyboard(card)
    if markup:
        payload["reply_markup"] = markup
    return await api("sendMessage", payload)


async def send_photo(chat_id: int, png: bytes, caption: str = "") -> dict:
    """Used for the patient's QR image, so the doctor can forward one picture."""
    if not enabled():
        return {"ok": False}
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        resp = await http.post(
            f"{API}/bot{TOKEN}/sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption[:1000]},
            files={"photo": ("link.png", png, "image/png")},
        )
        return resp.json()


async def answer_callback(callback_id: str, text: str = "") -> None:
    await api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


# --------------------------------------------------------------------------- #
# Receiving
# --------------------------------------------------------------------------- #
async def download(file_id: str) -> Optional[bytes]:
    """getFile then fetch the bytes: voice notes and photos both come this way."""
    if not enabled():
        return None
    info = await api("getFile", {"file_id": file_id})
    path = (info.get("result") or {}).get("file_path")
    if not path:
        return None
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        resp = await http.get(f"{API}/file/bot{TOKEN}/{path}")
        return resp.content if resp.status_code == 200 else None


async def chat_id_for(target_ref: str) -> Optional[int]:
    """'doctor:<token>' | 'patient:<id>' -> the bound chat id, or None."""
    kind, _, value = target_ref.partition(":")
    if kind == "doctor":
        doctor = await store.doctor_by_token(value)
        return doctor.telegram_chat_id if doctor else None
    if kind == "patient":
        patient = await store.get_patient(value)
        return (patient.channels or {}).get("telegram_chat_id") if patient else None
    return None
