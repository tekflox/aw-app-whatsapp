# aw-app-whatsapp

Links **personal** WhatsApp accounts to an aw-workspace by scanning a QR — the
same mechanism as WhatsApp Web, over the multi-device protocol via
[Baileys](https://github.com/WhiskeySockets/Baileys). Several accounts at once.

Ported from the `agentic-workspace` monolith's `whatsapp-connector`
(`src/whatsapp_connector/index.js`, `src/whatsapp/routes.py`, `src/mcp/whatsapp.py`).

## What changed in the port

| Monolith | Here | Why |
|---|---|---|
| One account, module-level socket + state | N `Account` instances in one Node process | The user asked for several numbers. A second account there meant a second service, a second port and a second database. |
| Baileys creds in a `whatsapp_connector` Postgres DB | `useMultiFileAuthState` under `<AW_WORKSPACE_HOME>/data/whatsapp/accounts/<id>/auth/` | An app's data dir is host-mounted and survives container recreation, which was the only thing Postgres was buying. Drops a database dependency. |
| `/api/whatsapp/qr.png`, `/api/whatsapp/send` | `/api/apps/whatsapp/accounts/{id}/…` | Nothing is implicit — an account id is a required argument, so "which number did that go from" always has an answer. |
| QR shown in Settings → AW → Workspace Agent → WhatsApp | Settings panel served as an iframe from `/panel` | Declarative windows have no image widget and no data-bound list; the QR is a live image that rotates every ~20s next to a live account list. |
| Inbound hard-wired to `POST /api/whatsapp/message` on awserv | Optional per-account `webhook_url` | There is no core WorkspaceAgent route in the decoupled world. Inbound routing is a workspace decision, not the connector's. |
| stdio MCP server (`src/mcp/whatsapp.py`) | in-process `POST /mcp`, registered by `self_register.py` | The gateway spawns a stdio child inside its OWN container, where `127.0.0.1:9310` is its loopback — the connector would be unreachable and the upstream would serve zero tools while looking healthy. |
| `whatsapp_send_message(jid, text)` | `account` **required** on sends, optional on reads | Reads default to the single connected account; a send never defaults, because it cannot be recalled. |

Kept verbatim, because each came from a real incident: the **15s minimum send
interval** and the **duplicate-text refusal** (a WhatsApp number was logged out
by spam detection on 2026-07-07), `markOnlineOnConnect: false` (Baileys' default
suppresses push notifications on the user's own phone), and read receipts
staying **off** by default.

## Layout

```
connector/        Node — index.js (registry + HTTP), account.js (one Baileys socket)
whatsapp_app/     Python — plugin, routes, ConnectorService, pairing panel,
                  mcp_tools.py (7 tools) + self_register.py (gateway upstream)
windows/main.json declarative window → iframe onto /panel
skills/aw-whatsapp/  agent-facing contract
```

Durable state lives in `<AW_WORKSPACE_HOME>/data/whatsapp/`, never in the
package dir (which an update replaces wholesale). That includes
`connector-runtime/` — the Node code plus `node_modules`, so a version bump is a
file copy rather than a fresh `npm install` of Baileys.

## Develop

```bash
python3 -m pytest tests/ -q            # route contract
python3 tests/validate_manifest.py     # manifest + declarative widgets
node --check connector/index.js
python3 -m whatsapp_app                # standalone on 127.0.0.1:9410
```

## Install

Through the marketplace, not a sideload:

```bash
aw-workspace-cli marketplace install whatsapp
```

Then **Apps → WhatsApp** (or the Settings gear), *Add an account*, and scan.
