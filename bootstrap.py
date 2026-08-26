"""One-time Threads OAuth bootstrap. Run on Pete's Mac, never in the container.

What it does:

  1. Opens the browser to Threads' authorization page.
  2. Catches the redirect at http://127.0.0.1:8766/callback, or — when that
     listener cannot fire — takes the redirect URL pasted on stdin.
  3. Exchanges the authorization code for a **short-lived** token (1 hour).
  4. Exchanges that for a **long-lived** token (60 days).
  5. Prints the long-lived token to seed THREADS_SEED_TOKEN once.

Usage::

    export THREADS_APP_ID=...        # Meta App Dashboard
    export THREADS_APP_SECRET=...    # App settings > Basic > Threads App secret
    python bootstrap.py

This script does **not** load ``.env``. It reads ``os.getenv`` directly, so
``source .env`` (or export the three vars) before running it, or it exits at
the first check with "set THREADS_APP_ID and THREADS_APP_SECRET in env".

Prerequisite: the Threads app must have this exact redirect URI registered::

    http://127.0.0.1:8766/callback

**Copy the redirect URI verbatim out of the App Dashboard after saving it.**
The dashboard may rewrite what you typed — notably by appending a trailing
slash — and the value must match EXACTLY at both the authorize step and the
code-exchange step or Meta rejects the exchange. This script's callback
handler accepts ``/callback`` and ``/callback/`` so either registration works;
if the dashboard shows a trailing slash, set ``THREADS_REDIRECT_URI`` to the
dashboard's exact string.

**RESOLVED 2026-08-16: Meta rejects a plain-http loopback redirect URI.**
Every redirect-URI example in Meta's docs uses HTTPS, and the authorize step
fails with error ``1349187`` ("Insecure Login Blocked") for
``http://127.0.0.1:8766/callback``. The working arrangement is an HTTPS URL on
a host Pete controls — ``https://brooksnewmedia.com/threads-callback`` — which
is registered in the dashboard and returns a plain 404 with **no redirect**,
so the browser's address bar keeps ``?code=...&state=...`` intact. There is no
callback route on that web server and there must not be one; the URL exists
only to be a legal HTTPS landing spot whose query string can be copied.

That means the local ``http.server`` listener can never fire in the normal
case, so this script has a **manual paste fallback**::

    export THREADS_REDIRECT_URI="https://brooksnewmedia.com/threads-callback"
    python bootstrap.py
    # authorize in the browser, land on the 404, copy the whole URL from the
    # address bar, paste it at the prompt.

The loopback listener remains the default path and is still tried first
whenever ``THREADS_REDIRECT_URI`` is a loopback address, so nothing regresses
if Meta ever allows one again. The pasted URL's ``state`` is compared against
the value this run generated exactly as the listener path compares it; a
mismatch aborts non-zero. Pasting a bare code instead of the full URL is
accepted but says loudly that ``state`` could not be verified.

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

**RESOLVED 2026-08-16: ``threads_delete`` is NOT grantable.** Meta does not
reject the authorize call over it; it silently drops the scope, and the
resulting token's granted list comes back without it. The defensive fallback
above stays in place because a silent drop is exactly the failure it makes
legible, but do not expect ``delete_post`` to work: it is expected-to-403.

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
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime, timedelta

PORT = 8766
# Override if the App Dashboard rewrote the URI (e.g. added a trailing slash),
# or — the normal case since 2026-08-16 — because Meta refuses plain-http
# loopback and the registered URI is https://brooksnewmedia.com/threads-callback.
REDIRECT_URI = os.getenv("THREADS_REDIRECT_URI", f"http://127.0.0.1:{PORT}/callback")
# How long to sit on the local listener before falling back to a manual paste.
# Only consulted when REDIRECT_URI is a loopback address.
CALLBACK_TIMEOUT = float(os.getenv("THREADS_CALLBACK_TIMEOUT", "180"))
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


SCOPE_FALLBACK_HINT = (
    "\nThis looks like an invalid-scope rejection, and the request\n"
    "included `threads_delete` — a permission the use-case dashboard\n"
    "offers but the authorize documentation does not list.\n\n"
    "Re-run without it:\n\n"
    f'    export THREADS_SCOPES="{FALLBACK_SCOPES}"\n'
    "    python bootstrap.py\n\n"
    "If that succeeds, `delete_post` will 403 at runtime. Either drop\n"
    "it from the tool surface or document it as expected-to-fail —\n"
    "do not ship a tool that silently does nothing."
)

_RESULT_KEYS = ("code", "state", "error", "error_description", "error_reason")


def is_loopback_redirect(uri: str) -> bool:
    """True if the redirect URI points back at this machine.

    Only a loopback URI can ever reach the local ``http.server`` listener. A
    hosted HTTPS callback (the arrangement Meta actually accepts) lands the
    browser on a remote web server instead, so the listener would wait forever.
    """
    try:
        host = urllib.parse.urlparse(uri).hostname
    except ValueError:
        return False
    return (host or "").lower() in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def parse_pasted_redirect(text: str) -> dict:
    """Parse a pasted redirect URL (or bare code) into the ``captured`` shape.

    Accepts three spellings, because Pete is copying out of an address bar:

    * a full URL — ``https://host/threads-callback?code=...&state=...``
    * a bare query string — ``code=...&state=...``
    * a bare authorization code with no ``=`` in it at all

    The bare-code form sets ``bare_code`` so the caller can say out loud that
    the CSRF ``state`` could not be checked. Meta appends a ``#_`` fragment to
    the redirect; it is stripped rather than treated as part of the code.
    """
    out: dict = dict.fromkeys(_RESULT_KEYS)
    out["bare_code"] = False

    cleaned = (text or "").strip().strip("'\"").strip()
    if not cleaned:
        return out

    parsed = urllib.parse.urlparse(cleaned)
    query = parsed.query
    if not query:
        head = cleaned.split("#", 1)[0].lstrip("?")
        if "=" not in head:
            # Bare authorization code. No state to compare against.
            out["code"] = head or None
            out["bare_code"] = out["code"] is not None
            return out
        query = head

    qs = urllib.parse.parse_qs(query)
    for key in _RESULT_KEYS:
        out[key] = qs.get(key, [None])[0]
    return out


def wait_for_callback(timeout: float, sink: dict | None = None) -> bool:
    """Block until the local listener captures a callback, or the timeout hits.

    Returns True if something was captured. False means fall back to a paste.
    """
    sink = captured if sink is None else sink
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "code" in sink or "error" in sink:
            return True
        time.sleep(0.1)
    return False


def prompt_for_redirect_url(read_line=input) -> dict:
    """Ask for the redirect URL the browser landed on and parse it."""
    print(
        "\nPaste the FULL URL from the browser's address bar after authorizing.\n"
        "It looks like:\n"
        f"  {REDIRECT_URI}?code=AQ...&state=...\n"
        "The page itself will 404 — that is expected and fine; the query string\n"
        "in the address bar is the whole point. A bare code also works, but then\n"
        "the CSRF `state` cannot be verified.\n"
    )
    try:
        raw = read_line("Redirect URL (or code): ")
    except (EOFError, KeyboardInterrupt):
        print("\nERROR: no input; aborting.", file=sys.stderr)
        sys.exit(1)
    return parse_pasted_redirect(raw)


def resolve_authorization(result: dict, expected_state: str) -> str:
    """Validate an authorization result and return the code, or exit non-zero.

    Applies identically to a listener capture and to a pasted URL: an
    ``error`` param aborts with the scope hint where relevant, and ``state``
    must match the value this run generated. The only relaxation is the
    bare-code paste, which carries no state and says so.
    """
    if result.get("error"):
        detail = result.get("error_description") or result.get("error_reason") or ""
        print(f"Authorization failed: {result['error']} {detail}".rstrip(), file=sys.stderr)
        if is_scope_error(result["error"], detail) and "threads_delete" in SCOPES:
            print(SCOPE_FALLBACK_HINT, file=sys.stderr)
        sys.exit(1)

    code = result.get("code")
    if not code:
        print(
            "ERROR: no authorization code found in that input. Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)

    if result.get("bare_code"):
        print(
            "WARNING: that was a bare code, not the full redirect URL, so the\n"
            "CSRF `state` value could NOT be verified. Continuing. If you did not\n"
            "just initiate this authorization yourself, stop and re-run.",
            file=sys.stderr,
        )
    elif result.get("state") != expected_state:
        print("ERROR: state mismatch; possible CSRF. Aborting.", file=sys.stderr)
        sys.exit(1)

    return code


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

    # The listener is still the default and is tried first, but it can only
    # ever fire for a loopback redirect URI. With a hosted HTTPS callback the
    # browser lands on a remote web server and nothing arrives here.
    loopback = is_loopback_redirect(REDIRECT_URI)
    server = None
    if loopback:
        server = http.server.HTTPServer(("127.0.0.1", PORT), CallbackHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    print("Opening browser to the Threads authorization page...")
    print(f"If it does not open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    result: dict | None = None
    if loopback:
        print(
            f"Waiting up to {int(CALLBACK_TIMEOUT)}s for a redirect to {REDIRECT_URI} ..."
        )
        got_it = wait_for_callback(CALLBACK_TIMEOUT)
        server.shutdown()
        if got_it:
            result = dict(captured)
        else:
            print("No callback arrived before the timeout.", file=sys.stderr)
    else:
        print(
            f"Redirect URI {REDIRECT_URI} is not a loopback address, so the local\n"
            "listener cannot catch it. Falling back to a manual paste."
        )

    if result is None:
        result = prompt_for_redirect_url()

    code = resolve_authorization(result, state)

    print("Got authorization code. Exchanging for a short-lived token...")
    short = exchange_code_for_short_token(app_id, app_secret, code)
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
