/** The developer-facing log viewer, served at GET /.
 *
 * One self-contained page: no external assets, no cookies, no server-side
 * session. The "login" is the API key -- a device token sees its own
 * sessions, a reader token sees what it is scoped to, exactly like the API,
 * because it IS the API: the page is a thin client over /v1/sessions,
 * /v1/chunks and /v1/blob with an Authorization header.
 *
 * The token is kept in sessionStorage (gone when the tab closes) unless the
 * user ticks "remember on this device".
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
h1 .dot{color:var(--red)}
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
.remember{font-size:12px;color:var(--muted);display:flex;gap:6px;align-items:center}
.error{color:var(--red);font-size:13px;margin-top:10px;min-height:1em}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;
margin-bottom:16px;flex-wrap:wrap}
.who{font-size:12px;color:var(--muted)}
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
footer{margin-top:30px;color:var(--faint);font-size:11.5px}
</style></head><body><div class="wrap">
<h1><span class="dot">●</span> ezup <span style="color:var(--faint)">log viewer</span></h1>
<div class="sub" id="origin"></div>
<div id="app"></div>
<footer>read-only · your API key never leaves this browser except as the
Authorization header to this host</footer>
</div>
<script>
"use strict";
const $ = (h) => { const t = document.createElement("template"); t.innerHTML = h.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const app = document.getElementById("app");
document.getElementById("origin").textContent = location.origin;

let TOKEN = sessionStorage.getItem("ezup_token") || localStorage.getItem("ezup_token") || "";

const api = async (path) => {
  const r = await fetch(path, { headers: { Authorization: "Bearer " + TOKEN } });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || ("HTTP " + r.status));
  return r;
};

const human = (n) => n > 1048576 ? (n/1048576).toFixed(1)+" MB" : n > 1024 ? (n/1024).toFixed(0)+" KB" : n+" B";
const when = (t) => (t || "").replace("T"," ").slice(0, 16);

function login(message) {
  app.replaceChildren($(\`<div class="card" style="max-width:460px">
    <label for="tok">API key</label>
    <input id="tok" type="password" autocomplete="off" placeholder="ezu_… or ezr_…">
    <div class="row">
      <button class="primary" id="go">View logs</button>
      <label class="remember"><input type="checkbox" id="keep"> remember on this device</label>
    </div>
    <div class="error">\${esc(message || "")}</div>
  </div>\`));
  const tok = document.getElementById("tok");
  const go = async () => {
    TOKEN = tok.value.trim();
    if (!TOKEN) return;
    sessionStorage.setItem("ezup_token", TOKEN);
    if (document.getElementById("keep").checked) localStorage.setItem("ezup_token", TOKEN);
    try { await list(); } catch (e) {
      sessionStorage.removeItem("ezup_token"); localStorage.removeItem("ezup_token");
      login(e.message === "unauthorized" ? "that key was refused" : e.message);
    }
  };
  document.getElementById("go").onclick = go;
  tok.onkeydown = (e) => { if (e.key === "Enter") go(); };
  tok.focus();
}

function logout() {
  TOKEN = "";
  sessionStorage.removeItem("ezup_token"); localStorage.removeItem("ezup_token");
  login();
}

async function list() {
  app.replaceChildren($('<div class="loading">loading sessions…</div>'));
  const rows = (await (await api("/v1/sessions")).json()).sessions || [];
  rows.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const table = rows.length ? \`<table><thead><tr>
      <th>updated</th><th>author</th><th>project</th><th>title</th><th>size</th>
    </tr></thead><tbody>\` + rows.map((s, i) => \`
      <tr class="sess" data-i="\${i}">
        <td class="mono">\${esc(when(s.updated_at))}</td>
        <td class="author">\${esc(s.author)}</td>
        <td>\${esc(s.project || "")}</td>
        <td>\${esc(s.title || s.session.slice(0, 8))}</td>
        <td class="mono">\${human(s.size || 0)}</td>
      </tr>\`).join("") + "</tbody></table>"
    : '<div class="empty">nothing shared yet — run <b>/ezup on</b> in a session</div>';
  app.replaceChildren($(\`<div>
    <div class="topbar">
      <span class="who">\${rows.length} session(s) this key can read</span>
      <button id="out">forget key</button>
    </div>
    <div class="card" style="padding:0">\${table}</div>
  </div>\`));
  document.getElementById("out").onclick = logout;
  app.querySelectorAll("tr.sess").forEach(tr =>
    tr.onclick = () => view(rows[Number(tr.dataset.i)]));
}

const TAIL_BYTES = 4 * 1048576;

async function view(sess) {
  app.replaceChildren($('<div class="loading">loading transcript…</div>'));
  const chunks = ((await (await api("/v1/chunks?session=" + encodeURIComponent(sess.session))).json()).chunks || [])
    .sort((a, b) => a.offset - b.offset);
  let picked = chunks, skipped = 0;
  let total = chunks.reduce((n, c) => n + c.length, 0);
  while (picked.length > 1 && picked.slice(1).reduce((n, c) => n + c.length, 0) >= TAIL_BYTES) {
    skipped += picked[0].length; picked = picked.slice(1);
  }
  const parts = [];
  for (const c of picked)
    parts.push(await (await api("/v1/blob?key=" + encodeURIComponent(c.key))).text());
  let text = parts.join("");
  if (skipped) text = text.slice(text.indexOf("\\n") + 1); // drop the cut line

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
      else if (b.type === "tool_use") turns.push({ role: "tool", name: b.name,
        text: JSON.stringify(b.input || {}).slice(0, 200) });
    }
  }
  const cap = 800, shown = turns.slice(-cap);
  app.replaceChildren($(\`<div>
    <div class="crumb"><a id="back">← sessions</a>
      <span class="mono"> · \${esc(sess.author)} · \${esc(sess.title || sess.session.slice(0,8))}
      · \${human(total)}</span></div>
    \${skipped ? \`<div class="banner">large session: showing the last \${human(total - skipped)};
      \${human(skipped)} earlier not loaded</div>\` : ""}
    \${turns.length > cap ? \`<div class="banner">long session: showing the last \${cap} of
      \${turns.length} turns</div>\` : ""}
    <div id="log"></div>
  </div>\`));
  document.getElementById("back").onclick = () => list();
  const log = document.getElementById("log");
  for (const turn of shown) {
    if (turn.role === "tool")
      log.appendChild($(\`<div class="tool">▸ <span class="name">\${esc(turn.name)}</span>
        <span class="trunc">\${esc(turn.text)}</span></div>\`));
    else
      log.appendChild($(\`<div class="turn \${turn.role}"><div class="who">\${turn.role}</div>
        <pre>\${esc(turn.text.length > 4000 ? turn.text.slice(0, 4000) + " …" : turn.text)}</pre></div>\`));
  }
}

if (TOKEN) list().catch(() => login("stored key no longer works")); else login();
</script></body></html>`;
