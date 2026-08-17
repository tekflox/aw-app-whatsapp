"""MCP surface — the way an agent actually talks to a linked WhatsApp account.

Ports the monolith's ``src/mcp/whatsapp.py`` (a stdio server talking to a
single-account connector on a fixed loopback port) with two structural changes.

**In-process HTTP, not stdio.** aw-mcp-gateway runs a stdio child *inside its
own container*, where ``127.0.0.1:9310`` is the gateway's loopback and not this
workspace's — the connector would be unreachable, and the failure looks like an
upstream that connects and serves zero tools. Handling JSON-RPC here and
exposing it at ``POST /api/apps/whatsapp/mcp`` (registered by
``self_register``) puts the handler in the same process as everything it needs.
Same shape the ``architecture`` and ``kb`` apps use.

**Every tool is account-aware, and the two halves have different defaults.**
The monolith had one linked number, so ``whatsapp_send_message(jid, text)`` was
unambiguous. With several accounts, "which number does this go out from" is a
real question, and the answer is not symmetric:

* **read tools** default to the single connected account when there is exactly
  one. Guessing wrong shows the wrong chat list — recoverable, and demanding an
  id for the common one-account case is friction with no safety payoff.
* **send tools require ``account`` explicitly, always.** A message sent from the
  wrong number cannot be recalled, and a workspace with a personal number and a
  store number is precisely where an implicit default does damage. There is no
  "obvious" account to fall back on.

``account`` accepts an id or a label, because the human sentence behind the call
is "send it from the store line", not "send it from a3f9c1d2". An ambiguous
label is an error listing the candidates — never a guess.
"""
from __future__ import annotations

import json
import logging

from .connector_client import ConnectorDown, ConnectorError

log = logging.getLogger("aw_apps.whatsapp")

SERVER_NAME = "aw-whatsapp"
SERVER_VERSION = "1.0.0"

_STR = {"type": "string"}

_ACCOUNT_OPTIONAL = {
    "type": "string",
    "description": (
        "Which linked account to read from — its id or its label (e.g. 'Personal'). "
        "Optional: if exactly one account is connected it is used. With several "
        "connected, this is required."
    ),
}
_ACCOUNT_REQUIRED = {
    "type": "string",
    "description": (
        "Which linked account to send FROM — its id or its label (e.g. 'Store line'). "
        "Required, with no default: a message cannot be unsent, and picking the "
        "wrong number is not recoverable. Call whatsapp_list_accounts first."
    ),
}

TOOLS = [
    {
        "name": "whatsapp_list_accounts",
        "description": (
            "List every WhatsApp account linked to this workspace: id, label, whether "
            "it is connected, and its phone number. START HERE — every other tool "
            "addresses an account, and there is no default one."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "whatsapp_status",
        "description": (
            "Whether WhatsApp is usable right now, and if not, why: the connector "
            "still installing, the connector down, or an account waiting for its "
            "pairing QR to be scanned. Only the user can scan a QR."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "whatsapp_list_conversations",
        "description": (
            "Recent chats on one account (jid, contact name, last message preview), "
            "most recent first. This is a rolling in-memory index of what the "
            "connector has seen since it last started — not the phone's full history. "
            "Use the jid from here rather than building one from a phone number."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"account": _ACCOUNT_OPTIONAL},
        },
    },
    {
        "name": "whatsapp_read_messages",
        "description": (
            "Recent messages in one chat. Each has id, fromMe, timestamp, text and "
            "mediaType (null when text-only) — pass the id to whatsapp_download_media "
            "for a media message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": _ACCOUNT_OPTIONAL,
                "jid": {"type": "string", "description": "Chat JID, e.g. 5521999999999@s.whatsapp.net"},
                "limit": {"type": "integer", "description": "Max messages to return (default 50, max 200)"},
            },
            "required": ["jid"],
        },
    },
    {
        "name": "whatsapp_send_message",
        "description": (
            "Send a text message from one of the linked accounts. Sends are throttled "
            "to one per 15 seconds per account and an exact repeat of the previous "
            "message is REFUSED — both guard against the spam pattern that gets a "
            "number banned by WhatsApp. A send cannot be undone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": _ACCOUNT_REQUIRED,
                "jid": {"type": "string", "description": "Recipient JID, e.g. 5521999999999@s.whatsapp.net"},
                "text": _STR,
            },
            "required": ["account", "jid", "text"],
        },
    },
    {
        "name": "whatsapp_send_media",
        "description": (
            "Send an image, video, audio or document from a local file path. The kind "
            "is inferred from the extension. Same throttle and same no-undo as "
            "whatsapp_send_message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": _ACCOUNT_REQUIRED,
                "jid": _STR,
                "file_path": {"type": "string", "description": "Absolute path to a file on this workspace"},
                "caption": {"type": "string", "description": "Optional caption (image/video/document)"},
            },
            "required": ["account", "jid", "file_path"],
        },
    },
    {
        "name": "whatsapp_download_media",
        "description": (
            "Download a media message seen via whatsapp_read_messages, returning the "
            "local path. Only works while the message is still in the connector's "
            "in-memory cache (recent messages since it last started)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": _ACCOUNT_OPTIONAL,
                "message_id": {"type": "string", "description": "Message id from whatsapp_read_messages"},
            },
            "required": ["message_id"],
        },
    },
]


def _result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _ok(obj) -> dict:
    return _result(json.dumps(obj, default=str, indent=2))


class ToolError(Exception):
    """Raised with a message written for the agent that has to act on it."""


def _describe(accounts: list[dict]) -> str:
    if not accounts:
        return "no accounts are linked"
    return "; ".join(
        f"{a['label']!r} (id {a['id']}, {'connected' if a['connected'] else 'not connected'})"
        for a in accounts
    )


def _match(accounts: list[dict], account: str) -> dict:
    """id first (exact), then label (case-insensitive)."""
    for a in accounts:
        if a["id"] == account:
            return a
    needle = account.strip().casefold()
    hits = [a for a in accounts if (a.get("label") or "").strip().casefold() == needle]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # Two accounts sharing a label is a user-created ambiguity; resolving it
        # by position would send from an arbitrary number.
        raise ToolError(
            f"{account!r} matches {len(hits)} accounts — use the id instead. "
            f"Accounts: {_describe(accounts)}"
        )
    raise ToolError(f"No account matches {account!r}. Accounts: {_describe(accounts)}")


async def _resolve(client, account: str | None, *, for_sending: bool) -> dict:
    try:
        accounts = await client.list_accounts()
    except ConnectorDown as e:
        raise ToolError(str(e)) from e

    if not accounts:
        raise ToolError(
            "No WhatsApp account is linked to this workspace. The user has to add one "
            "in Apps → WhatsApp and scan the QR from their phone — there is nothing to "
            "retry on this side."
        )

    if account:
        found = _match(accounts, account)
    elif for_sending:
        raise ToolError(
            "'account' is required when sending — a message cannot be unsent, so there "
            f"is no default. Accounts: {_describe(accounts)}"
        )
    else:
        connected = [a for a in accounts if a["connected"]]
        if len(connected) == 1:
            found = connected[0]
        elif not connected:
            raise ToolError(
                f"No account is connected right now. Accounts: {_describe(accounts)}. "
                "An account showing a QR needs the user to scan it in Apps → WhatsApp."
            )
        else:
            raise ToolError(
                f"{len(connected)} accounts are connected, so 'account' is required. "
                f"Accounts: {_describe(accounts)}"
            )

    if not found["connected"]:
        hint = (
            " It is waiting for its pairing QR to be scanned — only the user can do that, "
            "in Apps → WhatsApp."
            if found.get("has_qr") else
            " It is reconnecting; try again in a few seconds."
        )
        raise ToolError(f"Account {found['label']!r} is not connected.{hint}")
    return found


# ── tool bodies ─────────────────────────────────────────────────────────────
async def _list_accounts(service) -> dict:
    snap = await service.snapshot()
    if snap["provisioning"]:
        return _result("The WhatsApp connector is still installing its dependencies "
                       "(first run, about a minute). No accounts are reachable yet.")
    if not snap["connector_running"]:
        return _result(
            "The WhatsApp connector is not running"
            + (f": {snap['setup_error']}" if snap["setup_error"] else "")
            + ". It can be started from Apps → WhatsApp.", is_error=True)
    if not snap["accounts"]:
        return _result("No WhatsApp accounts are linked yet. The user adds one in "
                       "Apps → WhatsApp and scans the QR from the phone that owns the number.")
    return _ok([
        {"id": a["id"], "label": a["label"], "connected": a["connected"],
         "has_qr": a["has_qr"], "phone": a["phone"], "enabled": a["enabled"]}
        for a in snap["accounts"]
    ])


async def _status(service) -> dict:
    snap = await service.snapshot()
    if snap["provisioning"]:
        return _result("Installing the connector (first run, ~1 min). Not usable yet.")
    if not snap["connector_running"]:
        return _result("The connector is not running"
                       + (f": {snap['setup_error']}" if snap["setup_error"] else "")
                       + ". Start it in Apps → WhatsApp.", is_error=True)
    accounts = snap["accounts"]
    connected = [a for a in accounts if a["connected"]]
    waiting = [a for a in accounts if a["has_qr"]]
    lines = [f"Connector running. {len(connected)}/{len(accounts)} account(s) connected."]
    for a in accounts:
        state = ("connected" if a["connected"]
                 else "waiting for a QR scan" if a["has_qr"]
                 else "paused" if not a["enabled"] else "connecting")
        lines.append(f"  {a['label']!r} (id {a['id']}): {state}"
                     + (f" — +{a['phone']}" if a["phone"] else ""))
    if waiting:
        lines.append("An account waiting for a QR can only be fixed by the user, in "
                     "Apps → WhatsApp → Show QR.")
    return _result("\n".join(lines))


async def _list_conversations(service, args) -> dict:
    acc = await _resolve(service.client, args.get("account"), for_sending=False)
    chats = await service.client.chats(acc["id"])
    if not chats:
        return _result(f"No conversations seen on {acc['label']!r} since the connector "
                       f"last started. That is not the same as none existing.")
    return _ok({"account": {"id": acc["id"], "label": acc["label"]}, "chats": chats})


async def _read_messages(service, args) -> dict:
    acc = await _resolve(service.client, args.get("account"), for_sending=False)
    jid = args.get("jid") or ""
    if not jid:
        raise ToolError("jid is required.")
    msgs = await service.client.messages(acc["id"], jid, int(args.get("limit") or 50))
    if not msgs:
        return _result(f"No messages for {jid} on {acc['label']!r} since the connector "
                       f"last started.")
    return _ok({"account": {"id": acc["id"], "label": acc["label"]}, "jid": jid,
                "messages": msgs})


async def _send_message(service, args) -> dict:
    acc = await _resolve(service.client, args.get("account"), for_sending=True)
    jid, text = args.get("jid") or "", args.get("text") or ""
    if not jid or not text:
        raise ToolError("jid and text are required.")
    await service.client.send_text(acc["id"], jid, text)
    return _result(f"Sent from {acc['label']!r} to {jid}.")


async def _send_media(service, args) -> dict:
    acc = await _resolve(service.client, args.get("account"), for_sending=True)
    jid, path = args.get("jid") or "", args.get("file_path") or ""
    if not jid or not path:
        raise ToolError("jid and file_path are required.")
    await service.client.send_media(acc["id"], jid, path, args.get("caption"))
    return _result(f"Sent {path} from {acc['label']!r} to {jid}.")


async def _download_media(service, args) -> dict:
    acc = await _resolve(service.client, args.get("account"), for_sending=False)
    message_id = args.get("message_id") or ""
    if not message_id:
        raise ToolError("message_id is required.")
    out = await service.client.download_media(acc["id"], message_id)
    return _result(f"Downloaded {out.get('media_type')} to {out.get('path')}")


_HANDLERS = {
    "whatsapp_list_conversations": _list_conversations,
    "whatsapp_read_messages": _read_messages,
    "whatsapp_send_message": _send_message,
    "whatsapp_send_media": _send_media,
    "whatsapp_download_media": _download_media,
}


async def _call(service, name: str, args: dict) -> dict:
    try:
        if name == "whatsapp_list_accounts":
            return await _list_accounts(service)
        if name == "whatsapp_status":
            return await _status(service)
        handler = _HANDLERS.get(name)
        if handler is None:
            return _result(f"Unknown tool: {name}", is_error=True)
        return await handler(service, args)
    except ToolError as e:
        return _result(str(e), is_error=True)
    except ConnectorDown as e:
        return _result(str(e), is_error=True)
    except ConnectorError as e:
        # 409 is the duplicate-message guard. Surfacing it as a plain failure
        # invites a retry, which is the exact behaviour the guard exists to stop.
        if e.status == 409:
            return _result(f"Refused: {e.message} Rephrase the message — do not retry "
                           f"the same text.", is_error=True)
        return _result(f"WhatsApp connector returned {e.status}: {e.message}", is_error=True)
    except Exception as e:  # noqa: BLE001 - a tool must not 500 the gateway
        log.exception("whatsapp mcp tool %s failed", name)
        return _result(f"{name} failed: {e}", is_error=True)


async def handle_request(service, request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = request.get("params") or {}
        result = await _call(service, params.get("name"), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}
