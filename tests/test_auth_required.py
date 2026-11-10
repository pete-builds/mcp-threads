"""HTTP needs a bearer token, and the server refuses to run without one.

This server can publish to a public Threads account, and it used to answer
every unauthenticated request on the LAN with 200. From pete-mcp-core 18f2261
on, a network transport with no credential is refused at startup instead of
being logged and served. These tests hold that line against the real
``server`` module and the real ASGI app it builds, driven in process with the
lifespan running:

- HTTP with no token does not start, whichever way it is asked for.
- With a token, ``POST /mcp`` is 401 without it, 401 with the wrong one, and
  serves ``initialize`` with the right one.
- The path the container healthcheck probes still reads as healthy when the
  probe carries no token, so turning auth on cannot restart-loop the
  container, and that path is not a transport mount.
- stdio, a local pipe, still starts with no token.

The variable is ``MCP_AUTH_TOKEN`` and it has no second name:
``BaseCoreSettings.auth_token`` declares no alias.

Each test imports a fresh copy of ``server`` under an environment it controls.
The import is not free of side effects (logging setup, a token-store read), so
the fixture points the data directory at a temp path, stops ``load_dotenv`` and
pydantic from finding any real ``.env``, and puts the logging tree back.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import dotenv
import fastmcp
import httpx
import pytest
import yaml
from pete_mcp_core.auth import AuthRequiredError

REPO = Path(__file__).resolve().parent.parent
#: Obviously fake. The server only compares these with what a request presents.
TOKEN = "test-token-not-a-secret"
WRONG_TOKEN = "wrong-token-not-a-secret"
#: The address a LAN client types, so the Host header is the production one.
BASE_URL = "http://192.168.86.20:3726"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "auth-required-test", "version": "0"},
    },
}
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


def _bearer(token: str) -> dict[str, str]:
    return {**MCP_HEADERS, "authorization": f"Bearer {token}"}


def _snapshot_logging() -> list[tuple[logging.Logger, int, list, list, bool]]:
    loggers = [logging.getLogger()] + [
        lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)
    ]
    return [(lg, lg.level, lg.handlers[:], lg.filters[:], lg.propagate) for lg in loggers]


def _restore_logging(snapshot) -> None:
    for lg, level, handlers, filters, propagate in snapshot:
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.filters[:] = filters
        lg.propagate = propagate


@pytest.fixture
async def load_server(monkeypatch, tmp_path):
    """Return ``load(**env)``: import a fresh ``server`` under exactly that env.

    Starts from no ``MCP_*``, ``FASTMCP_*`` or ``THREADS_*`` variable at all, so
    "no token" in a test means no token, not "whatever the developer exported".
    """
    for key in list(os.environ):
        if key.startswith(("MCP_", "FASTMCP_", "THREADS_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("THREADS_APP_ID", "test-app")
    monkeypatch.setenv("THREADS_APP_SECRET", "test-secret")
    monkeypatch.setenv("THREADS_DATA_DIR", str(tmp_path / "data"))
    # server.py calls load_dotenv(), which walks up from the repo looking for a
    # .env, and pydantic reads ./.env. Neither may reach a real one from here.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.chdir(tmp_path)
    # server.main() fills this in when it is unset; hand back whatever was there.
    monkeypatch.setattr(
        fastmcp.settings,
        "http_session_idle_timeout",
        fastmcp.settings.http_session_idle_timeout,
    )

    logging_before = _snapshot_logging()
    loaded = []

    def load(**env: str):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        sys.modules.pop("server", None)
        module = importlib.import_module("server")
        loaded.append(module)
        return module

    yield load

    sys.modules.pop("server", None)
    for module in loaded:
        await module.client.close()
    _restore_logging(logging_before)


@asynccontextmanager
async def serving(server):
    """The app ``mcp.run`` would serve, with its lifespan up, and a client on it."""
    app = server.mcp.http_app(transport="streamable-http")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            yield client


def _rpc_body(resp: httpx.Response) -> dict:
    """The JSON-RPC message in a streamable-http reply, SSE-framed or plain."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line.removeprefix("data:"))
        pytest.fail(f"no data line in the event stream: {resp.text[:200]!r}")
    return resp.json()


def _refuse_to_serve(*_args, **_kwargs):
    pytest.fail("the server went on to mcp.run() instead of refusing to start")


# --- HTTP with no token does not start ------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="transport-unset-so-http-by-default"),
        pytest.param({"MCP_TRANSPORT": "streamable-http"}, id="transport-set-as-compose-does"),
        pytest.param({"MCP_AUTH_TOKEN": ""}, id="token-empty"),
        pytest.param({"MCP_AUTH_TOKEN": "   "}, id="token-only-whitespace"),
    ],
)
def test_http_without_a_token_refuses_to_load(load_server, env):
    """The container runs ``python server.py``. With no token that import must
    raise before a FastMCP app exists, so nothing can be listening."""
    with pytest.raises(AuthRequiredError, match="Bearer token required for client 'threads'"):
        load_server(**env)
    assert "server" not in sys.modules


def test_http_is_the_default_transport(load_server):
    """The refusal above keys on ``settings.transport``. If that drifted back to
    the library's stdio default while ``main()`` still served HTTP, the import
    would pass with no token and only ``run_server`` would be left to refuse."""
    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    assert server.DEFAULT_TRANSPORT == "streamable-http"
    assert server.settings.transport == "streamable-http"
    assert server.settings.auth_required is True


def test_auth_required_false_does_not_open_http(load_server, monkeypatch):
    """``MCP_AUTH_REQUIRED=false`` was the documented way to run open. It no
    longer is one: the import gets past the settings check with no provider,
    and ``run_server`` then refuses to put that on a network transport."""
    server = load_server(MCP_AUTH_REQUIRED="false")
    assert server.mcp.auth is None
    monkeypatch.setattr(server.mcp, "run", _refuse_to_serve)
    with pytest.raises(ValueError, match="Refusing to start streamable-http with no auth provider"):
        server.main()


#: ``python server.py`` as the container runs it, with uvicorn's serve loop
#: swapped for a marker so a regression prints LISTENING instead of binding a
#: socket, and with load_dotenv stubbed so no real .env is read.
_RUN_AS_MAIN = r"""
import runpy, sys
import dotenv, uvicorn

dotenv.load_dotenv = lambda *args, **kwargs: False

async def _serve(self, sockets=None):
    print("LISTENING", flush=True)

uvicorn.Server.serve = _serve
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _run_as_main(tmp_path, **env_overrides: str) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("MCP_", "FASTMCP_", "THREADS_")) and k != "PYTHONPATH"
    }
    env.update(
        {
            "THREADS_APP_ID": "test-app",
            "THREADS_APP_SECRET": "test-secret",
            "THREADS_DATA_DIR": str(tmp_path / "data"),
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "PYTHONPATH": str(REPO),
        }
    )
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", _RUN_AS_MAIN, str(REPO / "server.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_process_exits_nonzero_with_the_refusal_in_its_log(tmp_path):
    """What an operator sees: the process dies, non-zero, naming the reason.
    Docker reports that as a failed start rather than an open server."""
    proc = _run_as_main(tmp_path)
    assert proc.returncode != 0, proc.stdout + proc.stderr[-2000:]
    assert "LISTENING" not in proc.stdout
    assert "AuthRequiredError" in proc.stderr
    assert "Bearer token required for client 'threads'" in proc.stderr


def test_control_process_serves_once_a_token_is_set(tmp_path):
    """Positive control: the same launch reaches the serve loop with a token,
    so the refusal above is about the token and not a launch that cannot work."""
    proc = _run_as_main(tmp_path, MCP_AUTH_TOKEN=TOKEN)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "LISTENING" in proc.stdout


# --- With a token: the transport is closed to anyone without it -----------


async def test_initialize_without_a_token_is_401(load_server):
    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    async with serving(server) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")
    assert "mcp-session-id" not in resp.headers


async def test_initialize_with_the_wrong_token_is_401(load_server):
    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    async with serving(server) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(WRONG_TOKEN))
    assert resp.status_code == 401
    assert "invalid_token" in resp.headers["www-authenticate"]
    assert "mcp-session-id" not in resp.headers


async def test_initialize_with_the_right_token_succeeds(load_server):
    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    async with serving(server) as client:
        resp = await client.post("/mcp", json=INITIALIZE, headers=_bearer(TOKEN))
        body = _rpc_body(resp)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers.get("mcp-session-id")
    assert "error" not in body, body
    assert body["result"]["serverInfo"]["name"] == "Threads"
    assert body["result"]["protocolVersion"]


async def test_tool_calls_without_a_token_are_401(load_server):
    """Not only the handshake: a request that goes straight for a tool, the way
    a script that skips ``initialize`` would, is turned away too."""
    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "token_status", "arguments": {}},
    }
    async with serving(server) as client:
        listing = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, headers=MCP_HEADERS
        )
        resp = await client.post("/mcp", json=call, headers=MCP_HEADERS)
    assert listing.status_code == 401
    assert resp.status_code == 401


# --- The container healthcheck keeps passing with auth on -----------------
#
# Docker restarts a container that fails its healthcheck. The probe carries no
# token, so if it landed on a path that answers 401 and the shim did not count
# 401 as alive, turning auth on would restart-loop a server that is serving
# correctly. And if it landed on /mcp it would open a transport session on
# every probe. Both are read here from what actually ships: the Dockerfile,
# docker-compose.yml, and the real healthcheck.py shim.

_HEALTH_PATH_ENV = re.compile(r"^(?!\s*#).*\bMCP_HEALTH_PATH=(\S+)", re.MULTILINE)


def _dockerfile_health_path(dockerfile_text: str) -> str | None:
    """The MCP_HEALTH_PATH the image bakes in, or None when it sets none."""
    found = _HEALTH_PATH_ENV.findall(dockerfile_text)
    return found[-1].strip("\"'") if found else None


def _deployment_env(deployment: str) -> dict[str, str]:
    """MCP_HEALTH_PATH as each deployment sets it: a bare ``docker run`` gets
    the image's ENV; compose layers its ``environment:`` on top."""
    env: dict[str, str] = {}
    baked = _dockerfile_health_path((REPO / "Dockerfile").read_text())
    if baked is not None:
        env["MCP_HEALTH_PATH"] = baked
    if deployment == "compose":
        compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
        override = compose["services"]["mcp-threads"].get("environment", {})
        if "MCP_HEALTH_PATH" in override:
            env["MCP_HEALTH_PATH"] = str(override["MCP_HEALTH_PATH"])
    return env


def _what_the_shim_probes(monkeypatch, env: dict[str, str]) -> tuple[str, frozenset[int]]:
    """Run the real ``healthcheck.main()`` and record the path and the healthy
    codes it hands ``pete_mcp_core.healthcheck.check``."""
    import healthcheck

    monkeypatch.delenv("MCP_HEALTH_PATH", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen: dict = {}

    def record(port, *, path, healthy_codes, **_kwargs):
        seen.update(path=path, codes=frozenset(healthy_codes))
        return 0

    monkeypatch.setattr(healthcheck, "check", record)
    assert healthcheck.main() == 0
    return seen["path"], seen["codes"]


@pytest.mark.parametrize("deployment", ["image", "compose"])
async def test_healthcheck_stays_healthy_with_auth_on_and_no_token(
    load_server, monkeypatch, deployment
):
    from pete_mcp_core.healthcheck import TRANSPORT_PATHS

    path, healthy_codes = _what_the_shim_probes(monkeypatch, _deployment_env(deployment))
    assert path not in TRANSPORT_PATHS, f"the {deployment} healthcheck probes the MCP transport"
    assert not path.startswith(("/mcp", "/sse"))

    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    async with serving(server) as client:
        probe = await client.get(path)  # no Authorization header, like Docker
        transport_probe = await client.get("/mcp")

    assert probe.status_code in healthy_codes, (path, probe.status_code)
    # An unseeded volume is the state here, so this is the 503 branch: the
    # route answered without a token, and the shim reads that as alive.
    assert probe.status_code in {200, 503}, "/healthz must not sit behind the bearer token"
    assert "mcp-session-id" not in probe.headers
    # Control: auth really is on for this app. The transport mount, probed the
    # same tokenless way, is turned away.
    assert transport_probe.status_code == 401


def test_control_a_transport_health_path_is_detected():
    """Positive control for the Dockerfile reader: the line this image used to
    carry is read back as a transport path, and a commented one is ignored."""
    from pete_mcp_core.healthcheck import TRANSPORT_PATHS

    assert _dockerfile_health_path("USER mcp\nENV MCP_HEALTH_PATH=/mcp\n") in TRANSPORT_PATHS
    assert _dockerfile_health_path('ENV A=1 \\\n    MCP_HEALTH_PATH="/mcp"\n') == "/mcp"
    assert _dockerfile_health_path("# ENV MCP_HEALTH_PATH=/mcp\n") is None


# --- stdio still works for local development ------------------------------


def test_stdio_starts_with_no_token(load_server, monkeypatch):
    """stdio is a local pipe with no network surface. ``MCP_TRANSPORT=stdio``
    is the only thing a local user has to set: no token, no opt-out flag."""
    server = load_server(MCP_TRANSPORT="stdio")
    assert server.mcp.auth is None
    ran: dict = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: ran.update(kwargs))
    server.main()
    assert ran == {"transport": "stdio"}
