# MyRadius backend image.
#
# One Dockerfile, three targets:
#   deps     — dependency layer, shared by the other two
#   dev      — dev dependencies and auto-reload, used by docker-compose
#   runtime  — slim production image, non-root, Gunicorn with Uvicorn workers
#
# Dependencies are installed before the source is copied, so editing code does
# not reinstall the world. Each target's layers stay cached independently.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS deps

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /srv/app

COPY pyproject.toml uv.lock ./

# --no-install-project: the application itself is not installed as a package, it
# is run from the working directory. That keeps this layer valid across code changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------
FROM deps AS dev

# The dev group adds ruff, mypy, and pytest so the container can run the checks.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/srv/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY . .

EXPOSE 8000
# Compose bind-mounts the source over this, so --reload picks up host edits.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# curl is for the container healthcheck; libpq is not needed because asyncpg
# speaks the wire protocol itself. Nothing else is installed: a smaller image is
# a smaller attack surface.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# Runs unprivileged. A compromised dependency should not be able to write to the
# application directory.
RUN groupadd --system --gid 1001 myradius \
    && useradd --system --uid 1001 --gid myradius --create-home myradius

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/srv/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

COPY --from=deps --chown=root:root /opt/venv /opt/venv

WORKDIR /srv/app
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root app ./app
COPY --chown=root:root migrations ./migrations
# Operational scripts: the first-admin bootstrap, area seeding, off-site backups.
COPY --chown=root:root scripts ./scripts
# Area polygons, for `python -m scripts.seed_areas` on a new deployment.
COPY --chown=root:root seeds ./seeds
COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER myradius

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

ENTRYPOINT ["entrypoint.sh"]
CMD ["api"]
