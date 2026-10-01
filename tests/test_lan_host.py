"""The server must answer a LAN client, not only loopback.

Clients reach this server at http://192.168.86.20:<port>/mcp, so every request
carries a non-loopback ``Host`` header. MCP SDK 2 servers default to binding
``127.0.0.1`` and arm DNS-rebinding protection for a loopback bind, which
answers **421 Misdirected Request** to every LAN client while unit tests and a
loopback healthcheck stay green. FastMCP 4 owns that guard itself
(``HostOriginGuardMiddleware``), so the failure can come back through a bind
host change, a FastMCP default flip, or an env var, and nothing else in CI
would notice.

Each boot runs ``server.main()`` in a fresh interpreter with uvicorn's
``Server.serve`` replaced by a recorder. That captures the exact ASGI app and
bind host ``run_server`` hands uvicorn, without opening a socket. The app is
then driven the two ways uvicorn presents a LAN client: connected straight to
the LAN interface (``scope["server"]`` is 192.168.86.20), and through Docker's
published port, where the container sees its own bridge address while the
``Host`` header still says 192.168.86.20. The second is how nix1 runs it, and
it is the case FastMCP's guard rejects, because the guard only trusts a Host
that matches the address the socket accepted on. A subprocess keeps
``run_server``'s ``os.environ`` writes and the lifespan's ``client.close()``
away from every other test.

The positive controls build the server the loopback way and prove the same
probe reports 421 there, so a 200 here is evidence rather than a probe that
cannot fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LAN_IP = "192.168.86.20"
# What the container sees as its own address behind a published port
# (`ports: ["<port>:<port>"]`); host networking is the "direct" path.
BRIDGE_IP = "172.17.0.2"
PORT = 3726
REPO = Path(__file__).resolve().parent.parent

_PROBE = r"""
import asyncio, json, sys
import httpx, uvicorn

captured = {}

async def _record(self, sockets=None):
    captured["app"] = self.config.app
    captured["host"] = self.config.host

uvicorn.Server.serve = _record

import server
server.main()

app = captured["app"]
lan_ip, bridge_ip, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
init = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "lan-host-probe", "version": "0"},
    },
}
headers = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


def _session_manager(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        for candidate in (endpoint, getattr(endpoint, "app", None)):
            manager = getattr(candidate, "session_manager", None)
            if manager is not None:
                return manager
    return None


async def main():
    out = {"bind_host": captured["host"]}
    async with app.router.lifespan_context(app):
        manager = _session_manager(app)
        out["session_idle_timeout"] = getattr(manager, "session_idle_timeout", "missing")
        # httpx's ASGI transport sets scope["server"] from the URL: the local
        # address uvicorn reports for the accepted socket. The Host header is
        # what the client typed.
        transport = httpx.ASGITransport(app=app)
        lan_host = {**headers, "host": f"{lan_ip}:{port}"}
        paths = {
            "direct": f"http://{lan_ip}:{port}",
            "docker": f"http://{bridge_ip}:{port}",
        }
        for name, base in paths.items():
            async with httpx.AsyncClient(transport=transport, base_url=base) as client:
                resp = await client.post("/mcp", json=init, headers=lan_host)
            out[f"{name}_status"] = resp.status_code
            out[f"{name}_body"] = resp.text[:120]
    print("PROBE_RESULT " + json.dumps(out))

asyncio.run(main())
"""


def _boot(**env_overrides: str) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("FASTMCP_", "MCP_")) and k != "PYTHONPATH"
    }
    env.update(
        {
            "MCP_PORT": str(PORT),
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "PYTHONPATH": str(REPO),
        }
    )
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, LAN_IP, BRIDGE_IP, str(PORT)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # a crashed probe is reported below with its stderr
    )
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_RESULT "):
            return json.loads(line.removeprefix("PROBE_RESULT "))
    pytest.fail(f"probe produced no result (rc={proc.returncode}):\n{proc.stderr[-3000:]}")


def test_binds_every_interface() -> None:
    """A loopback bind is unreachable from the LAN before Host is even read."""
    assert _boot()["bind_host"] == "0.0.0.0"


@pytest.mark.parametrize("path", ["direct", "docker"])
def test_lan_host_header_is_served_not_421(path: str) -> None:
    result = _boot()
    assert result[f"{path}_status"] != 421, result
    assert result[f"{path}_status"] == 200, result


def test_idle_sessions_are_reaped() -> None:
    """FastMCP 4 passes session_idle_timeout=None to the SDK unless told
    otherwise, which overrides the SDK's own 1800s default and leaves every
    abandoned streamable-http session in memory for the life of the process."""
    assert _boot()["session_idle_timeout"] == 1800


def test_control_loopback_bind_is_detected() -> None:
    """Positive control: the bind assertion above can fail."""
    assert _boot(MCP_HOST="127.0.0.1")["bind_host"] == "127.0.0.1"


def test_control_loopback_build_answers_421() -> None:
    """Positive control: the probe sees a 421 when the server is built the
    loopback way, with FastMCP's Host guard armed the way it arms itself for a
    localhost bind. Same probe, same request; only the build differs."""
    result = _boot(MCP_HOST="127.0.0.1", FASTMCP_HTTP_HOST_ORIGIN_PROTECTION="auto")
    assert result["docker_status"] == 421, result


def test_control_idle_timeout_override_is_detected() -> None:
    """Positive control: an operator override reaches the session manager, so
    the 1800 above is read from the live manager, not a constant."""
    result = _boot(FASTMCP_HTTP_SESSION_IDLE_TIMEOUT="60")
    assert result["session_idle_timeout"] == 60
