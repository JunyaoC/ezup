/** The developer-facing log viewer, served at GET /.
 *
 * One self-contained page: no external assets, no cookies, no server-side
 * session, no external scripts (the CSP forbids them). All cryptography is
 * native WebCrypto (SubtleCrypto): HKDF-SHA-256 key derivation, AES-256-GCM
 * chunk decryption, and AES-256-GCM data-key unwrapping. The byte contract is
 * pinned in docs/E2E-CONTRACT.md (sections 1-3); the same vectors are checked
 * in-page at load (selfTest) so a broken build fails loud instead of silently
 * decrypting to garbage.
 *
 * Trust model (contract section 7.3): the page is JavaScript served by the
 * store operator, so browser E2E is conditional on the served code being
 * honest at load time. The CLI (pull + keyring) is the trust-anchored path.
 * Keys leave the page in exactly one form: the derived ezw_ bearer to this
 * origin. K_enc and every data key stay in memory only.
 *
 * KEYRING: the "login" is a list of pasted ezu_/ezr_ keys. Each is stored as
 * its pasted string (bearer + K_enc + keyid are re-derived every load) in
 * localStorage by default, or sessionStorage when "only this tab" is ticked
 * (contract D7).
 *
 * F1 DOWNGRADE PIN (the load-bearing rule): a wrapped key is cryptographic
 * proof a session is encrypted. If any held key holds a wrap for a session,
 * that session MUST present as enc == "aead-v1" -- a plaintext presentation is
 * a downgrade attempt and is refused, never rendered. A session with no wrap
 * for any held key is not on the E2E path: if the store marks it aead-v1 it is
 * shown locked; if it is genuine legacy plaintext it renders only behind the
 * explicit "show unverified legacy" toggle (default off) and always carries an
 * "unverified plaintext" badge.
 */
export const VIEWER_HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>ezup</title>
<style>
:root{--bg:#101114;--panel:#17181c;--line:#26282e;--fg:#d6d6d2;--muted:#8a8a85;
--faint:#5b5b57;--accent:#7fb0e0;--green:#86c793;--red:#e08c8c;--amber:#d9b06b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.wrap{max-width:1060px;margin:0 auto;padding:28px 18px 80px}
h1{font-size:16px;margin:0}
h1 .dot{color:var(--green)}
.sub{color:var(--muted);font-size:12px;margin:2px 0 22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}
label{display:block;font-size:12px;color:var(--muted);margin:0 0 6px}
input[type=password],input[type=text]{width:100%;font:inherit;padding:9px 11px;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:7px}
input:focus{outline:1px solid var(--accent)}
button{font:inherit;padding:8px 16px;border:1px solid var(--line);border-radius:7px;
background:var(--panel);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#0d1117;font-weight:600}
.row{display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap}
.error{color:var(--red);font-size:13px;margin-top:10px;min-height:1em}
.err{background:#2a1616;border:1px solid #5a2a2a;color:var(--red);border-radius:8px;
padding:13px 15px;font-size:13px;margin-bottom:14px;line-height:1.6}
.err b{color:#f2a6a6}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;
margin-bottom:14px;flex-wrap:wrap}
.who{font-size:12px;color:var(--muted)}
.toggles{display:flex;gap:16px;align-items:center;font-size:12px;color:var(--muted);flex-wrap:wrap}
.toggles label{display:flex;gap:6px;align-items:center;margin:0}
.keypanel{margin-bottom:18px}
.keypanel .addrow{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.keypanel .addrow>div{flex:1;min-width:180px}
.keychips{margin-top:6px}
.keychip{display:inline-flex;gap:7px;align-items:center;font-size:12px;padding:4px 10px;
border:1px solid var(--line);border-radius:20px;margin:6px 7px 0 0}
.keychip.bad{border-color:#5a2a2a;color:var(--red)}
.keychip .kd{width:8px;height:8px;border-radius:50%;flex:none}
.keychip .kx{cursor:pointer;color:var(--faint);font-weight:700}
.keychip .kx:hover{color:var(--red)}
.keychip .st{color:var(--faint);font-size:11px}
.keychip.bad .st{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--faint);text-align:left;font-weight:500;font-size:11px;
text-transform:uppercase;letter-spacing:.07em;padding:6px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr.sess{cursor:pointer}
tr.sess:hover td{background:#1c1e24}
.mono{color:var(--faint);font-size:12px}
.author{color:var(--green)}
.empty{color:var(--muted);padding:26px 8px}
.crumb{margin-bottom:14px;font-size:13px}
.crumb a{color:var(--accent);text-decoration:none;cursor:pointer}
.banner{background:#241f14;border:1px solid #4a3c1c;color:var(--amber);border-radius:8px;
padding:8px 12px;font-size:12.5px;margin-bottom:12px}
.banner.ok{background:#152016;border-color:#2c4a30;color:var(--green)}
.chip{display:inline-flex;gap:6px;align-items:center;font-size:11px;padding:1px 8px;
border-radius:20px;border:1px solid var(--line)}
.chip .kd{width:7px;height:7px;border-radius:50%;flex:none}
.tag{font-size:11px;padding:1px 8px;border-radius:20px;border:1px solid var(--line)}
.tag.enc{color:var(--green);border-color:#2c4a30}
.tag.locked{color:var(--faint)}
.tag.legacy{color:var(--amber);border-color:#4a3c1c}
.authorcard{margin-top:14px}
.authorhead{display:flex;justify-content:space-between;align-items:center;cursor:pointer;
padding:9px 12px;background:#1a1c22;border:1px solid var(--line);border-radius:8px}
.authorhead:hover{border-color:var(--accent)}
.authorhead .cnt{color:var(--faint);font-size:12px}
.authorbody{border:1px solid var(--line);border-top:none;border-radius:0 0 8px 8px}
.legend{font-size:11.5px;color:var(--faint);margin:10px 2px 0;display:flex;gap:14px;flex-wrap:wrap}
.turn{border-left:3px solid var(--line);margin:0 0 10px;padding:6px 12px;border-radius:0 8px 8px 0}
.turn.user{border-left-color:var(--green);background:#151a15}
.turn.assistant{border-left-color:var(--accent);background:#14171c}
.turn .who{font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.turn.user .who{color:var(--green)}
.turn.assistant .who{color:var(--accent)}
.turn pre{margin:0;white-space:pre-wrap;word-break:break-word;font:inherit}
.tool{color:var(--muted);font-size:12.5px;margin:0 0 6px;padding-left:14px}
.tool .name{color:var(--amber)}
.trunc{color:var(--faint)}
.loading{color:var(--muted);padding:20px 4px}
footer{margin-top:30px;color:var(--faint);font-size:11.5px;line-height:1.7}
</style></head><body><div class="wrap">
<h1><span class="dot">●</span> ezup <span style="color:var(--faint)">E2E log viewer</span></h1>
<div class="sub" id="origin"></div>
<div id="selftest"></div>
<div id="app"></div>
<footer>read-only · keys never leave this page except as the derived
<b>ezw_</b> Authorization bearer to this host · K_enc and data keys stay in
memory · for a store you do not trust, verify with the CLI (pull + keyring)</footer>
</div>
<script>
"use strict";
const $ = (h) => { const t = document.createElement("template"); t.innerHTML = h.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const app = document.getElementById("app");
document.getElementById("origin").textContent = location.origin;

const human = (n) => n > 1048576 ? (n/1048576).toFixed(1)+" MB" : n > 1024 ? (n/1024).toFixed(0)+" KB" : n+" B";
const when = (t) => (t || "").replace("T"," ").slice(0, 16);
const TAIL_BYTES = 4 * 1048576;

// --- WebCrypto: exact reproduction of ezchangelog/crypto.py (contract 1-3) --
// Every constant, encoding, and label below is interop with the Python client
// and the pinned vectors; changing any of them silently breaks decryption.
const TE = new TextEncoder();
const TD = new TextDecoder();
const hex2bytes = (h) => { const a = new Uint8Array(h.length/2); for (let i=0;i<a.length;i++) a[i]=parseInt(h.substr(i*2,2),16); return a; };
const bytes2hex = (b) => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2,"0")).join("");
const b64d = (s) => { const bin = atob(s); const b = new Uint8Array(bin.length); for (let i=0;i<bin.length;i++) b[i]=bin.charCodeAt(i); return b; };
// Big-endian fixed-width encoders for the deterministic nonce/AAD framing.
const be4 = (n) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, n>>>0, false); return b; };
const be8 = (n) => { const b = new Uint8Array(8); new DataView(b.buffer).setBigUint64(0, BigInt(n), false); return b; };
const concat = (...a) => { let n=0; for (const x of a) n+=x.length; const o=new Uint8Array(n); let p=0; for (const x of a){ o.set(x,p); p+=x.length; } return o; };
const NUL = new Uint8Array([0]);

// HKDF-SHA-256, IKM = raw 32 secret bytes, salt = utf8("ezup/v1/salt"), L=32.
async function hkdf32(secret, info) {
  const k = await crypto.subtle.importKey("raw", secret, "HKDF", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name:"HKDF", hash:"SHA-256", salt: TE.encode("ezup/v1/salt"), info: TE.encode(info) }, k, 256);
  return new Uint8Array(bits);
}
async function sha256hex(bytes) { return bytes2hex(await crypto.subtle.digest("SHA-256", bytes)); }

// Derive everything a pasted ezu_/ezr_ key yields. enc_key never leaves memory.
async function deriveKeyset(pasted) {
  const m = /^(ezu_|ezr_)([0-9a-f]{64})$/.exec(pasted);
  if (!m) throw new Error("not a pasted key: expected ezu_/ezr_ + 64 lowercase hex chars");
  const S = hex2bytes(m[2]);                          // IKM is the raw 32 bytes, not the ascii hex
  const kAuth = await hkdf32(S, "ezup/v1/auth");
  const encKey = await hkdf32(S, "ezup/v1/enc");
  const bearer = "ezw_" + bytes2hex(kAuth);
  const keyid = (await sha256hex(TE.encode(bearer))).slice(0, 16);
  return { kind: m[1] === "ezu_" ? "device" : "reader", bearer, encKey, keyid };
}

// nonce = BE4(gen) || BE8(offset); AADs bind scheme + session + gen + offset.
const chunkNonce = (gen, offset) => concat(be4(gen), be8(offset));
const chunkAad = (session, gen, offset) => concat(TE.encode("ezup/v1/chunk"), NUL, TE.encode(session), NUL, be4(gen), be8(offset));
const wrapAad = (session, rid, gen) => concat(TE.encode("ezup/v1/wrap"), NUL, TE.encode(session), NUL, TE.encode(rid), NUL, be4(gen));

// blob = 12 nonce || 32 ct || 16 tag; recipient id binds the wrap to one row.
async function unwrapDk(encKey, session, rid, gen, blob) {
  if (blob.length !== 60) throw new Error("wrapped key blob is " + blob.length + " bytes, expected 60");
  const k = await crypto.subtle.importKey("raw", encKey, "AES-GCM", false, ["decrypt"]);
  const dk = await crypto.subtle.decrypt(
    { name:"AES-GCM", iv: blob.slice(0,12), additionalData: wrapAad(session,rid,gen), tagLength:128 }, k, blob.slice(12));
  return new Uint8Array(dk);
}
// body = ct || tag; a tag failure throws (caught by the caller as an integrity
// error) rather than returning garbage.
async function decryptChunk(dk, session, gen, offset, body) {
  const k = await crypto.subtle.importKey("raw", dk, "AES-GCM", false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt(
    { name:"AES-GCM", iv: chunkNonce(gen,offset), additionalData: chunkAad(session,gen,offset), tagLength:128 }, k, body);
  return new Uint8Array(pt);
}

// Conformance check against the contract's pinned vectors, run at load. The
// viewer generated these vectors, so agreement here IS interop with Python.
async function selfTest() {
  const kAuth = bytes2hex(await hkdf32(hex2bytes("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"), "ezup/v1/auth"));
  const kEnc = bytes2hex(await hkdf32(hex2bytes("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"), "ezup/v1/enc"));
  const bearer = "ezw_" + kAuth;
  const tokenSha = await sha256hex(TE.encode(bearer));
  const okKdf = kAuth === "c587d5c13882bb99c0db1bdeb631f580a6af77dd47d646a7558d3d48c23c3677"
    && kEnc === "38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5"
    && tokenSha === "01d236f19c3dfb00fa29e633cd93cc5c8f97893db5fbd0c095280156499b58d8"
    && tokenSha.slice(0,16) === "01d236f19c3dfb00";
  let okChunk = false, okWrap = false;
  try {
    const pt = TD.decode(await decryptChunk(
      hex2bytes("0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"), "sess-abc", 1, 4096,
      hex2bytes("c80a96c53f560b528367d4bbe2a22c2f376bdc8b14c5abf9ddc7744b1acfb8611b649d6bd75ca706ea67a9822ef681")));
    okChunk = pt === '{"type":"user","text":"hello"}\\n';
  } catch (e) { okChunk = false; }
  try {
    const dk = bytes2hex(await unwrapDk(
      hex2bytes("38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5"), "sess-abc",
      "11111111-2222-3333-4444-555555555555", 1,
      hex2bytes("000102030405060708090a0bc53ab5ffb0ec857074691a69eb958a4e91b3a2bb2c44c7a0a13131afd2151c07bae3d4e43e892f61c6d4b0006c779373")));
    okWrap = dk === "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0";
  } catch (e) { okWrap = false; }
  return okKdf && okChunk && okWrap;
}

// --- Keyring state --------------------------------------------------------
// Persisted form is the pasted string + label only; bearer/enc_key/keyid are
// re-derived each load so key material is never serialized in a derived form.
const STORE_KEY = "ezup_keyring";
let TAB_ONLY = localStorage.getItem("ezup_tabonly") === "1";
let SHOW_LEGACY = false;
let KEYS = [];   // live: {token,label,kind,bearer,encKey,keyid,status,recipientId}

function persist() {
  const raw = JSON.stringify(KEYS.map(k => ({ token: k.token, label: k.label })));
  // A tab-only keyring must not leave a copy in localStorage, and vice versa.
  if (TAB_ONLY) { sessionStorage.setItem(STORE_KEY, raw); localStorage.removeItem(STORE_KEY); }
  else { localStorage.setItem(STORE_KEY, raw); sessionStorage.removeItem(STORE_KEY); }
}
const storedRaw = () => sessionStorage.getItem(STORE_KEY) || localStorage.getItem(STORE_KEY) || "";

async function hydrate() {
  KEYS = [];
  let saved = [];
  try { saved = JSON.parse(storedRaw() || "[]"); } catch { saved = []; }
  for (const e of saved) {
    try { const ks = await deriveKeyset(e.token); KEYS.push({ token: e.token, label: e.label || ks.keyid.slice(0,4), status: "unknown", recipientId: null, ...ks }); }
    catch { /* a corrupt stored entry is dropped, never fatal */ }
  }
}

// Stable per-keyid colour so a chip reads the same everywhere on the page.
function keyColor(keyid) { let h = 0; for (const c of keyid) h = (h * 31 + c.charCodeAt(0)) % 360; return "hsl(" + h + " 55% 62%)"; }

// --- HTTP (per-key bearer) ------------------------------------------------
async function api(path, bearer) {
  const r = await fetch(path, { headers: { Authorization: "Bearer " + bearer } });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("HTTP " + r.status));
  return r;
}

// --- Team model -----------------------------------------------------------
// Per key: list sessions and bulk-fetch this key's wraps (one round trip each,
// contract D8). The union is keyed by session id; a row records which keys
// listed it (decrypt fallbacks / legacy readers) and which keys hold a wrap
// (the E2E-readable set, and the recipient id needed for the unwrap AAD).
async function buildModel() {
  const model = new Map();
  for (const k of KEYS) {
    let sessions;
    try { sessions = ((await (await api("/v1/sessions", k.bearer)).json()).sessions) || []; k.status = "ok"; }
    catch (e) { k.status = e.message === "unauthorized" ? "revoked or refused" : e.message; continue; }
    let wraps = [];
    try { const w = await (await api("/v1/wrapped_keys", k.bearer)).json(); k.recipientId = w.recipient_id || null; wraps = w.wraps || []; }
    catch (e) { k.recipientId = null; wraps = []; }
    const wrapBy = new Map(wraps.map(w => [w.session, w]));
    for (const s of sessions) {
      let row = model.get(s.session);
      if (!row) { row = Object.assign({}, s, { listKeys: [], wrapKeys: [] }); model.set(s.session, row); }
      row.listKeys.push(k);
      const w = wrapBy.get(s.session);
      if (w) row.wrapKeys.push({ key: k, wrap: w, recipientId: k.recipientId });
    }
  }
  return model;
}

// A row is E2E-readable iff a held key holds a wrap for it. Otherwise the
// store's own enc flag decides locked (aead-v1, no key) vs legacy (plaintext).
function rowState(r) {
  if (r.wrapKeys.length) return { kind: "enc", key: r.wrapKeys[0].key };
  if (r.enc === "aead-v1") return { kind: "locked" };
  return { kind: "legacy" };
}

// --- Rendering: main / keyring / team -------------------------------------
async function renderMain() {
  app.replaceChildren($('<div class="loading">loading team view…</div>'));
  const model = await buildModel();
  const rows = [...model.values()];

  const keychips = KEYS.map(k => {
    const bad = k.status !== "ok" && k.status !== "unknown";
    return \`<span class="keychip \${bad ? "bad" : ""}" title="\${esc(k.keyid)} · \${esc(k.kind)}">
      <span class="kd" style="background:\${keyColor(k.keyid)}"></span>
      <b>\${esc(k.label)}</b><span class="mono">\${esc(k.keyid.slice(0,4))}</span>
      <span class="st">\${esc(bad ? k.status : k.kind)}</span>
      <span class="kx" data-forget="\${esc(k.keyid)}" title="forget this key">×</span></span>\`;
  }).join("");

  const panel = \`<div class="card keypanel">
    <div class="addrow">
      <div><label for="newkey">add key (ezu_ or ezr_)</label>
        <input id="newkey" type="password" autocomplete="off" placeholder="ezu_… or ezr_…"></div>
      <div style="flex:0 0 160px"><label for="newlabel">label (optional)</label>
        <input id="newlabel" type="text" autocomplete="off" placeholder="alice"></div>
      <button class="primary" id="addkey">add</button>
    </div>
    <div class="error" id="keyerr"></div>
    <div class="keychips">\${keychips || '<span class="mono">no keys yet — paste an ezr_ (reader) or ezu_ (device) key</span>'}</div>
    <div class="legend">forgetting a key removes it from this browser only — it is not revocation (run <b>token revoke</b> for that).</div>
  </div>\`;

  app.replaceChildren($(\`<div>
    <div class="topbar">
      <span class="who">\${KEYS.length} key(s) · \${rows.length} session(s)</span>
      <div class="toggles">
        <label><input type="checkbox" id="tabonly" \${TAB_ONLY ? "checked" : ""}> only this tab</label>
        <label><input type="checkbox" id="legacy" \${SHOW_LEGACY ? "checked" : ""}> show unverified legacy</label>
      </div>
    </div>
    \${panel}
    <div id="team"></div>
  </div>\`));

  document.getElementById("addkey").onclick = addKey;
  const nk = document.getElementById("newkey");
  nk.onkeydown = (e) => { if (e.key === "Enter") addKey(); };
  document.getElementById("tabonly").onchange = (e) => {
    TAB_ONLY = e.target.checked; localStorage.setItem("ezup_tabonly", TAB_ONLY ? "1" : "0"); persist();
  };
  document.getElementById("legacy").onchange = (e) => { SHOW_LEGACY = e.target.checked; renderMain(); };
  app.querySelectorAll("[data-forget]").forEach(x =>
    x.onclick = () => { KEYS = KEYS.filter(k => k.keyid !== x.dataset.forget); persist(); renderMain(); });

  renderTeam(rows);
  if (KEYS.length) nk.focus();
}

async function addKey() {
  const inp = document.getElementById("newkey");
  const lbl = document.getElementById("newlabel");
  const err = document.getElementById("keyerr");
  err.textContent = "";
  const pasted = inp.value.trim();
  if (!pasted) return;
  let ks;
  try { ks = await deriveKeyset(pasted); } catch { err.textContent = "not a valid ezu_/ezr_ key"; return; }
  if (KEYS.some(k => k.keyid === ks.keyid)) { err.textContent = "that key is already on the keyring"; return; }
  try { await api("/v1/sessions", ks.bearer); }                 // probe before storing
  catch (e) { err.textContent = e.message === "unauthorized" ? "that key was refused" : e.message; return; }
  KEYS.push({ token: pasted, label: lbl.value.trim() || ks.keyid.slice(0,4), status: "ok", recipientId: null, ...ks });
  persist();
  renderMain();
}

function tagFor(st) {
  if (st.kind === "enc") return '<span class="tag enc">encrypted</span>';
  if (st.kind === "locked") return '<span class="tag locked">locked</span>';
  return '<span class="tag legacy">legacy · unverified</span>';
}
function chipFor(r, st) {
  if (st.kind !== "enc") return "";
  const k = st.key;
  return \`<span class="chip" title="unlocked by \${esc(k.label)} (\${esc(k.keyid)})">
    <span class="kd" style="background:\${keyColor(k.keyid)}"></span>k:\${esc(k.keyid.slice(0,4))}</span>\`;
}

function sessRow(r) {
  const st = rowState(r);
  return \`<tr class="sess" data-s="\${esc(r.session)}">
    <td class="mono">\${esc(when(r.updated_at))}</td>
    <td class="author">\${esc(r.author)}</td>
    <td>\${esc(r.project || "")}</td>
    <td>\${esc(r.title || r.session.slice(0,8))}</td>
    <td class="mono">\${human(r.size || 0)}</td>
    <td>\${tagFor(st)} \${chipFor(r, st)}</td>
  </tr>\`;
}

const THEAD = \`<thead><tr><th>updated</th><th>author</th><th>project</th>
  <th>title</th><th>size</th><th>access</th></tr></thead>\`;

function bindRows(container, byId) {
  container.querySelectorAll("tr.sess").forEach(tr =>
    tr.onclick = () => openSession(byId.get(tr.dataset.s)));
}

function renderTeam(rows) {
  const team = document.getElementById("team");
  const byId = new Map(rows.map(r => [r.session, r]));
  if (!rows.length) {
    team.replaceChildren($('<div class="empty">nothing this keyring can read yet — add a key, or run <b>/ezup on</b> in a session</div>'));
    return;
  }
  // Single key: today's flat table, group furniture hidden (contract 7.2).
  if (KEYS.length <= 1) {
    rows.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    const el = $(\`<div class="card" style="padding:0">
      <table>\${THEAD}<tbody>\${rows.map(sessRow).join("")}</tbody></table></div>\`);
    team.replaceChildren(el);
    bindRows(el, byId);
    return;
  }
  // Multi-key: union grouped by author, per-author counts, recency order.
  const groups = new Map();
  for (const r of rows) { if (!groups.has(r.author)) groups.set(r.author, []); groups.get(r.author).push(r); }
  const authors = [...groups.entries()].map(([author, rs]) => {
    rs.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    return { author, rs, last: rs[0].updated_at || "", bytes: rs.reduce((n, x) => n + (x.size || 0), 0) };
  }).sort((a, b) => (b.last || "").localeCompare(a.last || ""));

  const frag = document.createDocumentFragment();
  for (const g of authors) {
    const card = $(\`<div class="authorcard">
      <div class="authorhead">
        <span class="author">\${esc(g.author)}</span>
        <span class="cnt">\${g.rs.length} session(s) · \${human(g.bytes)} · updated \${esc(when(g.last))}</span>
      </div>
      <div class="authorbody"><table>\${THEAD}<tbody>\${g.rs.map(sessRow).join("")}</tbody></table></div>
    </div>\`);
    const body = card.querySelector(".authorbody");
    card.querySelector(".authorhead").onclick = () => { body.style.display = body.style.display === "none" ? "" : "none"; };
    bindRows(card, byId);
    frag.appendChild(card);
  }
  team.replaceChildren(frag);
}

// --- Open one session -----------------------------------------------------
async function openSession(r) {
  app.replaceChildren($('<div class="loading">opening session…</div>'));
  const st = rowState(r);
  try {
    if (st.kind === "enc") await renderEncrypted(r);
    else if (st.kind === "locked") renderNotice(r,
      \`<div class="banner">🔒 no key on this page can open this session. It is
       encrypted (aead-v1) but none of your keys hold a wrapped data key for it.</div>\`);
    else if (!SHOW_LEGACY) renderNotice(r,
      \`<div class="banner">This session is stored as <b>unencrypted legacy plaintext</b>
       and no key here holds a wrap for it, so its authenticity cannot be verified.
       Enable <b>show unverified legacy</b> on the sessions page to view it anyway.</div>\`);
    else await renderLegacy(r);
  } catch (e) {
    renderError(r, e.message || String(e));
  }
}

// Shared header/back-link + optional banners, then a transcript container.
function shell(r, banners) {
  app.replaceChildren($(\`<div>
    <div class="crumb"><a id="back">← sessions</a>
      <span class="mono"> · \${esc(r.author)} · \${esc(r.title || r.session.slice(0,8))}</span></div>
    \${banners || ""}
    <div id="log"></div>
  </div>\`));
  document.getElementById("back").onclick = () => renderMain();
  return document.getElementById("log");
}
function renderNotice(r, banner) { shell(r, banner); }
function renderError(r, msg) { shell(r, \`<div class="err"><b>cannot render this session</b><br>\${esc(msg)}</div>\`); }

async function renderEncrypted(r) {
  const wk = r.wrapKeys[0], k = wk.key;
  const man = await (await api("/v1/chunks?session=" + encodeURIComponent(r.session), k.bearer)).json();

  // F1 DOWNGRADE PIN: we hold a wrap, which is cryptographic proof this session
  // is encrypted. The store presenting it as anything but aead-v1 is a
  // downgrade attempt -> refuse, never render.
  if (man.enc !== "aead-v1") {
    shell(r, \`<div class="err"><b>DOWNGRADE ATTEMPT — refusing to render.</b><br>
      A wrapped data key exists for one of your keys (proof this session is
      end-to-end encrypted), yet the store presents it as
      "\${esc(String(man.enc == null ? "plaintext" : man.enc))}". A reader must
      never trust the store's enc flag over a wrap it holds. Nothing was decoded.</div>\`);
    return;
  }
  // The wrap's generation must match the chunks' generation; a mismatch is a
  // rotation in flight (or a spliced manifest) -> refuse rather than decrypt
  // under the wrong generation and render garbage.
  const gen = Number(man.enc_gen);
  if (Number(wk.wrap.enc_gen) !== gen) {
    renderError(r, "generation mismatch: the store's chunks are gen " + gen +
      " but your wrapped key is gen " + wk.wrap.enc_gen + " — a key rotation is in flight, reload to retry");
    return;
  }

  let dk;
  try { dk = await unwrapDk(k.encKey, r.session, wk.recipientId, wk.wrap.enc_gen, b64d(wk.wrap.wrap)); }
  catch (e) { renderError(r, "could not unwrap the data key (wrong recipient, tampered wrap, or bad key): " + e.message); return; }

  const chunks = (man.chunks || []).slice().sort((a, b) => a.offset - b.offset);
  let picked = chunks, skipped = 0;
  const total = chunks.reduce((n, c) => n + c.length, 0);
  while (picked.length > 1 && picked.slice(1).reduce((n, c) => n + c.length, 0) >= TAIL_BYTES) {
    skipped += picked[0].length; picked = picked.slice(1);
  }
  const parts = [];
  for (const c of picked) {
    const body = new Uint8Array(await (await api("/v1/blob?key=" + encodeURIComponent(c.key), k.bearer)).arrayBuffer());
    // sha256 is over the ciphertext body on both sides (contract 3.2).
    if (c.sha256 && (await sha256hex(body)) !== c.sha256)
      throw new Error("chunk at offset " + c.offset + " failed its ciphertext checksum — the store served the wrong bytes (nothing rendered)");
    let pt;
    try { pt = await decryptChunk(dk, r.session, gen, c.offset, body); }
    catch { throw new Error("chunk at offset " + c.offset + " (gen " + gen + ") failed its GCM tag — corrupt, resealed, or forged ciphertext (nothing rendered)"); }
    parts.push(TD.decode(pt));
  }
  let text = parts.join("");
  if (skipped) text = text.slice(text.indexOf("\\n") + 1);   // drop the partial cut line

  const badge = \`<div class="banner ok">● end-to-end encrypted · verified · unlocked by
    <b>\${esc(k.label)}</b> <span class="mono">(\${esc(k.keyid.slice(0,4))})</span></div>\`;
  renderTranscript(shell(r, badge + tailBanner(total, skipped)), text, total);
}

async function renderLegacy(r) {
  const k = r.listKeys[0];
  const man = await (await api("/v1/chunks?session=" + encodeURIComponent(r.session), k.bearer)).json();
  // Guard: if the store now claims aead-v1 for a row we hold no wrap for, it is
  // not legacy plaintext — it is locked. Never render its bytes as plaintext.
  if (man.enc === "aead-v1") {
    renderNotice(r, \`<div class="banner">🔒 this session is now reported encrypted
      but no key here can open it.</div>\`);
    return;
  }
  const chunks = (man.chunks || []).slice().sort((a, b) => a.offset - b.offset);
  let picked = chunks, skipped = 0;
  const total = chunks.reduce((n, c) => n + c.length, 0);
  while (picked.length > 1 && picked.slice(1).reduce((n, c) => n + c.length, 0) >= TAIL_BYTES) {
    skipped += picked[0].length; picked = picked.slice(1);
  }
  const parts = [];
  for (const c of picked) parts.push(await (await api("/v1/blob?key=" + encodeURIComponent(c.key), k.bearer)).text());
  let text = parts.join("");
  if (skipped) text = text.slice(text.indexOf("\\n") + 1);

  const badge = '<div class="banner">⚠ legacy: stored unencrypted · <b>unverified plaintext</b> — the store could have altered these bytes and nothing here would detect it</div>';
  renderTranscript(shell(r, badge + tailBanner(total, skipped)), text, total);
}

function tailBanner(total, skipped) {
  return skipped ? \`<div class="banner">large session: showing the last
    \${human(total - skipped)}; \${human(skipped)} earlier not loaded</div>\` : "";
}

// Shared JSONL transcript parser/renderer (user / assistant / tool), identical
// in shape to the pre-E2E viewer; it now runs over decrypted plaintext.
function renderTranscript(log, text, total) {
  const turns = [];
  for (const line of text.split("\\n")) {
    if (!line.trim()) continue;
    let o; try { o = JSON.parse(line); } catch { continue; }
    const t = o.type;
    if (t !== "user" && t !== "assistant") continue;
    const c = o.message && o.message.content;
    if (typeof c === "string") { if (c.trim()) turns.push({ role: t, text: c }); continue; }
    if (!Array.isArray(c)) continue;
    for (const b of c) {
      if (!b || typeof b !== "object") continue;
      if (b.type === "text" && b.text && b.text.trim()) turns.push({ role: t, text: b.text });
      else if (b.type === "tool_use") turns.push({ role: "tool", name: b.name, text: JSON.stringify(b.input || {}).slice(0, 200) });
    }
  }
  const cap = 800, shown = turns.slice(-cap);
  if (turns.length > cap) log.appendChild($(\`<div class="banner">long session: showing the last \${cap} of \${turns.length} turns</div>\`));
  for (const turn of shown) {
    if (turn.role === "tool")
      log.appendChild($(\`<div class="tool">▸ <span class="name">\${esc(turn.name)}</span>
        <span class="trunc">\${esc(turn.text)}</span></div>\`));
    else
      log.appendChild($(\`<div class="turn \${turn.role}"><div class="who">\${turn.role}</div>
        <pre>\${esc(turn.text.length > 4000 ? turn.text.slice(0, 4000) + " …" : turn.text)}</pre></div>\`));
  }
  if (!shown.length) log.appendChild($('<div class="empty">no user/assistant turns in this session</div>'));
}

// --- Boot -----------------------------------------------------------------
(async () => {
  // A failing self-test means the served crypto does not match the pinned byte
  // contract; decryption would silently produce garbage, so refuse loudly.
  let ok = false;
  try { ok = await selfTest(); } catch { ok = false; }
  if (!ok) document.getElementById("selftest").replaceChildren($(\`<div class="err">
    <b>crypto self-test FAILED.</b> This build does not reproduce the pinned
    WebCrypto vectors, so it cannot be trusted to decrypt correctly. Do not
    paste keys into this page — verify with the CLI instead.</div>\`));
  await hydrate();
  renderMain();
})();
</script></body></html>\`;
