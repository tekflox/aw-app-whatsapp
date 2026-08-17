// One linked WhatsApp account = one Baileys socket, its own auth folder, its
// own rolling chat index and its own send throttle.
//
// Ported from the monolith's single-account connector
// (agentic-workspace/src/whatsapp_connector/index.js) with two deliberate
// changes:
//
//   1. **N accounts per process.** The monolith held exactly one socket in
//      module scope. Everything that was a module-level `const` there —
//      `state`, `chats`, `messagesByChat`, `contactNames`, the throttle file —
//      is an instance field here, so adding a second account is opening a
//      second socket rather than running a second copy of the service.
//   2. **Auth on disk, not in Postgres.** The monolith persisted Baileys creds
//      into a `whatsapp_connector` Postgres database because its own data dir
//      was container-scratch. An app's `fs:workspace-data` dir lives under
//      AW_WORKSPACE_HOME, which is host-mounted and survives container
//      recreation, so Baileys' own `useMultiFileAuthState` is enough and the
//      app carries no database dependency. Losing this folder means re-scanning
//      the QR — the same blast radius the Postgres table had.
import { mkdirSync, readFileSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { join, extname } from "node:path";
import QRCode from "qrcode";
import {
  default as makeWASocket,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import pino from "pino";

const MAX_MESSAGES_PER_CHAT = 200;
const MAX_RAW_CACHE = 500;
const FORWARD_TIMEOUT_MS = 10_000;

const EXT_MIME = {
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
  ".gif": "image/gif", ".webp": "image/webp",
  ".mp4": "video/mp4", ".mov": "video/quicktime",
  ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
  ".pdf": "application/pdf",
};

export function guessMime(filePath) {
  return EXT_MIME[extname(filePath).toLowerCase()] || "application/octet-stream";
}

export class DuplicateMessageError extends Error {
  constructor(text) {
    super(
      `Refusing to send — identical to the last message sent on this account ` +
      `(${JSON.stringify(text)}). Sending the same text twice in a row is exactly ` +
      `the pattern that got a WhatsApp number logged out on 2026-07-07. Rephrase first.`
    );
    this.isDuplicate = true;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function mediaTypeOf(message) {
  if (!message) return null;
  if (message.imageMessage) return "image";
  if (message.videoMessage) return "video";
  if (message.audioMessage) return "audio";
  if (message.documentMessage) return "document";
  if (message.stickerMessage) return "sticker";
  return null;
}

function extractText(message) {
  if (!message) return null;
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    null
  );
}

export class Account {
  // `meta` is the persisted row from accounts.json: { id, label, webhook_url,
  // enabled, created_at }. `opts.minSendIntervalMs` / `opts.markRead` come from
  // the app's config and are re-read on every save, so a settings change
  // doesn't need a reconnect.
  constructor(meta, dir, opts = {}) {
    this.meta = meta;
    this.dir = dir;
    this.authDir = join(dir, "auth");
    this.mediaDir = join(dir, "media");
    this.contactsFile = join(dir, "contacts.json");
    this.lastSendFile = join(dir, "last_send.json");
    this.opts = { minSendIntervalMs: 15_000, markRead: false, ...opts };

    this.connected = false;
    this.qr = null;              // data: URL (PNG) of the pending pairing QR
    this.qrGeneratedAt = null;
    this.selfJid = null;
    this.lastDisconnectReason = null;
    this.lastError = null;
    this.stopping = false;
    this.sock = null;

    this.chats = new Map();          // jid -> { jid, name, lastMessageAt, lastMessageText }
    this.messagesByChat = new Map(); // jid -> [entry]
    this.rawMessageById = new Map(); // id  -> { key, message, jid }
    this.contactNames = new Map();   // jid -> best-known display name
    this.sendQueueTail = Promise.resolve();

    mkdirSync(this.mediaDir, { recursive: true });
    this.#loadContacts();
  }

  get id() { return this.meta.id; }

  status() {
    return {
      id: this.meta.id,
      label: this.meta.label,
      enabled: this.meta.enabled !== false,
      webhook_url: this.meta.webhook_url || null,
      connected: this.connected,
      has_qr: Boolean(this.qr),
      qr_generated_at: this.qrGeneratedAt,
      self_jid: this.selfJid,
      phone: this.selfJid ? this.selfJid.split(":")[0].split("@")[0] : null,
      last_disconnect_reason: this.lastDisconnectReason,
      last_error: this.lastError,
      linked: existsSync(join(this.authDir, "creds.json")),
      chat_count: this.chats.size,
    };
  }

  // ── contacts (persisted; conversation content deliberately is not) ───────
  #loadContacts() {
    try {
      const raw = JSON.parse(readFileSync(this.contactsFile, "utf8"));
      for (const [jid, name] of Object.entries(raw)) this.contactNames.set(jid, name);
    } catch { /* first run */ }
  }

  #saveContactsSoon() {
    if (this.contactsTimer) return;
    this.contactsTimer = setTimeout(() => {
      this.contactsTimer = null;
      try {
        writeFileSync(this.contactsFile, JSON.stringify(Object.fromEntries(this.contactNames)));
      } catch (err) {
        console.error(`[${this.id}] failed to persist contacts:`, err);
      }
    }, 2000);
    this.contactsTimer.unref?.();
  }

  #upsertContactName(jid, { name, notify, verifiedName } = {}) {
    const best = name || verifiedName || notify;
    if (!jid || !best) return;
    if (this.contactNames.get(jid) === best) return;
    this.contactNames.set(jid, best);
    const existing = this.chats.get(jid);
    if (existing) this.chats.set(jid, { ...existing, name: best });
    this.#saveContactsSoon();
  }

  // ── outbound throttle ────────────────────────────────────────────────────
  // Two identical-looking messages fired back-to-back to fresh numbers got the
  // monolith's account instantly logged out by WhatsApp's spam detection
  // (2026-07-07). The last-send timestamp lives in a file so the throttle
  // survives a connector restart, not just an in-memory var.
  #readLastSend() {
    try {
      const parsed = JSON.parse(readFileSync(this.lastSendFile, "utf8"));
      return { ts: Number(parsed.ts) || 0, text: parsed.text ?? null };
    } catch {
      return { ts: 0, text: null };
    }
  }

  #throttle(text) {
    const result = this.sendQueueTail.then(async () => {
      const last = this.#readLastSend();
      if (text && last.text && text === last.text) throw new DuplicateMessageError(text);
      const elapsed = Date.now() - last.ts;
      if (elapsed < this.opts.minSendIntervalMs) {
        await sleep(this.opts.minSendIntervalMs - elapsed);
      }
      try {
        writeFileSync(this.lastSendFile, JSON.stringify({ ts: Date.now(), text: text ?? last.text }));
      } catch (err) {
        console.warn(`[${this.id}] failed to persist last_send:`, err);
      }
    });
    // Serialize sends so two concurrent callers can't both read the same stale
    // state and both slip through the window.
    this.sendQueueTail = result.catch(() => {});
    return result;
  }

  // ── lifecycle ────────────────────────────────────────────────────────────
  async start() {
    this.stopping = false;
    const { state, saveCreds } = await useMultiFileAuthState(this.authDir);
    const { version } = await fetchLatestBaileysVersion();

    const sock = makeWASocket({
      version,
      auth: state,
      logger: pino({ level: "silent" }),
      printQRInTerminal: false,
      syncFullHistory: true,
      // Baileys defaults this to true, which announces an "online" presence on
      // connect. WhatsApp then treats this as the active session and suppresses
      // push notifications on the PHONE for every chat, not just the ones an
      // agent handles. Keeping the phone as the only "online" device is what
      // restores normal notifications there.
      markOnlineOnConnect: false,
      getMessage: async (key) => this.rawMessageById.get(key.id)?.message,
    });
    this.sock = sock;

    sock.ev.on("creds.update", saveCreds);
    sock.ev.on("connection.update", (u) => this.#onConnectionUpdate(u));
    sock.ev.on("messages.upsert", (u) => this.#onMessages(u));
    sock.ev.on("contacts.upsert", (list) => {
      for (const c of list || []) this.#upsertContactName(c.id, c);
    });
    sock.ev.on("contacts.update", (list) => {
      for (const c of list || []) this.#upsertContactName(c.id, c);
    });
    sock.ev.on("messaging-history.set", (u) => this.#onHistory(u));
    return sock;
  }

  async stop() {
    this.stopping = true;
    this.connected = false;
    this.qr = null;
    try {
      this.sock?.end?.(undefined);
    } catch { /* already gone */ }
    this.sock = null;
  }

  // Wipe the pairing so the next start presents a fresh QR. `logout` first
  // tells WhatsApp to drop the device (so it disappears from Linked Devices on
  // the phone); if the socket is already dead we just delete the folder.
  async relink() {
    try {
      if (this.connected) await this.sock?.logout?.();
    } catch (err) {
      console.warn(`[${this.id}] logout failed, wiping local creds anyway:`, err?.message || err);
    }
    await this.stop();
    rmSync(this.authDir, { recursive: true, force: true });
    this.selfJid = null;
    this.lastDisconnectReason = null;
    this.lastError = null;
    await this.start();
  }

  async #onConnectionUpdate({ connection, lastDisconnect, qr }) {
    if (qr) {
      this.qr = await QRCode.toDataURL(qr);
      this.qrGeneratedAt = Date.now();
      this.connected = false;
      console.log(`[${this.id}] new pairing QR generated`);
    }

    if (connection === "open") {
      this.connected = true;
      this.qr = null;
      this.qrGeneratedAt = null;
      this.lastError = null;
      this.selfJid = this.sock?.user?.id || null;
      console.log(`[${this.id}] connected as ${this.selfJid}`);
    }

    if (connection === "close") {
      this.connected = false;
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      this.lastDisconnectReason = statusCode ?? null;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      if (this.stopping) return;
      if (loggedOut) {
        // The phone unlinked this device. Reconnecting with dead creds loops
        // forever — drop them so the next start shows a QR instead.
        console.log(`[${this.id}] logged out by WhatsApp — clearing creds, a new QR is needed`);
        rmSync(this.authDir, { recursive: true, force: true });
        this.selfJid = null;
        this.start().catch((err) => {
          this.lastError = String(err);
          console.error(`[${this.id}] restart after logout failed:`, err);
        });
        return;
      }
      console.log(`[${this.id}] disconnected (code=${statusCode}) — reconnecting…`);
      this.start().catch((err) => {
        this.lastError = String(err);
        console.error(`[${this.id}] reconnect failed:`, err);
      });
    }
  }

  #recordMessage(jid, msg, text) {
    const name = this.contactNames.get(jid) || msg.pushName || this.chats.get(jid)?.name || jid;
    if (msg.pushName && !this.contactNames.has(jid)) {
      this.#upsertContactName(jid, { notify: msg.pushName });
    }
    const timestamp = Number(msg.messageTimestamp) * 1000 || Date.now();
    const entry = {
      id: msg.key.id,
      fromMe: Boolean(msg.key.fromMe),
      timestamp,
      text: text || "",
      mediaType: mediaTypeOf(msg.message),
    };

    const list = this.messagesByChat.get(jid) || [];
    list.push(entry);
    if (list.length > MAX_MESSAGES_PER_CHAT) list.shift();
    this.messagesByChat.set(jid, list);

    this.chats.set(jid, {
      jid,
      name,
      lastMessageAt: timestamp,
      lastMessageText: text || (entry.mediaType ? `[${entry.mediaType}]` : ""),
    });

    this.rawMessageById.set(msg.key.id, { key: msg.key, message: msg.message, jid });
    if (this.rawMessageById.size > MAX_RAW_CACHE) {
      this.rawMessageById.delete(this.rawMessageById.keys().next().value);
    }
  }

  #onMessages({ messages, type }) {
    if (type !== "notify") return;
    for (const msg of messages) {
      const jid = msg.key.remoteJid;
      if (!jid || jid.endsWith("@g.us")) continue; // groups stay out, same as the monolith
      const text = extractText(msg.message);
      this.#recordMessage(jid, msg, text);

      if (msg.key.fromMe) continue;
      if (!text) continue;
      if (!this.meta.webhook_url) continue;
      this.#forward(jid, text, msg).catch((err) =>
        console.error(`[${this.id}] webhook forward failed:`, err)
      );
    }
  }

  // Backlog WhatsApp replays shortly after connecting. Recorded so
  // list_conversations/read_messages have something to show, never forwarded —
  // these are old messages, not new inbound.
  #onHistory({ chats: historyChats, contacts, messages: historyMessages }) {
    for (const c of contacts || []) {
      if (!c.id) continue;
      this.#upsertContactName(c.id, c);
      if (!this.chats.has(c.id)) {
        this.chats.set(c.id, { jid: c.id, name: this.contactNames.get(c.id) || c.id, lastMessageAt: 0, lastMessageText: "" });
      }
    }
    for (const c of historyChats || []) {
      if (!c.id || this.chats.has(c.id)) continue;
      this.chats.set(c.id, { jid: c.id, name: this.contactNames.get(c.id) || c.name || c.id, lastMessageAt: 0, lastMessageText: "" });
    }
    let count = 0;
    for (const msg of historyMessages || []) {
      const jid = msg.key?.remoteJid;
      if (!jid || jid.endsWith("@g.us") || !msg.message) continue;
      this.#recordMessage(jid, msg, extractText(msg.message));
      count++;
    }
    console.log(`[${this.id}] history sync: ${count} message(s), ${(contacts || []).length} contact(s)`);
  }

  async #forward(jid, text, msg) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FORWARD_TIMEOUT_MS);
    try {
      const res = await fetch(this.meta.webhook_url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: this.id,
          account_label: this.meta.label,
          jid,
          text,
          message_id: msg.key.id,
          push_name: msg.pushName || null,
          contact_name: this.contactNames.get(jid) || null,
          timestamp: Number(msg.messageTimestamp) * 1000 || Date.now(),
        }),
        signal: controller.signal,
      });
      if (!res.ok) {
        console.error(`[${this.id}] webhook returned ${res.status}: ${await res.text()}`);
        return;
      }
      // Read receipts are opt-in: a blue tick claims a human read the message.
      const body = await res.json().catch(() => ({}));
      if ((body.mark_read ?? this.opts.markRead) && this.sock) {
        await this.sock.readMessages([msg.key]).catch((err) =>
          console.error(`[${this.id}] failed to mark read:`, err)
        );
      }
    } finally {
      clearTimeout(timer);
    }
  }

  // ── outbound ─────────────────────────────────────────────────────────────
  #requireSocket() {
    if (!this.sock || !this.connected) {
      const err = new Error("account is not connected to WhatsApp");
      err.notConnected = true;
      throw err;
    }
  }

  async sendText(jid, text) {
    this.#requireSocket();
    await this.#throttle(text);
    await this.sock.sendMessage(jid, { text });
  }

  async sendMedia(jid, filePath, caption, mimeOverride) {
    this.#requireSocket();
    const buffer = readFileSync(filePath);
    const mimetype = mimeOverride || guessMime(filePath);
    await this.#throttle(caption);
    if (mimetype.startsWith("image/")) {
      await this.sock.sendMessage(jid, { image: buffer, caption });
    } else if (mimetype.startsWith("video/")) {
      await this.sock.sendMessage(jid, { video: buffer, caption });
    } else if (mimetype.startsWith("audio/")) {
      await this.sock.sendMessage(jid, { audio: buffer, mimetype, ptt: false });
    } else {
      await this.sock.sendMessage(jid, {
        document: buffer, mimetype, fileName: filePath.split("/").pop(), caption,
      });
    }
  }

  listChats() {
    return [...this.chats.values()].sort((a, b) => b.lastMessageAt - a.lastMessageAt);
  }

  listMessages(jid, limit) {
    const n = Math.max(1, Math.min(Number(limit) || 50, MAX_MESSAGES_PER_CHAT));
    return (this.messagesByChat.get(jid) || []).slice(-n);
  }

  async downloadMedia(messageId) {
    const raw = this.rawMessageById.get(messageId);
    if (!raw) {
      const err = new Error("no cached media for that message_id (only recent messages are kept in memory)");
      err.notFound = true;
      throw err;
    }
    const buffer = await downloadMediaMessage(
      { key: raw.key, message: raw.message },
      "buffer",
      {},
      { logger: pino({ level: "silent" }), reuploadRequest: this.sock?.updateMediaMessage }
    );
    const mediaType = mediaTypeOf(raw.message) || "file";
    const ext = { image: ".jpg", video: ".mp4", audio: ".ogg", document: "", sticker: ".webp" }[mediaType] || "";
    const outPath = join(this.mediaDir, `${messageId}${ext}`);
    writeFileSync(outPath, buffer);
    return { path: outPath, media_type: mediaType };
  }
}
