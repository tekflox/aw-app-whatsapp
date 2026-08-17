"""The accounts + QR pairing panel, served into the Settings window's ``iframe``
widget (``windows/main.json``).

Why an iframe and not declarative widgets: aw-workspace-ui's declarative
renderer supports ``markdown``, ``list``, ``button``, ``iframe``, ``app_iframe``,
``collapsible``, ``form`` and ``auth_status`` — its ``list`` takes STATIC items
from the spec, there's no data-bound list and no image widget at all. A pairing
QR is a live image that changes every ~20 seconds until it's scanned, next to a
live list of accounts. ``iframe { src: "/api/*" }`` is the vocabulary's own
escape hatch for exactly that, and ``apiUrl()`` rewrites the src to the
workspace API origin — the same origin this page's own fetches (and the ``<img>``
QR request) go to, so the apex ``aw_id_jwt`` cookie authorises them with no
token plumbing.

**Layout constraint:** the host renders this in ``.appwin-iframe``, a
``min-height: 320px`` box inside the Settings sidebar — narrow and short. One
stacked card per account, and the QR sized to fit the column rather than a
fixed 264px that would clip.

Deliberately dependency-free — no build step, no framework. Colours come from
the host's ``--color-*`` variables with rgba fallbacks so it reads correctly in
both themes.
"""
from __future__ import annotations

PANEL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp accounts</title>
<style>
  :root {
    color-scheme: dark light;
    --accent: var(--color-accent, #25d366);
    --line: var(--color-border, rgba(128,128,128,.28));
    --muted: var(--color-text-muted, #64748b);
    --panel: rgba(128,128,128,.06);
  }
  * { box-sizing: border-box; }
  /* The gutter has to live HERE: the host renders this page cross-origin, so
     none of its stylesheets reach inside, and padding on the <iframe> element
     only shifts the origin and clips the right edge. */
  body { margin: 0; padding: 12px; background: transparent; color: inherit;
         font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }

  .bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
         padding-bottom: 10px; border-bottom: 1px solid var(--line); }
  .bar .grow { flex: 1; min-width: 0; }
  .dot { width: 7px; height: 7px; border-radius: 50%; flex: none;
         background: var(--muted); }
  .dot.on { background: #22c55e; }
  .dot.warn { background: #f59e0b; }
  .dot.off { background: #f87171; }
  .sub { font-size: 11px; color: var(--muted); }

  .card { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
          margin-bottom: 8px; background: var(--panel); }
  .card-top { display: flex; align-items: center; gap: 8px; }
  .name { font-weight: 600; flex: 1; min-width: 0;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tag { font-size: 10px; font-weight: 600; letter-spacing: .04em; flex: none;
         padding: 2px 6px; border-radius: 4px;
         background: rgba(128,128,128,.16); color: var(--muted); }
  .tag.ok   { background: rgba(34,197,94,.16);  color: #22c55e; }
  .tag.warn { background: rgba(245,158,11,.16); color: #f59e0b; }
  .tag.err  { background: rgba(248,113,113,.15); color: #f87171; }
  .meta { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: 11px; color: var(--muted); margin-top: 4px;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .actions { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }

  button { font: inherit; font-size: 11px; font-weight: 500; padding: 4px 10px;
           border-radius: 6px; border: 1px solid var(--line);
           background: transparent; color: inherit; cursor: pointer;
           transition: background .12s, border-color .12s, color .12s; }
  button:hover:not(:disabled) { background: rgba(128,128,128,.16);
                                border-color: rgba(128,128,128,.45); }
  button:disabled { opacity: .5; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent);
                   color: #04240f; font-weight: 600; }
  button.primary:hover:not(:disabled) { filter: brightness(1.08); }
  button.danger:hover:not(:disabled) { background: rgba(248,113,113,.14);
                        border-color: rgba(248,113,113,.45); color: #f87171; }

  .qr { margin-top: 10px; padding: 10px; border-radius: 8px; background: #fff;
        text-align: center; }
  .qr img { width: 100%; max-width: 230px; height: auto; display: block; margin: 0 auto;
            image-rendering: pixelated; }
  .qr-help { font-size: 11px; color: var(--muted); margin-top: 8px; line-height: 1.5; }
  .qr-help ol { margin: 4px 0 0; padding-left: 18px; }

  h4 { margin: 16px 0 8px; font-size: 11px; text-transform: uppercase;
       letter-spacing: .05em; color: var(--muted); font-weight: 600; }
  input { font: inherit; font-size: 12px; padding: 6px 8px; border-radius: 6px;
          border: 1px solid var(--line); background: rgba(128,128,128,.08);
          color: inherit; width: 100%; }
  .row { display: flex; gap: 6px; align-items: center; }
  .row input { flex: 1; min-width: 0; }
  .empty { color: var(--muted); font-size: 12px; padding: 6px 0 2px; }
  .err { color: #f87171; font-size: 12px; margin: 6px 0; white-space: pre-wrap; }
  .spin { display: inline-block; width: 11px; height: 11px; border-radius: 50%;
          border: 2px solid rgba(128,128,128,.3); border-top-color: var(--muted);
          animation: sp .7s linear infinite; vertical-align: -1px; }
  @keyframes sp { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div id="bar" class="bar"><span class="dot"></span><span class="grow sub">Loading…</span></div>
<div id="err" class="err" hidden></div>
<div id="list"></div>

<h4>Add an account</h4>
<div class="row">
  <input id="new-label" placeholder="e.g. Personal, Store line" maxlength="40">
  <button id="add" class="primary">Add</button>
</div>
<div class="sub" style="margin-top:6px">
  A new account starts disconnected and shows a QR straight away — scan it with
  the phone that owns that number.
</div>

<script>
const BASE = '/api/apps/whatsapp';
const $ = (id) => document.getElementById(id);
// Which account's QR is expanded. Auto-set for a freshly added account so the
// common path (add → scan) is one click, not two.
let openQr = null;
let busy = false;

async function api(path, init) {
  const res = await fetch(BASE + path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  const text = await res.text();
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch { body = { detail: text }; }
  if (!res.ok) throw new Error(body.detail || body.error || ('HTTP ' + res.status));
  return body;
}

function showErr(msg) {
  const el = $('err');
  el.hidden = !msg;
  el.textContent = msg || '';
}

function badge(a) {
  if (!a.enabled) return ['tag', 'Off'];
  if (a.connected) return ['tag ok', 'Connected'];
  if (a.has_qr) return ['tag warn', 'Scan QR'];
  if (a.last_error) return ['tag err', 'Error'];
  return ['tag', 'Connecting…'];
}

function renderBar(state) {
  const bar = $('bar');
  const dot = state.connector_running ? (state.provisioning ? 'dot warn' : 'dot on')
            : (state.provisioning ? 'dot warn' : 'dot off');
  let text;
  if (state.provisioning) text = 'Installing the WhatsApp connector… (first run, ~1 min)';
  else if (state.connector_running) {
    const n = (state.accounts || []).filter((a) => a.connected).length;
    text = n + ' of ' + (state.accounts || []).length + ' account(s) connected';
  } else text = state.setup_error || 'Connector stopped';

  bar.innerHTML = '';
  const d = document.createElement('span'); d.className = dot; bar.appendChild(d);
  const s = document.createElement('span'); s.className = 'grow sub'; s.textContent = text;
  bar.appendChild(s);
  if (!state.connector_running && !state.provisioning) {
    const b = document.createElement('button');
    b.textContent = 'Start';
    b.onclick = () => act(() => api('/service/start', { method: 'POST' }));
    bar.appendChild(b);
  }
}

function renderAccounts(state) {
  const list = $('list');
  list.innerHTML = '';
  const accounts = state.accounts || [];
  if (!accounts.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = state.connector_running
      ? 'No accounts yet. Add one below to get a pairing QR.'
      : 'Start the connector to see your accounts.';
    list.appendChild(p);
    return;
  }

  for (const a of accounts) {
    const card = document.createElement('div');
    card.className = 'card';

    const top = document.createElement('div');
    top.className = 'card-top';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = a.label;
    const [cls, label] = badge(a);
    const tag = document.createElement('span');
    tag.className = cls; tag.textContent = label;
    top.append(name, tag);
    card.appendChild(top);

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = a.phone ? '+' + a.phone : (a.last_error || 'not linked yet');
    card.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'actions';

    if (!a.connected && a.enabled) {
      const qr = document.createElement('button');
      qr.className = openQr === a.id ? '' : 'primary';
      qr.textContent = openQr === a.id ? 'Hide QR' : 'Show QR';
      qr.onclick = () => { openQr = openQr === a.id ? null : a.id; refresh(); };
      actions.appendChild(qr);
    }
    if (a.connected) {
      const rl = document.createElement('button');
      rl.textContent = 'Re-link';
      rl.title = 'Unlink this device and show a fresh QR';
      rl.onclick = () => {
        if (!confirm('Unlink "' + a.label + '" and show a new QR?')) return;
        openQr = a.id;
        act(() => api('/accounts/' + a.id + '/relink', { method: 'POST' }));
      };
      actions.appendChild(rl);
    }
    const toggle = document.createElement('button');
    toggle.textContent = a.enabled ? 'Pause' : 'Resume';
    toggle.onclick = () => act(() =>
      api('/accounts/' + a.id + (a.enabled ? '/stop' : '/start'), { method: 'POST' }));
    actions.appendChild(toggle);

    const del = document.createElement('button');
    del.className = 'danger';
    del.textContent = 'Remove';
    del.onclick = () => {
      if (!confirm('Remove "' + a.label + '"? This unlinks it and deletes its local history.')) return;
      if (openQr === a.id) openQr = null;
      act(() => api('/accounts/' + a.id, { method: 'DELETE' }));
    };
    actions.appendChild(del);
    card.appendChild(actions);

    if (openQr === a.id) card.appendChild(qrBlock(a));
    list.appendChild(card);
  }
}

function qrBlock(a) {
  const box = document.createElement('div');
  if (!a.has_qr) {
    box.className = 'qr-help';
    box.textContent = a.connected
      ? 'Already linked — nothing to scan.'
      : 'Waiting for WhatsApp to hand out a pairing code…';
    const s = document.createElement('span');
    s.className = 'spin'; s.style.marginLeft = '6px';
    if (!a.connected) box.appendChild(s);
    return box;
  }
  box.className = 'qr';
  const img = document.createElement('img');
  // Cache-buster keyed to the QR's own generation time: WhatsApp rotates the
  // code every ~20s and a cached <img> would show an expired one that simply
  // never works, with nothing on screen saying so.
  img.src = BASE + '/accounts/' + a.id + '/qr.png?v=' + (a.qr_generated_at || Date.now());
  img.alt = 'WhatsApp pairing QR';
  box.appendChild(img);

  const help = document.createElement('div');
  help.className = 'qr-help';
  help.innerHTML = 'On the phone that owns this number:<ol>' +
    '<li>WhatsApp → <b>Settings</b> → <b>Linked devices</b></li>' +
    '<li><b>Link a device</b></li>' +
    '<li>Point the camera here</li></ol>';
  box.appendChild(help);
  return box;
}

async function act(fn) {
  if (busy) return;
  busy = true;
  showErr('');
  try {
    await fn();
  } catch (e) {
    showErr(String(e.message || e));
  } finally {
    busy = false;
    refresh();
  }
}

let timer = null;
async function refresh() {
  try {
    const state = await api('/state');
    showErr('');
    renderBar(state);
    renderAccounts(state);
    // Poll fast while something is in flight (a QR rotating, an account
    // connecting, npm still running), slowly once everything is settled.
    const live = state.provisioning ||
      (state.accounts || []).some((a) => a.enabled && !a.connected);
    schedule(live ? 3000 : 15000);
  } catch (e) {
    showErr(String(e.message || e));
    schedule(5000);
  }
}

function schedule(ms) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(refresh, ms);
}

$('add').onclick = () => {
  const label = $('new-label').value.trim();
  if (!label) { showErr('Give the account a name first.'); return; }
  act(async () => {
    const created = await api('/accounts', {
      method: 'POST', body: JSON.stringify({ label }),
    });
    $('new-label').value = '';
    openQr = created.id;   // straight to the QR — that's why they clicked Add
  });
};
$('new-label').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('add').click();
});

refresh();
</script>
</body>
</html>
"""
