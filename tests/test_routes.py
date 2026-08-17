"""Route contract tests against a fake ConnectorService.

The Node connector is not started here — these assert the HTTP shape this app
owns: account-scoped paths, the panel being served outside ``/ui/``, and the
error mapping that the Settings panel depends on to tell "the connector is
down" apart from "WhatsApp says no".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whatsapp_app.connector_client import ConnectorDown, ConnectorError  # noqa: E402
from whatsapp_app.routes import build_routes  # noqa: E402

ACCOUNT = {
    "id": "abc123", "label": "Personal", "enabled": True, "webhook_url": None,
    "connected": True, "has_qr": False, "qr_generated_at": None,
    "self_jid": "5521999999999:12@s.whatsapp.net", "phone": "5521999999999",
    "last_disconnect_reason": None, "last_error": None, "linked": True,
    "chat_count": 3,
}


class FakeClient:
    def __init__(self) -> None:
        self.raise_with: Exception | None = None
        self.sent: list[tuple] = []

    def _maybe_raise(self):
        if self.raise_with:
            raise self.raise_with

    async def list_accounts(self):
        self._maybe_raise()
        return [ACCOUNT]

    async def create_account(self, label, webhook_url=None):
        self._maybe_raise()
        return {**ACCOUNT, "label": label, "connected": False, "has_qr": True}

    async def qr_png(self, account_id):
        self._maybe_raise()
        return b"\x89PNG\r\n\x1a\n-fake-"

    async def send_text(self, account_id, jid, text):
        self._maybe_raise()
        self.sent.append((account_id, jid, text))
        return {"ok": True}

    async def messages(self, account_id, jid, limit=50):
        self._maybe_raise()
        return [{"id": "m1", "fromMe": False, "timestamp": 1, "text": "oi", "mediaType": None}]


class FakeService:
    def __init__(self) -> None:
        self.client = FakeClient()
        self.running = True
        self.provisioning = False

    def start(self):
        self.running = True
        return {"service": "connector", "running": True, "pid": 1}

    def stop(self):
        self.running = False
        return {"service": "connector", "running": False, "pid": None}

    def status(self):
        return {"service": "connector", "running": self.running, "pid": 1 if self.running else None}

    def logs(self):
        return ["line one", "line two"]

    async def snapshot(self):
        return {
            "connector_running": self.running,
            "connector_pid": 1 if self.running else None,
            "provisioning": self.provisioning,
            "setup_error": None,
            "port": 9310,
            "accounts": await self.client.list_accounts() if self.running else [],
        }


@pytest.fixture()
def env():
    service = FakeService()
    app = FastAPI()
    app.mount("/api/apps/whatsapp", build_routes(service))
    return service, TestClient(app)


def test_panel_is_html_and_not_under_ui(env):
    _, client = env
    res = client.get("/api/apps/whatsapp/panel")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    # Core owns /api/apps/<slug>/ui/* for ESM bundles and shadows anything an
    # app mounts there — a panel that drifted under /ui/ would 404 on a real
    # install while passing every unit test that didn't check this.
    assert "/ui/" not in "/api/apps/whatsapp/panel"


def test_state_reports_accounts(env):
    _, client = env
    body = client.get("/api/apps/whatsapp/state").json()
    assert body["connector_running"] is True
    assert body["accounts"][0]["label"] == "Personal"


def test_status_is_ok_only_when_an_account_is_connected(env):
    service, client = env
    assert client.get("/api/apps/whatsapp/status").json()["ok"] is True
    service.running = False
    body = client.get("/api/apps/whatsapp/status").json()
    # 200 with ok:false — a caller asking "can I send right now" needs the
    # reason, not an exception.
    assert body["ok"] is False and body["connector_running"] is False


def test_create_account_requires_a_label(env):
    _, client = env
    assert client.post("/api/apps/whatsapp/accounts", json={"label": "  "}).status_code == 400
    res = client.post("/api/apps/whatsapp/accounts", json={"label": "Store"})
    assert res.status_code == 201 and res.json()["has_qr"] is True


def test_qr_is_never_cached(env):
    _, client = env
    res = client.get("/api/apps/whatsapp/accounts/abc123/qr.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    # WhatsApp rotates the code every ~20s; a cached PNG is a code that
    # silently never works.
    assert res.headers["cache-control"] == "no-store"


def test_send_requires_jid_and_text(env):
    service, client = env
    assert client.post("/api/apps/whatsapp/accounts/abc123/send",
                       json={"jid": "x@s.whatsapp.net"}).status_code == 400
    client.post("/api/apps/whatsapp/accounts/abc123/send",
                json={"jid": "x@s.whatsapp.net", "text": "oi"})
    assert service.client.sent == [("abc123", "x@s.whatsapp.net", "oi")]


def test_connector_down_maps_to_503(env):
    service, client = env
    service.client.raise_with = ConnectorDown("connector not answering")
    assert client.get("/api/apps/whatsapp/accounts").status_code == 503


def test_duplicate_send_keeps_the_connectors_status(env):
    service, client = env
    # The 409 the connector raises on a repeated message is the ban guard —
    # flattening it to 500 would read as a transient error and invite a retry.
    service.client.raise_with = ConnectorError(409, "identical to the last message")
    res = client.post("/api/apps/whatsapp/accounts/abc123/send",
                      json={"jid": "x@s.whatsapp.net", "text": "oi"})
    assert res.status_code == 409
    assert "identical" in res.json()["detail"]


def test_messages_route_is_account_scoped(env):
    _, client = env
    res = client.get("/api/apps/whatsapp/accounts/abc123/messages",
                     params={"jid": "x@s.whatsapp.net"})
    assert res.status_code == 200 and res.json()["messages"][0]["text"] == "oi"
    # No un-scoped fallback: the monolith's single-account /messages must not
    # come back by accident, because it would silently pick an account.
    assert client.get("/api/apps/whatsapp/messages",
                      params={"jid": "x@s.whatsapp.net"}).status_code == 404
