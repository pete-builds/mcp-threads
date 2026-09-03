# Pinned digest so rebuilds are reproducible. Refresh with:
#   docker pull python:3.13-slim && docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
# Dependabot keeps it current weekly via .github/dependabot.yml.
FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2

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

# Drop pip now that the dependencies above are installed. Nothing at runtime
# uses it: the entrypoint and the healthcheck are plain `python` calls, and the packages
# themselves are already unpacked into site-packages.
#
# This is the only fix for two recurring Trivy HIGHs. pip ships a vendored
# dependency set (see pip/_vendor/vendor.txt) that Trivy scans as real packages:
# msgpack 1.1.2 (GHSA-6v7p-g79w-8964) and setuptools 70.3.0 (CVE-2025-47273).
# Neither is an application dependency, so no lockfile change can move them, and
# no pip release ships fixed versions. Removing the unused component is the fix.
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.*/site-packages/pip \
              /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

USER mcp

VOLUME ["/data"]
EXPOSE 3726

ENV MCP_HEALTH_PATH=/mcp

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD python healthcheck.py || exit 1

CMD ["python", "server.py"]
