"""Every tool declares what it does, and the public writes say so.

Nothing in an MCP manifest distinguishes delete_post from whoami unless the
tool says so, so a client has no basis on which to prompt before a call. That
gap is sharper here than on most servers: publish_post and post_chain write to
a PUBLIC account under Pete's name, and delete_post removes something already
published and possibly already read.

These tests drive the live manifest, so they assert what a client would
actually receive rather than what the source appears to say.
"""

from __future__ import annotations

import pytest

from tests.test_tools import build

#: Writes that reach a public timeline. A read-only hint on any of these is
#: worse than no hint at all: it tells a client not to bother asking.
PUBLIC_WRITES = {"create_post", "publish_post", "post_chain"}

#: Removes something already published.
DESTRUCTIVE = {"delete_post"}


@pytest.fixture
async def tools(tmp_path):
    mcp, _ = build(tmp_path)
    return {tool.name: tool for tool in await mcp.list_tools()}


async def test_every_tool_is_annotated(tools):
    assert [name for name, t in tools.items() if t.annotations is None] == []


async def test_deletion_is_marked_destructive(tools):
    for name in DESTRUCTIVE:
        assert tools[name].annotations.destructiveHint is True, name


async def test_public_writes_are_never_marked_read_only(tools):
    mislabelled = [n for n in PUBLIC_WRITES if tools[n].annotations.readOnlyHint]
    assert mislabelled == []


async def test_publishing_is_not_idempotent(tools):
    """A retried publish is a duplicate post on a public timeline, not a no-op.

    This is the annotation that actually changes client behaviour here: an
    idempotent hint invites a retry, and the cost of retrying this one is
    visible to everyone who follows the account.
    """
    for name in PUBLIC_WRITES:
        assert tools[name].annotations.idempotentHint is False, name


async def test_preview_chain_is_the_one_closed_world_tool(tools):
    """It segments text locally and makes no network call, and says so."""
    ann = tools["preview_chain"].annotations
    assert ann.readOnlyHint is True
    assert ann.openWorldHint is False


async def test_every_other_tool_declares_an_open_world(tools):
    closed = [
        name for name, t in tools.items()
        if name != "preview_chain" and t.annotations.openWorldHint is not True
    ]
    assert closed == []


async def test_no_tool_is_both_read_only_and_destructive(tools):
    contradictory = [
        name for name, t in tools.items()
        if t.annotations.readOnlyHint and t.annotations.destructiveHint
    ]
    assert contradictory == []
