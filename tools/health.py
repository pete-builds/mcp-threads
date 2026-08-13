"""The ``GET /healthz`` route: token expiry as an HTTP status code.

Not an MCP tool. Uptime Kuma polls HTTP and reads status codes; it has no way
to call ``token_status``. This registers a plain Starlette route on FastMCP's
app so the monitor can watch the credential.

**Unauthenticated by design.** FastMCP wraps only the ``/mcp`` route in the
bearer-auth middleware; custom routes are mounted outside it. That is what
makes the endpoint pollable, and it is safe because the body carries no
credential material — see :mod:`clients.health`.

**Not the container healthcheck.** Docker restarts a container that fails its
healthcheck, and a restart cannot renew an expiring token, so wiring this to
``HEALTHCHECK`` would turn a warning into a restart loop. The container probe
stays on ``/mcp`` (liveness); this endpoint is credential health.
"""

from __future__ import annotations

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from clients.health import health_snapshot
from clients.threads import ThreadsClient


def register_health_route(mcp: FastMCP, client: ThreadsClient, *, version: str) -> None:
    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        """Credential health for an HTTP poller.

        ``200`` while the token has more than 14 whole days left; ``503`` when
        it has 14 or fewer, has expired, is missing, or is corrupt. Reads the
        persisted token store only: no network call, no refresh, no rate-limit
        cost.
        """
        payload, status_code = health_snapshot(client.store, version=version)
        return JSONResponse(payload, status_code=status_code)
