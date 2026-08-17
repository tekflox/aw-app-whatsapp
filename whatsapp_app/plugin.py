"""Entrypoint referenced by aw-app.json's runtime.entrypoint
("whatsapp_app.plugin:WhatsAppPlugin").

Ports the monolith's whatsapp-connector component (agentic-workspace
``src/start/whatsapp_connector.py`` + ``src/whatsapp/routes.py``) onto the F4
``ctx`` facades:

* ``ctx.routes`` (``routes:register``) — the HTTP surface + Settings panel,
  mounted at ``/api/apps/whatsapp``.
* ``ctx.services`` (``service:manage``) — the Node connector as a managed
  service, so the runtime stops it on uninstall instead of leaking a process
  that keeps N WhatsApp sockets alive.
* ``ctx.watchdog`` (``watchdog:tasks``) — restart it if it dies. The service
  supervisor starts a process and forgets it; without this a crash leaves every
  linked account offline with nothing but a container log to say so.

Routes are registered **synchronously**; provisioning is not. A first install
has to ``npm install`` Baileys (~60s) and blocking ``activate()`` on that would
stall the whole reconcile pass. Registering routes first means the Settings
panel is already reachable and can show "installing…" instead of a dead window.
"""
from __future__ import annotations

import asyncio
import logging

from . import routes as routes_mod
from .service import ConnectorService

log = logging.getLogger("aw_apps.whatsapp")

HEAL_INTERVAL_S = 60


class WhatsAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx
        self.service = ConnectorService(ctx, ctx.package_dir, dict(getattr(ctx, "config", {}) or {}))

        ctx.routes.register(routes_mod.build_routes(self.service))

        self._boot = asyncio.create_task(self.service.provision_and_start())

        try:
            ctx.watchdog.register("connector-heal", self.service.heal,
                                  interval_s=HEAL_INTERVAL_S, run_immediately=False)
        except Exception:
            # A missing watchdog grant must not take the app down — the
            # connector still works, it just won't self-heal.
            log.warning("whatsapp: watchdog task not registered", exc_info=True)

        log.info("aw-app-whatsapp activated (connector port %s)", self.service.port)

    async def on_config_saved(self, ctx) -> None:
        self.ctx = ctx
        new = dict(getattr(ctx, "config", {}) or {})
        old_port = self.service.port
        self.service.config = new
        # A port change is the one setting that can't be hot-applied — the
        # process is bound to the old one and the client would keep talking to
        # nothing. Everything else goes over the wire.
        if new.get("connector_port") != old_port:
            log.info("whatsapp: connector port changed %s → %s, restarting",
                     old_port, new.get("connector_port"))
            try:
                self.service.stop()
            except Exception:
                log.warning("whatsapp: stop before port change failed", exc_info=True)
            self.service._registered = False
            self._boot = asyncio.create_task(self.service.provision_and_start())
            return
        await self.service.push_config()
        log.info("aw-app-whatsapp config saved")

    async def deactivate(self) -> None:
        # The runtime stops registered services itself (journal reverse replay).
        # The data dir under AW_WORKSPACE_HOME deliberately SURVIVES: an update
        # is uninstall+install, and wiping it here would make every version bump
        # cost the user a fresh QR scan per linked account.
        task = getattr(self, "_boot", None)
        if task and not task.done():
            task.cancel()
        log.info("aw-app-whatsapp deactivated")
