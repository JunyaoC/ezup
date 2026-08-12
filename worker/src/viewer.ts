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
 * F1 DOWNGRADE PIN (the load-bearing rule): a wrapped key is cryptographic
 * proof a session is encrypted. If any held key holds a wrap for a session,
 * that session MUST present as enc == "aead-v1" -- a plaintext presentation is
 * a downgrade attempt and is refused, never rendered.
 *
 * LAYOUT: a fixed sidebar (keyring + session list grouped by author) and a
 * reading pane. Opening a session pushes #s/<id> so it is linkable and the
 * browser back button works. Transcripts render in pages, lazy-loaded on scroll.
 */
export const VIEWER_HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>ezup</title>
<style>
:root{
  --bg:#ffffff; --bg-side:#fbfbfa; --panel:#fafafa; --panel-2:#f4f4f5;
  --ink:#18181b; --ink-2:#3f3f46; --muted:#71717a; --faint:#a1a1aa;
  --line:#ececec; --line-2:#e0e0e2; --line-3:#d4d4d8;
  --accent:#18181b;
  --ok:#067647; --ok-bg:#ecfdf3; --ok-bd:#a9efc5;
  --warn:#b54708; --warn-bg:#fffaeb; --warn-bd:#fedf89;
  --err:#b42318; --err-bg:#fef3f2; --err-bd:#fecdca;
  --info:#1849a9; --info-bg:#eff6ff;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;
  --side-w:288px; --r:8px; --r-sm:6px;
  --shadow-sm:0 1px 2px rgba(24,24,27,.06);
  --shadow:0 4px 16px -6px rgba(24,24,27,.14),0 1px 2px rgba(24,24,27,.06);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none;cursor:pointer}
::selection{background:var(--ink);color:#fff}
button{font:inherit;cursor:pointer}
input{font:inherit}
.mono{font-family:var(--mono)}

.layout{display:grid;grid-template-columns:var(--side-w) 1fr;min-height:100vh}

/* sidebar */
aside{background:var(--bg-side);border-right:1px solid var(--line);
display:flex;flex-direction:column;height:100vh;position:sticky;top:0;overflow:hidden}
.brand{display:flex;align-items:center;gap:9px;padding:16px 16px 12px}
.brand .mark{width:22px;height:22px;border-radius:6px;background:var(--ink);color:#fff;
display:grid;place-items:center;font-family:var(--mono);font-weight:600;font-size:11px}
.brand .t{font-weight:600;font-size:14px}
.brand .h{color:var(--faint);font-size:10px;font-family:var(--mono);margin-top:1px}
.side-label{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
color:var(--faint);padding:10px 16px 5px;display:flex;justify-content:space-between}
.side-scroll{overflow-y:auto;flex:1;padding-bottom:12px}

.keybox{padding:0 12px 6px}
.keyrow{display:flex;gap:6px;padding:0 4px}
.keyrow input{flex:1;min-width:0;padding:7px 9px;background:var(--bg);color:var(--ink);
border:1px solid var(--line-2);border-radius:var(--r-sm);font-size:12.5px}
.keyrow input:focus{outline:2px solid var(--ink);outline-offset:-1px}
.keyrow button{padding:7px 12px;border:1px solid var(--ink);background:var(--ink);color:#fff;
border-radius:var(--r-sm);font-size:12.5px;font-weight:550}
.keyrow button:hover{opacity:.9}
.keyerr{color:var(--err);font-size:11.5px;padding:5px 8px 0;min-height:1em}
.keychip{display:flex;align-items:center;gap:7px;font-size:12px;padding:5px 8px;margin:4px 4px 0;
border:1px solid var(--line-2);border-radius:var(--r-sm);background:var(--bg)}
.keychip.bad{border-color:var(--err-bd);background:var(--err-bg)}
.keychip .kd{width:8px;height:8px;border-radius:50%;flex:none}
.keychip b{font-weight:550}
.keychip .st{color:var(--faint);font-size:10.5px;margin-left:auto}
.keychip.bad .st{color:var(--err)}
.keychip .kx{cursor:pointer;color:var(--faint);font-weight:700;padding:0 2px}
.keychip .kx:hover{color:var(--err)}

.authorhdr{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;color:var(--muted);
padding:12px 16px 4px;display:flex;justify-content:space-between;align-items:baseline}
.authorhdr .n{color:var(--faint);font-size:10px}
.slink{display:block;width:100%;text-align:left;border:0;background:none;color:var(--ink-2);
padding:7px 16px;border-left:2px solid transparent;line-height:1.35}
.slink:hover{background:var(--panel-2)}
.slink.active{background:var(--panel-2);border-left-color:var(--ink);color:var(--ink)}
.slink .st{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
display:flex;align-items:center;gap:6px}
.slink .sm{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:2px}
.dot{width:6px;height:6px;border-radius:50%;flex:none}
.dot.enc{background:var(--ok)} .dot.locked{background:var(--line-3)} .dot.legacy{background:var(--warn)}
.side-foot{padding:10px 16px;border-top:1px solid var(--line);font-family:var(--mono);
font-size:10px;color:var(--faint);line-height:1.7}
.toggles{padding:8px 16px 0;font-size:11.5px;color:var(--muted)}
.toggles label{display:flex;gap:6px;align-items:center;margin-top:5px;cursor:pointer}

/* main reading pane */
main{min-width:0;display:flex;flex-direction:column;height:100vh;overflow:hidden}
.topbar{display:flex;align-items:center;gap:8px;padding:14px 26px;border-bottom:1px solid var(--line);
background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:saturate(160%) blur(8px);
position:sticky;top:0;z-index:2}
.crumb{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);min-width:0}
.crumb b{color:var(--ink);font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.crumb .sep{color:var(--line-3)}
.crumb .back{color:var(--muted);font-family:var(--mono);font-size:12px}
.crumb .back:hover{color:var(--ink)}
.reader{overflow-y:auto;flex:1;padding:22px 26px 80px;max-width:920px;width:100%}

.banner{border:1px solid var(--line-2);background:var(--panel);border-radius:var(--r);
padding:9px 13px;font-size:12.5px;margin-bottom:14px;color:var(--ink-2)}
.banner.ok{background:var(--ok-bg);border-color:var(--ok-bd);color:var(--ok)}
.banner.warn{background:var(--warn-bg);border-color:var(--warn-bd);color:var(--warn)}
.err{background:var(--err-bg);border:1px solid var(--err-bd);color:var(--err);border-radius:var(--r);
padding:14px 16px;font-size:13px;margin-bottom:14px;line-height:1.6}
.err b{color:#912018}
.empty{color:var(--muted);padding:60px 8px;text-align:center}
.empty b{color:var(--ink)}
.loading{color:var(--muted);padding:30px 4px;font-family:var(--mono);font-size:12px}

.turn{border:1px solid var(--line);border-radius:var(--r);padding:11px 14px;margin:0 0 10px;
background:var(--bg);box-shadow:var(--shadow-sm)}
.turn .meta{display:flex;align-items:center;gap:9px;margin-bottom:6px}
.turn .who{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;
padding:1px 7px;border-radius:20px}
.turn.user .who{color:var(--ok);background:var(--ok-bg)}
.turn.assistant .who{color:var(--info);background:var(--info-bg)}
.turn .ts{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:auto}
.turn pre{margin:0;white-space:pre-wrap;word-break:break-word;font:inherit;color:var(--ink-2)}
.tool{font-family:var(--mono);font-size:12px;color:var(--muted);margin:0 0 8px;padding:5px 14px;
display:flex;gap:8px;align-items:baseline}
.tool .name{color:var(--warn);font-weight:600}
.tool .ts{color:var(--faint);margin-left:auto;flex:none}
.tool .trunc{color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.more{text-align:center;padding:16px;color:var(--faint);font-family:var(--mono);font-size:11.5px}
footer{padding:14px 26px;border-top:1px solid var(--line);color:var(--faint);font-size:10.5px;
font-family:var(--mono);line-height:1.7}
#selftest:empty{display:none}
@media(max-width:760px){
  .layout{grid-template-columns:1fr}
  aside{position:static;height:auto;max-height:46vh}
  main{height:auto}
}
</style></head><body>
<div class="layout">
  <aside>
    <div class="brand">
      <div class="mark">ez</div>
      <div><div class="t">ezup</div><div class="h" id="origin"></div></div>
    </div>
    <div id="selftest"></div>
    <div class="side-scroll">
      <div class="side-label">keys</div>
      <div class="keybox">
        <div class="keyrow">
          <input id="newkey" type="password" autocomplete="off" placeholder="ezu_ or ezr_ key">
          <button id="addkey">add</button>
        </div>
        <div class="keyerr" id="keyerr"></div>
        <div id="keychips"></div>
        <div class="toggles">
          <label><input type="checkbox" id="tabonly"> remember only this tab</label>
          <label><input type="checkbox" id="legacy"> show unverified legacy</label>
        </div>
      </div>
      <div id="sessions"></div>
    </div>
    <div class="side-foot" id="foot"></div>
  </aside>
  <main>
    <div class="topbar"><div class="crumb" id="crumb"></div></div>
    <div class="reader" id="reader"></div>
  </main>
</div>
<script>
"use strict";
const $ = (h) => { const t = document.createElement("template"); t.innerHTML = h.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
document.getElementById("origin").textContent = location.host;

const human = (n) => n > 1048576 ? (n/1048576).toFixed(1)+" MB" : n > 1024 ? (n/1024).toFixed(0)+" KB" : n+" B";
const when = (t) => (t || "").replace("T"," ").slice(0, 16);
const clock = (t) => { const s = String(t||""); const i = s.indexOf("T"); return i<0 ? "" : s.slice(i+1, i+9); };
const TAIL_BYTES = 4 * 1048576;

// --- WebCrypto: exact reproduction of ezchangelog/crypto.py (contract 1-3) --
// Every constant, encoding, and label below is interop with the Python client
// and the pinned vectors; changing any of them silently breaks decryption.
const TE = new TextEncoder();
const TD = new TextDecoder();
const hex2bytes = (h) => { const a = new Uint8Array(h.length/2); for (let i=0;i<a.length;i++) a[i]=parseInt(h.substr(i*2,2),16); return a; };
const bytes2hex = (b) => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2,"0")).join("");
const b64d = (s) => { const bin = atob(s); const b = new Uint8Array(bin.length); for (let i=0;i<bin.length;i++) b[i]=bin.charCodeAt(i); return b; };
const be4 = (n) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, n>>>0, false); return b; };
const be8 = (n) => { const b = new Uint8Array(8); new DataView(b.buffer).setBigUint64(0, BigInt(n), false); return b; };
const concat = (...a) => { let n=0; for (const x of a) n+=x.length; const o=new Uint8Array(n); let p=0; for (const x of a){ o.set(x,p); p+=x.length; } return o; };
const NUL = new Uint8Array([0]);

async function hkdf32(secret, info) {
  const k = await crypto.subtle.importKey("raw", secret, "HKDF", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name:"HKDF", hash:"SHA-256", salt: TE.encode("ezup/v1/salt"), info: TE.encode(info) }, k, 256);
  return new Uint8Array(bits);
}
async function sha256hex(bytes) { return bytes2hex(await crypto.subtle.digest("SHA-256", bytes)); }

async function deriveKeyset(pasted) {
  const m = /^(ezu_|ezr_)([0-9a-f]{64})$/.exec(pasted);
  if (!m) throw new Error("not a pasted key: expected ezu_/ezr_ + 64 lowercase hex chars");
  const S = hex2bytes(m[2]);
  const kAuth = await hkdf32(S, "ezup/v1/auth");
  const encKey = await hkdf32(S, "ezup/v1/enc");
  const bearer = "ezw_" + bytes2hex(kAuth);
  const keyid = (await sha256hex(TE.encode(bearer))).slice(0, 16);
  return { kind: m[1] === "ezu_" ? "device" : "reader", bearer, encKey, keyid };
}

const chunkNonce = (gen, offset) => concat(be4(gen), be8(offset));
const chunkAad = (session, gen, offset) => concat(TE.encode("ezup/v1/chunk"), NUL, TE.encode(session), NUL, be4(gen), be8(offset));
const wrapAad = (session, rid, gen) => concat(TE.encode("ezup/v1/wrap"), NUL, TE.encode(session), NUL, TE.encode(rid), NUL, be4(gen));

async function unwrapDk(encKey, session, rid, gen, blob) {
  if (blob.length !== 60) throw new Error("wrapped key blob is " + blob.length + " bytes, expected 60");
  const k = await crypto.subtle.importKey("raw", encKey, "AES-GCM", false, ["decrypt"]);
  const dk = await crypto.subtle.decrypt(
    { name:"AES-GCM", iv: blob.slice(0,12), additionalData: wrapAad(session,rid,gen), tagLength:128 }, k, blob.slice(12));
  return new Uint8Array(dk);
}
async function decryptChunk(dk, session, gen, offset, body) {
  const k = await crypto.subtle.importKey("raw", dk, "AES-GCM", false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt(
    { name:"AES-GCM", iv: chunkNonce(gen,offset), additionalData: chunkAad(session,gen,offset), tagLength:128 }, k, body);
  return new Uint8Array(pt);
}

async function selfTest() {
  const kAuth = bytes2hex(await hkdf32(hex2bytes("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"), "ezup/v1/auth"));
  const kEnc = bytes2hex(await hkdf32(hex2bytes("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"), "ezup/v1/enc"));
  const bearer = "ezw_" + kAuth;
  const tokenSha = await sha256hex(TE.encode(bearer));
  const okKdf = kAuth === "c587d5c13882bb99c0db1bdeb631f580a6af77dd47d646a7558d3d48c23c3677"
    && kEnc === "38b074ce889e57c645145ef370ba7e63478b188603a463c875cfde5f8652eef5"
    && tokenSha === "01d236f19c3dfb00fa29e633cd93cc5c8f97893db5fbd0c095280156499b58d8";
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
const STORE_KEY = "ezup_keyring";
let TAB_ONLY = localStorage.getItem("ezup_tabonly") === "1";
let SHOW_LEGACY = false;
let KEYS = [];
let MODEL = new Map();     // session id -> row
let ROWS = [];

function persist() {
  const raw = JSON.stringify(KEYS.map(k => ({ token: k.token, label: k.label })));
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
    catch { /* corrupt stored entry dropped */ }
  }
}

function keyColor(keyid) { let h = 0; for (const c of keyid) h = (h * 31 + c.charCodeAt(0)) % 360; return "hsl(" + h + " 60% 45%)"; }

async function api(path, bearer) {
  const r = await fetch(path, { headers: { Authorization: "Bearer " + bearer } });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("HTTP " + r.status));
  return r;
}

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

function rowState(r) {
  if (r.wrapKeys.length) return { kind: "enc", key: r.wrapKeys[0].key };
  if (r.enc === "aead-v1") return { kind: "locked" };
  return { kind: "legacy" };
}

// --- Sidebar --------------------------------------------------------------
function renderKeychips() {
  const box = document.getElementById("keychips");
  box.innerHTML = KEYS.map(k => {
    const bad = k.status !== "ok" && k.status !== "unknown";
    return \`<div class="keychip \${bad?"bad":""}" title="\${esc(k.keyid)} · \${esc(k.kind)}">
      <span class="kd" style="background:\${keyColor(k.keyid)}"></span>
      <b>\${esc(k.label)}</b>
      <span class="st">\${esc(bad ? k.status : k.kind)}</span>
      <span class="kx" data-forget="\${esc(k.keyid)}" title="forget">×</span></div>\`;
  }).join("") || '<div class="keychip" style="color:var(--faint);border-style:dashed">no keys yet</div>';
  box.querySelectorAll("[data-forget]").forEach(x => x.onclick = () => {
    KEYS = KEYS.filter(k => k.keyid !== x.dataset.forget); persist(); refresh();
  });
}

function renderSessions() {
  const el = document.getElementById("sessions");
  document.getElementById("foot").textContent =
    KEYS.length + " key(s) · " + ROWS.length + " session(s)";
  if (!ROWS.length) {
    el.innerHTML = '<div class="empty" style="padding:30px 12px;font-size:12.5px">nothing to read yet.<br>add a reader key above.</div>';
    return;
  }
  // group by author, recent first within each; authors by most-recent activity
  const groups = new Map();
  for (const r of ROWS) { const a = r.author || "unknown"; if (!groups.has(a)) groups.set(a, []); groups.get(a).push(r); }
  const authors = [...groups.entries()].map(([author, rs]) => {
    rs.sort((a,b) => (b.updated_at||"").localeCompare(a.updated_at||""));
    return { author, rs, last: rs[0].updated_at||"" };
  }).sort((a,b) => (b.last||"").localeCompare(a.last||""));

  const active = location.hash.slice(3);   // #s/<id>
  let html = "";
  for (const g of authors) {
    html += \`<div class="authorhdr"><span>\${esc(g.author)}</span><span class="n">\${g.rs.length}</span></div>\`;
    for (const r of g.rs) {
      const st = rowState(r);
      html += \`<button class="slink \${r.session===active?"active":""}" data-s="\${esc(r.session)}">
        <div class="st"><span class="dot \${st.kind}"></span>\${esc(r.title || r.session.slice(0,8))}</div>
        <div class="sm">\${esc(r.project||"")} · \${esc(when(r.updated_at))} · \${human(r.size||0)}</div>
      </button>\`;
    }
  }
  el.innerHTML = html;
  el.querySelectorAll(".slink").forEach(b => b.onclick = () => { location.hash = "s/" + b.dataset.s; });
}

async function addKey() {
  const inp = document.getElementById("newkey");
  const err = document.getElementById("keyerr");
  err.textContent = "";
  const pasted = inp.value.trim();
  if (!pasted) return;
  let ks;
  try { ks = await deriveKeyset(pasted); } catch { err.textContent = "not a valid ezu_/ezr_ key"; return; }
  if (KEYS.some(k => k.keyid === ks.keyid)) { err.textContent = "already on the keyring"; return; }
  try { await api("/v1/sessions", ks.bearer); } catch (e) { err.textContent = e.message === "unauthorized" ? "key refused" : e.message; return; }
  KEYS.push({ token: pasted, label: ks.keyid.slice(0,4), status: "ok", recipientId: null, ...ks });
  inp.value = ""; persist();
  await refresh();
}

// --- Reading pane ---------------------------------------------------------
function crumb(r) {
  const c = document.getElementById("crumb");
  if (!r) { c.innerHTML = '<b>Sessions</b>'; return; }
  c.innerHTML = \`<a class="back" onclick="location.hash=''">← all</a>
    <span class="sep">/</span><span>\${esc(r.author)}</span>
    <span class="sep">/</span><b>\${esc(r.title || r.session.slice(0,8))}</b>\`;
}

function reader() { return document.getElementById("reader"); }

function showEmpty() {
  crumb(null);
  reader().replaceChildren($(\`<div class="empty">
    <b>Select a session</b><br>pick one from the sidebar to read it.
    \${ROWS.length ? "" : "<br><br>No sessions yet — add a reader key, or run <b>/ezup on</b> in a session."}</div>\`));
}

async function openSession(r) {
  crumb(r);
  reader().replaceChildren($('<div class="loading">opening…</div>'));
  const st = rowState(r);
  try {
    if (st.kind === "enc") await renderEncrypted(r);
    else if (st.kind === "locked") notice(r,
      \`🔒 no key here can open this session. It is encrypted (aead-v1) but none of your keys hold a wrapped data key for it.\`, "");
    else if (!SHOW_LEGACY) notice(r,
      \`This session is stored as unencrypted legacy plaintext and no key here holds a wrap for it, so its authenticity cannot be verified. Tick <b>show unverified legacy</b> to view it anyway.\`, "");
    else await renderLegacy(r);
  } catch (e) { fail(r, e.message || String(e)); }
}

function frame(banners) {
  const r = reader();
  r.replaceChildren($(\`<div>\${banners||""}<div id="log"></div></div>\`));
  return document.getElementById("log");
}
function notice(r, msg, cls) { frame(\`<div class="banner \${cls}">\${msg}</div>\`); }
function fail(r, msg) { frame(\`<div class="err"><b>cannot render this session</b><br>\${esc(msg)}</div>\`); }
function tailBanner(total, skipped) {
  return skipped ? \`<div class="banner">large session: showing the last \${human(total-skipped)}; \${human(skipped)} earlier not loaded</div>\` : "";
}

async function fetchDecrypted(r, enc) {
  const wk = enc ? r.wrapKeys[0] : null;
  const k = enc ? wk.key : r.listKeys[0];
  const man = await (await api("/v1/chunks?session=" + encodeURIComponent(r.session), k.bearer)).json();

  if (enc) {
    // F1 DOWNGRADE PIN — a held wrap proves this is aead-v1; anything else is a downgrade.
    if (man.enc !== "aead-v1") { const e = new Error("DOWNGRADE:" + (man.enc==null?"plaintext":man.enc)); e.downgrade = true; throw e; }
    const gen = Number(man.enc_gen);
    if (Number(wk.wrap.enc_gen) !== gen) throw new Error("generation mismatch: chunks are gen " + gen + " but your wrapped key is gen " + wk.wrap.enc_gen + " — a rotation is in flight, reload to retry");
    var dk;
    try { dk = await unwrapDk(k.encKey, r.session, wk.recipientId, wk.wrap.enc_gen, b64d(wk.wrap.wrap)); }
    catch (e) { throw new Error("could not unwrap the data key (wrong recipient, tampered wrap, or bad key): " + e.message); }
    var GEN = gen;
  } else if (man.enc === "aead-v1") {
    const e = new Error("now reported encrypted but no key here can open it"); e.locked = true; throw e;
  }

  const chunks = (man.chunks || []).slice().sort((a,b) => a.offset - b.offset);
  let picked = chunks, skipped = 0;
  const total = chunks.reduce((n,c) => n + c.length, 0);
  while (picked.length > 1 && picked.slice(1).reduce((n,c) => n + c.length, 0) >= TAIL_BYTES) { skipped += picked[0].length; picked = picked.slice(1); }
  const parts = [];
  for (const c of picked) {
    if (enc) {
      const body = new Uint8Array(await (await api("/v1/blob?key=" + encodeURIComponent(c.key), k.bearer)).arrayBuffer());
      if (c.sha256 && (await sha256hex(body)) !== c.sha256) throw new Error("chunk at offset " + c.offset + " failed its ciphertext checksum — the store served the wrong bytes (nothing rendered)");
      let pt;
      try { pt = await decryptChunk(dk, r.session, GEN, c.offset, body); }
      catch { throw new Error("chunk at offset " + c.offset + " (gen " + GEN + ") failed its GCM tag — corrupt, resealed, or forged ciphertext (nothing rendered)"); }
      parts.push(TD.decode(pt));
    } else {
      parts.push(await (await api("/v1/blob?key=" + encodeURIComponent(c.key), k.bearer)).text());
    }
  }
  let text = parts.join("");
  if (skipped) text = text.slice(text.indexOf("\\n") + 1);
  return { text, total, skipped, key: k };
}

async function renderEncrypted(r) {
  let d;
  try { d = await fetchDecrypted(r, true); }
  catch (e) {
    if (e.downgrade) return frame(\`<div class="err"><b>DOWNGRADE ATTEMPT — refusing to render.</b><br>
      A wrapped data key exists for one of your keys (proof this session is end-to-end encrypted), yet the store
      presents it as "\${esc(e.message.slice(9))}". A reader must never trust the store's enc flag over a wrap it holds.
      Nothing was decoded.</div>\`);
    return fail(r, e.message);
  }
  const k = d.key;
  const badge = \`<div class="banner ok">● end-to-end encrypted · verified · unlocked by <b>\${esc(k.label)}</b> <span class="mono">(\${esc(k.keyid.slice(0,4))})</span></div>\`;
  renderTranscript(frame(badge + tailBanner(d.total, d.skipped)), d.text);
}

async function renderLegacy(r) {
  let d;
  try { d = await fetchDecrypted(r, false); }
  catch (e) { if (e.locked) return notice(r, "🔒 this session is now reported encrypted but no key here can open it.", ""); return fail(r, e.message); }
  const badge = '<div class="banner warn">⚠ legacy: stored unencrypted · <b>unverified plaintext</b> — the store could have altered these bytes and nothing here would detect it</div>';
  renderTranscript(frame(badge + tailBanner(d.total, d.skipped)), d.text);
}

// --- Transcript: parse once, render in lazy-loaded pages ------------------
function parseTurns(text) {
  const turns = [];
  for (const line of text.split("\\n")) {
    if (!line.trim()) continue;
    let o; try { o = JSON.parse(line); } catch { continue; }
    const t = o.type, ts = o.timestamp || "";
    if (t !== "user" && t !== "assistant") continue;
    const c = o.message && o.message.content;
    if (typeof c === "string") { if (c.trim()) turns.push({ role: t, text: c, ts }); continue; }
    if (!Array.isArray(c)) continue;
    for (const b of c) {
      if (!b || typeof b !== "object") continue;
      if (b.type === "text" && b.text && b.text.trim()) turns.push({ role: t, text: b.text, ts });
      else if (b.type === "tool_use") turns.push({ role: "tool", name: b.name, text: JSON.stringify(b.input || {}).slice(0, 240), ts });
    }
  }
  return turns;
}

const PAGE = 60;
function turnEl(turn) {
  const ts = clock(turn.ts);
  if (turn.role === "tool")
    return \`<div class="tool">▸ <span class="name">\${esc(turn.name)}</span>
      <span class="trunc">\${esc(turn.text)}</span>\${ts ? \`<span class="ts">\${esc(ts)}</span>\` : ""}</div>\`;
  const body = turn.text.length > 6000 ? turn.text.slice(0, 6000) + " …" : turn.text;
  return \`<div class="turn \${turn.role}"><div class="meta"><span class="who">\${turn.role}</span>
    \${ts ? \`<span class="ts">\${esc(ts)}</span>\` : ""}</div><pre>\${esc(body)}</pre></div>\`;
}

function renderTranscript(log, text) {
  const turns = parseTurns(text);
  if (!turns.length) { log.appendChild($('<div class="empty">no user/assistant turns in this session</div>')); return; }
  let shown = 0;
  const sentinel = $('<div class="more">loading…</div>');
  const drawPage = () => {
    const end = Math.min(turns.length, shown + PAGE);
    const frag = document.createElement("div");
    frag.innerHTML = turns.slice(shown, end).map(turnEl).join("");
    log.insertBefore(frag, sentinel);
    while (frag.firstChild) log.insertBefore(frag.firstChild, sentinel);
    frag.remove();
    shown = end;
    if (shown >= turns.length) { sentinel.remove(); obs.disconnect(); }
    else sentinel.textContent = (turns.length - shown) + " more turn(s) — scroll to load";
  };
  log.appendChild(sentinel);
  // Lazy load: draw the next page whenever the sentinel scrolls into view.
  const obs = new IntersectionObserver((entries) => {
    if (entries.some(e => e.isIntersecting)) drawPage();
  }, { root: reader(), rootMargin: "400px" });
  drawPage();                 // first page immediately
  obs.observe(sentinel);
}

// --- Router: #s/<session> is the reading pane, empty hash is the list -----
async function route() {
  document.querySelectorAll(".slink").forEach(b =>
    b.classList.toggle("active", "s/" + b.dataset.s === location.hash.slice(1)));
  const id = location.hash.startsWith("#s/") ? location.hash.slice(3) : "";
  if (!id) return showEmpty();
  const r = MODEL.get(id);
  if (!r) return notice_full("session not on this keyring — it may need a key you have not added, or it was removed");
}
function notice_full(msg) {
  crumb(null);
  reader().replaceChildren($(\`<div class="empty"><b>\${esc(msg)}</b></div>\`));
}

async function refresh() {
  renderKeychips();
  MODEL = await buildModel();
  ROWS = [...MODEL.values()];
  renderSessions();
  const id = location.hash.startsWith("#s/") ? location.hash.slice(3) : "";
  const r = id ? MODEL.get(id) : null;
  if (r) openSession(r); else showEmpty();
}

window.addEventListener("hashchange", () => {
  const id = location.hash.startsWith("#s/") ? location.hash.slice(3) : "";
  document.querySelectorAll(".slink").forEach(b => b.classList.toggle("active", b.dataset.s === id));
  const r = id ? MODEL.get(id) : null;
  if (r) openSession(r); else showEmpty();
});

// --- Boot -----------------------------------------------------------------
(async () => {
  let ok = false;
  try { ok = await selfTest(); } catch { ok = false; }
  if (!ok) document.getElementById("selftest").replaceChildren($(\`<div class="err" style="margin:8px 12px">
    <b>crypto self-test FAILED.</b> This build does not reproduce the pinned WebCrypto vectors, so it cannot be
    trusted to decrypt correctly. Do not paste keys here — verify with the CLI instead.</div>\`));

  document.getElementById("addkey").onclick = addKey;
  document.getElementById("newkey").onkeydown = (e) => { if (e.key === "Enter") addKey(); };
  const tab = document.getElementById("tabonly"); tab.checked = TAB_ONLY;
  tab.onchange = (e) => { TAB_ONLY = e.target.checked; localStorage.setItem("ezup_tabonly", TAB_ONLY?"1":"0"); persist(); };
  document.getElementById("legacy").onchange = (e) => { SHOW_LEGACY = e.target.checked; refresh(); };

  await hydrate();
  await refresh();
})();
</script></body></html>`;
