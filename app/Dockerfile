FROM python:3.12-slim

# ffmpeg is a hard dependency: Telegram voice notes arrive as OGG/Opus and
# Gemini wants 16 kHz mono WAV.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The sentinel phrase table and the output validator are rails, not documents:
# if either regression suite breaks, the image never builds.
RUN PYTHONPATH=. SANAD_TEST_MODE=1 python -m unittest discover -s tests -t . -q

ENV PORT=8080
# --forwarded-allow-ips=*: Cloud Run is the only ingress and always sets
# x-forwarded-proto, so without this /admin/seed hands back an http:// URL.
#
# --no-access-log: security audit H1. uvicorn's access log wrote the full path
# of every request into Cloud Logging, so `GET /c/<32 hex token>/feed` put every
# doctor's bearer token there twice a second for thirty days. Cloud Run's own
# request log already records method, path and status for anyone who needs it;
# this stops the container writing a second copy. The application log is
# untouched, and that is the one that carries the audit trail.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --forwarded-allow-ips='*' --no-access-log"]
