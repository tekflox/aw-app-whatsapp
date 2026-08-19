---
repo: architecture
path: docs/architecture/aw-app-whatsapp.md
source: generated
edited: false
checksum: sha256:343755ea8070b5513a651a26a97c36766a44aa02f43cc0731ea05a1200d2abf3
---
# WhatsApp

- **repo**: aw-app-whatsapp
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Link your own WhatsApp accounts to this workspace by scanning a QR code, then read and send messages from any agent. Several accounts at once — personal, work, a store line — each with its own pairing, its own chats and its own contacts.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/whatsapp
- `stdio-mcp` → **mcp-gateway** — MCP surface aggregated by the gateway

## MCP tools
- `whatsapp_download_media`
- `whatsapp_list_accounts`
- `whatsapp_list_conversations`
- `whatsapp_read_messages`
- `whatsapp_send_media`
- `whatsapp_send_message`
- `whatsapp_status`

## Requirements
_none documented_
