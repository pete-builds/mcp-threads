# mcp-threads

An MCP server for Meta's [Threads API](https://developers.facebook.com/docs/threads). Thirteen tools over Streamable HTTP: publish posts, reply chains, images, and replies to anyone; read your timeline, replies, and insights; and keep the credential alive without ever touching it.

Built around a problem most Threads integrations defer: **the credential expires permanently in 60 days, and the failure is silent.**

It runs as a long-lived service on your own infrastructure rather than as a process your desktop client spawns, and the limits that matter are enforced in code rather than described in a prompt.

## The problem this server solves

Threads does **not** issue a separate, immutable refresh token. The long-lived access token *is* the refreshable credential, it lives 60 days, and **every refresh replaces it**. Miss the window and it is permanently dead: no API call recovers it, only a human re-running the browser OAuth flow.

The obvious design puts the token in `.env` and refreshes it in memory. That works perfectly for 60 days and then dies on the next container restart, when the process re-reads a token that expired weeks ago. The failure lands two months after the last code change, with nothing recent to blame.

So instead:

| Mitigation | Where |
|---|---|
| Token persists to a named Docker volume, written atomically (temp file, then `os.replace`), mode 600 | `clients/tokenstore.py` |
| `.env` holds a **one-time seed**, consumed only when the volume is empty | `ThreadsClient.load_state` |
| Refresh fires proactively at **day 45**, never at the last moment | `TokenState.needs_refresh` |
| Refresh guarded by an `asyncio.Lock` with a double-check inside it, so concurrent tool calls cannot race two tokens into the store | `ThreadsClient.ensure_token` |
| Days-to-expiry logged at every startup | `ThreadsClient.log_startup_status` |
| `token_status` tool exposes expiry for external alerting | `tools/token.py` |

There is an explicit regression test for the trap: refresh, discard the client, rebuild it from the same stale `.env` seed, and assert it loads the **new** token from the volume. Reintroducing the naive pattern turns that test red.

## Why it runs centrally

Most MCP servers are stdio processes a desktop client spawns and kills with the session. That is a fine model for a stateless API wrapper and the wrong one here.

**A refresh deadline needs a process that exists.** A server that only runs while a desktop app is open cannot promise to refresh a credential inside 60 days. Close the laptop for two months and the token is gone. Refreshing "on next use" is not a guarantee, it is a hope about your own habits.

**One credential, many clients.** Running centrally means the token exists in exactly one place, on one box, at mode 600 on a volume. Your laptop never holds it. A workflow engine, a scheduler, or a second machine reach the same server over HTTP instead of each keeping a copy of a live posting credential.

**Webhooks need an address.** Real-time mentions and replies require a stable endpoint. A stdio server has to tunnel out to fake one.

The tradeoff is honest: this needs somewhere to run. If you want something that works on a laptop with no infrastructure, several stdio Threads MCP servers exist and one of them is a better fit. This is built for a box that is already on.

## The approval layer

Publishing is irreversible in the way that matters: a deleted post was still seen. So the constraints live in code, at the tool boundary, where a model cannot talk its way past them.

- **Two-step publishing.** `create_post` builds an inert container with zero timeline effect; `publish_post` commits it. A misfiring agent produces an unused container, not a live post.
- **Byte-accurate limits.** The 500 limit is counted in UTF-8 bytes because an emoji costs 4, and a model asked to count characters will eventually be wrong in public.
- **Quota from the API, not a local tally.** A local counter cannot see the post you made from your phone. Measured on a real account: Meta reported 1 post used while the local log said 0.
- **Missing scopes refuse before the call**, with the remedy, instead of forwarding a raw 403 that reads like a bug in the server.

The human gate lives one layer up, in the agent skill that drives these tools: draft, show the operator the exact bytes and segment counts, stop, and publish only on explicit approval. Approval is never inferred from silence or from a vague "sounds good." Replying to a stranger is treated as higher risk than posting, because it puts your name in someone else's mentions: the skill must read the target post back verbatim before it will compose a reply.

The server enforces what is enforceable. The skill enforces what requires judgment. Neither trusts the model to remember a rule.

## Tools

| Tool | Idempotent | Notes |
|---|---|---|
| `token_status` | yes | Credential health. No network call. Never returns the token. |
| `whoami` | yes | Profile + connectivity check. |
| `list_posts` | yes | Recent posts with permalinks. |
| `get_replies` | yes | Top-level replies, or the whole conversation. |
| `get_publishing_limit` | yes | Meta's authoritative post/reply/delete/location quotas. |
| `get_post_insights` | yes | Views, likes, replies, reposts, quotes, shares for one post. |
| `get_account_insights` | yes | Profile-level metrics, optional date range and demographic breakdown. |
| `preview_chain` | yes | Shows how long text would split. No network call, nothing published. |
| `create_post` | no | Creates an inert container: text, image, or reply to any post. **No timeline effect.** |
| `get_container_status` | yes | Whether an image container finished processing. Read-only. |
| `publish_post` | no | Commits a container. Goes live. |
| `post_chain` | no | Splits long text and publishes it as a reply chain. Goes live. |
| `delete_post` | no | Destructive, and currently blocked: the token has no `threads_delete` scope. |

13 tools. The surface is kept deliberately small: image support, replies to other
people's posts, and quote posts are **parameters on `create_post`**, not separate
tools, because they are the same two-step flow with a different payload.

Every tool returns a JSON string in one of two shapes:

```jsonc
{"data": ...}                                     // success
{"error": "...", "code": "...", "details": {...}} // failure
```

`code` comes from a fixed enum: `UPSTREAM_DOWN`, `AUTH_FAILED`, `INVALID_INPUT`, `NOT_FOUND`, `RATE_LIMITED`, `INTERNAL`. Exceptions never escape a tool.

### Two-step publishing, on purpose

`create_post` then `publish_post`. The Threads API offers `auto_publish_text` to collapse both into one call; this server deliberately does not use it. The separation is a safety boundary: an agent that misfires produces an inert container instead of a live post. One extra HTTP call is cheap insurance when an LLM holds publish rights.

### Chain splitting

`post_chain` splits on paragraph boundaries first, then sentences, then words. It only breaks inside a word when a single word exceeds the limit on its own (a very long URL), and never inside a multi-byte character.

Length is measured in **UTF-8 bytes**, because Threads counts emoji as bytes rather than as single characters. `len(str)` says 400 emoji fit in a 500-character post; Threads says they are 1600 and rejects it.

Each segment after the first replies to the **published media ID** of the previous segment, not its container ID — using the container ID produces orphaned replies that never attach to the thread.

A mid-chain failure returns the IDs already published, the remaining text, and a `resume_reply_to_id`, so a partial chain can be resumed or rolled back. It is never silently swallowed.

### Limits enforced client-side

- 500 per post, measured in UTF-8 bytes.
- Max 5 unique links per post: every unique URL in `text`, plus `link_attachment` when it differs from all of them. Checked before the call, so the API never has to answer with `THREADS_API__LINK_LIMIT_EXCEEDED`.
- Four independent rolling-24h quotas, read from Meta's `threads_publishing_limit`
  endpoint: 250 posts, **1000 replies**, 100 deletes, 500 location searches. The
  API is the source of truth, because a local counter cannot see posts made from
  the Threads app. `publish_log.json` on the volume is a labelled fallback used
  only when that call fails, and every response carries a `source` field saying
  which one answered.
- A chain of N segments spends **1 post and N-1 replies**, against two separate
  budgets. `post_chain` pre-flights both and refuses before creating a container.
- Image posts: JPEG/PNG, publicly reachable URL (Meta fetches it server-side), and
  1000-character alt text. Size, aspect ratio and width are enforced by Meta during
  container processing and surface through `get_container_status`; this server does
  not download caller-supplied URLs.

### Missing scopes fail honestly

`delete_post` needs `threads_delete`, which this token was never granted. Rather
than forwarding a raw 403, the tool refuses before the call and returns the exact
remedy: add the permission on the Meta App Dashboard Use cases page and mint a new
token, because scopes bind at authorization time and no refresh can widen them. Set `THREADS_GRANTED_SCOPES` empty to disable the pre-flight and let the API
decide.

## Setup

### 1. Create the Threads app

In the Meta App Dashboard, create an app with the Threads use case against the account you want to post from. Capture the app ID and the **Threads App secret** (App settings > Basic).

Scopes: `threads_basic`, `threads_content_publish`, `threads_read_replies`, `threads_manage_replies`, `threads_manage_insights`, `threads_delete`.

Scopes bind at authorization time, so a missing one means redoing the entire flow. `get_replies` 403s without `threads_read_replies`, the two insight tools need `threads_manage_insights`, and `delete_post` is gated on `threads_delete`. Request the full set on the first and only run.

> `threads_delete` is offered by the use-case dashboard but absent from the authorize doc's scope list, so the authorize call may reject it. If it does, `bootstrap.py` prints the exact `THREADS_SCOPES` value to re-run with. Dropping it means `delete_post` will 403 at runtime — drop the tool or document it as expected-to-fail rather than shipping one that silently does nothing.

Posting to your own account and to app tester accounts works with **standard access**. Advanced access and App Review are only required to post on behalf of other users.

Register this redirect URI:

```
http://127.0.0.1:8766/callback
```

Then **copy it back out of the dashboard verbatim** — the dashboard may rewrite what you typed, notably by appending a trailing slash, and it must match exactly at both the authorize and the exchange step. `bootstrap.py` accepts `/callback` and `/callback/`; if the saved value differs in any other way, set `THREADS_REDIRECT_URI` to the dashboard's exact string.

> **Unverified:** every redirect-URI example in Meta's docs uses HTTPS. Whether the Threads use-case settings accept a plain-http loopback URI has not been confirmed. If the dashboard rejects it, this flow needs an HTTPS tunnel or a hosted callback instead.

### 2. Get a token

**The fast path is not OAuth.** The use-case Settings page has a **User Token Generator** that mints a long-lived token directly for Threads Testers of the app, skipping the callback flow entirely:

1. Add the account under **Add or Remove Threads Testers**.
2. Accept the invite in Threads: Settings > Account > Website permissions > Invites.
3. Reload Settings; the account now has a generate action.
4. Generate, and paste the result into `THREADS_SEED_TOKEN`.

Two requirements that fail quietly if missed. **The account must be public**; generation is blocked for private profiles. And **add the permissions before generating**: the token carries whatever the app holds at that instant, so generating early yields a token that authenticates fine, passes a profile call, and then fails every publish.

This also sidesteps the redirect-URI problem entirely, which matters because the dashboard rejects plain-http loopback URIs.

#### Fallback: the OAuth flow

Run once, on a workstation, never in the container:

```bash
export THREADS_APP_ID=...
export THREADS_APP_SECRET=...
python bootstrap.py
```

It opens the browser, catches the redirect, exchanges the code for a short-lived token, then exchanges that for a long-lived (60-day) token and prints it.

Three hosts are involved, which is not a typo — authorize on `threads.net`, the short-lived code exchange as a **POST** to `graph.threads.net/oauth/access_token`, and both the long-lived exchange and the refresh as **GET**s to `graph.threads.com`. Override with `THREADS_OAUTH_BASE` and `THREADS_AUTH_BASE` if Meta's migration moves them.

### 3. Configure and run

```bash
cp .env.example .env   # fill in THREADS_APP_ID, THREADS_APP_SECRET, THREADS_SEED_TOKEN
docker compose up -d
```

The seed is consumed on first boot and written to the `threads-data` volume. After that the volume is authoritative and the `.env` value goes stale — that is expected, not a bug. **Deleting the volume destroys the credential permanently** and forces a `bootstrap.py` re-run.

Register with an MCP client:

```bash
claude mcp add threads --transport http --scope user --url http://<host>:3726/mcp
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `THREADS_APP_ID` | — | Required. |
| `THREADS_APP_SECRET` | — | Required. Server-side only, never sent to a client. |
| `THREADS_SEED_TOKEN` | — | One-time seed. Ignored once the volume holds a token. |
| `THREADS_DATA_DIR` | `/data` | Token store location. Must be a durable volume. |
| `THREADS_GRAPH_BASE` | `https://graph.threads.net/v1.0` | Publishing and read endpoints. |
| `THREADS_AUTH_BASE` | `https://graph.threads.com` | Token endpoints. |
| `THREADS_GRANTED_SCOPES` | the five granted scopes | Comma list used for the pre-flight scope check. Empty disables it. |
| `MCP_PORT` | `3726` | Bind port. |
| `MCP_HOST` | `0.0.0.0` | Bind host. |
| `MCP_TRANSPORT` | `streamable-http` | Transport. |
| `MCP_AUTH_TOKEN` | unset | Optional bearer token for the HTTP transport. |
| `MCP_HEALTH_PATH` | `/healthz` | Path the **container** healthcheck probes. Do not point it at `/mcp` — see [Monitoring](#monitoring-get-healthz). |

## Monitoring: `GET /healthz`

Uptime Kuma polls HTTP and cannot call an MCP tool, so credential health is also exposed as a plain HTTP route. It reads the persisted token store only: **no network call, no refresh, no rate-limit cost**, safe to poll every 60s.

```bash
curl -i http://<host>:3726/healthz
```

| Condition | Code | `status` |
|---|---|---|
| More than 14 whole days to expiry | `200` | `ok` |
| 14 or fewer days to expiry | `503` | `warning` |
| Token expired | `503` | `critical` |
| Token file present but unreadable or corrupt | `503` | `critical` |
| No token at all (unseeded volume, no seed) | `503` | `unseeded` |

```json
{
  "status": "ok",
  "days_remaining": 45,
  "token_source": "refresh",
  "detail": "token healthy",
  "expires_at": "2026-10-11T14:03:22+00:00",
  "refresh_count": 3,
  "version": "0.2.0"
}
```

Notes:

- **Unseeded is 503 on purpose.** A server with no credential is not serving; the monitor should say so rather than showing green.
- `days_remaining` is floored to whole days and the threshold is applied to that floored value, so the body and the status code can never disagree. Alerting can therefore fire up to a day early (14.9 days reads as 14). Proactive refresh runs at day 45 (15 days remaining), so reaching this endpoint's warning state already means refresh has stopped working.
- **No secret material in the body under any state.** Every field is an integer, an ISO timestamp, or a value from a closed vocabulary; `token_source` is clamped to `seed` / `refresh` / `null` so nothing read out of the token file is echoed back. `tests/test_health.py` asserts the token and every prefix of it are absent in each state, including the corrupt-file path.
- **Unauthenticated by design.** FastMCP wraps only `/mcp` in the bearer-auth middleware; custom routes sit outside it. That is what makes the endpoint pollable, and it is safe because the body carries no credential material.
- **The container healthcheck probes this path too, but reads it differently.** Docker restarts a container that fails `HEALTHCHECK`, and a restart cannot renew a credential, so a probe that failed on 503 would restart-loop a server whose only problem is an expiring token. The shim therefore treats `401` and `503` as alive: "the app answered" is the liveness signal. `500` is deliberately excluded, so a genuine fault still fails. Uptime Kuma accepts only `200-299` and is what actually alerts you.
- **Never point the container healthcheck at `/mcp`.** A bare request to the MCP mount allocates a transport session that is never reaped, roughly 40 KB each, before method dispatch and before auth. At a 30-second interval that is about 115 MiB/day of permanent growth. Measured: 300 probes against `/mcp` cost 11 MiB, 300 against `/healthz` cost 0.

Uptime Kuma monitor: HTTP(s), URL `http://<host>:3726/healthz`, interval 60s, accepted status codes `200-299` (the default). No keyword match needed — the status code carries the signal.

Both base hosts are configurable because Meta's own documentation is inconsistent: token endpoints are documented on `graph.threads.com`, publishing and read endpoints on `graph.threads.net/v1.0`, and Meta has been migrating `.net` to `.com`. If one host starts 404ing, switch it in `.env` rather than patching code.

## Secrets

The token endpoints pass the credential as a **query parameter**, and httpx logs full request URLs at INFO. That is a real leak vector, so `clients/redact.py` drops the HTTP-client loggers to WARNING *and* installs a filter that scrubs `access_token=`, `client_secret=`, `Bearer <token>`, and bare Threads tokens from every log record. There is a test that runs a real (mocked) refresh with logging wide open and scans every emitted record, plus a positive control asserting the unredacted string really would have leaked.

`token_status` never returns the token, and neither does `GET /healthz` — the latter is unauthenticated, so its body is restricted to integers, ISO timestamps, and a closed status vocabulary. The store file is mode 600. `.env` is gitignored.

## Development

```bash
uv venv .venv && uv pip install -r requirements-dev.in --python .venv/bin/python
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

Regenerate the hash-pinned lockfile (must be universal — a macOS-only resolution omits Linux transitives and the image build fails on `--require-hashes`):

```bash
uv pip compile requirements.in -o requirements.lock --generate-hashes --universal --python-version 3.13
```

## Layout

```
server.py              FastMCP wiring only, no tool bodies
bootstrap.py           one-time OAuth, run locally
healthcheck.py         Docker HEALTHCHECK shim
clients/
  threads.py           API client, token lifecycle, request plumbing
  errors.py            exception hierarchy (kept separate to avoid an import cycle)
  quota.py             threads_publishing_limit parsing + the budget gate
  insights.py          three-shape insight parser + request validation
  media.py             image URL and alt-text validation
  tokenstore.py        atomic token persistence + per-kind fallback log
  chain.py             chain orchestration and partial-failure semantics
  text.py              byte-aware length, splitter, link counting
  redact.py            log redaction
  health.py            token-expiry snapshot + HTTP status mapping (no network)
tools/
  token.py             token_status, whoami
  health.py            GET /healthz route (not an MCP tool)
  read.py              list_posts, get_replies
  quota.py             get_publishing_limit
  insights.py          get_post_insights, get_account_insights
  publish.py           create_post, get_container_status, publish_post, preview_chain,
                       post_chain, delete_post
  common.py            Standard Error Contract helpers
```

## License

MIT.
