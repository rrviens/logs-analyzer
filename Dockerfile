# Hosted Healer Review: every visitor signs in with Warcraft Logs (AUTH_MODE=wcl).
# Secrets come from the host's environment settings, never from this image. Mount a persistent disk at /data.
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY data data

ENV AUTH_MODE=wcl \
    DATA_DIR=/data \
    PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"

# One process: jobs live in memory and the cache is SQLite, so run a single instance.
CMD uvicorn app.web:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
