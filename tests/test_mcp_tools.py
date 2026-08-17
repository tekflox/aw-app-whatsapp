"""MCP surface tests — mostly about account resolution.

That's where the risk is. The tools themselves are thin wrappers over the
connector client; what can actually hurt someone is a send going out from the
wrong number, so every branch of "which account did this resolve to" is pinned
here, including the ones that must REFUSE to resolve.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whatsapp_app import mcp_tools  # noqa: E402
from whatsapp_app.connector_client import ConnectorError  # noqa: E402


def acct(aid, label, connected=True, has_qr=False, enabled=True):
    return {"id": aid, "label": label, "connected": connected, "has_qr": has_qr,
            "phone": "5521999999999" if connected else None, "enabled": enabled}


class FakeClient:
    def __init__(self, accounts):
        self.accounts = accounts
        self.sent = []
        self.raise_with = None

    async def list_accounts(self):
        return self.accounts

    async def send_text(self, account_id, jid, text):
        if self.raise_with:
            raise self.raise_with
        self.sent.append((account_id, jid, text))
        return {"ok": True}

    async def chats(self, account_id):
        return [{"jid": "x@s.whatsapp.net", "name": "Maria", "lastMessageAt": 1,
                 "lastMessageText": "oi"}]

    async def messages(self, account_id, jid, limit=50):
        return [{"id": "m1", "fromMe": False, "timestamp": 1, "text": "oi", "mediaType": None}]


class FakeService:
    def __init__(self, accounts, provisioning=False, running=True):
        self.client = FakeClient(accounts)
        self.provisioning = provisioning
        self.running = running

    async def snapshot(self):
        return {"connector_running": self.running, "connector_pid": 1,
                "provisioning": self.provisioning, "setup_error": None,
                "port": 9310, "accounts": self.client.accounts}


def call(service, name, **args):
    return asyncio.run(mcp_tools._call(service, name, args))


def text_of(result):
    return result["content"][0]["text"]


# ── the tool list itself ────────────────────────────────────────────────────
def test_manifest_and_tool_list_agree():
    import json
    manifest = json.loads((Path(__file__).resolve().parents[1] / "aw-app.json").read_text())
    declared = set(manifest["contributes"]["mcp"]["provides"])
    actual = {t["name"] for t in mcp_tools.TOOLS}
    # contributes.mcp.provides is marketplace copy, so nothing enforces it at
    # runtime — an app can advertise seven tools and serve none. Pin it.
    assert declared == actual


def test_send_tools_require_account_in_their_schema():
    for name in ("whatsapp_send_message", "whatsapp_send_media"):
        tool = next(t for t in mcp_tools.TOOLS if t["name"] == name)
        assert "account" in tool["inputSchema"]["required"], name
    for name in ("whatsapp_list_conversations", "whatsapp_read_messages",
                 "whatsapp_download_media"):
        tool = next(t for t in mcp_tools.TOOLS if t["name"] == name)
        assert "account" not in tool["inputSchema"].get("required", []), name


# ── reads: default to the single connected account ──────────────────────────
def test_read_resolves_the_only_connected_account():
    s = FakeService([acct("a1", "Personal")])
    res = call(s, "whatsapp_list_conversations")
    assert res.get("isError") is not True
    assert "Maria" in text_of(res)


def test_read_refuses_to_guess_between_two_connected_accounts():
    s = FakeService([acct("a1", "Personal"), acct("a2", "Store line")])
    res = call(s, "whatsapp_list_conversations")
    assert res["isError"] is True
    assert "Personal" in text_of(res) and "Store line" in text_of(res)


def test_read_ignores_a_disconnected_account_when_defaulting():
    s = FakeService([acct("a1", "Personal"), acct("a2", "Old", connected=False)])
    res = call(s, "whatsapp_list_conversations")
    assert res.get("isError") is not True


# ── sends: never default ────────────────────────────────────────────────────
def test_send_requires_account_even_with_one_account():
    s = FakeService([acct("a1", "Personal")])
    res = call(s, "whatsapp_send_message", jid="x@s.whatsapp.net", text="oi")
    assert res["isError"] is True
    assert "required" in text_of(res)
    assert s.client.sent == []


def test_send_by_label_is_case_insensitive():
    s = FakeService([acct("a1", "Personal"), acct("a2", "Store line")])
    res = call(s, "whatsapp_send_message", account="store LINE",
               jid="x@s.whatsapp.net", text="oi")
    assert res.get("isError") is not True
    assert s.client.sent == [("a2", "x@s.whatsapp.net", "oi")]


def test_send_by_id_wins_over_label():
    s = FakeService([acct("a1", "a2"), acct("a2", "Store line")])
    # An account labelled with another account's id is pathological but cheap to
    # get right: id matches first, so "a2" addresses the account WHOSE ID it is.
    call(s, "whatsapp_send_message", account="a2", jid="x@s.whatsapp.net", text="oi")
    assert s.client.sent == [("a2", "x@s.whatsapp.net", "oi")]


def test_duplicate_labels_refuse_rather_than_pick_one():
    s = FakeService([acct("a1", "Work"), acct("a2", "Work")])
    res = call(s, "whatsapp_send_message", account="Work",
               jid="x@s.whatsapp.net", text="oi")
    assert res["isError"] is True
    assert "use the id" in text_of(res)
    assert s.client.sent == []


def test_unknown_account_lists_the_real_ones():
    s = FakeService([acct("a1", "Personal")])
    res = call(s, "whatsapp_send_message", account="Nope",
               jid="x@s.whatsapp.net", text="oi")
    assert res["isError"] is True and "Personal" in text_of(res)


def test_sending_from_an_unpaired_account_points_at_the_user():
    s = FakeService([acct("a1", "Personal", connected=False, has_qr=True)])
    res = call(s, "whatsapp_send_message", account="Personal",
               jid="x@s.whatsapp.net", text="oi")
    assert res["isError"] is True
    # The agent must not offer to retry — only a human with the phone can fix it.
    assert "only the user" in text_of(res)


def test_duplicate_message_tells_the_agent_not_to_retry():
    s = FakeService([acct("a1", "Personal")])
    s.client.raise_with = ConnectorError(409, "identical to the last message sent.")
    res = call(s, "whatsapp_send_message", account="a1",
               jid="x@s.whatsapp.net", text="oi")
    assert res["isError"] is True
    assert "do not retry" in text_of(res).lower()


# ── status / accounts ───────────────────────────────────────────────────────
def test_no_accounts_explains_that_only_the_user_can_link_one():
    s = FakeService([])
    res = call(s, "whatsapp_send_message", account="whatever",
               jid="x@s.whatsapp.net", text="oi")
    assert res["isError"] is True and "scan the QR" in text_of(res)


def test_provisioning_is_not_reported_as_a_missing_account():
    s = FakeService([], provisioning=True)
    assert "installing" in text_of(call(s, "whatsapp_list_accounts")).lower()


def test_status_names_each_account_state():
    s = FakeService([acct("a1", "Personal"),
                     acct("a2", "Store line", connected=False, has_qr=True),
                     acct("a3", "Old", connected=False, enabled=False)])
    out = text_of(call(s, "whatsapp_status"))
    assert "1/3" in out
    assert "waiting for a QR scan" in out and "paused" in out


# ── protocol ────────────────────────────────────────────────────────────────
def test_tools_list_over_jsonrpc():
    s = FakeService([acct("a1", "Personal")])
    res = asyncio.run(mcp_tools.handle_request(s, {"jsonrpc": "2.0", "id": 1,
                                                   "method": "tools/list"}))
    assert {t["name"] for t in res["result"]["tools"]} == {t["name"] for t in mcp_tools.TOOLS}


def test_initialized_notification_gets_no_reply():
    s = FakeService([])
    assert asyncio.run(mcp_tools.handle_request(
        s, {"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


def test_unknown_tool_is_an_error_not_an_exception():
    s = FakeService([acct("a1", "Personal")])
    res = call(s, "whatsapp_nope")
    assert res["isError"] is True


@pytest.mark.parametrize("name", [t["name"] for t in mcp_tools.TOOLS])
def test_every_tool_is_dispatchable(name):
    s = FakeService([acct("a1", "Personal")])
    # A tool in TOOLS with no handler answers "Unknown tool" — advertised and
    # dead, the exact gap this release exists to close.
    assert "Unknown tool" not in text_of(call(s, name))
