"""Mode-agnostic FastAPI sub-app for aw-app-whatsapp.

Ports the monolith's ``/api/whatsapp/*`` (agentic-workspace
``src/whatsapp/routes.py``) with one shape change that runs through everything
here: **every path is account-scoped**. The monolith had exactly one linked
number, so ``GET /api/whatsapp/qr.png`` and ``POST /api/whatsapp/message`` were
unambiguous. Here they're ``/accounts/{account_id}/…`` — an account id is a
required argument, not a default, so a second WhatsApp is a second row rather
than a second deployment.

Paths stay RELATIVE (no ``/api/apps/whatsapp`` prefix) so client code reads the
same in integrated and standalone mode. Nothing here implements auth: the
runtime mounts this behind ``IdentityGuard``.

Note the panel lives at ``/panel`` and NOT under ``/ui/`` — core owns
``GET /api/apps/{slug}/ui/{path:path}`` for component-mode ESM bundles and that
route shadows anything an app mounts there.
"""
from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from .connector_client import ConnectorDown, ConnectorError
from .panel_ui import PANEL_HTML


def _http(e: Exception) -> HTTPException:
    if isinstance(e, ConnectorDown):
        return HTTPException(503, str(e))
    if isinstance(e, ConnectorError):
        return HTTPException(e.status, e.message)
    return HTTPException(500, str(e))


def build_routes(service) -> FastAPI:
    """`service` is the ConnectorService owned by the plugin (or by
    ``__main__`` in standalone mode)."""
    app = FastAPI(title="whatsapp")

    # ── panel + state ────────────────────────────────────────────────────────
    @app.get("/panel", response_class=HTMLResponse)
    async def panel() -> HTMLResponse:
        return HTMLResponse(PANEL_HTML)

    @app.get("/state")
    async def state() -> dict:
        return await service.snapshot()

    @app.get("/status")
    async def status() -> dict:
        """Compact summary for agents and health checks.

        Deliberately answers 200 with ``ok: false`` rather than raising when the
        connector is down — a caller checking "can I send a WhatsApp right now"
        needs the reason, not an exception.
        """
        snap = await service.snapshot()
        accounts = snap["accounts"]
        return {
            "ok": snap["connector_running"] and any(a["connected"] for a in accounts),
            "connector_running": snap["connector_running"],
            "provisioning": snap["provisioning"],
            "error": snap["setup_error"],
            "accounts": [
                {"id": a["id"], "label": a["label"], "connected": a["connected"],
                 "has_qr": a["has_qr"], "phone": a["phone"]}
                for a in accounts
            ],
        }

    # ── connector service ────────────────────────────────────────────────────
    @app.post("/service/start")
    async def service_start() -> dict:
        try:
            return service.start()
        except Exception as e:
            raise HTTPException(409, str(e))

    @app.post("/service/stop")
    async def service_stop() -> dict:
        return service.stop()

    @app.get("/service/logs")
    async def service_logs(limit: int = Query(200, ge=1, le=500)) -> dict:
        return {"lines": service.logs()[-limit:]}

    # ── accounts ─────────────────────────────────────────────────────────────
    @app.get("/accounts")
    async def list_accounts() -> dict:
        try:
            return {"accounts": await service.client.list_accounts()}
        except Exception as e:
            raise _http(e)

    @app.post("/accounts", status_code=201)
    async def create_account(body: dict = Body(...)) -> dict:
        label = (body.get("label") or "").strip()
        if not label:
            raise HTTPException(400, "label is required")
        try:
            return await service.client.create_account(label, body.get("webhook_url"))
        except Exception as e:
            raise _http(e)

    @app.get("/accounts/{account_id}")
    async def get_account(account_id: str) -> dict:
        try:
            return await service.client.get_account(account_id)
        except Exception as e:
            raise _http(e)

    @app.patch("/accounts/{account_id}")
    async def patch_account(account_id: str, body: dict = Body(...)) -> dict:
        patch = {k: v for k, v in body.items() if k in ("label", "webhook_url")}
        if not patch:
            raise HTTPException(400, "nothing to update (label, webhook_url)")
        try:
            return await service.client.update_account(account_id, patch)
        except Exception as e:
            raise _http(e)

    @app.delete("/accounts/{account_id}")
    async def delete_account(account_id: str) -> dict:
        try:
            return await service.client.delete_account(account_id)
        except Exception as e:
            raise _http(e)

    @app.post("/accounts/{account_id}/relink")
    async def relink(account_id: str) -> dict:
        try:
            return await service.client.relink(account_id)
        except Exception as e:
            raise _http(e)

    @app.post("/accounts/{account_id}/start")
    async def start_account(account_id: str) -> dict:
        try:
            return await service.client.start_account(account_id)
        except Exception as e:
            raise _http(e)

    @app.post("/accounts/{account_id}/stop")
    async def stop_account(account_id: str) -> dict:
        try:
            return await service.client.stop_account(account_id)
        except Exception as e:
            raise _http(e)

    @app.get("/accounts/{account_id}/qr.png")
    async def qr_png(account_id: str) -> Response:
        try:
            png = await service.client.qr_png(account_id)
        except Exception as e:
            raise _http(e)
        # no-store, not just no-cache: WhatsApp rotates the pairing code every
        # ~20s and a cached PNG is a code that silently never works.
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    # ── messaging ────────────────────────────────────────────────────────────
    @app.get("/accounts/{account_id}/chats")
    async def chats(account_id: str) -> dict:
        try:
            return {"chats": await service.client.chats(account_id)}
        except Exception as e:
            raise _http(e)

    @app.get("/accounts/{account_id}/messages")
    async def messages(account_id: str, jid: str = Query(...),
                       limit: int = Query(50, ge=1, le=200)) -> dict:
        try:
            return {"jid": jid, "messages": await service.client.messages(account_id, jid, limit)}
        except Exception as e:
            raise _http(e)

    @app.post("/accounts/{account_id}/send")
    async def send(account_id: str, body: dict = Body(...)) -> dict:
        jid, text = body.get("jid"), body.get("text")
        if not jid or not text:
            raise HTTPException(400, "jid and text are required")
        try:
            return await service.client.send_text(account_id, jid, text)
        except Exception as e:
            raise _http(e)

    @app.post("/accounts/{account_id}/send-media")
    async def send_media(account_id: str, body: dict = Body(...)) -> dict:
        jid, file_path = body.get("jid"), body.get("file_path")
        if not jid or not file_path:
            raise HTTPException(400, "jid and file_path are required")
        try:
            return await service.client.send_media(
                account_id, jid, file_path, body.get("caption"), body.get("mimetype"))
        except Exception as e:
            raise _http(e)

    @app.get("/accounts/{account_id}/media")
    async def download_media(account_id: str, message_id: str = Query(...)) -> dict:
        try:
            return await service.client.download_media(account_id, message_id)
        except Exception as e:
            raise _http(e)

    return app
