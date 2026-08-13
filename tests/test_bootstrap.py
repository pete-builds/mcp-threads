"""Bootstrap OAuth constants and callback handling.

These pin corrections that are otherwise invisible until Pete has already
burned an authorization round trip. Scopes are granted at authorization time,
so a missing scope means redoing the whole browser flow.
"""

from __future__ import annotations

import urllib.parse

import bootstrap


def test_scopes_include_read_replies():
    """get_replies GETs the reply endpoints and 403s without this scope."""
    scopes = set(bootstrap.SCOPES.split(","))
    assert "threads_read_replies" in scopes


def test_scopes_cover_the_whole_mvp_tool_surface():
    scopes = set(bootstrap.DEFAULT_SCOPES.split(","))
    assert scopes == {
        "threads_basic",
        "threads_content_publish",
        "threads_read_replies",
        "threads_manage_replies",
        "threads_delete",
    }


def test_delete_scope_is_requested_for_the_delete_post_tool():
    """The use-case dashboard offers threads_delete even though the authorize
    doc's Values list omits it. delete_post is gated on it."""
    assert "threads_delete" in bootstrap.DEFAULT_SCOPES


def test_fallback_scopes_drop_only_the_undocumented_one():
    default = set(bootstrap.DEFAULT_SCOPES.split(","))
    fallback = set(bootstrap.FALLBACK_SCOPES.split(","))
    assert default - fallback == {"threads_delete"}


def test_scopes_are_overridable_by_env(monkeypatch):
    import importlib

    monkeypatch.setenv("THREADS_SCOPES", "threads_basic")
    reloaded = importlib.reload(bootstrap)
    try:
        assert reloaded.SCOPES == "threads_basic"
    finally:
        monkeypatch.delenv("THREADS_SCOPES", raising=False)
        importlib.reload(bootstrap)


def test_invalid_scope_rejection_is_detected():
    assert bootstrap.is_scope_error("invalid_scope", "") is True
    assert bootstrap.is_scope_error("invalid_request", "Invalid scope: threads_delete") is True
    assert bootstrap.is_scope_error("access_denied", "user cancelled") is False


def test_insights_scope_is_not_requested():
    """Insights are deferred past MVP; do not ask for what we do not use."""
    assert "threads_manage_insights" not in bootstrap.DEFAULT_SCOPES


def test_short_and_long_token_exchanges_use_different_hosts():
    """.net serves the short-lived code exchange, .com the long-lived one."""
    short_host = urllib.parse.urlparse(bootstrap.SHORT_TOKEN_URL).netloc
    long_host = urllib.parse.urlparse(bootstrap.LONG_TOKEN_URL).netloc
    assert short_host == "graph.threads.net"
    assert long_host == "graph.threads.com"
    assert short_host != long_host


def test_short_token_url_path_is_the_oauth_endpoint():
    assert bootstrap.SHORT_TOKEN_URL.endswith("/oauth/access_token")
    assert bootstrap.LONG_TOKEN_URL.endswith("/access_token")
    assert not bootstrap.LONG_TOKEN_URL.endswith("/oauth/access_token")


def test_short_token_exchange_posts_the_required_form_fields(monkeypatch):
    captured: dict = {}

    def fake_post_form(url, fields):
        captured["url"] = url
        captured["fields"] = fields
        return {"access_token": "short-token"}

    monkeypatch.setattr(bootstrap, "_post_form", fake_post_form)
    out = bootstrap.exchange_code_for_short_token("app", "secret", "the-code")

    assert out["access_token"] == "short-token"
    assert captured["url"] == bootstrap.SHORT_TOKEN_URL
    assert captured["fields"] == {
        "client_id": "app",
        "client_secret": "secret",
        "grant_type": "authorization_code",
        "redirect_uri": bootstrap.REDIRECT_URI,
        "code": "the-code",
    }


def test_long_token_exchange_uses_the_th_exchange_grant(monkeypatch):
    captured: dict = {}

    def fake_get_json(url, params):
        captured["url"] = url
        captured["params"] = params
        return {"access_token": "long-token", "expires_in": 5183944}

    monkeypatch.setattr(bootstrap, "_get_json", fake_get_json)
    out = bootstrap.exchange_for_long_token("secret", "short-token")

    assert out["access_token"] == "long-token"
    assert captured["url"] == bootstrap.LONG_TOKEN_URL
    assert captured["params"]["grant_type"] == "th_exchange_token"
    assert captured["params"]["client_secret"] == "secret"
    assert captured["params"]["access_token"] == "short-token"


def test_callback_handler_accepts_both_slash_spellings():
    """The App Dashboard may append a trailing slash to the registered URI."""
    for path in ("/callback", "/callback/"):
        assert urllib.parse.urlparse(path).path.rstrip("/") == "/callback"
    # And still rejects anything else.
    assert urllib.parse.urlparse("/other").path.rstrip("/") != "/callback"


def test_redirect_uri_is_overridable_for_a_rewritten_dashboard_value(monkeypatch):
    """Reload the module with the env var set and confirm it takes effect."""
    import importlib

    monkeypatch.setenv("THREADS_REDIRECT_URI", "https://example.test/cb/")
    reloaded = importlib.reload(bootstrap)
    try:
        assert reloaded.REDIRECT_URI == "https://example.test/cb/"
    finally:
        monkeypatch.delenv("THREADS_REDIRECT_URI", raising=False)
        importlib.reload(bootstrap)
