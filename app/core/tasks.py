"""Owns waking Sanad up later: one enqueue interface, two engines behind it.

The product engine is Cloud Tasks. A nudge that is due in nine days is an HTTP
task with a schedule time nine days out; nothing in Sanad has to stay alive in
between, which is the point of a service that scales to zero.

The task calls this same service back at /tasks/nudge carrying a Google-signed
OIDC identity token for the runtime service account. The handler verifies that
token and rejects everything else, so the public URL is useless to anyone who
cannot mint one. Creating such a task needs two permissions, and both of them
are granted in deploy.sh:

  roles/cloudtasks.enqueuer      on the project - may add tasks to the queue
  roles/iam.serviceAccountUser   on itself      - may create a task that runs as
                                                  itself (the actAs trap: without
                                                  it every create fails with
                                                  PERMISSION_DENIED on actAs)

The fallback engine (CHASER_ENGINE=inprocess) is an asyncio timer inside this
process, behind the same `enqueue()` signature. It exists because the identity
setup above is the one part of S3 that can fight back; it is strictly worse
(a restart forgets every pending nudge, and it only works while one instance is
warm) and it is never used unless the environment says so.

The 720-hour ceiling
--------------------
Cloud Tasks refuses a schedule time more than 720 hours (30 days) in the future.
That is a hard API limit, not a quota, and it was proved live on rev
sanad-00015-p6x: "come back in a month" opens a visit due in 30 days whose third
ladder rung lands at 33, the create call threw InvalidArgument, and the whole
Confirm returned 500 with no patient link ever minted.

So no task ever carries a delay longer than MAX_DELAY_SECONDS. A contact further
away than that is carried in hops: the task is scheduled for 28 days, its body
remembers the real due moment in `not_before`, and when it fires the handler
sees that the moment has not arrived, puts the task back on the queue for what
is left, sends nothing, and writes "re-armed for <date>" so the audit trail
explains the extra wake-up rather than leaving it looking like a lost nudge.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("sanad.tasks")

# 28 days, comfortably inside the 720 hour (30 day) ceiling Cloud Tasks
# enforces on scheduleTime. The two days of headroom are for the queue's own
# clock and for a task that waits in the queue before it is dispatched.
MAX_DELAY_SECONDS = 28 * 24 * 3600
# The payload key that carries the real due moment of a task that had to be
# split into hops. Absent means the task fires when it means to.
NOT_BEFORE = "not_before"

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "sanad-506914")
REGION = os.environ.get("TASKS_REGION", "europe-west1")
QUEUE = os.environ.get("TASKS_QUEUE", "sanad-chase")
SERVICE_URL = os.environ.get("SERVICE_URL", "").rstrip("/")
SERVICE_ACCOUNT = os.environ.get("SANAD_SA", "")
ENGINE = os.environ.get("CHASER_ENGINE", "cloudtasks").strip().lower()

# Registered by the application composition root.  The in-process fallback
# enters the same Cloud Tasks adapter as the HTTP route, and the generated task
# name is its provider identity for durable replay.
_local_handler: Optional[
    Callable[[dict[str, Any], str], Awaitable[Any]]
] = None
_pending: set[asyncio.Task] = set()


def register_local_handler(
    fn: Callable[[dict[str, Any], str], Awaitable[Any]],
) -> None:
    """Where the fallback engine delivers. Cloud Tasks never uses this."""
    global _local_handler
    _local_handler = fn


def engine() -> str:
    """'cloudtasks' or 'inprocess'. Reported by /health so it is never a guess."""
    return "inprocess" if ENGINE == "inprocess" else "cloudtasks"


def configured() -> bool:
    return bool(SERVICE_URL and SERVICE_ACCOUNT)


# --------------------------------------------------------------------------- #
# Cloud Tasks
# --------------------------------------------------------------------------- #
def _create_task(path: str, payload: dict[str, Any], delay_seconds: float) -> str:
    """Blocking: the Cloud Tasks client is sync, so callers hand this to a thread."""
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    # The clamp is applied again here, at the last line before the API call, so
    # that a caller reaching past `enqueue()` still cannot create a task the
    # service will refuse.
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=min(max(0.0, delay_seconds), MAX_DELAY_SECONDS)
    )
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{SERVICE_URL}{path}",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            # Audience is the bare service URL, and the handler verifies exactly
            # that string. A token minted for anything else does not open it.
            "oidc_token": {
                "service_account_email": SERVICE_ACCOUNT,
                "audience": SERVICE_URL,
            },
        },
        "schedule_time": when,
    }
    created = client.create_task(
        parent=client.queue_path(PROJECT, REGION, QUEUE), task=task
    )
    return created.name


def _verify(token: str) -> dict[str, Any]:
    """Blocking: verifying a Google-signed token fetches (and caches) certs."""
    from google.auth.transport import requests as ga_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(
        token, ga_requests.Request(), audience=SERVICE_URL
    )


async def verify_caller(authorization: Optional[str]) -> dict[str, Any]:
    """Accept only Cloud Tasks calling as our own service account.

    Raises ValueError on anything else: no header, a token Google did not sign,
    a token minted for another audience, or a token belonging to some other
    service account. The caller turns that into a 403.
    """
    if not SERVICE_URL:
        raise ValueError("SERVICE_URL is not configured")
    header = (authorization or "").strip()
    if not header.lower().startswith("bearer "):
        raise ValueError("no bearer identity token")
    claims = await asyncio.to_thread(_verify, header[7:].strip())
    if not claims.get("email_verified"):
        raise ValueError("unverified token email")
    if claims.get("email") != SERVICE_ACCOUNT:
        raise ValueError("token is not the runtime service account")
    return claims


# --------------------------------------------------------------------------- #
# In-process fallback
# --------------------------------------------------------------------------- #
async def _sleep_then_run(
    payload: dict[str, Any], delay_seconds: float, task_name: str
) -> None:
    await asyncio.sleep(max(0.0, delay_seconds))
    if _local_handler is None:
        log.error("fallback scheduler has no handler registered")
        return
    try:
        await _local_handler(payload, task_name)
    except Exception:  # a lost nudge must not take the process with it
        log.exception("fallback nudge failed task=%s", task_name)


def _schedule_locally(payload: dict[str, Any], delay_seconds: float) -> str:
    task_name = f"inprocess/{uuid.uuid4().hex}"
    task = asyncio.create_task(
        _sleep_then_run(payload, delay_seconds, task_name)
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task_name


# --------------------------------------------------------------------------- #
# The 720 hour ceiling: one hop at a time
# --------------------------------------------------------------------------- #
def hop(delay_seconds: float) -> tuple[float, bool]:
    """(the delay a task may actually carry, whether this is only a hop)."""
    delay = max(0.0, float(delay_seconds or 0.0))
    if delay > MAX_DELAY_SECONDS:
        return MAX_DELAY_SECONDS, True
    return delay, False


def body_for(payload: dict[str, Any], delay_seconds: float,
             now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """The task body: the caller's payload, plus `not_before` when it hops.

    A task that can be scheduled for its real moment carries no `not_before` at
    all, and any stale one is dropped, so the flag means exactly one thing: this
    task is going to fire before it is due.
    """
    body = dict(payload or {})
    _, hops = hop(delay_seconds)
    if hops:
        at = (now or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(
            seconds=max(0.0, float(delay_seconds))
        )
        body[NOT_BEFORE] = at.isoformat()
    else:
        body.pop(NOT_BEFORE, None)
    return body


def due_at(payload: dict[str, Any]) -> Optional[dt.datetime]:
    """When this task is really due, or None when it is due on arrival.

    Unreadable is None, which is the fail-closed answer: the handler then does
    what it did before hops existed, which is to carry on and send.
    """
    raw = (payload or {}).get(NOT_BEFORE)
    if not raw:
        return None
    try:
        when = dt.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- #
# The one interface
# --------------------------------------------------------------------------- #
async def enqueue(path: str, payload: dict[str, Any], delay_seconds: float) -> str:
    """Call `path` on this service with `payload`, `delay_seconds` from now.

    A delay past the 720 hour ceiling is clamped and the body remembers the real
    moment, so the handler re-arms instead of sending early (see the module
    docstring). Returns the task name, which is what the Cloud Tasks console
    lists.
    """
    delay, _ = hop(delay_seconds)
    body = body_for(payload, delay_seconds)
    if engine() == "inprocess":
        return _schedule_locally(body, delay)
    if not configured():
        raise RuntimeError("SERVICE_URL and SANAD_SA must be set to enqueue tasks")
    name = await asyncio.to_thread(_create_task, path, body, delay)
    task_ref = hashlib.sha256(name.encode("utf-8")).hexdigest()
    log.info("task queued ref=%s delay=%.1fs payload=%s", task_ref, delay, body)
    return name
