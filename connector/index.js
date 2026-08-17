// aw-app-whatsapp connector — one Node process, N linked WhatsApp accounts.
//
// The monolith ran one process per account by construction: its socket, its
// state and its throttle were all module-level (agentic-workspace/src/
// whatsapp_connector/index.js). Adding a second account there meant a second
// service, a second port and a second Postgres database. Here an account is an
// `Account` instance (account.js) and this file is just a registry + an HTTP
// surface over it, so "add another WhatsApp" is one POST.
//
// Bound to 127.0.0.1 with no auth of its own: the only caller is the workspace
// process (whatsapp_app/routes.py), which sits behind the runtime's
// IdentityGuard. Nothing here is reachable from outside the workspace.
import { createServer } from "node:http";
import { mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Account } from "./account.js";

// Config arrives as argv, not env: the workspace's ServiceSupervisor launches a
// managed service as a bare command line inheriting the parent environment
// (aw-workspace src/apps/services.py), so there is nowhere to inject per-service
// env vars. Flags also mean `aw-workspace-cli` service status shows the actual
// configuration rather than an opaque `node index.js`.
function flag(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const PORT = Number(flag("port", process.env.AW_WHATSAPP_PORT || 9310));
const DATA_DIR = flag("data-dir", process.env.AW_WHATSAPP_DATA_DIR || "/tmp/aw-whatsapp");
const ACCOUNTS_FILE = join(DATA_DIR, "accounts.json");
const ACCOUNTS_DIR = join(DATA_DIR, "accounts");

mkdirSync(ACCOUNTS_DIR, { recursive: true });

// Applied to every account; refreshed by POST /config when the user saves the
// app's settings, so changing the throttle doesn't drop a live connection.
const opts = {
  minSendIntervalMs: Number(flag("min-send-interval-ms", 15_000)),
  markRead: flag("mark-read", "0") === "1",
};

/** @type {Map<string, Account>} */
const accounts = new Map();

function loadMeta() {
  try {
    const parsed = JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8"));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveMeta() {
  const rows = [...accounts.values()].map((a) => a.meta);
  writeFileSync(ACCOUNTS_FILE, JSON.stringify(rows, null, 2));
}

function accountDir(id) {
  return join(ACCOUNTS_DIR, id);
}

async function bootAccount(meta) {
  const dir = accountDir(meta.id);
  mkdirSync(dir, { recursive: true });
  const account = new Account(meta, dir, opts);
  accounts.set(meta.id, account);
  if (meta.enabled !== false) {
    // Never let one broken account take the whole connector down — an account
    // whose auth folder is corrupt should show an error in Settings, not stop
    // the other accounts from connecting.
    account.start().catch((err) => {
      account.lastError = String(err);
      console.error(`[${meta.id}] failed to start:`, err);
    });
  }
  return account;
}

for (const meta of loadMeta()) {
  bootAccount(meta).catch((err) => console.error("boot failed:", err));
}
console.log(`aw-whatsapp connector: ${accounts.size} account(s) restored from ${ACCOUNTS_FILE}`);

// ── HTTP helpers ───────────────────────────────────────────────────────────
function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      try { resolve(body ? JSON.parse(body) : {}); } catch (err) { reject(err); }
    });
    req.on("error", reject);
  });
}

function json(res, code, payload) {
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(JSON.stringify(payload));
}

function sendError(res, err) {
  if (err?.isDuplicate) return json(res, 409, { error: err.message });
  if (err?.notConnected) return json(res, 503, { error: err.message });
  if (err?.notFound) return json(res, 404, { error: err.message });
  return json(res, 500, { error: String(err?.message || err) });
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const path = url.pathname;
  const method = req.method;

  try {
    if (path === "/health") return json(res, 200, { status: "ok", accounts: accounts.size });

    if (path === "/config" && method === "POST") {
      const body = await readJsonBody(req);
      if (typeof body.min_send_interval_ms === "number") {
        opts.minSendIntervalMs = body.min_send_interval_ms;
      }
      if (typeof body.mark_read === "boolean") opts.markRead = body.mark_read;
      for (const a of accounts.values()) Object.assign(a.opts, opts);
      return json(res, 200, { ok: true, ...opts });
    }

    if (path === "/accounts" && method === "GET") {
      return json(res, 200, { accounts: [...accounts.values()].map((a) => a.status()) });
    }

    if (path === "/accounts" && method === "POST") {
      const body = await readJsonBody(req);
      const label = (body.label || "").trim();
      if (!label) return json(res, 400, { error: "label is required" });
      const meta = {
        id: body.id || randomUUID().slice(0, 8),
        label,
        webhook_url: body.webhook_url || null,
        enabled: true,
        created_at: Date.now(),
      };
      if (accounts.has(meta.id)) return json(res, 409, { error: "account id already exists" });
      const account = await bootAccount(meta);
      saveMeta();
      return json(res, 201, account.status());
    }

    // Everything below is /accounts/<id>/...
    const m = path.match(/^\/accounts\/([^/]+)(\/.*)?$/);
    if (!m) return json(res, 404, { error: "not found" });
    const account = accounts.get(m[1]);
    if (!account) return json(res, 404, { error: `no such account: ${m[1]}` });
    const sub = m[2] || "";

    if (sub === "" && method === "GET") return json(res, 200, account.status());

    if (sub === "" && method === "PATCH") {
      const body = await readJsonBody(req);
      if (typeof body.label === "string" && body.label.trim()) account.meta.label = body.label.trim();
      if ("webhook_url" in body) account.meta.webhook_url = body.webhook_url || null;
      saveMeta();
      return json(res, 200, account.status());
    }

    if (sub === "" && method === "DELETE") {
      await account.stop();
      accounts.delete(account.id);
      saveMeta();
      // The auth folder goes too — leaving it behind means "delete" silently
      // keeps a linked device alive on the user's phone.
      rmSync(accountDir(account.id), { recursive: true, force: true });
      return json(res, 200, { ok: true });
    }

    if (sub === "/qr" && method === "GET") {
      if (!account.qr) {
        return json(res, 404, {
          error: account.connected
            ? "already connected — no pairing QR needed"
            : "no pairing QR yet; the account is still connecting",
        });
      }
      const png = Buffer.from(account.qr.split(",")[1], "base64");
      res.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "no-store" });
      return res.end(png);
    }

    if (sub === "/relink" && method === "POST") {
      await account.relink();
      return json(res, 200, account.status());
    }

    if (sub === "/start" && method === "POST") {
      account.meta.enabled = true;
      saveMeta();
      if (!account.connected) await account.start();
      return json(res, 200, account.status());
    }

    if (sub === "/stop" && method === "POST") {
      account.meta.enabled = false;
      saveMeta();
      await account.stop();
      return json(res, 200, account.status());
    }

    if (sub === "/send" && method === "POST") {
      const { jid, text } = await readJsonBody(req);
      if (!jid || !text) return json(res, 400, { error: "jid and text are required" });
      await account.sendText(jid, text);
      return json(res, 200, { ok: true });
    }

    if (sub === "/send-media" && method === "POST") {
      const { jid, file_path, caption, mimetype } = await readJsonBody(req);
      if (!jid || !file_path) return json(res, 400, { error: "jid and file_path are required" });
      await account.sendMedia(jid, file_path, caption, mimetype);
      return json(res, 200, { ok: true });
    }

    if (sub === "/chats" && method === "GET") {
      return json(res, 200, { chats: account.listChats() });
    }

    if (sub === "/messages" && method === "GET") {
      const jid = url.searchParams.get("jid");
      if (!jid) return json(res, 400, { error: "jid query param is required" });
      return json(res, 200, { jid, messages: account.listMessages(jid, url.searchParams.get("limit")) });
    }

    if (sub === "/media" && method === "GET") {
      const messageId = url.searchParams.get("message_id");
      if (!messageId) return json(res, 400, { error: "message_id query param is required" });
      const out = await account.downloadMedia(messageId);
      return json(res, 200, { ok: true, ...out });
    }

    return json(res, 404, { error: "not found" });
  } catch (err) {
    return sendError(res, err);
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`aw-whatsapp connector listening on 127.0.0.1:${PORT} (data: ${DATA_DIR})`);
});

for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, async () => {
    console.log(`aw-whatsapp connector: ${sig} — closing ${accounts.size} socket(s)`);
    await Promise.allSettled([...accounts.values()].map((a) => a.stop()));
    process.exit(0);
  });
}
