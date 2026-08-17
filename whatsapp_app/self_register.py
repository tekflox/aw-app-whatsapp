"""Write this app's own ``mcp.json`` so aw-mcp-gateway's app-scan
(``scan_app_mcp_servers()``, reading ``<installed-app-dir>/mcp.json``) discovers
the ``/mcp`` endpoint in ``routes.py``.

**This is not optional plumbing.** Declaring ``contributes.mcp.provides`` in the
manifest is marketplace copy — it does NOT register an upstream. Without this
file the app installs clean, ``doctor`` reports no degradation, ``POST /mcp``
answers correctly if you call it by hand, and the gateway serves **zero** of the
tools. That gap is invisible from every surface except the gateway's own
upstream list.

Tier-1, so ``socket.gethostname()`` is exactly the value ContainerSupervisor
injects into sibling containers as ``AW_WORKSPACE_HOST`` — the gateway
container can reach us at that name. Tier-1 routes are IdentityGuard-gated, so
the entry carries ``X-Api-Key`` for the gateway's HttpUpstream.

The key is re-read and rewritten on every activation, which is what keeps a
regenerated workspace API key from silently leaving the upstream serving 401s.
"""
from __future__ import annotations

import json
import logging
import os
import socket

log = logging.getLogger("aw_apps.whatsapp")

MCP_SERVER_NAME = "whatsapp"


def register_self(package_dir: str, port: int) -> None:
    """Best-effort — a dev run with no installed package dir simply no-ops."""
    if not os.path.isdir(package_dir):
        return

    entry: dict = {
        "type": "http",
        "url": f"http://{socket.gethostname()}:{port}/api/apps/whatsapp/mcp",
        "enabled": True,
    }
    api_key = os.environ.get("AW_WORKSPACE_API_KEY")
    if api_key:
        entry["headers"] = {"X-Api-Key": api_key}

    path = os.path.join(package_dir, "mcp.json")
    data: dict = {"mcpServers": {}}
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        if isinstance(existing, dict) and isinstance(existing.get("mcpServers"), dict):
            data = existing
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # An identical rewrite churns the file's mtime on every boot, and the
    # gateway reloads on change — a no-op write becomes a reload loop.
    if data["mcpServers"].get(MCP_SERVER_NAME) == entry:
        return
    data["mcpServers"][MCP_SERVER_NAME] = entry
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        log.info("whatsapp: registered MCP upstream in %s (%s)", path, entry["url"])
    except OSError as e:
        log.warning("whatsapp: could not write %s: %s", path, e)
