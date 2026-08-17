# Docker Hub official image index digest, verified 2026-08-14:
# https://hub.docker.com/layers/library/python/3.12.11-slim-bookworm/images/sha256-c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TAPD_CAPABILITY_HOME=/data

WORKDIR /app

COPY LICENSE THIRD_PARTY_NOTICES.md /licenses/tapd-capability/

COPY adapters/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && mkdir -p /data \
    && chown 10001:10001 /data \
    && chmod 0750 /data

FROM base AS test

COPY Dockerfile .dockerignore Jenkinsfile /app/
COPY README.md LICENSE THIRD_PARTY_NOTICES.md /app/
COPY .github/workflows/ci.yml /app/.github/workflows/ci.yml
COPY ci/ /app/ci/
COPY src/ /app/src/
COPY adapters/mcp_server.py adapters/requirements.txt /app/adapters/
COPY deploy/ /app/deploy/
COPY tests/ /app/tests/
RUN python -X utf8 -m unittest discover -s tests -v

FROM base AS runtime

ARG SOURCE_REVISION
RUN case "${SOURCE_REVISION}" in \
      ""|*[!0-9a-f]*) echo "invalid SOURCE_REVISION" >&2; exit 64 ;; \
    esac \
    && test "${#SOURCE_REVISION}" -eq 40
ENV TAPD_CAPABILITY_SOURCE_REVISION=${SOURCE_REVISION}
LABEL org.opencontainers.image.title="tapd-capability" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.description="Review-only streamable MCP staging candidate"

COPY src/ /app/src/
COPY adapters/mcp_server.py /app/adapters/mcp_server.py

USER 10001:10001
EXPOSE 3796

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:3796/healthz', timeout=2)) == {'status': 'ok'}"]

CMD ["python", "adapters/mcp_server.py", "--transport", "http", "--host", "0.0.0.0", "--port", "3796"]
