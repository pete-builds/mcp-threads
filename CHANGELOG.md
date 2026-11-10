# Changelog

Notable changes to mcp-threads. Versions follow [Semantic Versioning](https://semver.org/). While the version is 0.x, a minor bump can break a deployment, and when it does the entry leads with that.

Releases before 0.3.0 predate this file. Their changes are in the git history.

## 0.3.0

### Breaking

- **The server no longer starts on HTTP without a bearer token.** Until now it ran with no token and answered any request on the network, so anything that could reach port 3726 could post to the account. From this version, `MCP_AUTH_TOKEN` must be set before the container starts. If it is missing or blank, the server exits non-zero with `Bearer token required for client 'threads' but not provided` in its log and never listens. Under `restart: unless-stopped` that shows up as a container that keeps restarting.

  **Before deploying this image:** generate a token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, put it in `.env` on the host as `MCP_AUTH_TOKEN=...` (mode 600, never in `docker-compose.yml` or git), and add `Authorization: Bearer <token>` to every client that connects. A client without the header gets `401` on every request.

- **There is no setting that runs HTTP open.** `MCP_AUTH_REQUIRED=false` used to be the default and meant "serve without a token". It no longer opens HTTP: with it set and no token, the server refuses with `Refusing to start streamable-http with no auth provider`.

### Changed

- pete-mcp-core moves from `15d2106` to `18f2261`, which is where the fail-closed behaviour comes from. The same pin makes the shared library install an idle-session timeout on the HTTP transport; this server already set one of 1800 seconds itself, and that is unchanged.
- stdio is unaffected: `MCP_TRANSPORT=stdio` starts with no bearer token, because a local pipe has no network surface. HTTP stays the default transport when `MCP_TRANSPORT` is unset.
- `GET /healthz` stays open without a token, on purpose, so Uptime Kuma and the container healthcheck keep working with auth on.

### Fixed

- **The image's own healthcheck default probed the MCP transport.** The Dockerfile set `MCP_HEALTH_PATH=/mcp`, so a container started without the compose file probed `/mcp` every 30 seconds, opening a transport session each time. `docker-compose.yml` overrode it to `/healthz`, which hid the problem on the compose deployment. The image now carries no override and the healthcheck shim's own default, `/healthz`, applies everywhere.
- **`/healthz` reported version `0.2.0` on the 0.2.1 and 0.2.2 images.** The constant in `server.py` was never bumped. It now reads `0.3.0`, and a test fails if `server.py`, `pyproject.toml` and the image tag in `docker-compose.yml` disagree.

### Tests

- `tests/test_auth_required.py` drives the real server module and its real ASGI app: no token refuses to start, a missing or wrong token gets `401`, the right token completes `initialize`, the container healthcheck path stays healthy with no token, and stdio starts without one.
- `tests/test_lan_host.py` presents a token on every probe and adds the case of a LAN client with none, which must get `401`.
