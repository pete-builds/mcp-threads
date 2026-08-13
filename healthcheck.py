"""Health check script for Docker HEALTHCHECK.

Wraps ``pete_mcp_core.healthcheck.check`` rather than its ``main()``, because
this server needs two things ``main()`` cannot express:

1. **Probe ``/healthz``, never ``/mcp``.** A bare ``GET /mcp`` makes the MCP
   SDK create a transport session *before* returning 406, and nothing reaps
   it (~40 KB/request, measured). At a 30s interval that is ~115 MiB/day of
   permanently leaked memory on a server meant to run unattended for years.
   ``GET /healthz`` is a plain custom route: no session, no leak (measured at
   +0.07 MiB over 300 requests).

2. **Treat 503 as alive.** ``/healthz`` returns 503 when the token is near
   expiry, expired, unseeded, or corrupt. That is a *credential* warning, not
   a liveness failure, and restarting cannot renew a token — so a 503 must
   never fail the container healthcheck or Docker turns a warning into a
   restart loop that drops the MCP transport. Here "responds at all" means
   alive; Uptime Kuma polls ``/healthz`` directly for the credential state.

401 is included for the same reason: with ``MCP_AUTH_REQUIRED=true`` an
unauthenticated probe is rejected by a server that is serving correctly.

Env precedence for the port matches ``pete_mcp_core.serve`` exactly
(``FASTMCP_PORT`` > ``MCP_PORT`` > default) so the probe can never target a
different port than the server.
"""

from __future__ import annotations

import os
import sys

from pete_mcp_core.healthcheck import DEFAULT_HEALTHY_CODES, check

DEFAULT_PORT = 3726

# "Responds at all" == alive. See the module docstring for why 503 and 401
# are in here; do not narrow this set without reading it.
HEALTHY_CODES = frozenset(DEFAULT_HEALTHY_CODES | {401, 503})


def main(default_port: int = DEFAULT_PORT) -> int:
    port_str = os.getenv("FASTMCP_PORT") or os.getenv("MCP_PORT") or str(default_port)
    path = os.getenv("MCP_HEALTH_PATH", "/healthz")
    try:
        port = int(port_str)
    except ValueError:
        return 1
    return check(port, path=path, healthy_codes=HEALTHY_CODES)


if __name__ == "__main__":
    sys.exit(main())
