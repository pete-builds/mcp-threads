"""One-time Threads OAuth bootstrap. Run on Pete's Mac, never in the container.

What it does:

  1. Opens the browser to Threads' authorization page.
  2. Catches the redirect at http://127.0.0.1:8766/callback.
  3. Exchanges the authorization code for a **short-lived** token (1 hour).
  4. Exchanges that for a **long-lived** token (60 days).
  5. Prints the long-lived token to seed THREADS_SEED_TOKEN once.

Usage::

    export THREADS_APP_ID=...        # Meta App Dashboard
    export THREADS_APP_SECRET=...    # App settings > Basic > Threads App secret
    python bootstrap.py

Prerequisite: the Threads app must have this exact redirect URI registered::

    http://127.0.0.1:8766/callback

**Copy the redirect URI verbatim out of the App Dashboard after saving it.**
The dashboard may rewrite what you typed — notably by appending a trailing
slash — and the value must match EXACTLY at both the authorize step and the
code-exchange step or Meta rejects the exchange. This script's callback
handler accepts ``/callback`` and ``/callback/`` so either registration works;
if the dashboard shows a trailing slash, set ``THREADS_REDIRECT_URI`` to the
dashboard's exact string.

**Unverified:** every redirect-URI example in Meta's docs uses HTTPS. Whether
the Threads use-case settings accept a plain-http loopback URI at all has not
been confirmed. If the dashboard rejects ``http://127.0.0.1:8766/callback``,
this flow needs an HTTPS tunnel or a hosted callback instead.

Scopes requested (override with ``THREADS_SCOPES``): ``threads_basic``,
``threads_content_publish``, ``threads_read_replies``,
``threads_manage_replies``, ``threads_delete``.

Scopes are granted at authorization time, so a missing one means redoing this
entire flow. ``threads_read_replies`` is required for GET calls to the reply
endpoints (the ``get_replies`` tool 403s without it). ``threads_manage_replies``
is requested because the docs are ambiguous about where reply-publishing sits
and the hide-reply endpoint definitely needs it; it costs nothing on a self or
tester account. ``threads_delete`` gates the ``delete_post`` tool; the use-case dashboard
offers it but the authorize doc's scope list omits it, so if authorization is
rejected with an invalid-scope error this script prints the exact
``THREADS_SCOPES`` fallback to re-run with. ``threads_manage_insights`` is
deliberately omitted — insights are deferred past MVP.

Three different hosts are involved, which is not a typo:

* authorize:            ``https://threads.net/oauth/authorize``
* short-lived exchange: ``POST https://graph.threads.net/oauth/access_token``
* long-lived exchange:  ``GET  https://graph.threads.com/access_token``
* refresh (server):     ``GET  https://graph.threads.com/refresh_access_token``

**The seed is consumed once.** The server writes it to its data volume on
first boot and refreshes replace it there. Do not expect the value in ``.env``
to stay valid — after the first refresh it is dead, and that is by design.
The app secret must never leave a server-side context.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime, timedelta

PORT = 8766
# Override if the App Dashboard rewrote the URI (e.g. added a trailing slash).
REDIRECT_URI = os.getenv("THREADS_REDIRECT_URI", f"http://127.0.0.1:{PORT}/callback")
# The use-case dashboard exposes ten permissions; the authorize doc's "Values"
# column lists only five and omits threads_delete entirely, which looks stale.
# threads_delete is the gate on our delete_post tool, so we ask for it — but
# defensively, because the authorize call may reject an unlisted scope value.
DEFAULT_SCOPES = (
    "threads_basic,threads_content_publish,"
    "threads_read_replies,threads_manage_replies,threads_delete"
)
# Fallback if the authorize window rejects the request with an invalid-scope
# error. Costs Pete an env var, not a code edit.
FALLBACK_SCOPES = (
    "threads_basic,threads_content_publish,"
    "threads_read_replies,threads_manage_replies"
)
SCOPES = os.getenv("THREADS_SCOPES", DEFAULT_SCOPES)

AUTH_URL = "https://threads.net/oauth/authorize"

# The short-lived code exchange is documented on graph.threads.NET and is a
# POST; the long-lived exchange and the refresh are on graph.threads.COM and
# are GETs. Separate overrides because they are genuinely different hosts and
# Meta is mid-migration between the two domains.
OAUTH_BASE = os.getenv("THREADS_OAUTH_BASE", "https://graph.threads.net")
AUTH_BASE = os.getenv("THREADS_AUTH_BASE", "https://graph.threads.com")
SHORT_TOKEN_URL = f"{OAUTH_BASE}/oauth/access_token"
LONG_TOKEN_URL = f"{AUTH_BASE}/access_token"

captured: dict = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence the default access log
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        # Accept both spellings: the App Dashboard may append a trailing slash
        # to the registered URI, and the redirect then arrives with one.
        if parsed.path.rstrip("/") != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        captured["code"] = qs.get("code", [None])[0]
        captured["state"] = qs.get("state", [None])[0]
        captured["error"] = qs.get("error", [None])[0]
        captured["error_description"] = qs.get("error_description", [None])[0]
        captured["error_reason"] = qs.get("error_reason", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if captured["error"]:
            body = f"<h1>Authorization failed</h1><p>{captured['error']}</p>"
        else:
            body = (
                "<h1>Authorization received</h1>"
                "<p>You can close this tab and return to the terminal.</p>"
            )
        self.wfile.write(body.encode())


def is_scope_error(error: str | None, description: str | None) -> bool:
    """True if an authorize rejection looks like an invalid/unsupported scope."""
    blob = f"{error or ''} {description or ''}".lower()
    return "scope" in blob or "permission" in blob


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get_json(url: str, params: dict) -> dict:
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def exchange_code_for_short_token(app_id: str, app_secret: str, code: str) -> dict:
    """POST /oauth/access_token -> short-lived token (1 hour)."""
    return _post_form(
        SHORT_TOKEN_URL,
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )


def exchange_for_long_token(app_secret: str, short_token: str) -> dict:
    """GET /access_token?grant_type=th_exchange_token -> long-lived token (60 days)."""
    return _get_json(
        LONG_TOKEN_URL,
        {
            "grant_type": "th_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
    )


def main() -> None:
    app_id = os.getenv("THREADS_APP_ID")
    app_secret = os.getenv("THREADS_APP_SECRET")
    if not app_id or not app_secret:
        print(
            "ERROR: set THREADS_APP_ID and THREADS_APP_SECRET in env.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = secrets.token_urlsafe(16)
    auth_url = f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "response_type": "code",
            "state": state,
        }
    )

    server = http.server.HTTPServer(("127.0.0.1", PORT), CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("Opening browser to the Threads authorization page...")
    print(f"If it does not open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)
    print(f"Waiting for redirect to {REDIRECT_URI} ...")

    while "code" not in captured and "error" not in captured:
        pass
    server.shutdown()

    if captured.get("error"):
        detail = captured.get("error_description") or captured.get("error_reason") or ""
        print(f"Authorization failed: {captured['error']} {detail}".rstrip(), file=sys.stderr)
        if is_scope_error(captured["error"], detail) and "threads_delete" in SCOPES:
            print(
                "\nThis looks like an invalid-scope rejection, and the request\n"
                "included `threads_delete` — a permission the use-case dashboard\n"
                "offers but the authorize documentation does not list.\n\n"
                "Re-run without it:\n\n"
                f'    export THREADS_SCOPES="{FALLBACK_SCOPES}"\n'
                "    python bootstrap.py\n\n"
                "If that succeeds, `delete_post` will 403 at runtime. Either drop\n"
                "it from the tool surface or document it as expected-to-fail —\n"
                "do not ship a tool that silently does nothing.",
                file=sys.stderr,
            )
        sys.exit(1)
    if captured.get("state") != state:
        print("ERROR: state mismatch; possible CSRF. Aborting.", file=sys.stderr)
        sys.exit(1)

    print("Got authorization code. Exchanging for a short-lived token...")
    short = exchange_code_for_short_token(app_id, app_secret, captured["code"])
    short_token = short.get("access_token")
    if not short_token:
        print("ERROR: no access_token in the short-lived response.", file=sys.stderr)
        print(json.dumps({k: v for k, v in short.items() if k != "access_token"}, indent=2), file=sys.stderr)
        sys.exit(1)

    print("Exchanging the short-lived token for a long-lived one...")
    long_resp = exchange_for_long_token(app_secret, short_token)
    long_token = long_resp.get("access_token")
    if not long_token:
        print("ERROR: no access_token in the long-lived response.", file=sys.stderr)
        print(json.dumps({k: v for k, v in long_resp.items() if k != "access_token"}, indent=2), file=sys.stderr)
        sys.exit(1)

    expires_in = int(long_resp.get("expires_in", 60 * 24 * 3600))
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

    print("\n" + "=" * 68)
    print("SUCCESS. Seed this into .env on nix1 as THREADS_SEED_TOKEN:")
    print("=" * 68)
    print(long_token)
    print("=" * 68)
    print(f"Valid for {expires_in / 86400:.1f} days (until {expires_at.isoformat()}).")
    print(
        "\nThe server consumes this ONCE, writes it to its data volume, and\n"
        "refreshes it there at day 45. The value above goes stale after the\n"
        "first refresh — that is expected. Never commit it."
    )


if __name__ == "__main__":
    main()
