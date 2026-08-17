"""Where this app keeps things that must outlive an update.

`fs:workspace-data` means ``<AW_WORKSPACE_HOME>/data/whatsapp/`` — the
host-mounted tree (aw-workspace ``src/apps/paths.py``), not the package dir.
That distinction is the whole reason the layout below exists:

* the **package dir** (``/opt/aw-workspace/apps/whatsapp/``) is replaced
  wholesale on every version bump, so nothing durable may live there;
* ``accounts/<id>/auth/`` holds Baileys' credentials — losing it means the
  user re-scans a QR for every linked account, which is exactly the surprise
  an update must not spring on them;
* ``connector-runtime/`` holds the Node runtime + ``node_modules``. Keeping it
  out of the package dir is what makes an update a file copy instead of a
  60-second ``npm install`` on every version bump AND every workspace boot.
"""
from __future__ import annotations

import os

APP_ID = "whatsapp"
DEFAULT_CONTAINER_DIR = "/opt/aw-workspace"


def workspace_home() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME")
    if home:
        return home
    root = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_CONTAINER_DIR)
    return os.path.join(root, ".aw-workspace")


def data_dir() -> str:
    """``<home>/data/whatsapp`` — accounts, auth folders, contacts, media."""
    path = os.environ.get("AW_WHATSAPP_DATA_DIR") or os.path.join(
        workspace_home(), "data", APP_ID
    )
    os.makedirs(path, exist_ok=True)
    return path


def runtime_dir() -> str:
    """``<data>/connector-runtime`` — the Node connector + its node_modules."""
    path = os.path.join(data_dir(), "connector-runtime")
    os.makedirs(path, exist_ok=True)
    return path
