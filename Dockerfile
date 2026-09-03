# ─────────────────────────────────────────────────────────────────────────────
#  🌊 RPMStream — Telegram → RPMShare streaming uploader
#  Small image, no video ever lands on disk.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first → cached layer for fast rebuilds
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY README.md ./

# Non root user; /app/work only holds the Pyrogram session file
RUN groupadd --system rpmstream \
    && useradd --system --gid rpmstream --home-dir /app --no-create-home rpmstream \
    && mkdir -p /app/logs /app/work \
    && chown -R rpmstream:rpmstream /app

USER rpmstream

# Session storage (a few KB). Mount a volume so logins survive redeploys.
VOLUME ["/app/work"]

ENTRYPOINT ["python", "-m", "app.main"]
