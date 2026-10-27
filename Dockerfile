# WhatsApp support bot (main_http.py) + optional WhatsApp group bridge (wa_bridge/, Node.js)

# --- Stage 1: install the bridge's Node.js dependencies ---
FROM node:22-slim AS bridge
WORKDIR /bridge
COPY wa_bridge/package.json wa_bridge/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY wa_bridge/*.js ./

# --- Stage 2: the app ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=3000 \
    SUPPORT_DB_PATH=/data/support.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Node.js runtime + the bridge (both images are Debian bookworm, so the binary runs as-is)
COPY --from=bridge /usr/local/bin/node /usr/local/bin/node
COPY --from=bridge /bridge /app/wa_bridge

# Run as a non-root user; /data holds the database and the WhatsApp session - mount a volume there
RUN useradd --create-home --uid 10001 app && mkdir -p /data /app/data && chown app:app /data /app/data
USER app
VOLUME ["/data"]

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"3000\")}/health', timeout=4)"

# Starts main_http.py, plus the WhatsApp bridge when GROUP_AGENT_ENABLED=true
CMD ["sh", "/app/start.sh"]
