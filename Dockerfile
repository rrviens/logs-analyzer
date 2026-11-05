# Hosted Healer Review: every visitor signs in with Warcraft Logs (AUTH_MODE=wcl).
# Secrets come from the host's environment settings, never from this image. Mount a persistent disk at /data.
# Stage 1: build the new React front end (web/) - only its output ships, not Node.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# install, not ci: the lockfile is made on Windows and can lack Linux-only native packages (Vite's bundler)
RUN npm install --no-audit --no-fund
COPY web ./
RUN npx vite build --outDir /web/dist  # types are checked locally (npm run build); the image only needs the bundle

FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY --from=web /web/dist app/static/next
COPY data data

ENV AUTH_MODE=wcl \
    DATA_DIR=/data \
    PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"

# One process: jobs live in memory and the cache is SQLite, so run a single instance.
CMD uvicorn app.web:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
