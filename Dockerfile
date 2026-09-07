# Local/prod app image. uv base ships Python 3.12 + uv.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
# Put the project venv on PATH so we call alembic/uvicorn directly at runtime
# (no `uv run`, so the container never re-syncs or pulls dev deps on start).
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Install dependencies first (cached unless the lock changes). The `embeddings`
# extra pulls torch + sentence-transformers so the image can run bge-m3 (IR-3);
# the model weights themselves live in a mounted cache volume, not the image.
#
# THE DOWNLOAD CACHE LIVES OUTSIDE THE IMAGE, in a BuildKit cache mount. Without it the
# downloaded wheels land inside THIS layer, so a lock change — which invalidates the layer
# — rebuilds from the parent layer, where the cache no longer exists: every rebuild
# re-downloads the full ~3 GB dependency set even when one package moved. It also baked a
# second copy of every archive into the shipped image (measured 2026-08-31: 4.9 GB of
# /root/.cache inside an 11.4 GB image). The mount fixes both: uv re-fetches only what
# actually changed, and the image stops carrying the cache. UV_LINK_MODE=copy above is
# required for this — the cache mount is a different filesystem, where hardlinks fail.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv     uv sync --frozen --no-dev --no-install-project --extra embeddings

# System deps placed AFTER the (cached) torch layer so they don't bust it. Claude Code CLI for the
# strong tier (headless `claude -p` runs IN this container, reaches its own MCP over loopback);
# pg_dump v16 (>= the pg16 server) for the daily Drive backup (ops/gdrive_backup). Auth for both is
# runtime, not baked. Cached independent of app-code changes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates ripgrep \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && claude --version \
 && rm -rf /var/lib/apt/lists/*
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
 && pg_dump --version \
 && rm -rf /var/lib/apt/lists/*

# Then the application itself.
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv     uv sync --frozen --no-dev --extra embeddings

# B.9 self-identification: the image knows the git commit it was built from, so the
# review launcher endpoint can name the deployed ref for self-hosting reviews (.git is
# deliberately NOT in the build context — the value must come in as a build arg; the
# deploy command passes GIT_COMMIT=$(git rev-parse HEAD), see docker-compose.yml).
# Empty on images built without the arg — consumers treat that as "unknown", never fail.
ARG GIT_COMMIT=""
ENV AM_GIT_COMMIT=${GIT_COMMIT}
# The RELEASE TAG rides the same inject step (B.13 A-4, review f46a31d2, finding
# initial-release-bypasses-capture-and-loses-tag): "tag where one exists, the commit
# always". Optional — empty when the checkout is not at a tag; the first-bring-up
# release synthesis reads it so a tag-deployed install does not lose its tag.
ARG GIT_TAG=""
ENV AM_GIT_TAG=${GIT_TAG}

EXPOSE 8000

# Apply migrations, then serve API + MCP + web admin. --proxy-headers +
# --forwarded-allow-ips=* make uvicorn honor ngrok's X-Forwarded-Proto: https, so
# app-generated URLs (esp. the /mcp -> /mcp/ redirect) keep the https scheme —
# otherwise the client drops its Authorization header on the https->http downgrade.
CMD ["sh", "-c", "alembic upgrade head && uvicorn assistant_memory.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*"]
