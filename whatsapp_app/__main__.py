"""Standalone mode — ``python -m whatsapp_app``.

Runs the app completely outside the workspace runtime (ADR Decision 4): same
``build_routes()`` sub-app, mounted at the SAME prefix, so every path and every
line of client code reads identically in both modes. Useful for iterating on
the pairing panel without a workspace reload behind every edit.

**No IdentityGuard here** — that is runtime machinery, not app code. This binds
127.0.0.1 for that reason: the routes below can start and stop WhatsApp
sessions and send messages as the linked number, which is not something to
expose on 0.0.0.0 because a dev server was convenient.
"""
from __future__ import annotations

import argparse
import asyncio
import os

import uvicorn
from fastapi import FastAPI

from .routes import build_routes
from .service import ConnectorService


def build_standalone_app(config: dict | None = None) -> FastAPI:
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    service = ConnectorService(None, package_dir, config or {})
    app = FastAPI(title="aw-app-whatsapp (standalone)")
    app.mount("/api/apps/whatsapp", build_routes(service))

    @app.on_event("startup")
    async def _boot() -> None:
        asyncio.create_task(service.provision_and_start())

    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="whatsapp_app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9410)
    p.add_argument("--connector-port", type=int, default=9310)
    args = p.parse_args()
    uvicorn.run(build_standalone_app({"connector_port": args.connector_port}),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
