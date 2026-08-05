# CrowdSight backend image.
#
# Python 3.11, pinned deliberately. The spec originally called for 3.12, but
# every published version of camel-oasis (including the pinned 0.2.5) declares
# Requires-Python <3.12 — so 3.12 cannot install the simulation engine at all.
# camel-ai 0.2.78 allows <3.13. 3.11 is the only version satisfying both.
# The reference host runs system Python 3.14, which is why this is
# containerised rather than run from a host virtualenv.
#
# Two targets:
#   runtime — production. Application dependencies only.
#   dev     — adds pytest and friends. Compose builds this by default so the
#             suite runs against the real dependency set rather than an
#             approximation of it. Build production with --target runtime.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build toolchain for packages without wheels; removed in the same layer so it
# does not survive into the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

COPY backend/ /app/

# Match the host UID so the bind-mounted ./data stays writable. Override at
# build time with --build-arg UID=$(id -u) if yours differs.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" crowdsight 2>/dev/null || true \
    && useradd -u "${UID}" -g "${GID}" -m -s /usr/sbin/nologin crowdsight 2>/dev/null || true \
    && mkdir -p /app/data \
    && chown -R "${UID}:${GID}" /app


FROM base AS runtime
USER crowdsight
EXPOSE 5000
CMD ["python", "-m", "app.main"]


FROM base AS dev
# requirements-dev.txt arrived with the COPY of backend/ above and pulls in
# requirements.txt, which is already satisfied.
RUN pip install -r requirements-dev.txt
USER crowdsight
EXPOSE 5000
CMD ["python", "-m", "app.main"]
