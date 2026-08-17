"""Owns the connector process: provisioning it, starting it, and telling the
UI which of those two is currently the reason nothing works.

Split out of ``plugin.py`` because the failure mode this app has to make
visible is a *sequence*: the connector can be un-provisioned (npm still
running), provisioned but stopped, running but with zero accounts, or running
with an account whose auth folder is dead. The monolith collapsed all of those
into one boolean (``connected``), and a connector that had simply crashed read
in the UI as "WhatsApp is not connected" — pointing the user at their phone
instead of at the process. ``snapshot()`` keeps them distinguishable.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

from . import runtime_setup
from .connector_client import ConnectorClient, ConnectorDown

log = logging.getLogger("aw_apps.whatsapp")

SERVICE_ID = "connector"


class ConnectorService:
    """Provision + lifecycle for the Node connector.

    Two backends: ``ctx.services`` when running inside the workspace runtime
    (journaled, stopped on uninstall), and a plain ``Popen`` in standalone mode
    where no such facade exists.
    """

    def __init__(self, ctx, package_dir: str, config: dict) -> None:
        self.ctx = ctx
        self.package_dir = package_dir
        self.config = config or {}
        self.provisioning = False
        self.setup_error: str | None = None
        self._registered = False
        self._proc: subprocess.Popen | None = None  # standalone only

    # ── config ───────────────────────────────────────────────────────────────
    @property
    def port(self) -> int:
        return int(self.config.get("connector_port") or 9310)

    @property
    def min_send_interval_ms(self) -> int:
        return int(self.config.get("min_send_interval_ms") or 15000)

    @property
    def mark_read(self) -> bool:
        return bool(self.config.get("mark_read", False))

    @property
    def auto_start(self) -> bool:
        return bool(self.config.get("auto_start", True))

    @property
    def client(self) -> ConnectorClient:
        return ConnectorClient(self.port)

    def _start_cmd(self) -> str:
        return runtime_setup.start_command(
            self.port, self.min_send_interval_ms, self.mark_read)

    # ── provisioning ─────────────────────────────────────────────────────────
    async def provision_and_start(self) -> None:
        """Copy the connector into its durable dir, npm-install if needed, then
        register + start the service.

        Runs as a background task from ``activate()``: a first install pays a
        ~60s ``npm install`` and blocking activation on that would stall the
        whole app reconcile pass (and every other app behind it in the queue).
        The panel shows ``provisioning: true`` meanwhile, so the wait is
        visible rather than looking like a broken app.
        """
        self.provisioning = True
        self.setup_error = None
        try:
            await asyncio.to_thread(runtime_setup.ensure_connector_runtime, self.package_dir)
        except Exception as e:
            self.setup_error = str(e)
            log.exception("whatsapp: connector provisioning failed")
            return
        finally:
            self.provisioning = False

        try:
            self.register(autostart=self.auto_start)
        except Exception as e:
            self.setup_error = str(e)
            log.exception("whatsapp: could not start the connector service")

    def register(self, autostart: bool = True) -> None:
        if self.ctx is None:
            if autostart:
                self.start()
            return
        if self._registered:
            if autostart:
                self.start()
            return
        # The runtime drops an app's service registrations on unload
        # (ServiceSupervisor.stop_all_for), so a re-activate normally lands on a
        # clean registry. Tolerate the case where it doesn't rather than failing
        # activation over a duplicate.
        try:
            self.ctx.services.register(SERVICE_ID, self._start_cmd(), autostart=autostart)
        except Exception as e:
            if "already registered" not in str(e):
                raise
            log.info("whatsapp: service already registered, reusing it")
            if autostart:
                self.start()
        self._registered = True

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> dict:
        if not runtime_setup.is_ready():
            raise RuntimeError(
                "the connector isn't installed yet — its dependencies are still "
                "downloading" if self.provisioning else
                f"the connector isn't installed: {self.setup_error or 'provisioning has not run yet'}"
            )
        if self.ctx is not None:
            if not self._registered:
                self.register(autostart=True)
                return self.status()
            return self.ctx.services.start(SERVICE_ID)
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(self._start_cmd().split(), start_new_session=True)
        return self.status()

    def stop(self) -> dict:
        if self.ctx is not None and self._registered:
            return self.ctx.services.stop(SERVICE_ID)
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        return self.status()

    def status(self) -> dict:
        if self.ctx is not None and self._registered:
            try:
                return self.ctx.services.status(SERVICE_ID)
            except Exception:
                return {"service": SERVICE_ID, "running": False, "pid": None}
        running = self._proc is not None and self._proc.poll() is None
        return {"service": SERVICE_ID, "running": running,
                "pid": self._proc.pid if running and self._proc else None}

    def logs(self) -> list[str]:
        if self.ctx is not None and self._registered:
            try:
                return self.ctx.services.logs(SERVICE_ID)
            except Exception:
                return []
        return []

    async def heal(self) -> None:
        """Watchdog tick: restart the connector if it died.

        ``ServiceSupervisor`` has no restart-on-crash of its own — it starts a
        process and forgets it. Without this, one OOM or an unhandled rejection
        leaves every linked account silently offline until someone opens
        Settings, which is precisely the silent-degradation shape this
        workspace keeps getting bitten by.
        """
        if self.provisioning or not runtime_setup.is_ready() or not self.auto_start:
            return
        if self.status().get("running"):
            return
        log.warning("whatsapp: connector is not running — restarting it")
        try:
            await asyncio.to_thread(self.start)
        except Exception:
            log.exception("whatsapp: watchdog restart failed")

    # ── the one call the panel makes ─────────────────────────────────────────
    async def snapshot(self) -> dict:
        svc = self.status()
        state = {
            "connector_running": bool(svc.get("running")),
            "connector_pid": svc.get("pid"),
            "provisioning": self.provisioning,
            "setup_error": self.setup_error,
            "port": self.port,
            "accounts": [],
        }
        if not state["connector_running"]:
            return state
        try:
            state["accounts"] = await self.client.list_accounts()
        except ConnectorDown as e:
            # Registered and "running" but not answering — a start that failed
            # a moment after fork. Say so instead of showing zero accounts.
            state["connector_running"] = False
            state["setup_error"] = str(e)
        return state

    async def push_config(self) -> None:
        """Send throttle/read-receipt changes to a live connector.

        Restarting to apply a settings change would drop every socket and, for
        an account WhatsApp is mid-handshake with, cost a re-scan.
        """
        try:
            await self.client.push_config(self.min_send_interval_ms, self.mark_read)
        except ConnectorDown:
            pass  # picked up from argv on its next start
