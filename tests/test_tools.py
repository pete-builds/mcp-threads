"""Tool-layer contract: the Standard Error Contract envelope on every path."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP

from clients.threads import ThreadsAPIError
from tests.test_client import (
    GRAPH,
    SEED,
    make_client,
    mock_publishing_limit,
    seed_volume,
)
from tools.insights import register_insight_tools
from tools.publish import register_publish_tools
from tools.quota import register_quota_tools
from tools.read import register_read_tools
from tools.token import register_token_tools

EXPECTED_TOOLS = {
    "token_status",
    "whoami",
    "list_posts",
    "get_replies",
    "get_publishing_limit",
    "get_post_insights",
    "get_account_insights",
    "create_post",
    "get_container_status",
    "publish_post",
    "preview_chain",
    "post_chain",
    "delete_post",
}

#: The design constraint is a small, sharp surface. Competing Threads MCP
#: servers ship 16 to 26 tools and are worse to drive for it.
MAX_TOOLS = 14


def build(tmp_path, **kw):
    client = make_client(tmp_path, seed=None, **kw)
    mcp = FastMCP("Threads-test")
    register_token_tools(mcp, client)
    register_read_tools(mcp, client)
    register_quota_tools(mcp, client)
    register_insight_tools(mcp, client)
    register_publish_tools(mcp, client)
    return mcp, client


async def call(mcp, name, **kwargs) -> dict:
    tool = await mcp.get_tool(name)
    return json.loads(await tool.fn(**kwargs))


# --- registration ------------------------------------------------------


async def test_all_tools_register_and_are_listable_over_the_transport(tmp_path):
    mcp, client = build(tmp_path)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == EXPECTED_TOOLS
    await client.close()


async def test_the_tool_surface_stays_small(tmp_path):
    mcp, client = build(tmp_path)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert len(names) <= MAX_TOOLS, sorted(names)
    await client.close()


async def test_every_tool_has_a_docstring(tmp_path):
    mcp, client = build(tmp_path)
    for name in EXPECTED_TOOLS:
        tool = await mcp.get_tool(name)
        assert tool.description and len(tool.description) > 40, name
    await client.close()


# --- success envelope --------------------------------------------------


async def test_token_status_returns_the_data_envelope(tmp_path):
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "token_status")
    assert "data" in out and "error" not in out
    assert out["data"]["valid"] is True
    assert "access_token" not in json.dumps(out)
    await client.close()


@respx.mock
async def test_whoami_returns_the_data_envelope(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "pete_builds"})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "whoami")
    assert out["data"]["username"] == "pete_builds"
    await client.close()


@respx.mock
async def test_list_posts_returns_shaped_posts(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    respx.get(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "m1",
                        "text": "hello",
                        "media_type": "TEXT",
                        "permalink": "https://threads.net/p/m1",
                        "timestamp": "2026-08-12T10:00:00+0000",
                    }
                ]
            },
        )
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "list_posts", limit=5)
    assert out["data"]["count"] == 1
    assert out["data"]["posts"][0]["id"] == "m1"
    await client.close()


async def test_preview_chain_is_offline_and_shows_segments(tmp_path):
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    text = "\n\n".join(("word " * 90).strip() for _ in range(3))
    out = await call(mcp, "preview_chain", text=text)
    assert out["data"]["segments"] == 3
    assert len(out["data"]["preview"]) == 3
    assert all(p["length"] <= 500 for p in out["data"]["preview"])
    await client.close()


# --- failure envelope --------------------------------------------------


async def test_delete_post_refuses_without_explicit_confirmation(tmp_path):
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "delete_post", media_id="m1")
    assert out["code"] == "INVALID_INPUT"
    assert "confirm=True" in out["error"]
    assert "data" not in out
    await client.close()


@respx.mock
async def test_delete_post_fails_honestly_on_the_ungranted_scope(tmp_path):
    """The live token has no threads_delete. That must read as a permission gap."""
    route = respx.delete(f"{GRAPH}/m1").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)  # default granted scopes: no threads_delete
    out = await call(mcp, "delete_post", media_id="m1", confirm=True)

    assert "data" not in out
    assert out["code"] == "AUTH_FAILED"
    assert out["details"]["missing_scope"] == "threads_delete"
    assert "threads_delete" not in out["details"]["granted_scopes"]
    assert out["details"]["refresh_will_not_help"] is True
    # Actionable, and unmistakably not a code defect.
    assert "not a bug in mcp-threads" in out["error"]
    assert "Use cases" in out["error"]
    assert "bootstrap.py" in out["error"]
    # Failed BEFORE the call, so no quota and no API round trip were spent.
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_delete_post_works_once_the_scope_is_granted(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    mock_publishing_limit()
    respx.delete(f"{GRAPH}/m1").mock(return_value=httpx.Response(200, json={"success": True}))
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path, granted_scopes=("threads_basic", "threads_delete"))
    out = await call(mcp, "delete_post", media_id="m1", confirm=True)
    assert out["data"] == {"deleted": True, "media_id": "m1"}
    assert client.publish_log.used("deletes") == 1
    await client.close()


@respx.mock
async def test_an_upstream_403_on_delete_still_produces_the_scope_remedy(tmp_path):
    """Belt and braces: pre-flight disabled, Meta refuses, same guidance."""
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    mock_publishing_limit()
    respx.delete(f"{GRAPH}/m1").mock(
        return_value=httpx.Response(
            403, json={"error": {"message": "Insufficient permission", "code": 10}}
        )
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path, granted_scopes=None)  # pre-flight off
    out = await call(mcp, "delete_post", media_id="m1", confirm=True)
    assert out["code"] == "AUTH_FAILED"
    assert out["details"]["missing_scope"] == "threads_delete"
    assert client.publish_log.used("deletes") == 0
    await client.close()


async def test_missing_credential_surfaces_as_auth_failed_not_an_exception(tmp_path):
    mcp, client = build(tmp_path)  # empty volume, no seed
    out = await call(mcp, "whoami")
    assert out["code"] == "AUTH_FAILED"
    assert "bootstrap.py" in out["error"]
    await client.close()


@respx.mock
async def test_over_length_post_surfaces_as_invalid_input(tmp_path):
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "create_post", text="\U0001f600" * 200)
    assert out["code"] == "INVALID_INPUT"
    assert out["details"]["limit"] == 500
    await client.close()


@respx.mock
async def test_upstream_failure_surfaces_as_the_error_contract(tmp_path):
    respx.get(f"{GRAPH}/me").mock(return_value=httpx.Response(503, text="down"))
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "whoami")
    assert set(out) >= {"error", "code", "details"}
    assert out["code"] == "UPSTREAM_DOWN"
    assert out["details"]["status"] == 503
    await client.close()


async def test_an_unexpected_exception_still_returns_the_contract(tmp_path, monkeypatch):
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)

    async def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(client, "get_profile", boom)
    out = await call(mcp, "whoami")
    assert out["code"] == "INTERNAL"
    assert "kaboom" in out["error"]
    await client.close()


@respx.mock
async def test_post_chain_partial_failure_is_reported_not_swallowed(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    respx.post(f"{GRAPH}/77/threads").mock(
        side_effect=lambda req: httpx.Response(200, json={"id": "c1"})
    )
    publishes = {"n": 0}

    def publish_side_effect(request):
        publishes["n"] += 1
        if publishes["n"] == 2:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"id": f"media-{publishes['n']}"})

    respx.post(f"{GRAPH}/77/threads_publish").mock(side_effect=publish_side_effect)
    respx.get(url__regex=rf"{GRAPH}/media-\d+").mock(
        return_value=httpx.Response(200, json={"permalink": "https://threads.net/p/x"})
    )

    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    text = "\n\n".join(("word " * 90).strip() for _ in range(3))
    out = await call(mcp, "post_chain", text=text)

    assert "data" in out  # partial success is DATA, not a bare error
    assert out["data"]["complete"] is False
    assert out["data"]["failed_at"] == 2
    assert len(out["data"]["published"]) == 1
    assert out["data"]["resume_reply_to_id"] == "media-1"
    await client.close()


# --- sanity ------------------------------------------------------------


def test_threads_error_codes_are_from_the_fixed_enum():
    from clients.threads import (
        ThreadsAuthError,
        ThreadsInputError,
        ThreadsRateLimitError,
    )

    allowed = {
        "UPSTREAM_DOWN",
        "AUTH_FAILED",
        "INVALID_INPUT",
        "NOT_FOUND",
        "RATE_LIMITED",
        "INTERNAL",
    }
    for cls in (ThreadsAPIError, ThreadsAuthError, ThreadsInputError, ThreadsRateLimitError):
        assert cls.code in allowed, cls


def test_pytest_collects_from_the_package_root():
    """Guard: tests import `clients.*` / `tools.*`, so rootdir must be on sys.path."""
    import clients.threads  # noqa: F401
    import tools.common  # noqa: F401

    assert True


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tool_names_are_snake_case(name):
    assert name == name.lower()
    assert " " not in name
