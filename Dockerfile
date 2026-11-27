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

ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500

FROM ${UV_IMAGE} AS uv

# ----- Stage 1: install the service and its locked dependencies into /app/.venv -----
# This stage uses the same Python image as the runtime stage, so the virtual environment's
# interpreter path (/usr/local/bin/python3.14) exists unchanged in the final image.
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.14 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# git fetches the shared contract package, which uv.lock pins to a tagged commit. It is
# installed in this build stage only; the runtime image never has it.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# --no-editable installs the workspace packages themselves into the venv, so the runtime
# stage needs neither the source tree nor uv.
# One image runs all three services: main.py starts the API service, agent_main.py the agent
# service and web_main.py the interface (HW3). Each Cloud Run service picks its entry point
# with its own start command.
RUN uv sync --locked --no-dev --no-editable \
      --package cloud-storage-service --package course-agent-service --package course-web

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
COPY main.py agent_main.py web_main.py ./

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
