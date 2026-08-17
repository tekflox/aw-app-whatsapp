"""Thin async client for the local Node connector.

The connector binds 127.0.0.1 and carries no auth of its own — same posture as
the monolith's (agentic-workspace ``src/whatsapp_connector/index.js``, reached
from ``src/whatsapp/routes.py`` over plain loopback HTTP). The authenticated
edge is this app's own routes, which the runtime mounts behind IdentityGuard.

Every method raises ``ConnectorDown`` when the service isn't up, so callers can
tell "connector not running" from "connector said no" — the two need very
different messages in the Settings panel and the two got conflated in the
monolith, where a dead connector read as "not connected to WhatsApp".
"""
from __future__ import annotations

import httpx


class ConnectorDown(RuntimeError):
    """The connector process isn't reachable on its loopback port."""


class ConnectorError(RuntimeError):
    """The connector answered with a non-2xx. Carries its status + message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ConnectorClient:
    def __init__(self, port: int, timeout: float = 20.0) -> None:
        self.base = f"http://127.0.0.1:{int(port)}"
        self.timeout = timeout

    async def _request(self, method: str, path: str, **kw):
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                return await c.request(method, f"{self.base}{path}", **kw)
        except httpx.RequestError as e:
            raise ConnectorDown(
                f"WhatsApp connector is not answering on {self.base} ({e.__class__.__name__}). "
                f"Start it from the app's settings, or check `aw-workspace-cli logs whatsapp`."
            ) from e

    async def _json(self, method: str, path: str, **kw) -> dict:
        r = await self._request(method, path, **kw)
        if r.status_code >= 400:
            try:
                message = r.json().get("error") or r.text
            except ValueError:
                message = r.text
            raise ConnectorError(r.status_code, message)
        return r.json() if r.content else {}

    # ── accounts ─────────────────────────────────────────────────────────────
    async def list_accounts(self) -> list[dict]:
        return (await self._json("GET", "/accounts")).get("accounts", [])

    async def create_account(self, label: str, webhook_url: str | None = None) -> dict:
        return await self._json("POST", "/accounts",
                                json={"label": label, "webhook_url": webhook_url})

    async def get_account(self, account_id: str) -> dict:
        return await self._json("GET", f"/accounts/{account_id}")

    async def update_account(self, account_id: str, patch: dict) -> dict:
        return await self._json("PATCH", f"/accounts/{account_id}", json=patch)

    async def delete_account(self, account_id: str) -> dict:
        return await self._json("DELETE", f"/accounts/{account_id}")

    async def relink(self, account_id: str) -> dict:
        return await self._json("POST", f"/accounts/{account_id}/relink")

    async def start_account(self, account_id: str) -> dict:
        return await self._json("POST", f"/accounts/{account_id}/start")

    async def stop_account(self, account_id: str) -> dict:
        return await self._json("POST", f"/accounts/{account_id}/stop")

    async def qr_png(self, account_id: str) -> bytes:
        r = await self._request("GET", f"/accounts/{account_id}/qr")
        if r.status_code >= 400:
            try:
                message = r.json().get("error") or r.text
            except ValueError:
                message = r.text
            raise ConnectorError(r.status_code, message)
        return r.content

    # ── messaging ────────────────────────────────────────────────────────────
    async def send_text(self, account_id: str, jid: str, text: str) -> dict:
        return await self._json("POST", f"/accounts/{account_id}/send",
                                json={"jid": jid, "text": text})

    async def send_media(self, account_id: str, jid: str, file_path: str,
                         caption: str | None = None, mimetype: str | None = None) -> dict:
        body = {"jid": jid, "file_path": file_path}
        if caption:
            body["caption"] = caption
        if mimetype:
            body["mimetype"] = mimetype
        return await self._json("POST", f"/accounts/{account_id}/send-media", json=body)

    async def chats(self, account_id: str) -> list[dict]:
        return (await self._json("GET", f"/accounts/{account_id}/chats")).get("chats", [])

    async def messages(self, account_id: str, jid: str, limit: int = 50) -> list[dict]:
        data = await self._json("GET", f"/accounts/{account_id}/messages",
                                params={"jid": jid, "limit": limit})
        return data.get("messages", [])

    async def download_media(self, account_id: str, message_id: str) -> dict:
        return await self._json("GET", f"/accounts/{account_id}/media",
                                params={"message_id": message_id})

    async def push_config(self, min_send_interval_ms: int, mark_read: bool) -> dict:
        return await self._json("POST", "/config", json={
            "min_send_interval_ms": int(min_send_interval_ms),
            "mark_read": bool(mark_read),
        })
