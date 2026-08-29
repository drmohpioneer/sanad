"""Owns patient onboarding: the one-time token, its deep link, its QR, hello.

The doctor never types a phone number. When he confirms a record, Sanad mints a
token, and the commit card carries the t.me link plus a QR of the same link so
he can forward one picture. The first /start that presents the token binds that
chat to that patient; the token is then burnt and cannot bind a second phone.

`welcome` below is the other half of onboarding, added at rev 17. Until then a
patient who opened the web page saw an empty grey chat: nothing said who this
was, and the plan the doctor had confirmed an hour earlier was never sent to him
at all, so he only ever saw it if he later said "I lost the prescription". The
Telegram bind did have a good hello, but it was written straight to Telegram and
never as an event, so the web page could not show it either.

Three bubbles now, on whichever channel he opens first, through `fanout` so both
channels and the doctor's own feed see the same three:

  who this is, and that it is not a doctor   (templates.welcome)
  the doctor's confirmed plan text, verbatim (templates.plan_again + the text)
  what happens next, and how to talk back    (templates.welcome_next)

The plan is the doctor's own words, which SAFETY.md already names as the
trusted path: it is not generated, not summarised and not a template field, so
it goes out as its own message rather than inside one.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import Optional

from . import gender, lang, names, store, telegram, templates
from .adapters import OutboundMessage, fanout
from .models import Doctor, LinkToken, Patient

# How long a patient link opens anything. codex item 14: it opened a patient's
# whole record and it never expired, so a QR printed on a slip in March was a
# bearer credential in December. Thirty days is longer than any follow-up in
# the demo and short enough that a lost link stops being a key. A patient whose
# link has run out gets a new one the next time his doctor confirms anything
# about him, which is the same gesture that made the first.
LINK_TTL_DAYS = 30


def expired(token: LinkToken, at: Optional[datetime] = None) -> bool:
    """Is this link past its life? A pure function of the clock, so it is tested
    without a store and cannot be raced: expiry is not a state anybody writes."""
    return ((at or store.now()) - token.created_at) > timedelta(days=LINK_TTL_DAYS)


def usable(token: Optional[LinkToken], at: Optional[datetime] = None) -> bool:
    """May this token open anything at all?

    Being used is deliberately not part of this. Burning a token is about
    binding a second phone to a patient, and the web page has always stayed
    readable afterwards: the runbook depends on it, because a judge with no
    Telegram plays the patient on that page.
    """
    return token is not None and not token.revoked and not expired(token, at)


async def mint(doctor: Doctor, patient: Patient,
               token_id: str = "") -> LinkToken:
    """One patient link. `token_id` lets the caller make the mint repeatable.

    The Registrar passes an id derived from the confirmation (codex item 6), so
    a Confirm that ran twice hands the doctor one link and not two. Everything
    else mints a fresh one.
    """
    return await store.save_link_token(
        LinkToken(
            id=token_id or store.new_id(),
            doctor_id=doctor.id,
            patient_id=patient.id,
            created_at=store.now(),
        )
    )


async def consume(token_id: str) -> Optional[LinkToken]:
    """Bind this token, once, or answer None. The bind is one operation.

    codex item 14. The read and the burn used to be two calls with an await
    between them, so two /start messages inside the same second both saw an
    unused token and both bound themselves to the patient. That is not a race
    needing bad luck: it is one person forwarding a link to himself twice.
    `store.consume_link_token` is a transaction and exactly one caller wins it.

    Expiry is checked here rather than in the store, because it is a function of
    the clock and not of the record: nothing has to be written for a link to run
    out, and nothing can race a date.
    """
    token = await store.get_link_token(token_id)
    if not usable(token):
        return None
    return await store.consume_link_token(token_id)


def qr_png(url: str) -> bytes:
    """Deep link -> PNG. Generated per request; nothing is stored."""
    import qrcode

    buf = io.BytesIO()
    qrcode.make(url).save(buf, format="PNG")
    return buf.getvalue()


async def card_lines(token: LinkToken, base_url: str) -> list[str]:
    """The two lines the commit card shows the doctor."""
    link = await telegram.deep_link(token.id)
    if not link:
        return ["Patient link: pending bot token (Telegram not configured yet)."]
    return [f"Patient link: {link}", f"QR: {base_url.rstrip('/')}/qr/{token.id}.png"]


# --------------------------------------------------------------------------- #
# Hello, once per patient, on whichever channel he opens first
# --------------------------------------------------------------------------- #
def _audit(template: str) -> dict:
    return {"audit": {"tier": "onboarding", "generated": "code template",
                      "template": template}}


async def welcome(patient: Patient, doctor: Doctor) -> bool:
    """The first three bubbles. False when this patient has already had them.

    The flag is written before the first message is sent, not after. A patient
    who reloads the page while the first send is still in flight is the case
    that matters here, and sending the plan twice is a worse failure than the
    rare one where the flag is set and a send then throws: the second is a
    missing hello on a chat he can talk into, the first is a bot that looks
    broken in the first ten seconds.
    """
    if patient.welcomed_at is not None:
        return False
    await store.update_patient(patient.id, welcomed_at=store.now())

    speak = await lang.for_patient(patient, doctor.id)
    who = gender.of_patient(patient)
    first = names.vocative(patient.name, speak)
    out = fanout()
    ref = f"patient:{patient.id}"

    await out.send(ref, OutboundMessage(
        text=templates.render("welcome", speak, who, patient=first,
                              doctor=doctor.name),
        meta=_audit("welcome")))

    plan = (patient.plan_text or "").strip()
    if plan:
        head = templates.render("plan_again", speak, who, doctor=doctor.name)
        await out.send(ref, OutboundMessage(
            text=f"{head}\n{plan}",
            meta={"audit": {"tier": "onboarding",
                            "generated": "the doctor's own confirmed plan text",
                            "template": "plan_again"}}))

    await out.send(ref, OutboundMessage(
        text=templates.render("welcome_next", speak, who, doctor=doctor.name),
        meta=_audit("welcome_next")))
    return True
