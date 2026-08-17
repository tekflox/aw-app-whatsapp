---
name: aw-whatsapp
description: >-
  Read and send messages on the WhatsApp accounts linked to this workspace by
  the aw-app-whatsapp app — personal accounts over the WhatsApp Web protocol
  (Baileys), not the Cloud API and not a business number. SEVERAL accounts can
  be linked at once and there is no default one, so every call names an
  account. Use whenever a task involves sending a WhatsApp message, reading a
  conversation, downloading media someone sent, or checking whether a number is
  still paired — and read the send rules BEFORE sending anything, because the
  wrong pattern gets the number banned by WhatsApp, not rate-limited.
---

# aw-whatsapp — talking to a linked WhatsApp account

`aw-app-whatsapp` links **personal** WhatsApp accounts the same way WhatsApp Web
does: the user scans a QR in **Apps → WhatsApp** with the phone that owns the
number, and this workspace becomes a linked device on it.

Hold that mental model: **you are a linked device on someone's real phone.** Not
a bot API, not a business channel. Everything you send comes from their number,
lands in their chat history, and is subject to WhatsApp's own anti-spam
enforcement — which bans, it does not throttle.

## Tools

| Tool | `account` | Notes |
|---|---|---|
| `whatsapp_list_accounts` | — | **Start here.** id, label, connected, phone. |
| `whatsapp_status` | — | Is WhatsApp usable right now, and if not, why. |
| `whatsapp_list_conversations` | optional | Recent chats, most recent first. |
| `whatsapp_read_messages` | optional | Needs `jid`. |
| `whatsapp_send_message` | **required** | Needs `jid`, `text`. |
| `whatsapp_send_media` | **required** | Needs `jid`, `file_path`. |
| `whatsapp_download_media` | optional | Needs `message_id`. |

Through the gateway they're prefixed: `aw__whatsapp__whatsapp_send_message`.

`account` takes an **id** (`a3f9c1d2`) or a **label** (`Personal`,
case-insensitive) — use the label the user said. An ambiguous label errors with
the candidates listed; it never picks one.

## The account rule, and why it isn't symmetric

**Reads default; sends do not.**

On a read tool, omitting `account` is fine when exactly one account is
connected — it resolves to that one. With several connected, it errors and
lists them. Guessing wrong here shows you the wrong chat list, which is
recoverable.

On `whatsapp_send_message` / `whatsapp_send_media`, `account` is **required,
always, with no fallback**. A sent message cannot be recalled, and a workspace
holding a personal number and a store number is exactly where an implicit
default does damage.

So when a task says "send Maria a WhatsApp" and more than one account exists,
**ask which number** — don't pick. When only one is linked, name it anyway; the
tool needs the argument regardless.

## JIDs

A chat id, not a phone number:

| Shape | Meaning |
|---|---|
| `5521999999999@s.whatsapp.net` | 1:1 chat — country code + number, no `+`, no spaces |
| `<id>@g.us` | group |

**Groups are not handled.** The connector skips `@g.us` inbound and there is no
group send path — the same boundary the monolith drew. If a task needs groups,
say so rather than trying.

Get the JID from `whatsapp_list_conversations` (it carries the contact name)
rather than building one from a phone number you were told. A number that isn't
on WhatsApp yields a JID that accepts a send and delivers nothing.

## Before you send

1. **15-second minimum gap between sends on the same account**, enforced
   server-side — a call inside the window waits it out rather than failing. This
   exists because two identical-looking messages fired back-to-back to fresh
   numbers got a number instantly logged out on 2026-07-07. Design *for* it: if
   a task implies five messages, that is over a minute of wall clock, and
   batching them into one message is usually the better answer anyway.
2. **The exact same text twice in a row is refused.** That is the ban pattern,
   caught at the door. Rephrase — **never retry the same string.**
3. **A send is not undoable.** There is no delete-for-everyone here.
4. **Don't message people the user didn't name.** "Tell everyone" against a
   contact list is the fastest way to get the number banned, and it is the
   user's personal number.

## Reading

`whatsapp_list_conversations` and `whatsapp_read_messages` come from a **rolling
in-memory index**: WhatsApp replays a backlog when a device links, and the
connector keeps ~200 messages per chat since it last started. It is not the
phone's full history, and a connector restart loses it (contacts do persist).

"I can't find that conversation" therefore means *not seen since the connector
last started*, not *doesn't exist*. Say that — reporting the chat as missing is
wrong and the user will believe you.

Media messages carry a `mediaType`; `whatsapp_download_media` writes the file
locally and returns the path. Only works while the message is still cached.

## When nothing is connected

`whatsapp_status` distinguishes four states, and they are different problems:

| State | Means | What to do |
|---|---|---|
| installing the connector | first run, npm fetching Baileys | wait ~a minute |
| connector not running | the Node service is down | `aw-workspace-cli logs whatsapp`, then **Start** in Apps → WhatsApp |
| waiting for a QR scan | account unpaired | **only the user can fix this** |
| connecting | reconnecting, or auth was wiped | wait a few seconds, re-check |

You cannot pair an account. When one needs a QR, tell the user to open
**Apps → WhatsApp**, hit **Show QR** on that account, and scan it from the phone
(WhatsApp → Settings → Linked devices → Link a device). Don't offer to "try
again" — there is nothing on this side to retry.

**Never re-link or delete an account on your own initiative.** Both unlink a
real device from the user's phone; delete also drops its local history. Those
are the user's buttons in the panel, and there is deliberately no tool for
either.

## Read receipts

Off by default, and the reason matters: a blue tick tells the sender **a human
read this**, which is untrue for an agent-handled message. It's a per-workspace
opt-in in the app's settings. Don't suggest turning it on to make replies "feel
faster".

## REST, if you need it

Everything above is also `/api/apps/whatsapp/...`, account-scoped the same way
(`/accounts/{id}/send`, `/accounts/{id}/messages?jid=…`). Use it for things the
tools deliberately don't expose — the pairing panel is at `/panel`, `/state`
drives it. Prefer the tools; the REST surface has no account-resolution or
guard-rails.

## Where things live

- App repo: `repos/aw-app-whatsapp` · installed at `apps/whatsapp/`
- Durable data: `<AW_WORKSPACE_HOME>/data/whatsapp/` — `accounts.json`, plus
  `accounts/<id>/auth/` (Baileys creds — **deleting this costs a QR re-scan**),
  `contacts.json`, `media/`
- Node connector: `…/data/whatsapp/connector-runtime/`, run as the managed
  service `whatsapp/connector` on `127.0.0.1:9310`
- MCP: in-process at `POST /api/apps/whatsapp/mcp`, registered with the gateway
  by `whatsapp_app/self_register.py`. If the tools vanish, that upstream is what
  to check — `aw-workspace-cli restart mcp-gateway` after a reinstall.
- Ported from the monolith's `src/whatsapp_connector/`, `src/whatsapp/routes.py`
  and `src/mcp/whatsapp.py`
