---
name: aw-whatsapp
description: >-
  Read and send messages on the WhatsApp accounts linked to this workspace by
  the aw-app-whatsapp app (personal accounts over the WhatsApp Web protocol via
  Baileys — not the Cloud API, not a business number). Several accounts can be
  linked at once, so every call is account-scoped. Use whenever a task involves
  sending a WhatsApp message, reading a conversation, downloading media someone
  sent, or checking whether a number is still paired — and read the send rules
  below BEFORE sending anything, because the wrong pattern gets the number
  banned by WhatsApp, not rate-limited.
---

# aw-whatsapp — talking to a linked WhatsApp account

`aw-app-whatsapp` links **personal** WhatsApp accounts to this workspace the
same way WhatsApp Web does: the user scans a QR in **Apps → WhatsApp** (or
Settings → WhatsApp) with the phone that owns the number, and this workspace
becomes a linked device on it.

That is the mental model to hold: **you are a linked device on someone's real
phone.** Not a bot API, not a business channel. Every message you send is from
their number, shows in their chat history, and is subject to WhatsApp's own
anti-spam enforcement — which bans, it does not throttle.

## Several accounts, so nothing is implicit

Unlike the monolith this was ported from, there is **no default account**.
Every route takes an `account_id`:

```
GET  /api/apps/whatsapp/status                                  → all accounts, compact
GET  /api/apps/whatsapp/accounts                                → all accounts, full
GET  /api/apps/whatsapp/accounts/{id}/chats
GET  /api/apps/whatsapp/accounts/{id}/messages?jid=…&limit=50
POST /api/apps/whatsapp/accounts/{id}/send          {jid, text}
POST /api/apps/whatsapp/accounts/{id}/send-media    {jid, file_path, caption?}
GET  /api/apps/whatsapp/accounts/{id}/media?message_id=…
```

**Always call `/status` first** and pick the account deliberately. If more than
one is connected and the task doesn't say which, ask — "send a WhatsApp to
Maria" from a workspace with a personal number and a store number is ambiguous,
and sending from the wrong one is not undoable.

`id` is a short opaque string (e.g. `a3f9c1d2`); `label` is what the user named
it ("Personal", "Store line"). Match on the label when a human names an
account, never guess by position.

## JIDs

A chat id, not a phone number:

| Shape | Meaning |
|---|---|
| `5521999999999@s.whatsapp.net` | 1:1 chat — country code + number, no `+`, no spaces |
| `<id>@g.us` | group |

**Groups are not handled.** The connector skips `@g.us` on the way in and the
app has no group send path — same boundary the monolith drew. If a task needs
groups, say so rather than trying.

Get the JID from `/chats` (it carries the contact name) rather than building one
from a phone number you were told — a number that isn't on WhatsApp produces a
JID that accepts a send and delivers nothing.

## Before you send — read this

1. **There is a 15-second minimum gap between sends on the same account**, and
   it is enforced server-side (a call inside the window waits it out, it does
   not error). This exists because two identical-looking messages fired
   back-to-back to fresh numbers got the monolith's number instantly logged out
   on 2026-07-07. Don't design around it — design *for* it.
2. **Sending the exact same text twice in a row is refused** (`409`). That is
   the ban pattern, caught at the door. Rephrase; don't retry.
3. **A send is not undoable.** There is no delete-for-everyone here.
4. **Don't message people the user didn't name.** "Tell everyone" against a
   contact list is the single fastest way to get the number banned, and it is
   the user's personal number.

## Reading

`/chats` and `/messages` come from a **rolling in-memory index** — WhatsApp
replays a backlog when a device links, and the connector keeps ~200 messages
per chat since it last started. It is not the phone's full history, and a
restart loses it (contacts do persist).

So: "I can't find that conversation" usually means *not seen since the last
connector start*, not *doesn't exist*. Say that, rather than reporting the
chat as missing.

Media (`mediaType` on a message) needs `/media?message_id=…` to download; it
writes the file locally and returns the path. Only works while the message is
still in that in-memory cache.

## When nothing is connected

Check in this order — the states are different problems and the panel
distinguishes them:

| `/status` says | Means | Fix |
|---|---|---|
| `provisioning: true` | first install, npm is still fetching Baileys | wait ~a minute |
| `connector_running: false` | the Node service is down | `aw-workspace-cli logs whatsapp`, then **Start** in the panel |
| account `has_qr: true` | waiting to be paired | **only the user can fix this** — they scan the QR in Apps → WhatsApp |
| account `connected: false`, no QR | reconnecting, or auth was wiped | wait a few seconds, then re-check |

You cannot pair an account yourself. When an account needs a QR, tell the user
to open **Apps → WhatsApp**, hit **Show QR** on that account, and scan it from
the phone (WhatsApp → Settings → Linked devices → Link a device). Don't offer
to "try again" — there is nothing on this side to retry.

**Never call `/relink` or `DELETE /accounts/{id}` on your own initiative.**
Both unlink a real device from the user's phone; `DELETE` also drops its local
history. Those are the user's buttons in the panel.

## Read receipts

Off by default, and the reason matters: a blue tick tells the sender **a human
read this**, which is untrue for an agent-handled message. It's a per-workspace
opt-in in the app's settings. Don't suggest turning it on to "make replies feel
faster".

## Inbound

An account can carry a `webhook_url`; the connector POSTs each inbound 1:1 text
message there (`{account_id, jid, text, message_id, push_name, contact_name,
timestamp}`) and honours a `{"mark_read": true}` in the reply. Nothing is wired
to an agent by default — inbound routing is a workspace-level decision, not
something to switch on because a task would find it convenient.

## Where things live

- App repo: `repos/aw-app-whatsapp` · installed at `apps/whatsapp/`
- Durable data: `<AW_WORKSPACE_HOME>/data/whatsapp/` — `accounts.json`, plus
  `accounts/<id>/auth/` (Baileys creds — **deleting this costs a QR re-scan**),
  `contacts.json`, `media/`
- Node connector: `<AW_WORKSPACE_HOME>/data/whatsapp/connector-runtime/`, run as
  the managed service `whatsapp/connector` on `127.0.0.1:9310`
- Ported from the monolith's `src/whatsapp_connector/` + `src/whatsapp/routes.py`
