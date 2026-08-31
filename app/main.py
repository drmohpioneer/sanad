"""Sanad HTTP surface: routes only, no product logic.

Four groups:
  /health            - liveness (never /healthz, see the note on the handler)
  /c/{token}/*       - the doctor's web console
  /tg, /qr/*         - the Telegram webhook and the patient link's QR image
  /p/{link_token}    - the patient's own phone page (the no-Telegram fallback)
  /tasks/*           - Cloud Tasks calling Sanad back, OIDC-verified
  /admin/*           - seed, reset, the twenty background patients, bind the
                       doctor's phone, webhook, demo knobs

Every route is stateless: resolve the token, load what it needs from Firestore,
hand off to core/, return. Nothing is cached between requests. Rev 17 deleted
the last two exceptions: POST /spike/gemini and POST /spike/voice, the S0
spikes, had no check of any kind on a public service, so anyone with the URL
had a free Gemini proxy and a free transcription service on this billing
account, and the module-level ADK session service they shared grew a session
per call and never dropped one. The removal is preserved as public regression
coverage in `app/tests/test_dashboard_routes.py`; those unauthenticated routes
do not need to stay reachable in production to prove the product path.
"""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, StrictBool

from core import (
    background,
    board,
    cards,
    chaser,
    concierge,
    contract,
    dispatch,
    doctor_actions,
    events,
    extractor,
    gender,
    lang,
    links,
    live_transport,
    media,
    monitoring,
    policy,
    registrar,
    runtime,
    settings,
    storage,
    store,
    summary,
    tasks,
    telegram,
    tg_router,
    timing,
    uploads,
    views,
    workspace,
)
from core.adapters import (
    InboundMessage,
    OutboundMessage,
    fanout,
    send_card as send_edge_card,
)
from core.channel_contracts import ActorRef, Command, CommandResult, CommandStatus
from core.models import Doctor, Patient
from core.transport_runtime import CommandSpec, TransportRuntime, legacy_result

MODEL = media.MODEL
WEB = os.path.join(os.path.dirname(__file__), "web")
CONSOLE_HTML = os.path.join(WEB, "console.html")
PATIENT_HTML = os.path.join(WEB, "patient.html")
DASHBOARD_HTML = os.path.join(WEB, "dashboard.html")
DASHBOARD_V2_HTML = os.path.join(WEB, "dashboard_v2.html")

# Cloud Run captures stdout, but an unconfigured root logger drops INFO, which
# is where the Chaser says why it dropped a task ("stale run id", "already
# sent"). That reasoning is the audit trail, so it is turned on here.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
# httpx writes one INFO line per request, and it prints the whole URL. The
# Telegram API carries the bot token IN THE PATH
# (api.telegram.org/bot<token>/sendMessage), so every card Sanad sent wrote the
# live bot token into Cloud Logging, where it is kept for thirty days and can be
# read by anyone with roles/logging.viewer. That is the same defect as security
# audit H1, which took the admin secret out of the query string for exactly this
# reason, and it was found in the logs of the deployed service, not in a test:
#   INFO httpx HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage
#     "HTTP/1.1 400 Bad Request"
# Warnings and errors from httpx still come through; only the per-request line
# with the URL in it goes.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sanad")


_TRANSPORT: Optional[TransportRuntime] = None


async def _message_command(command: Command) -> CommandResult:
    envelope = command.envelope
    inbound = envelope.transient_payload if envelope is not None else None
    if not isinstance(inbound, InboundMessage):
        return CommandResult.rejected("invalid_message_bridge")
    await dispatch.handle_inbound(inbound)
    return legacy_result({"ok": True})


async def _action_command(command: Command) -> CommandResult:
    envelope = command.envelope
    invocation = envelope.transient_payload if envelope is not None else None
    if (
        not isinstance(invocation, tuple)
        or len(invocation) != 3
        or not isinstance(invocation[0], ActionIn)
        or not isinstance(invocation[2], Doctor)
    ):
        return CommandResult.rejected("invalid_action_bridge")
    body, request, doctor = invocation
    return legacy_result(await _legacy_action(body, request, doctor))


async def _telegram_command(command: Command) -> CommandResult:
    envelope = command.envelope
    invocation = envelope.transient_payload if envelope is not None else None
    if not isinstance(invocation, live_transport.TelegramInvocation):
        return CommandResult.rejected("invalid_telegram_bridge")
    try:
        await tg_router.handle_provider_update(
            dict(invocation.update),
            invocation.base_url,
            secret_token=invocation.secret_token,
        )
    except tg_router.RetryableCallbackError:
        # The retiring action claim is known to be released. Releasing the
        # outer provider receipt lets Telegram redeliver this same update.
        return CommandResult.retryable(
            "legacy_callback_retry",
            "the callback action claim was released for retry",
        )
    return legacy_result({"ok": True})


async def _nudge_command(command: Command) -> CommandResult:
    envelope = command.envelope
    payload = envelope.transient_payload if envelope is not None else None
    if not isinstance(payload, dict):
        return CommandResult.rejected("invalid_task_bridge")
    # The sweep is part of the claimed command. It can no longer run before a
    # replay decision and thereby turn a duplicate task into a second mutation.
    try:
        freed = await store.reclaim_stale()
        if any(freed.values()):
            log.info("reclaimed stale claims: %s", freed)
    except Exception:  # noqa: BLE001 - the nudge is what this command is for
        log.warning("the stale-claim sweep could not run", exc_info=True)
    try:
        result = await chaser.fire(payload)
    except chaser.RetryableNudgeError:
        # The Chaser has already persisted a failed-send receipt (or rolled
        # every pre-send reservation back). Releasing the outer command claim
        # is what lets Cloud Tasks re-enter that explicit legacy retry path.
        return CommandResult.retryable(
            "legacy_nudge_retry",
            "the Chaser recorded a retry-safe failure",
        )
    durable_result = live_transport.durable_task_result(result)
    log.info("nudge result=%s", durable_result)
    return legacy_result(
        result,
        durable_body=durable_result,
    )


def transport_runtime() -> TransportRuntime:
    """The one registry and CommandBus shared by every live ingress route."""
    global _TRANSPORT
    if _TRANSPORT is None:
        _TRANSPORT = live_transport.build(
            {
                live_transport.MESSAGE: _message_command,
                live_transport.ACTION: _action_command,
                live_transport.TELEGRAM_UPDATE: _telegram_command,
                live_transport.NUDGE: _nudge_command,
            }
        )
    return _TRANSPORT


async def _local_nudge(payload: dict, task_name: str) -> dict:
    """The in-process fallback enters the same task adapter and command bus."""
    identity = live_transport.task_identity(payload, task_name)
    outcome = await transport_runtime().execute(
        "cloud_tasks",
        payload,
        live_transport.TaskContext(
            task_name=task_name,
            verified_provider=False,
        ),
        CommandSpec(
            id=uuid.uuid4().hex,
            idempotency_key=identity,
            kind=live_transport.NUDGE,
            payload={"ingress_sha256": live_transport.ingress_digest(payload)},
        ),
    )
    if outcome.result.status == CommandStatus.CONFLICT:
        return {"sent": False, "reason": outcome.result.code}
    if outcome.result.status == CommandStatus.RETRYABLE:
        raise chaser.RetryableNudgeError(outcome.result.detail)
    return outcome.legacy_response()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """One check when the container comes up, and nothing else.

    S12 item 2. A patient record holding a doctor's own Telegram chat is the
    quietest wrong state this system has: everything keeps working, the doctor's
    typed messages simply arrive as that patient's words. It survived a whole
    evening of live testing on 2026-08-29 because nothing anywhere said so. It
    is said now, on the doctor's own board and in the log, every time an
    instance starts and every time a board is reset.

    It is best effort by design. A startup check that could stop the service
    would be a worse failure than the one it is looking for, so a Firestore
    that is not answering yet costs a log line and the instance still serves.
    """
    runtime.validate_gate2()
    transport_runtime()
    tasks.register_local_handler(_local_nudge)
    try:
        await tg_router.wrong_bindings()
    except Exception:  # noqa: BLE001 - the service starts either way
        log.warning("the doctor-chat binding check could not run", exc_info=True)
    yield


app = FastAPI(title="Sanad", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
# Cloud Run's Google Frontend swallows the exact path /healthz before it reaches
# the container (proved 2026-08-28: /healthz returns a GFE HTML 404 with no
# `server: Google Frontend` header, while /health, /healthz2 and /nonexistent all
# reach FastAPI). /health is the reachable alias - use it, never /healthz.
@app.get("/healthz")
@app.get("/health")
async def healthz() -> dict:
    run_id, time_scale = await settings.current()
    return {
        "ok": True,
        "ffmpeg": await media.ffmpeg_version_async(),
        "model": MODEL,
        "telegram": telegram.enabled(),
        # Named from the runtime's own environment, never hard-coded: what the
        # console header shows is what this container actually is.
        "service": os.environ.get("K_SERVICE"),
        "region": os.environ.get("TASKS_REGION"),
        "project": store.PROJECT,
        "revision": os.environ.get("K_REVISION"),
        # The Chaser's engine is reported, never assumed: "cloudtasks" is the
        # product path, "inprocess" is the documented fallback.
        "chaser": tasks.engine(),
        "labs_bucket": storage.enabled(),
        "run_id": run_id,
        "time_scale": time_scale,
    }


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
# The admin secret travels in this header and nowhere else (security audit H1).
ADMIN_HEADER = "X-Sanad-Admin"
QUERY_SECRET_REFUSED = (
    "the admin secret goes in the X-Sanad-Admin header, never in the URL"
)


def require_admin(request: Request) -> None:
    """Admin routes share one secret, in a header, and 404 when it is wrong.

    Security audit H1. The secret used to be a query parameter, which put it in
    the path of every access-log line: `POST /admin/seed?secret=<48 hex>` sat in
    Cloud Logging for thirty days, readable by anyone with roles/logging.viewer,
    and the same log carried every doctor's console token in `GET /c/<token>/
    feed`. Moving it to a header is the half that matters, because Cloud Run's
    own request log records the query string whatever uvicorn does; the
    Dockerfile turns uvicorn's access log off as well, so the container stops
    writing a second copy of the console tokens.

    A secret in the query string is refused with 401 and never compared, so
    nothing keeps writing one out of habit: a caller that has not been updated
    is told, once, in the response body. A wrong header is still a 404, not a
    403, so an attacker cannot tell an admin route from a typo.

    `hmac.compare_digest` for the comparison, which is what `secrets.compare_
    digest` already was (it is the same function), named here as the thing the
    audit asked for so a reader does not have to know that.
    """
    if "secret" in request.query_params:
        raise HTTPException(401, QUERY_SECRET_REFUSED)
    expected = os.environ.get("ADMIN_SECRET", "")
    if not expected:
        raise HTTPException(503, "ADMIN_SECRET is not configured")
    if not hmac.compare_digest(request.headers.get(ADMIN_HEADER, ""), expected):
        raise HTTPException(404, "Not Found")


DEMO_DOCTOR = "Dr Mohamed"
DEMO_SPECIALTY = "cardiology"


@app.post("/admin/seed")
async def seed(
    request: Request, name: str = DEMO_DOCTOR,
    specialty: str = DEMO_SPECIALTY,
    _: None = Depends(require_admin),
) -> dict:
    """Create the doctor if he is not there yet, and return his console URL.

    `name` and `specialty` default to the demo doctor, so the runbook's command
    is unchanged. They are parameters because automated tests need a doctor of
    their own: every card Sanad produces fans out to whatever Telegram chat is
    bound, and an agent running the suite against the demo doctor put about
    thirty cards on Mohamed's phone in one night (S4). A second doctor with no
    chat bound is visible in his own console and silent everywhere else, so a
    test run cannot reach a real phone at all.
    """

    name = (name or DEMO_DOCTOR).strip() or DEMO_DOCTOR
    doctor = await store.doctor_by_name(name)
    created = doctor is None
    if doctor is None:
        doctor = await store.create_doctor(name, specialty=specialty)
    elif doctor.specialty != specialty:
        # Doctors seeded before specialties existed default to general practice.
        await store.update_doctor(doctor.id, specialty=specialty)
        doctor = await store.doctor_by_id(doctor.id) or doctor
    log.info("seed doctor_id=%s name=%s created=%s", doctor.id, name, created)
    return {
        "created": created,
        "doctor_id": doctor.id,
        "synthetic": doctor.synthetic,
        "name": doctor.name,
        "telegram_bound": doctor.telegram_chat_id is not None,
        "console_url": str(request.base_url).rstrip("/") + f"/c/{doctor.web_token}",
    }


@app.post("/admin/reset")
async def reset(name: str = DEMO_DOCTOR,
                _: None = Depends(require_admin)) -> dict:
    """Wipe one doctor's board so a rehearsal starts from nothing.

    Patients, loops, events, pending confirms, link tokens and relays go; the
    doctor and his console token survive, so the URL in the runbook keeps
    working. `name` defaults to the demo doctor, and naming a test doctor here
    clears that doctor's board without touching the demo board.

    Every per-doctor knob goes with the board (rev 17 item 16). The stored
    policy is the one that matters: a rehearsal that set `max_contacts` to 2 to
    film a refusal left that 2 on the record, so the next reseeded board ran on
    a ceiling nobody remembered setting and the guard refused things the runbook
    said would be sent. Reset means defaults, and the defaults live in
    core/policy.py, so an empty policy is the whole of it. The half-typed relay
    and note the doctor was in the middle of go too: they point at rows that no
    longer exist.
    """
    doctor = await store.doctor_by_name((name or DEMO_DOCTOR).strip() or DEMO_DOCTOR)
    if doctor is None:
        raise HTTPException(404, "Not Found")
    deleted = await store.wipe_doctor(doctor.id)
    await store.update_doctor(
        doctor.id, policy={}, awaiting_relay_id=None,
        awaiting_note_loop_id=None, awaiting_since=None,
    )
    # S12 item 2. A doctor's own chat bound to a patient record is silent and
    # wrong: his typed messages arrive as that patient's and every message meant
    # for the patient reaches his phone instead. It happened live on 2026-08-29
    # and nothing on the board said so. The wipe above is the repair, because
    # the binding lives on a patient record and reset deletes those; this is the
    # report, and it runs AFTER the wipe so what it names is what is still
    # wrong. It runs at startup too (`check_doctor_chats`).
    bindings = await tg_router.wrong_bindings()
    log.info("reset doctor_id=%s deleted=%s policy=cleared", doctor.id, deleted)
    return {"ok": True, "doctor_id": doctor.id, "deleted": deleted,
            "policy": "cleared, back to the defaults in core/policy.py",
            "doctor_chats_bound_as_patients": bindings}


@app.post("/admin/rotate-token")
async def rotate_token(
    request: Request, name: str = DEMO_DOCTOR,
    revoke_links: bool = False,
    _: None = Depends(require_admin),
) -> dict:
    """Mint this doctor a new console token and kill the old one.

    rev 17 item 8. The console URL is a bearer credential: whoever has it can
    dictate, confirm and answer cards on that board. The submission video is
    required to show a live `.run` URL in the address bar, and the demo is the
    console, so the token is legible on YouTube for the weeks between upload and
    the result. Rotating it after the final take and before the upload is what
    makes the one on camera worthless.

    Nothing else changes: the doctor, his patients, his loops and his whole feed
    are the same records. Only the door key is new, so re-open the console at
    the URL this returns before recording anything else.

    `revoke_links=true` also kills every patient link on the board (codex item
    14). Use it when a patient page or a QR was on camera: the record survives,
    and each patient is given a fresh link the next time the doctor confirms
    anything about him.
    """
    doctor = await store.doctor_by_name((name or DEMO_DOCTOR).strip() or DEMO_DOCTOR)
    if doctor is None:
        raise HTTPException(404, "Not Found")
    token = store.new_web_token()
    await store.update_doctor(doctor.id, web_token=token)
    log.info("rotated console token doctor_id=%s", doctor.id)
    # `revoke_links=true` kills every patient link this doctor has minted, which
    # is the doctor-side revoke of codex item 14. It hangs off this call because
    # this is the gesture that already exists for "everything on camera is now
    # worthless", and a patient link on camera is worth more than the console
    # one: it opens one person's record with no second factor at all.
    revoked = await store.revoke_link_tokens(doctor.id) if revoke_links else 0
    return {
        "ok": True,
        "doctor_id": doctor.id,
        "name": doctor.name,
        "console_url": str(request.base_url).rstrip("/") + f"/c/{token}",
        "old_token": "dead: the URL in the recording is now a 404",
        "patient_links_revoked": revoked,
    }


@app.post("/admin/seed-background")
async def seed_background(name: str = DEMO_DOCTOR,
                          _: None = Depends(require_admin)) -> dict:
    """Put the twenty synthetic background patients on one doctor's board.

    S6++ item J. All twenty are invented (core/background.py says so at the top
    and the names, the phone block and the diagnoses are all made up), and the
    seeder writes patients, loops, events and relays and nothing else: no Cloud
    Task is created and no message is sent, so seeding cannot reach anybody's
    phone. The document ids are derived from the doctor, so running this twice
    replaces the same twenty rather than creating forty.

    `name` defaults to the demo doctor and the runbook names the Test Doctor.
    """
    doctor = await store.doctor_by_name((name or DEMO_DOCTOR).strip() or DEMO_DOCTOR)
    if doctor is None:
        raise HTTPException(404, "Not Found")
    written = await background.seed(doctor)
    log.info("seed-background doctor_id=%s wrote=%s", doctor.id, written)
    return {"ok": True, "doctor_id": doctor.id, "doctor": doctor.name, **written}


class PolicyIn(BaseModel):
    """One doctor's Coordinator policy. Every field is optional.

    Anything missing, and anything unreadable, falls back to the defaults in
    core/policy.py, which is what the demo runs on: there is no settings screen
    in this build and there does not need to be one.
    """

    earliest_days: Optional[int] = None
    grace_days: Optional[int] = None
    max_contacts: Optional[int] = None
    max_per_day: Optional[int] = None
    quiet_from: Optional[int] = None
    quiet_until: Optional[int] = None
    max_evidence_requests: Optional[int] = None
    cost_escalate_only: Optional[bool] = None
    followup_reason: Optional[str] = None


@app.post("/admin/settings")
async def admin_settings(
    body: Optional[PolicyIn] = None,
    run_id: Optional[str] = None,
    time_scale: Optional[int] = None, doctor_name: str = "",
    _: None = Depends(require_admin),
) -> dict:
    """Turn the two demo knobs, and set a doctor's policy, without a redeploy.

    `run_id` bumps the demo run: tasks created under the old one are dropped
    when they fire. `time_scale` is how many real seconds make one Sanad day.
    A body, with `doctor_name`, stores that doctor's Coordinator policy.
    Passing none of them just reads everything back.
    """
    await store.set_settings(run_id=run_id, time_scale=time_scale)
    now_run_id, now_scale = await settings.current()

    stored = None
    if body is not None and doctor_name.strip():
        doctor = await store.doctor_by_name(doctor_name.strip())
        if doctor is None:
            raise HTTPException(404, "Not Found")
        given = {k: v for k, v in body.model_dump().items() if v is not None}
        # Read back through core/policy.parse, so what is stored is what the
        # guards will actually enforce and not what was typed.
        await store.update_doctor(doctor.id, policy=given)
        stored = policy.parse(given).as_meta()

    return {"ok": True, "run_id": now_run_id, "time_scale": now_scale,
            "policy": stored}


class DoctorFeaturesIn(BaseModel):
    """The explicit, reversible doctor-scoped Gate 3 rollout switch."""

    doctor_id: str
    cockpit_v2_enabled: StrictBool


@app.post("/admin/doctor-features")
async def admin_doctor_features(
    body: DoctorFeaturesIn,
    _: None = Depends(require_admin),
) -> dict:
    """Enable or roll back the v2 cockpit for exactly one doctor.

    The flag is not accepted from a console request, query string, cookie, or
    local storage.  Reset and token rotation leave it alone; rollback is this
    same authenticated write with ``cockpit_v2_enabled=false``.
    """
    doctor = await store.doctor_by_id(body.doctor_id)
    if doctor is None:
        raise HTTPException(404, "Not Found")
    feature_fields = {"cockpit_v2_enabled": body.cockpit_v2_enabled}
    if body.cockpit_v2_enabled:
        # Fact capture is a one-way enrollment. Rolling the presentation back
        # must not create a silent hole before a later re-enable.
        feature_fields["workspace_facts_enabled"] = True
    await store.update_doctor(doctor.id, **feature_fields)
    facts_enabled = doctor.workspace_facts_enabled or body.cockpit_v2_enabled
    log.info(
        "doctor feature doctor_id=%s cockpit_v2_enabled=%s",
        doctor.id,
        body.cockpit_v2_enabled,
    )
    return {
        "ok": True,
        "doctor_id": doctor.id,
        "cockpit_v2_enabled": body.cockpit_v2_enabled,
        "workspace_facts_enabled": facts_enabled,
    }


BIND_NEEDS_CHAT_ID = (
    "chat_id is required: send /start to the bot, read the chat id off "
    "GET /admin/pending-starts or the bot's own reply, and pass it here"
)


@app.get("/admin/pending-starts")
async def pending_starts(_: None = Depends(require_admin)) -> dict:
    """Which chats have said /start, so the bind call can name one.

    The other half of the M3 fix below: binding needs an explicit chat id, so
    there has to be somewhere to read one that is not "the newest". This lists
    them with their display names, behind the same admin secret, and creates
    nothing.
    """
    rows = await store.list_pending_starts()
    return {"pending": [{"chat_id": r.chat_id, "name": r.display_name,
                         "at": r.created_at.isoformat()} for r in rows]}


@app.post("/admin/bind-doctor")
async def bind_doctor(chat_id: Optional[int] = None,
                      _: None = Depends(require_admin)) -> dict:
    """Bind Mohamed's Telegram chat to the doctor record, once.

    Security audit M3. This used to bind "the newest unknown /start" when no
    chat id was given, and the bot's username is public: anyone who sent /start
    in the seconds between Mohamed's /start and his bind call became the
    doctor's phone, and every card, every patient message and every lab result
    went to that chat. The window was small and the consequence was total.

    So the chat id is required. He reads it from the bot's own reply or from
    GET /admin/pending-starts, and passes it. Nothing is guessed.

    Passing chat_id=0 unbinds, for when a rehearsal bound the wrong phone.
    """
    doctor = await store.doctor_by_name(DEMO_DOCTOR)
    if doctor is None:
        raise HTTPException(404, "Not Found")
    if chat_id is None:
        raise HTTPException(400, BIND_NEEDS_CHAT_ID)
    if chat_id == 0:
        await store.update_doctor(doctor.id, telegram_chat_id=None)
        return {"ok": True, "doctor_id": doctor.id, "chat_id": None}
    await store.update_doctor(doctor.id, telegram_chat_id=chat_id)
    await send_edge_card(
        chat_id,
        f"Bound. This phone is now {doctor.name}'s.",
        target_ref=f"doctor:{doctor.web_token}",
    )
    return {"ok": True, "doctor_id": doctor.id, "chat_id": chat_id}


@app.post("/admin/telegram/setup")
async def telegram_setup(request: Request,
                         _: None = Depends(require_admin)) -> dict:
    """Point the bot at this service's /tg, with the webhook secret attached.

    Done from inside the container on purpose: the bot token never has to appear
    in a shell command to register the webhook.
    """
    if not telegram.enabled():
        return {"ok": False, "reason": "no bot token configured"}
    url = str(request.base_url).rstrip("/") + "/tg"
    result = await telegram.set_webhook(url)
    return {"ok": bool(result.get("ok")), "webhook": url,
            "bot": await telegram.bot_username(),
            "description": result.get("description", "")}


# --------------------------------------------------------------------------- #
# Cloud Tasks calling back
# --------------------------------------------------------------------------- #
@app.post("/tasks/nudge")
async def tasks_nudge(request: Request) -> dict:
    """The Chaser's only door, and it is locked.

    Cloud Tasks presents a Google-signed OIDC token for the runtime service
    account, minted for this service's own URL as its audience. Anything else -
    no token, another audience, another service account, a hand-rolled JWT - is
    a 403, so the fact that this URL is public buys an attacker nothing.

    It always answers 200. A refusal ("stale run id", "already sent") is a
    decision, not a failure, and a non-2xx would have Cloud Tasks retry it.
    """
    try:
        await tasks.verify_caller(request.headers.get("authorization"))
    except Exception as exc:  # noqa: BLE001 - every failure is the same 403
        log.warning("rejected /tasks/nudge: %s", exc)
        raise HTTPException(403, "Forbidden")
    payload = await request.json()
    task_name = request.headers.get("x-cloudtasks-taskname") or ""
    if not task_name.strip():
        raise HTTPException(403, "Forbidden")
    identity = live_transport.task_identity(payload, task_name)
    outcome = await transport_runtime().execute(
        "cloud_tasks",
        payload,
        live_transport.TaskContext(task_name=task_name),
        CommandSpec(
            id=uuid.uuid4().hex,
            idempotency_key=identity,
            kind=live_transport.NUDGE,
            payload={"ingress_sha256": live_transport.ingress_digest(payload)},
        ),
    )
    if outcome.result.status == CommandStatus.CONFLICT:
        return {"sent": False, "reason": outcome.result.code}
    if outcome.result.status == CommandStatus.RETRYABLE:
        # A non-2xx response asks Cloud Tasks to retry the same task name. The
        # command claim was released by CommandBus, while the Chaser's own
        # failed receipt constrains the retry to its one resend.
        raise HTTPException(503, "Retryable task failure")
    return outcome.legacy_response()


# --------------------------------------------------------------------------- #
# Telegram webhook + the patient link's QR image
# --------------------------------------------------------------------------- #
@app.post("/tg")
async def telegram_webhook(request: Request) -> dict:
    """Every update is rejected unless it carries the secret we registered."""
    secret_token = request.headers.get("x-telegram-bot-api-secret-token")
    if not telegram.verify_secret(secret_token):
        raise HTTPException(404, "Not Found")
    update = await request.json()
    log.info("tg update=%s", update.get("update_id"))
    update_id = str(update.get("update_id") or "").strip()
    if not update_id:
        raise HTTPException(400, "Telegram update_id is required")
    outcome = await transport_runtime().execute(
        "telegram",
        update,
        live_transport.TelegramContext(
            base_url=str(request.base_url),
            secret_token=secret_token or "",
        ),
        CommandSpec(
            id=uuid.uuid4().hex,
            idempotency_key=f"sanad-bot:{update_id}",
            kind=live_transport.TELEGRAM_UPDATE,
            payload={"ingress_sha256": live_transport.ingress_digest(update)},
        ),
    )
    # A provider replay that is already running is acknowledged, never invoked
    # through a second domain path. Completed replays carry this same body.
    if outcome.result.status == CommandStatus.CONFLICT:
        return {"ok": True}
    if outcome.result.status == CommandStatus.RETRYABLE:
        # Telegram retries the same update_id on non-2xx. The bus has already
        # released its outer receipt, and the callback action claim is free.
        raise HTTPException(503, "Retryable Telegram callback failure")
    return {"ok": True}


@app.get("/qr/{link_token}.png")
async def qr(link_token: str) -> Response:
    """QR of one patient's deep link. Rendered per request, never stored."""
    token = await store.get_link_token(link_token)
    if token is None:
        raise HTTPException(404, "Not Found")
    url = await telegram.deep_link(token.id)
    if not url:
        raise HTTPException(503, "Telegram is not configured yet")
    return Response(content=links.qr_png(url), media_type="image/png")


# --------------------------------------------------------------------------- #
# Patient page - the same one-time link, opened in a browser instead of Telegram
# --------------------------------------------------------------------------- #
# Telegram is the patient's real channel. This page exists so a judge with no
# Telegram can still play the patient from a phone: same link, same patient
# record, same brain, same gates. It does not burn the link token, so opening
# the page never costs the patient his Telegram binding.
async def patient_from_link(link_token: str) -> Patient:
    """The patient behind a link, or a 404. Expiry and revocation are 404s too.

    codex item 14: this page is a patient's whole record and the link had no
    life at all. `core/links.usable` is the one rule, so the page, the QR and
    the Telegram bind cannot disagree about which links are dead. A link that is
    merely used still opens the page, which is what makes the no-Telegram
    demo path work.
    """
    token = await store.get_link_token(link_token)
    if not links.usable(token):
        raise HTTPException(404, "Not Found")
    patient = await store.get_patient(token.patient_id)
    if patient is None:
        raise HTTPException(404, "Not Found")
    return patient


@app.get("/p/{link_token}")
async def patient_page(link_token: str) -> FileResponse:
    """The patient's own page, and the first thing he ever reads on it.

    rev 17 item 9: the chat opens with the doctor's confirmed plan already in
    it, under the doctor's name, before the patient has typed anything. It is
    written as ordinary agent_out events, so the feed poll two seconds later
    shows it and the doctor's board shows it too. `links.welcome` is idempotent
    on the patient record, so a reload sends nothing.
    """
    patient = await patient_from_link(link_token)
    doctor = await store.doctor_by_id(patient.doctor_id)
    if doctor is not None:
        await links.welcome(patient, doctor)
    return FileResponse(PATIENT_HTML, media_type="text/html")


@app.get("/p/{link_token}/feed")
async def patient_feed(link_token: str, since: int = 0) -> dict:
    """Only this patient's own conversation. No cards, no other patients."""
    patient = await patient_from_link(link_token)
    rows = await events.last_events(patient.doctor_id, since)
    return {
        "name": patient.name,
        "events": [
            {"kind": e.kind, "text": e.text, "synthetic": e.synthetic,
             "ts_ms": events.ts_ms(e)}
            for e in rows
            if e.patient_id == patient.id and e.kind in ("patient_in", "agent_out")
        ],
    }


async def refuse_upload(patient: Patient, why: uploads.Rejected) -> dict:
    """One polite line, one event, and a 200. Security audit M2.

    An upload that is too large or is not a photo or a voice note is not an
    error and it is not a clinical event: nothing about the patient's care has
    happened. He is told, in his own language, what to send instead; the board
    carries the attempt so the doctor is not surprised by a gap; and the route
    answers 200 so the page shows the line rather than "not sent".
    """
    doctor = await store.doctor_by_id(patient.doctor_id)
    speak = await lang.for_patient(patient, patient.doctor_id)
    await events.append_event(
        patient.doctor_id, "system", f"upload refused: {why.reason}",
        patient_id=patient.id, channel="web",
        # `refusal`, not `refused`. Everywhere else in this system a meta
        # `refused` is the LIST of guard calls code turned down, and the board
        # renders it with `list.forEach` (web/dashboard.html refusedRows). This
        # event put a plain string there, and one oversized upload then threw
        # `TypeError: list.forEach is not a function` on the doctor's dashboard,
        # killing the render and with it the 2 s poll, so the board stopped
        # updating while the live pill still said "Live · synced". Found live on
        # rev 22, not in a test. The board is hardened as well, but the string
        # does not belong in that field.
        meta={"refusal": why.reason, "too_large": why.too_large,
              "decided_by": "code (core/uploads.py size and type rules)"},
    )
    if doctor is not None:
        await fanout().send(f"patient:{patient.id}", OutboundMessage(
            text=uploads.refusal_text(speak, gender.of_patient(patient),
                                      too_large=why.too_large),
            meta={"audit": {"tier": "upload", "refusal": why.reason}}))
    return {"ok": False, "refused": why.reason}


async def _web_message(
    inbound: InboundMessage,
    *,
    tenant_id: str,
    actor: ActorRef,
    principal: ActorRef,
    endpoint_id: str,
    thread_id: str,
    identity_method: str,
) -> dict:
    """Normalize one admitted browser message and execute the shared bus."""
    command_id = uuid.uuid4().hex
    outcome = await transport_runtime().execute(
        "web",
        inbound,
        live_transport.WebContext(
            provider_message_id=command_id,
            tenant_id=tenant_id,
            actor=actor,
            principal=principal,
            endpoint_id=endpoint_id,
            thread_id=thread_id,
            identity_method=identity_method,
        ),
        CommandSpec(
            id=command_id,
            idempotency_key=command_id,
            kind=live_transport.MESSAGE,
        ),
    )
    return outcome.legacy_response()


@app.post("/p/{link_token}")
async def patient_send(
    link_token: str, text: str = Form(""), file: Optional[UploadFile] = File(None)
) -> dict:
    patient = await patient_from_link(link_token)
    if len(text or "") > dispatch.MAX_PATIENT_TEXT:
        raise HTTPException(413, dispatch.patient_limit_text(text))
    raw, lane, mime = None, "", None
    if file is not None:
        # Security audit M2. The cap is applied while reading, and the lane is
        # decided from the bytes: the client's own content_type used to choose
        # between ffmpeg and Pillow, and it is a claim, not a fact.
        try:
            raw, lane, mime = await uploads.take(file)
        except uploads.Rejected as why:
            log.info("patient_page refused patient_id=%s why=%s",
                     patient.id, why.reason)
            return await refuse_upload(patient, why)
    is_audio = lane == uploads.AUDIO
    log.info("patient_page patient_id=%s attachment=%s", patient.id, mime)
    return await _web_message(
        InboundMessage(
            channel="web",
            synthetic=True,
            sender_ref=f"patient:{patient.id}",
            text=text,
            audio_bytes=raw if is_audio else None,
            image_bytes=None if is_audio else raw,
            mime=mime,
        ),
        tenant_id=patient.doctor_id,
        actor=ActorRef(kind="patient", id=patient.id),
        principal=ActorRef(kind="patient", id=patient.id),
        endpoint_id=f"web:patient:{patient.id}",
        thread_id=f"patient:{patient.id}",
        identity_method="patient_link",
    )


# --------------------------------------------------------------------------- #
# Console - token check on every route, wrong token is a 404
# --------------------------------------------------------------------------- #
async def current_doctor(token: str) -> Doctor:
    doctor = await store.doctor_by_token(token)
    if doctor is None:
        raise HTTPException(404, "Not Found")
    return doctor


@app.get("/c/{token}")
async def console(doctor: Doctor = Depends(current_doctor)) -> FileResponse:
    return FileResponse(CONSOLE_HTML, media_type="text/html")


@app.get("/c/{token}/app")
async def dashboard(doctor: Doctor = Depends(current_doctor)) -> FileResponse:
    """The designed dashboard, behind the same token check as the console.

    Same dependency, so a wrong token is the same 404 it has always been. The
    plain console keeps /c/{token} and is untouched: the two pages read the same
    routes, and this one is the one built to the design system.

    The page reads its own token out of `location.pathname.split("/")[2]`, which
    is `<token>` at both /c/<token> and /c/<token>/app, so nothing about the
    token scheme changes by serving it one level deeper.
    """
    page = DASHBOARD_V2_HTML if doctor.cockpit_v2_enabled else DASHBOARD_HTML
    return FileResponse(page, media_type="text/html")


async def current_workspace_doctor(request: Request) -> Doctor:
    """Authenticate the pathless v2 read without putting a token in the URL."""
    if "token" in request.query_params:
        raise HTTPException(404, "Not Found")
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme != "Bearer" or separator != " " or not token or " " in token:
        raise HTTPException(404, "Not Found")
    doctor = await store.doctor_by_token(token)
    if doctor is None or not doctor.cockpit_v2_enabled:
        raise HTTPException(404, "Not Found")
    return doctor


@app.get("/api/v2/workspace-snapshot")
async def workspace_snapshot(
    response: Response,
    patient_offset: int = 0,
    patient_limit: int = 50,
    patient_id: Optional[str] = None,
    event_cursor: Optional[str] = None,
    event_limit: int = 500,
    doctor: Doctor = Depends(current_workspace_doctor),
) -> dict:
    """One versioned projection over one atomic, doctor-scoped record read."""
    if patient_offset < 0 or not 1 <= patient_limit <= 100:
        raise HTTPException(422, "invalid patient page")
    if not 1 <= event_limit <= 1000:
        raise HTTPException(422, "invalid event page")

    records = await store.read_workspace(doctor.id)
    # The transaction re-reads the Doctor row.  This closes the small window
    # where an admin can disable the flag after bearer authentication but
    # before the record bundle is read.
    if (
        records is None
        or records.doctor.id != doctor.id
        or not records.doctor.cockpit_v2_enabled
        or not hmac.compare_digest(records.doctor.web_token, doctor.web_token)
    ):
        raise HTTPException(404, "Not Found")

    at = store.now()
    effective_run_id = str(records.settings.get("run_id") or settings.ENV_RUN_ID)
    try:
        effective_time_scale = int(
            records.settings.get("time_scale") or settings.ENV_TIME_SCALE
        )
    except (TypeError, ValueError):
        effective_time_scale = settings.ENV_TIME_SCALE
    effective_time_scale = max(1, effective_time_scale)
    delivery_health = {
        "web": {"configured": True, "latest_outcome": "UNKNOWN"},
        "telegram": {
            "configured": telegram.enabled(),
            "latest_outcome": "UNKNOWN",
        },
        "basis": (
            "Configuration is not delivery proof; no confirmed receipt "
            "projection exists in current records."
        ),
    }
    system_health = {
        "service": os.environ.get("K_SERVICE"),
        "revision": os.environ.get("K_REVISION"),
        "region": os.environ.get("TASKS_REGION"),
        "project": store.PROJECT,
        "chaser": tasks.engine(),
        "labs_storage_configured": storage.enabled(),
        "run_id": effective_run_id,
        "time_scale": effective_time_scale,
    }
    try:
        snapshot = workspace.build_snapshot(
            records,
            at,
            patient_offset=patient_offset,
            patient_limit=patient_limit,
            selected_patient_id=patient_id,
            event_cursor=event_cursor,
            event_limit=event_limit,
            delivery_health=delivery_health,
            system_health=system_health,
        )
    except workspace.InvalidCursor as exc:
        raise HTTPException(422, "invalid event cursor") from exc
    except workspace.UnknownPatient as exc:
        raise HTTPException(404, "Not Found") from exc
    except workspace.InvalidWorkspace as exc:
        # A record bundle that fails a storage invariant is a degraded read,
        # not a permanently broken console. A 500 here would be exactly that:
        # every poll fails the same way and the doctor is left with a dead
        # page. The v2 browser already treats any non-OK response as "keep the
        # last good snapshot and show a banner", so 503 is the honest answer
        # and the page stays usable on the last truth it had.
        #
        # The body and the log line carry failure kinds and counts only. The
        # exception is built that way on purpose (core/workspace.py): link ids
        # are bearer credentials, and no record id may reach a doctor's browser
        # or a structured log over a validation failure.
        failures = getattr(exc, "failures", {})
        log.warning(
            "workspace snapshot could not be projected for one doctor: %s",
            ", ".join(f"{kind}={count}" for kind, count in failures.items())
            or "unspecified",
        )
        raise HTTPException(
            503,
            {
                "error": "workspace_unprojectable",
                "retryable": True,
                "failures": failures,
            },
        ) from exc

    response.headers["Cache-Control"] = "private, no-store"
    return snapshot


@app.get("/c/{token}/board")
async def board_view(doctor: Doctor = Depends(current_doctor)) -> dict:
    """The doctor's whole board, plus the four counts above it and the QR.

    Named `board_view`, not `board`: a route function called `board` would
    shadow the `core.board` module it calls, which is exactly what it did once.

    `last_event_ms`, `last_event_kind`, `next_due`, `channel` and `link` are
    read from the records here rather than derived in the browser. The page used
    to take the last event time from the feed window, which is the newest two
    hundred events for the whole board, so a quiet patient on a busy board lost
    his last event entirely; and it took the channel from `/health.telegram`,
    which is a fact about the deployment and not about the patient.
    """
    history = await store.list_events(doctor.id)
    tokens = views.links_by_patient(await store.list_link_tokens(doctor.id))
    patients = []
    for patient in await store.list_patients(doctor.id):
        loops = await store.list_loops(patient.id)
        patients.append(
            {
                "id": patient.id,
                "synthetic": patient.synthetic,
                "name": patient.name,
                "diagnosis": patient.diagnosis,
                "status": patient.status,
                # The plan is what the Concierge answers from, and what a
                # doctor's reply appends to, so the board shows it verbatim.
                "plan": patient.plan_text,
                "next_due": views.next_due(loops),
                **views.last_event(history, patient.id),
                **views.reach(patient, tokens.get(patient.id)),
                "loops": [
                    {
                        "id": l.id,
                        "synthetic": l.synthetic,
                        "type": l.type,
                        "title": l.title,
                        "state": l.state,
                        "details": l.details,
                        "due_at": l.due_at.isoformat() if l.due_at else None,
                    }
                    for l in loops
                ],
            }
        )
    counts = board.tally(
        l["state"] for p in patients for l in p["loops"]
    )
    latest = await store.latest_link_token(doctor.id)
    qr = None
    if latest is not None:
        who = await store.get_patient(latest.patient_id)
        qr = {"url": f"/qr/{latest.id}.png",
              "patient": who.name if who else "",
              "page": f"/p/{latest.id}"}
    return {"doctor": doctor.name, "synthetic": doctor.synthetic,
            "patients": patients, "counts": counts, "qr": qr}


@app.get("/c/{token}/cards")
async def open_cards(doctor: Doctor = Depends(current_doctor)) -> dict:
    """Only the cards that still need the doctor, newest first.

    The whole feed still comes back from /c/{token}/feed, resolved cards and
    all, because the feed is the history. This is the inbox: one rule, in
    core/cards.is_open, applied on the server so a page reload cannot resurrect
    a card the doctor already finished.
    """
    rows = cards.open_cards(await store.list_events(doctor.id))
    return {"cards": [cards.row(e) for e in rows]}


@app.get("/c/{token}/reports")
async def reports(doctor: Doctor = Depends(current_doctor)) -> dict:
    """Completion reports and digests, newest first, as records.

    Stored at the moment each one is written (core/report.record), so this never
    matches text against a heading. Reports written before this route existed
    are not backfilled and do not appear here; they are still in the feed.
    """
    return {"reports": [views.report_row(r)
                        for r in await store.list_reports(doctor.id)]}


@app.get("/c/{token}/settings")
async def doctor_settings(doctor: Doctor = Depends(current_doctor)) -> dict:
    """The doctor's own record and his Coordinator policy. Read only.

    Nothing here writes: the policy is set through POST /admin/settings, behind
    the admin secret, and a console token is not an admin credential. The
    Telegram chat id itself is never returned either, only whether one is bound,
    because that number identifies a real phone.
    """
    return views.settings_view(doctor, policy.for_doctor(doctor))


@app.get("/c/{token}/feed")
async def feed(since: int = 0, doctor: Doctor = Depends(current_doctor)) -> dict:
    return {
        "events": [
            {
                "id": e.id,
                "synthetic": e.synthetic,
                "kind": e.kind,
                "patient_id": e.patient_id,
                "text": e.text,
                "media": e.media,
                "meta": e.meta,
                "ts_ms": events.ts_ms(e),
            }
            for e in await events.last_events(doctor.id, since)
        ]
    }


@app.post("/c/{token}/doctor")
async def doctor_in(
    text: str = Form(""),
    file: Optional[UploadFile] = File(None),
    doctor: Doctor = Depends(current_doctor),
) -> dict:
    """The doctor's box: typed, a voice note, or a photo of his prescription.

    Which of the three it is comes from the upload's own content type, in code.
    All three end up in the Registrar and all three end at the same confirm card.
    """
    raw, lane, mime = None, "", None
    if file is not None:
        # The same cap and the same sniff as the patient lane (M2). A doctor
        # who uploads the wrong thing is told plainly and nothing is created.
        try:
            raw, lane, mime = await uploads.take(file)
        except uploads.Rejected as why:
            return {"ok": False, "refused": why.reason,
                    "detail": "Send a photo of the prescription, or a voice "
                              "note, under 10 MB."}
    is_audio = lane == uploads.AUDIO
    log.info("doctor_in doctor_id=%s attachment=%s", doctor.id, mime)
    return await _web_message(
        InboundMessage(
            channel="web",
            synthetic=True,
            sender_ref=f"doctor:{doctor.web_token}",
            text=text,
            audio_bytes=raw if is_audio else None,
            image_bytes=None if is_audio else raw,
            mime=mime,
        ),
        tenant_id=doctor.id,
        actor=ActorRef(kind="doctor", id=doctor.id),
        principal=ActorRef(kind="doctor", id=doctor.id),
        endpoint_id=f"web:doctor:{doctor.id}",
        thread_id=f"doctor:{doctor.id}",
        identity_method="doctor_console_token",
    )


@app.post("/c/{token}/patient/{patient_id}")
async def patient_in(
    patient_id: str,
    text: str = Form(""),
    file: Optional[UploadFile] = File(None),
    doctor: Doctor = Depends(current_doctor),
) -> dict:
    patient = await store.get_patient(patient_id)
    if patient is None or patient.doctor_id != doctor.id:
        raise HTTPException(404, "Not Found")
    if len(text or "") > dispatch.MAX_PATIENT_TEXT:
        raise HTTPException(413, dispatch.patient_limit_text(text))

    raw, lane, mime = None, "", None
    if file is not None:
        try:
            raw, lane, mime = await uploads.take(file)
        except uploads.Rejected as why:
            return await refuse_upload(patient, why)
    is_audio = lane == uploads.AUDIO
    log.info("patient_in patient_id=%s attachment=%s", patient.id, mime)
    return await _web_message(
        InboundMessage(
            channel="web",
            synthetic=True,
            sender_ref=f"patient:{patient.id}",
            text=text,
            audio_bytes=raw if is_audio else None,
            image_bytes=None if is_audio else raw,
            mime=mime,
        ),
        tenant_id=doctor.id,
        actor=ActorRef(kind="patient", id=patient.id),
        principal=ActorRef(kind="doctor", id=doctor.id),
        endpoint_id=f"web:doctor:{doctor.id}",
        thread_id=f"patient:{patient.id}",
        identity_method="doctor_console_simulation",
    )


@app.get("/c/{token}/patient/{patient_id}")
async def patient_view(
    patient_id: str, doctor: Doctor = Depends(current_doctor)
) -> dict:
    """One patient: the record, the loops, and everything that has happened."""
    patient = await store.get_patient(patient_id)
    if patient is None or patient.doctor_id != doctor.id:
        raise HTTPException(404, "Not Found")
    loops = await store.list_loops(patient.id)
    history = [e for e in await events.last_events(doctor.id, 0)
               if e.patient_id == patient.id]
    tokens = views.links_by_patient(await store.list_link_tokens(doctor.id))
    # A rehearsal at time_scale=3 makes a Sanad day three real seconds long, and
    # the monitoring summary counts days (wave A F11). Reading the live scale
    # here is what stops this panel reporting slots nobody was ever asked on.
    _, time_scale = await settings.current()
    return {
        # Which channel this patient is actually on, and where his own link
        # is. Both are facts about him, not about whether the bot is
        # configured, which is what the page had to guess from before.
        **views.reach(patient, tokens.get(patient.id)),
        "patient": {
            "id": patient.id, "synthetic": patient.synthetic,
            "name": patient.name, "age": patient.age,
            "sex": patient.sex, "diagnosis": patient.diagnosis,
            "plan": patient.plan_text, "targets": patient.targets,
            "baseline": patient.baseline, "status": patient.status,
            "results": patient.results,
            # Who this person is, in the doctor's own words, dated (S9). Doctor
            # text only: the Registrar writes it at confirm time and nothing on
            # the patient path can reach it.
            "notes": patient.notes,
        },
        "loops": [
            {"id": l.id, "synthetic": l.synthetic,
             "type": l.type, "title": l.title, "state": l.state,
             "details": l.details, "attempts": l.attempts,
             "due_at": l.due_at.isoformat() if l.due_at else None,
             "results": l.results, "readings": l.readings,
             "contacts": l.contacts, "barrier": l.barrier, "paused": l.paused,
             "doctor_reviewed": l.doctor_reviewed, "verified": l.verified}
            for l in loops
        ],
        # The same loops, said as the contracts they are: objective, evidence,
        # permitted actions, the fixed safety sentence, deadline, escalation
        # conditions. Nothing new is stored; this is a rendering of the loop
        # plus the doctor's policy (core/contract.py).
        "contracts": [
            contract.render(l, policy.for_doctor(doctor), doctor.name, patient.name)
            for l in loops
        ],
        # One monitoring loop, counted: what was asked for, what arrived, which
        # slots did not, the trend, the threshold alerts and the barrier the
        # patient reported (core/monitoring.py, S6++ item H). Counted from the
        # readings in code; no model is asked and nothing is stored.
        "monitoring": [
            {"loop_id": l.id, "title": l.title,
             **monitoring.summary(l, time_scale).as_dict()}
            for l in loops if monitoring.is_monitoring(l)
        ],
        "timeline": [
            {"kind": e.kind, "text": e.text, "meta": e.meta,
             "synthetic": e.synthetic,
             "ts_ms": events.ts_ms(e)}
            for e in history
        ],
    }


@app.get("/c/{token}/summary")
async def summary_view(doctor: Doctor = Depends(current_doctor)) -> dict:
    """The end of the day, counted from the records. No model is asked.

    "Lost" is zero by construction and not by hope: every obligation the doctor
    has falls into exactly one of six buckets in core/summary.classify, which is
    a total function with an else branch, so the buckets always add up to the
    number carried. The suite drives every combination through it and asserts
    that sum.
    """
    patients = await store.list_patients(doctor.id)
    loops = []
    for patient in patients:
        loops += await store.list_loops(patient.id)
    history = await events.last_events(doctor.id, 0)
    relays = await store.open_relays(doctor.id)
    # The doctor's day is Cairo's day, on both sides of this call (kernel
    # review F13). `store.now().date()` is a UTC date, so between midnight and
    # 03:00 Cairo this route asked for the previous day's summary while the
    # event-side comparison inside core/summary.py was already Cairo: one
    # endpoint, two calendars. `summary.today()` is the Cairo date, and the card
    # is stamped from the same clock, so the title cannot name a different day
    # from the one that was counted.
    cairo_now = store.now().astimezone(timing.CAIRO)
    counts = summary.compute(loops, history, relays, on=summary.today(store.now()))
    # A card the phone stayed quiet for is owed to the doctor somewhere, and
    # until now the only place that listed one was the Telegram /digest
    # command. A doctor with no bound chat could not reach a parked
    # REVIEW_READY card on any surface at all. `summary.card` has taken the
    # block since S24-G; this route simply never handed it over. Reading is all
    # that happens here: unlike the digest, this does not release anything, so
    # the card stays owed until the digest actually delivers it.
    # The block goes on the card and nowhere else: the legacy API shape is
    # sealed by the Gate 0B goldens, so this route may not grow a key.
    return {
        "doctor": doctor.name,
        "line": summary.line(counts),
        "counts": counts.as_dict(),
        "card": summary.card(
            counts, doctor.name, cairo_now,
            parked=summary.parked_rows(history),
            names={person.id: person.name for person in patients},
        ),
    }


class ActionIn(BaseModel):
    # "confirm:<id>" | "cancel:<id>" | "reply:<relay_id>" |
    # "reviewed:<loop_id>" | "note:<loop_id>" (these two carry text) |
    # "attach:<event_id>" | "openloop:<event_id>" (an unexpected lab result) |
    # "seen:<event_id>" (a red card, which carries no other button) |
    # "existing:<patient_id>:<proposal_id>" and "newpatient:<proposal_id>"
    # (S9: which record a dictation is about) |
    # "openpatient:<patient_id>" (a row on a lookup list)
    action_id: str
    text: str = ""


@app.post("/c/{token}/action")
async def action(
    body: ActionIn, request: Request, doctor: Doctor = Depends(current_doctor)
) -> dict:
    command_id = uuid.uuid4().hex
    outcome = await transport_runtime().execute(
        "web",
        InboundMessage(
            channel="web",
            synthetic=True,
            sender_ref=f"doctor:{doctor.web_token}",
            text=body.action_id,
        ),
        live_transport.WebContext(
            provider_message_id=command_id,
            tenant_id=doctor.id,
            actor=ActorRef(kind="doctor", id=doctor.id),
            principal=ActorRef(kind="doctor", id=doctor.id),
            endpoint_id=f"web:doctor:{doctor.id}",
            thread_id=f"doctor:{doctor.id}",
            identity_method="doctor_console_token",
            transient_payload=(body, request, doctor),
        ),
        CommandSpec(
            id=command_id,
            idempotency_key=body.action_id,
            kind=live_transport.ACTION,
            payload={"action_id": body.action_id, "text": body.text},
        ),
    )
    return outcome.legacy_response()


async def _legacy_action(
    body: ActionIn, request: Request, doctor: Doctor
) -> dict:
    """The web door onto the one doctor-action path.

    S24-C. The ritual this function used to spell out (claim the card, take the
    action key, do the work, retire the card, and which of those two claims a
    failure gives back) is `core/doctor_actions.perform` now, because Telegram
    has to run the same one. Nothing on the wire moved: the bodies it returns
    are built by the same code that built them here, and the golden replay
    drives this route hard enough to say so.

    The only thing that stays on this side of the seam is the HTTP status. A
    verb the domain cannot name is a `doctor_actions.UnknownAction` there and a
    400 here, because core/ owns no web framework.
    """
    verb, _, ident = body.action_id.partition(":")
    log.info("action doctor_id=%s verb=%s id=%s", doctor.id, verb, ident)
    try:
        return await doctor_actions.perform(
            doctor, body.action_id, body.text, base_url=str(request.base_url)
        )
    except doctor_actions.UnknownAction:
        raise HTTPException(400, "unknown action")
