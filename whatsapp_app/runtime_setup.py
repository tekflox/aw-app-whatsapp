"""Materialise the Node connector into its durable runtime dir.

Baileys is a ~200-package dependency tree; ``npm install`` takes the better
part of a minute. Doing that inside the package dir would mean paying it on
every version bump, and (because the package dir is recreated when the
workspace container is) on plenty of plain boots too.

So the connector's JS is *copied* into ``<data>/connector-runtime/`` and
``npm install`` runs there, guarded by a hash of the dependency block. Node
resolves ``node_modules`` from the script's own directory, so the copied
``index.js`` finds the install regardless of the service's cwd.

The guard hashes ``dependencies`` only — the JS changing every release must
not trigger a reinstall, and a dependency changing must.

Everything here follows §6b of the aw-create-app skill's installer contract:
**verify, don't detect**. The check is "does Node actually resolve Baileys",
not "does a node_modules folder exist" — a half-finished install leaves the
folder behind and would otherwise be reported as ready forever.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess

from .paths import runtime_dir

log = logging.getLogger("aw_apps.whatsapp")

#: Files copied out of the package's ``connector/`` on every activation.
CONNECTOR_FILES = ("index.js", "account.js", "package.json")

NPM_TIMEOUT_S = 600


class ConnectorSetupError(RuntimeError):
    pass


def _deps_hash(package_json_path: str) -> str:
    with open(package_json_path, encoding="utf-8") as f:
        deps = json.load(f).get("dependencies", {})
    return hashlib.sha256(
        json.dumps(deps, sort_keys=True).encode()
    ).hexdigest()


def _baileys_resolves(rt: str) -> bool:
    """The real capability check: can Node import Baileys from here?"""
    try:
        proc = subprocess.run(
            ["node", "-e", "import('@whiskeysockets/baileys').then(()=>process.exit(0),()=>process.exit(1))"],
            cwd=rt, capture_output=True, timeout=120,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def connector_entrypoint() -> str:
    return os.path.join(runtime_dir(), "index.js")


def is_ready() -> bool:
    return os.path.exists(connector_entrypoint()) and os.path.isdir(
        os.path.join(runtime_dir(), "node_modules", "@whiskeysockets", "baileys")
    )


def ensure_connector_runtime(package_dir: str) -> str:
    """Copy the connector into its durable dir and install deps if needed.

    Blocking — call it off the event loop. Idempotent: on a boot where nothing
    changed it does three file copies and one stat, and returns.
    """
    rt = runtime_dir()
    src = os.path.join(package_dir, "connector")

    for name in CONNECTOR_FILES:
        source = os.path.join(src, name)
        if not os.path.exists(source):
            raise ConnectorSetupError(f"connector/{name} missing from {package_dir}")
        shutil.copy2(source, os.path.join(rt, name))

    stamp_path = os.path.join(rt, ".deps-hash")
    want = _deps_hash(os.path.join(rt, "package.json"))
    have = None
    if os.path.exists(stamp_path):
        with open(stamp_path, encoding="utf-8") as f:
            have = f.read().strip()

    if have == want and _baileys_resolves(rt):
        log.info("whatsapp: connector runtime already provisioned at %s", rt)
        return connector_entrypoint()

    log.info("whatsapp: installing connector dependencies in %s (this takes a minute)", rt)
    # --cache inside our own dir, never $HOME/.npm: the workspace container and
    # every agent-runner container share $HOME/.npm, and a single root-owned
    # entry left there by another process makes every later install die with
    # EACCES. Hit for real on the first provisioning run. The app's own data dir
    # is the one place we know is ours.
    proc = subprocess.run(
        ["npm", "install", "--omit=dev", "--no-audit", "--no-fund",
         "--cache", os.path.join(rt, ".npm-cache")],
        cwd=rt, capture_output=True, text=True, timeout=NPM_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise ConnectorSetupError(
            f"npm install failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-2000:]}"
        )
    if not _baileys_resolves(rt):
        raise ConnectorSetupError(
            "npm install reported success but Node still cannot import "
            "@whiskeysockets/baileys — the install is incomplete."
        )

    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(want)
    log.info("whatsapp: connector dependencies installed")
    return connector_entrypoint()


def start_command(port: int, min_send_interval_ms: int, mark_read: bool) -> str:
    """The command line handed to ``ctx.services.register``.

    Config goes in argv rather than env because the supervisor launches a
    managed service with the workspace process's own environment and no hook
    to add to it (aw-workspace ``src/apps/services.py``).
    """
    from .paths import data_dir
    return (
        f"node {connector_entrypoint()}"
        f" --port {int(port)}"
        f" --data-dir {data_dir()}"
        f" --min-send-interval-ms {int(min_send_interval_ms)}"
        f" --mark-read {'1' if mark_read else '0'}"
    )
