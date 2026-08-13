# Pinned digest so rebuilds are reproducible. Refresh with:
#   docker pull python:3.13-slim && docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
# Dependabot keeps it current weekly via .github/dependabot.yml.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Hash-pinned lockfile. Regenerate with:
#   uv pip compile requirements.in -o requirements.lock --generate-hashes --universal --python-version 3.13
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY clients/ ./clients/
COPY tools/ ./tools/
COPY server.py .
COPY healthcheck.py .

# Non-root, pinned UID 1000. /data is created here and owned by mcp so the
# named volume mounts writable for the non-root user — the token store lives
# there and MUST be writable or every refresh fails.
RUN useradd --create-home --uid 1000 --shell /bin/bash mcp \
    && mkdir -p /data \
    && chown -R mcp:mcp /app /data
USER mcp

VOLUME ["/data"]
EXPOSE 3726

ENV MCP_HEALTH_PATH=/mcp

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD python healthcheck.py || exit 1

CMD ["python", "server.py"]
