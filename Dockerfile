# syntax=docker/dockerfile:1
#
# Container image for the Cloud Storage Service (see docs/deployment.md).
#
#   scripts/build_image.sh        # builds for linux/amd64 and tags with the commit SHA
#
# The build is hermetic: both base images are pinned by digest, and Python packages come
# only from uv.lock (`--locked` fails if the lock does not match pyproject.toml). The image
# holds code and locked dependencies only. Credentials and other settings come from the
# environment at run time, and .dockerignore keeps .env and .dropbox_token.json out.
#
# To update a pin, change the tag and its digest together in a reviewed pull request
# (`docker buildx imagetools inspect <tag>` prints the digest).

ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500

FROM ${UV_IMAGE} AS uv

# ----- Stage 1: install the service and its locked dependencies into /app/.venv -----
# This stage uses the same Python image as the runtime stage, so the virtual environment's
# interpreter path (/usr/local/bin/python3.12) exists unchanged in the final image.
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# --no-editable installs the workspace packages themselves into the venv, so the runtime
# stage needs neither the source tree nor uv.
RUN uv sync --locked --no-dev --no-editable --package cloud-storage-service

# ----- Stage 2: the runtime image -----
FROM ${PYTHON_IMAGE} AS runtime

# The commit the image was built from; scripts/build_image.sh passes it.
ARG RELEASE=unknown
LABEL org.opencontainers.image.title="cloud-storage-service" \
      org.opencontainers.image.source="https://github.com/Shreyas191/OSPSD-Team-3" \
      org.opencontainers.image.revision="${RELEASE}"

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY main.py ./

# HOST is 0.0.0.0 so the platform can reach the server; Cloud Run overrides PORT.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    RELEASE="${RELEASE}"

USER app
EXPOSE 8080

# main.py checks the configuration first and exits with one line if a credential is missing.
CMD ["python", "main.py"]
