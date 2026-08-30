# syntax=docker/dockerfile:1
#
# Two-stage build. The base image is pinned by tag *and* digest so a rebuild is
# reproducible and cannot silently pick up a different upstream image.
FROM python:3.14-slim-bookworm@sha256:416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# lxml needs a compiler only if no wheel matches; keep the build deps in this
# stage so they never reach the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock


FROM python:3.14-slim-bookworm@sha256:416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63 AS runtime

LABEL org.opencontainers.image.title="GSMA RCS Auto Configuration Server" \
      org.opencontainers.image.description="RCC.14/RCC.07 OMA-CP ACS with an OMA-DM device management plane" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/jeonghun-app/auto-configuration-server"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH="/opt/venv/bin:$PATH"

# Non-root, no shell, no home: nothing to escalate to.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin acs

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root src/ ./src/

USER 10001:10001
EXPOSE 8080

# curl is not installed in slim images, so the check uses the interpreter that
# is already present.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]

# --no-access-log is deliberate: the access log line would contain the full
#   query string, i.e. IMSI, IMEI, MSISDN, OTP and provisioning token.
# --timeout-keep-alive 65 is deliberately above the ALB's 60s idle timeout so the
#   load balancer, not the server, closes idle connections.
CMD ["uvicorn", "acs.app:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--no-access-log", \
     "--timeout-keep-alive", "65", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
