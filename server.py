"""MCP Threads — post to a Threads account via Meta's official Threads API.

FastMCP wiring only. Tool bodies live in ``tools/``; API and credential logic
lives in ``clients/``.

The governing constraint is the credential lifecycle: Threads issues no
separate refresh token, so the long-lived access token IS the refreshable
credential and every refresh replaces it. It is persisted to a named Docker
volume with atomic writes; ``.env`` is a one-time seed only. See
``clients/tokenstore.py``.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastmcp import FastMCP
from pete_mcp_core import build_auth_provider, configure_logging, run_server
from pete_mcp_core.settings import BaseCoreSettings
from pydantic import AliasChoices, Field, SecretStr, ValidationError

from clients.redact import install_log_redaction
from clients.threads import (
    DEFAULT_AUTH_BASE,
    DEFAULT_GRANTED_SCOPES,
    DEFAULT_GRAPH_BASE,
    ThreadsClient,
)
from tools.health import register_health_route
from tools.insights import register_insight_tools
from tools.publish import register_publish_tools
from tools.quota import register_quota_tools
from tools.read import register_read_tools
from tools.token import register_token_tools

load_dotenv()

DEFAULT_PORT = 3726
VERSION = "0.2.0"


class ThreadsSettings(BaseCoreSettings):
    threads_app_id: str = Field(
        default="",
        validation_alias=AliasChoices("THREADS_APP_ID", "MCP_THREADS_APP_ID"),
    )
    threads_app_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("THREADS_APP_SECRET", "MCP_THREADS_APP_SECRET"),
    )
    # One-time seed only. Once the data volume holds a token, this is ignored
    # forever — refreshes replace the credential and only the volume is
    # authoritative.
    threads_seed_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("THREADS_SEED_TOKEN", "MCP_THREADS_SEED_TOKEN"),
    )
    threads_data_dir: str = Field(
        default="/data",
        validation_alias=AliasChoices("THREADS_DATA_DIR", "MCP_THREADS_DATA_DIR"),
    )
    threads_graph_base: str = Field(
        default=DEFAULT_GRAPH_BASE,
        validation_alias=AliasChoices("THREADS_GRAPH_BASE", "MCP_THREADS_GRAPH_BASE"),
    )
    threads_auth_base: str = Field(
        default=DEFAULT_AUTH_BASE,
        validation_alias=AliasChoices("THREADS_AUTH_BASE", "MCP_THREADS_AUTH_BASE"),
    )
    # Scopes the live token actually carries, so a call needing one it lacks
    # fails with a remedy instead of a raw 403. Set empty to disable the
    # pre-flight and let the API decide.
    threads_granted_scopes: str = Field(
        default=",".join(DEFAULT_GRANTED_SCOPES),
        validation_alias=AliasChoices(
            "THREADS_GRANTED_SCOPES", "MCP_THREADS_GRANTED_SCOPES"
        ),
    )


try:
    settings = ThreadsSettings()
except ValidationError as exc:
    print(f"FATAL: invalid configuration: {exc}", file=sys.stderr)
    sys.exit(1)

configure_logging(
    settings.log_level,
    settings.log_format,
    extra_sensitive_keys=[
        "threads_app_secret",
        "threads_seed_token",
        "app_secret",
        "seed_token",
    ],
)
# The Threads token endpoints pass the token as a QUERY PARAM and httpx logs
# full request URLs at INFO. Silence and redact before any request is made.
install_log_redaction()

log = logging.getLogger("mcp-threads")

_missing = [
    name
    for name, value in (
        ("THREADS_APP_ID", settings.threads_app_id),
        ("THREADS_APP_SECRET", settings.threads_app_secret.get_secret_value()),
    )
    if not value
]
if _missing:
    log.critical("Missing required environment variables: %s", ", ".join(_missing))
    log.critical(
        "Copy .env.example to .env, create the Threads app in the Meta App "
        "Dashboard, then run bootstrap.py locally to obtain THREADS_SEED_TOKEN."
    )
    sys.exit(1)

_granted = tuple(
    s.strip() for s in settings.threads_granted_scopes.split(",") if s.strip()
)

client = ThreadsClient(
    app_id=settings.threads_app_id,
    app_secret=settings.threads_app_secret.get_secret_value(),
    data_dir=settings.threads_data_dir,
    seed_token=settings.threads_seed_token.get_secret_value() or None,
    graph_base=settings.threads_graph_base,
    auth_base=settings.threads_auth_base,
    granted_scopes=_granted or None,
)

# Mandated by the design spec: surface days-to-expiry at boot so a dying
# credential is visible in `docker logs`, not only via the tool.
client.log_startup_status()


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await client.close()


mcp = FastMCP(
    "Threads",
    lifespan=lifespan,
    auth=build_auth_provider(
        settings.auth_token,
        client_id="threads",
        required=settings.auth_required,
        logger=log,
    ),
)

register_token_tools(mcp, client)
register_read_tools(mcp, client)
register_quota_tools(mcp, client)
register_insight_tools(mcp, client)
register_publish_tools(mcp, client)
# Plain HTTP, not a tool: Uptime Kuma polls status codes and cannot call an
# MCP tool. Deliberately NOT the container healthcheck — see tools/health.py.
register_health_route(mcp, client, version=VERSION)


def main() -> None:
    run_server(
        mcp,
        default_port=DEFAULT_PORT,
        default_transport="streamable-http",
        default_host="0.0.0.0",
    )


if __name__ == "__main__":
    main()
