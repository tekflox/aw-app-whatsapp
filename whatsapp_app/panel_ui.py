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
  /* The armed half of the two-click confirm — filled, so "one more click and
     it's gone" is not something you have to read the label to notice.
     The :hover variant repeats the whole declaration on purpose: the plain
     `button.danger:hover:not(:disabled)` rule above is MORE specific than a
     bare `.danger.confirm`, so without this the armed button washes out to the
     pale hover style — and it is always hovered, because it appears exactly
     under the cursor that just clicked Remove. Caught in a screenshot. */
  button.danger.confirm,
  button.danger.confirm:hover:not(:disabled) {
    background: #f87171; border-color: #f87171; color: #2a0808; font-weight: 600; }
  button.danger.confirm:hover:not(:disabled) { filter: brightness(1.08); }

  .qr { margin-top: 10px; padding: 10px; border-radius: 8px; background: #fff;
        text-align: center; }
  .qr img { width: 100%; max-width: 230px; height: auto; display: block; margin: 0 auto;
            image-rendering: pixelated; }

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

<script>
const BASE = '/api/apps/whatsapp';
const $ = (id) => document.getElementById(id);
// A pending QR shows itself. An account only ever has one because it is waiting
// to be paired, and the single thing anyone opens this panel to do is scan it —
// making that cost a click meant the panel's main job was hidden behind a
// button. This set is the opt-OUT, for when you want the code off the screen.
const hiddenQr = new Set();
const qrOpen = (a) => a.has_qr && !hiddenQr.has(a.id);
// { id, what } while a destructive action is one click from happening — this
// panel cannot use confirm() (see the note in renderAccounts).
let confirming = null;
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

    // A destructive action asks in-place instead of through confirm(). The host
    // renders this page in a sandbox WITHOUT allow-modals
    // (aw-workspace-ui AppWindow.jsx: "allow-scripts allow-forms
    // allow-same-origin"), so window.confirm() is ignored by the browser and
    // returns false — every `if (!confirm(...)) return;` became an unconditional
    // return, and Remove/Re-link silently did nothing. Two clicks on the button
    // itself needs no capability the sandbox withholds.
    const pending = (id, what) => confirming
      && confirming.id === id && confirming.what === what;

    const danger = (what, label, run) => {
      if (pending(a.id, what)) {
        const yes = document.createElement('button');
        yes.className = 'danger confirm';
        yes.textContent = 'Confirm';
        yes.onclick = () => { confirming = null; run(); };
        const no = document.createElement('button');
        no.className = 'ghost';
        no.textContent = 'Cancel';
        no.onclick = () => { confirming = null; refresh(); };
        actions.append(yes, no);
        return;
      }
      const b = document.createElement('button');
      b.className = 'danger';
      b.textContent = label;
      b.onclick = () => { confirming = { id: a.id, what }; refresh(); };
      actions.appendChild(b);
    };

    if (a.has_qr) {
      const qr = document.createElement('button');
      qr.textContent = qrOpen(a) ? 'Hide QR' : 'Show QR';
      qr.onclick = () => {
        if (hiddenQr.has(a.id)) hiddenQr.delete(a.id); else hiddenQr.add(a.id);
        refresh();
      };
      actions.appendChild(qr);
    }

    const toggle = document.createElement('button');
    toggle.textContent = a.enabled ? 'Pause' : 'Resume';
    toggle.onclick = () => act(() =>
      api('/accounts/' + a.id + (a.enabled ? '/stop' : '/start'), { method: 'POST' }));
    actions.appendChild(toggle);

    if (a.connected) {
      danger('relink', 'Re-link', () => {
        hiddenQr.delete(a.id);   // the new QR is the point of re-linking
        act(() => api('/accounts/' + a.id + '/relink', { method: 'POST' }));
      });
    }
    danger('remove', 'Remove', () => {
      hiddenQr.delete(a.id);
      act(() => api('/accounts/' + a.id, { method: 'DELETE' }));
    });
    card.appendChild(actions);

    if (pending(a.id, 'relink')) card.appendChild(hint(
      'Unlinks this device from the phone and shows a new QR.'));
    if (pending(a.id, 'remove')) card.appendChild(hint(
      'Unlinks this device and deletes its local history.'));

    if (qrOpen(a)) card.appendChild(qrBlock(a));
    list.appendChild(card);
  }
}

function hint(text) {
  const el = document.createElement('div');
  el.className = 'note';
  el.textContent = text;
  return el;
}

// The QR pane is the image and nothing else. The step-by-step "open WhatsApp →
// Linked devices → Link a device" list that used to sit under it is what
// everyone already does on reflex when a QR appears, and in a 320px-tall
// settings box it pushed the code itself out of view.
function qrBlock(a) {
  if (!a.has_qr) {
    const box = hint(a.connected ? 'Already linked.' : 'Waiting for a pairing code…');
    if (!a.connected) {
      const s = document.createElement('span');
      s.className = 'spin'; s.style.marginLeft = '6px';
      box.appendChild(s);
    }
    return box;
  }
  const box = document.createElement('div');
  box.className = 'qr';
  const img = document.createElement('img');
  // Cache-buster keyed to the QR's own generation time: WhatsApp rotates the
  // code every ~20s and a cached <img> would show an expired one that simply
  // never works, with nothing on screen saying so.
  img.src = BASE + '/accounts/' + a.id + '/qr.png?v=' + (a.qr_generated_at || Date.now());
  img.alt = 'WhatsApp pairing QR';
  box.appendChild(img);
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
    await api('/accounts', { method: 'POST', body: JSON.stringify({ label }) });
    $('new-label').value = '';
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
