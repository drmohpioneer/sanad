#!/usr/bin/env bash
# Deploy Sanad to Cloud Run. Idempotent: safe to re-run, creates nothing twice.
set -euo pipefail

# Security audit I1: a forker who runs this without thinking should not deploy
# into somebody else's project. PROJECT= in the environment wins, and the only
# fallback is this machine's own gcloud configuration. There is no hard-coded
# project id here to inherit by accident: an unconfigured shell stops.
PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}
if [ -z "${PROJECT}" ] || [ "${PROJECT}" = "(unset)" ]; then
  echo "No project: set PROJECT=your-gcp-project-id, or run gcloud config set project your-gcp-project-id" >&2
  exit 1
fi
REGION=europe-west1
SERVICE=sanad
SA=sanad-run@${PROJECT}.iam.gserviceaccount.com
SECRET=sanad-admin-secret
TG_SECRET=sanad-tg-webhook-secret
BOT_SECRET=sanad-bot-token
# S19. The Places API (New) key the Resolver searches with. Created out of
# band, exactly like the bot token, and optional in exactly the same way: with
# no secret the service deploys without MAPS_API_KEY, every search comes back
# unavailable and the barrier is handed to the doctor saying so. Nothing
# crashes and nothing is invented.
MAPS_SECRET=sanad-maps-key
QUEUE=sanad-chase
BUCKET=${PROJECT}-labs
# Demo knobs. Both can be changed at run time through POST /admin/settings, so
# a rehearsal never needs a redeploy; these are only the defaults.
RUN_ID=${DEMO_RUN_ID:-dev}
TIME_SCALE=${TIME_SCALE:-86400}
LEGACY_RUNTIME=${LEGACY_RUNTIME:-true}
OUTBOX_MODE=${OUTBOX_MODE:-off}

if [ "$LEGACY_RUNTIME" != "true" ]; then
  echo "Gate 2 requires LEGACY_RUNTIME=true; the replacement runtime is not active." >&2
  exit 1
fi
if [ "$OUTBOX_MODE" != "off" ] && [ "$OUTBOX_MODE" != "shadow" ]; then
  echo "OUTBOX_MODE must be off or shadow." >&2
  exit 1
fi

# 1. Runtime service account (create once, ignore "already exists").
if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create sanad-run \
    --project "$PROJECT" \
    --display-name "Sanad Cloud Run runtime"
  # IAM is eventually consistent: a freshly created SA is not immediately
  # visible to add-iam-policy-binding ("service account does not exist").
  for _ in $(seq 1 12); do
    gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 && break
    sleep 5
  done
  sleep 10
fi

# 2. Roles for that SA: Vertex, Firestore, reading the admin secret, and adding
#    tasks to the queue.
for ROLE in roles/aiplatform.user roles/datastore.user \
            roles/secretmanager.secretAccessor roles/cloudtasks.enqueuer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${SA}" \
    --role "$ROLE" \
    --condition None >/dev/null
done

# 2b. THE actAs TRAP. A Cloud Tasks task that carries an OIDC token for a
#     service account can only be created by an identity allowed to act as that
#     service account. Sanad creates tasks that run as itself, so the runtime SA
#     needs roles/iam.serviceAccountUser ON ITSELF. Without this every
#     create_task fails with PERMISSION_DENIED on iam.serviceAccounts.actAs, and
#     nothing about the queue or the enqueuer role hints at why.
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --project "$PROJECT" \
  --member "serviceAccount:${SA}" \
  --role roles/iam.serviceAccountUser >/dev/null

# 3. Firestore, Native mode, europe-west1. One-way door: the mode and location
#    of the (default) database can never be changed, only deleted and remade.
if ! gcloud firestore databases describe --database='(default)' \
      --project "$PROJECT" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --project "$PROJECT" \
    --location="$REGION" \
    --type=firestore-native
fi

# 3b. APIs S3 needs. Enabling an already-enabled API is a no-op.
gcloud services enable cloudtasks.googleapis.com storage.googleapis.com \
  --project "$PROJECT" >/dev/null

# 3c. The Chaser's queue, in the same region as everything else.
if ! gcloud tasks queues describe "$QUEUE" --location "$REGION" \
      --project "$PROJECT" >/dev/null 2>&1; then
  gcloud tasks queues create "$QUEUE" --location "$REGION" --project "$PROJECT"
fi

# 3d. Private bucket for lab-slip images. Uniform access and public access
#     prevention: there is no code path that makes an object public, and this
#     makes sure there cannot be one.
if ! gcloud storage buckets describe "gs://${BUCKET}" \
      --project "$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "$PROJECT" \
    --location "$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${SA}" \
  --role roles/storage.objectAdmin \
  --project "$PROJECT" >/dev/null

# 4. Admin secret for POST /admin/seed. Generated here, read only by Cloud Run.
#    The value is never printed, never written to disk, never held in a variable.
if ! gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  openssl rand -hex 24 | tr -d '\n' | gcloud secrets create "$SECRET" \
    --project "$PROJECT" \
    --replication-policy=automatic \
    --data-file=-
  echo "Created secret ${SECRET}. Read it with:"
  echo "  gcloud secrets versions access latest --secret=${SECRET} --project=${PROJECT}"
  echo "It goes in the X-Sanad-Admin header on every /admin call, never in the"
  echo "URL: a query string is written into Cloud Logging and kept 30 days"
  echo "(security audit H1). Rotation: docs/RUNBOOK.md section 5."
fi

# 5. Webhook secret. Telegram echoes it on every update; /tg rejects anything
#    else. Generated here, never printed, never written to disk.
if ! gcloud secrets describe "$TG_SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  openssl rand -hex 32 | tr -d '\n' | gcloud secrets create "$TG_SECRET" \
    --project "$PROJECT" \
    --replication-policy=automatic \
    --data-file=-
fi

# 6. Bot token, if it exists. It is created out of band, straight from the .env
#    line into Secret Manager, so the value never reaches a shell variable:
#      grep '^SANAD_BOT_TOKEN=' <env file> | cut -d= -f2- | tr -d '\n' \
#        | gcloud secrets create sanad-bot-token --project sanad-506914 \
#            --replication-policy=automatic --data-file=-
#    Until it exists Sanad runs web-only and every Telegram send is a no-op.
SECRETS="ADMIN_SECRET=${SECRET}:latest,TG_WEBHOOK_SECRET=${TG_SECRET}:latest"
if gcloud secrets describe "$BOT_SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  SECRETS="${SECRETS},SANAD_BOT_TOKEN=${BOT_SECRET}:latest"
  echo "Bot token found: Telegram is enabled."
else
  echo "No ${BOT_SECRET} secret: deploying web-only (Telegram pending token)."
fi

# 6b. Maps key, if it exists. Same shape as the bot token above and the same
#     rule: the value never reaches a shell variable here. Create it out of
#     band once the key exists:
#       printf %s 'AIza...' | gcloud secrets create sanad-maps-key \
#         --project "$PROJECT" --replication-policy=automatic --data-file=-
#     Cloud Console -> APIs & Services -> Library -> "Places API (New)" ->
#     Enable, then Credentials -> Create API key -> restrict it to that one API.
if gcloud secrets describe "$MAPS_SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  SECRETS="${SECRETS},MAPS_API_KEY=${MAPS_SECRET}:latest"
  echo "Maps key found: the Resolver can search for labs and pharmacies."
else
  echo "No ${MAPS_SECRET} secret: the Resolver runs without Maps. Every search"
  echo "answers 'unavailable' and the barrier goes to the doctor saying so."
fi

# 7. The service's own URL. Cloud Tasks calls it back, and the OIDC token it
#    carries is minted for exactly this string as its audience, which /tasks/*
#    then verifies. On the very first deploy of a brand new service this is
#    empty; deploy once, then run this script again to fill it in.
URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
        --region "$REGION" --format 'value(status.url)' 2>/dev/null || true)
if [ -z "$URL" ]; then
  echo "No service URL yet (first deploy). Re-run deploy.sh afterwards so the"
  echo "Chaser can create tasks; until then /tasks enqueue will refuse."
fi

# 8. Build (Cloud Build) + deploy.
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global,SERVICE_URL=${URL},SANAD_SA=${SA},TASKS_QUEUE=${QUEUE},TASKS_REGION=${REGION},LABS_BUCKET=${BUCKET},CHASER_ENGINE=${CHASER_ENGINE:-cloudtasks},DEMO_RUN_ID=${RUN_ID},TIME_SCALE=${TIME_SCALE},LEGACY_RUNTIME=${LEGACY_RUNTIME},OUTBOX_MODE=${OUTBOX_MODE} \
  --set-secrets "$SECRETS" \
  --memory 1Gi \
  --cpu 1 \
  --max-instances 3

# 9. Send the traffic to what was just built, and then PROVE it is serving.
#
#    This is not defensive decoration. On rev 17 this script exited 0, printed
#    "has been deployed and is serving 100 percent of traffic", and deployed
#    nothing: the service's traffic block named a revision
#    (`revisionName: sanad-00018-gwq`) instead of carrying `latestRevision:
#    true`, so Cloud Run built the new image, imported it, found it had no
#    traffic allocation and retired it seconds later. The pin comes from the
#    mid-demo restart command (`update-traffic --to-revisions <name>=100`), and
#    once it is set it survives every later deploy. A silent no-op with a
#    success message is the worst failure this script can have, because the
#    thing it breaks is "the revision on camera is the revision we tested".
#
#    `--to-latest` is idempotent: on a service that already carries
#    `latestRevision: true` it changes nothing.
gcloud run services update-traffic "$SERVICE" \
  --to-latest \
  --project "$PROJECT" \
  --region "$REGION" >/dev/null

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
        --region "$REGION" --format 'value(status.url)')
BUILT=$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
          --region "$REGION" --format 'value(status.latestCreatedRevisionName)')

# /health names the revision that answered the request, which is the only
# statement about what is serving that comes from the running container itself.
# Cloud Run takes a few seconds to move the traffic, so this asks a few times
# before it calls it a failure.
SERVING=""
for _ in $(seq 1 10); do
  SERVING=$(curl -fsS --max-time 20 "${URL}/health" 2>/dev/null \
    | sed -n 's/.*"revision"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p') || true
  if [ -n "$SERVING" ] && [ "$SERVING" = "$BUILT" ]; then
    break
  fi
  sleep 6
done

if [ "$SERVING" != "$BUILT" ]; then
  echo "" >&2
  echo "DEPLOY FAILED THE SERVING CHECK." >&2
  echo "  built:   ${BUILT}" >&2
  echo "  serving: ${SERVING:-<no revision in /health>}" >&2
  echo "" >&2
  echo "The build succeeded and the traffic is somewhere else. Look at:" >&2
  echo "  gcloud run services describe ${SERVICE} --project ${PROJECT} \\" >&2
  echo "    --region ${REGION} --format='yaml(spec.traffic)'" >&2
  echo "A traffic block that names a revision instead of saying" >&2
  echo "latestRevision: true is the pin this script's step 9 undoes." >&2
  exit 1
fi

echo "Serving revision: ${SERVING} (built and verified through /health)."
echo "$URL"
