# --- Airi CLI (Silo Benefits, VIC_MODE=live) ---------------------------------
# Pinned to an exact npm release. The package's postinstall downloads the Go
# binary and verifies it against the SHA256 in the package's own checksums.json
# (never set AIRI_CLI_SKIP_CHECKSUM). Only the binary is copied into the final
# image, so Node is not needed at runtime. No `curl | sh`.
FROM node:22-slim AS airi-cli
ENV AIRI_CLI_SKIP_SKILLS=1 AIRI_TELEMETRY_DISABLED=1
RUN npm install -g --no-audit --no-fund @airwallex/airi-cli@0.1.0-beta.4 \
 && cp /usr/local/lib/node_modules/@airwallex/airi-cli/vendor/airi-cli /airi-cli \
 && /airi-cli version

FROM python:3.12-slim

COPY --from=airi-cli /airi-cli /usr/local/bin/airi-cli

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/srv/.venv

WORKDIR /srv
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY db ./db

# The airi-cli keeps its signed-in session under $HOME of the runtime user. The exact
# subdirectory is UNVERIFIED (not documented; only visible after a real login), so the
# whole HOME is the persistent volume: without it every redeploy needs "Connect Airi" again.
ENV HOME=/home/paysilo AIRI_TELEMETRY_DISABLED=1
RUN mkdir -p /home/paysilo
VOLUME ["/home/paysilo"]

ENV PATH="/srv/.venv/bin:$PATH" APP_ENV=demo
EXPOSE 8000
# Single worker: the agent runtime holds paused runs in process memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
