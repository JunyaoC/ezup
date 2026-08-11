"""Render journal entries to markdown and a single self-contained HTML file."""

from __future__ import annotations

import html
import json
from typing import Any

SECTIONS = [
    ("delivered", "Delivered"),
    ("decisions", "Decisions"),
]


def _as_lines(value: Any) -> list[str]:
    """Entries may arrive as strings or objects; normalise to plain lines."""
    if not value:
        return []
    lines: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, dict):
            text = item.get("what") or item.get("summary") or ""
            why = item.get("why")
            if why:
                text = f"{text} — {why}"
            if text:
                lines.append(text)
    return lines


def _blockers(entry: dict[str, Any]) -> list[dict[str, str]]:
    """Only blockers that carry a resolution are reportable."""
    out: list[dict[str, str]] = []
    for item in entry.get("blockers") or []:
        if not isinstance(item, dict):
            continue
        what = str(item.get("what") or "").strip()
        resolution = str(item.get("resolution") or "").strip()
        if what and resolution:
            out.append({"what": what, "resolution": resolution})
    return out


def _time_of(stamp: str) -> str:
    return stamp[11:16] if len(stamp) >= 16 else ""


def render_markdown(composed: dict[str, Any], meta: dict[str, Any]) -> str:
    entries = composed.get("entries") or []
    window = meta.get("window", {})
    out: list[str] = [
        f"# Journal · {window.get('since','')[:10]} → {window.get('until','')[:10]}",
        "",
        f"{len(entries)} items of work across {len(meta.get('sessions', []))} sessions.",
        "",
    ]

    # Same hierarchy as the page: project is the top level, days nested under it.
    tree: dict[str, dict[str, list[dict]]] = {}
    for entry in entries:
        project = entry.get("project") or "unknown"
        tree.setdefault(project, {}).setdefault(
            entry.get("date") or "undated", []
        ).append(entry)

    for project in sorted(tree):
        out += [f"## {project}", ""]
        days = sorted((d for d in tree[project] if d != "undated"), reverse=True)
        days += ["undated"] if "undated" in tree[project] else []
        for date in days:
            out += [f"### {date}", ""]
            for entry in tree[project][date]:
                out.append(f"#### {entry.get('title','(untitled)')}")
                tags = " · ".join(
                    t for t in (entry.get("kind"), entry.get("status")) if t
                )
                if tags:
                    out.append(f"*{tags}*")
                if entry.get("summary"):
                    out += ["", entry["summary"]]
                for key, label in SECTIONS:
                    lines = _as_lines(entry.get(key))
                    if lines:
                        out += ["", f"**{label}**"] + [f"- {line}" for line in lines]
                blocked = _blockers(entry)
                if blocked:
                    out += ["", "**Blockers**"]
                    out += [f"- {b['what']} → {b['resolution']}" for b in blocked]
                for ref in entry.get("references") or []:
                    if ref.get("file"):
                        out.append(f"- `{ref['file']}`" + (
                            f" · `{ref['commit']}`" if ref.get("commit") else ""))
                snippets = [
                    s for s in (entry.get("snippets") or []) if isinstance(s, dict)
                ]
                if snippets:
                    out += ["", "**Detail**"]
                    for snippet in snippets:
                        if snippet.get("caption"):
                            out.append(f"- {snippet['caption']}")
                        if snippet.get("code"):
                            out += ["", "  ```", f"  {snippet['code']}", "  ```"]
                beats = entry.get("timeline") or []
                if beats:
                    out += ["", "**How it went**"]
                    out += [f"- `{_time_of(b['ts'])}` {b['what']}" for b in beats]
                out.append("")

    skipped = composed.get("skipped") or []
    if skipped:
        out += ["## Collected but not journaled", ""]
        for item in skipped:
            if isinstance(item, dict):
                out.append(f"- `{str(item.get('session',''))[:8]}` — {item.get('why','')}")
        out.append("")
    return "\n".join(out)


_CSS = """
:root{color-scheme:light;
--bg:#f7f7f5;--panel:#fff;--fg:#1c1c1a;--muted:#71716b;--faint:#9a9a93;
--line:#e6e6e1;--accent:#2f5d8a;--accent-soft:#eaf1f8;
--ok:#2c6e4b;--ok-soft:#e8f3ec;--warn:#9a5b16;--warn-soft:#fbf0e2;
--radius:12px;--shadow:0 1px 2px rgba(20,20,18,.05),0 8px 24px rgba(20,20,18,.04)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.62 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif;
-webkit-font-smoothing:antialiased}
.layout{display:grid;grid-template-columns:236px minmax(0,1fr);gap:0;min-height:100vh}

/* sidebar */
aside{position:sticky;top:0;height:100vh;overflow-y:auto;background:var(--panel);
border-right:1px solid var(--line);padding:22px 16px}
aside h1{font-size:15px;margin:0 0 2px;letter-spacing:-.01em}
aside .range{font-size:12px;color:var(--muted);margin-bottom:16px}
.switch{display:flex;background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:2px;margin-bottom:6px}
.switch button{flex:1;font:inherit;font-size:12px;padding:5px 4px;border:0;
background:none;color:var(--muted);border-radius:6px;cursor:pointer}
.switch button[aria-pressed=true]{background:var(--panel);color:var(--fg);
box-shadow:var(--shadow);font-weight:600}
.navlabel{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;
color:var(--faint);margin:18px 0 7px}
nav a{display:flex;justify-content:space-between;gap:8px;align-items:center;
padding:6px 9px;border-radius:7px;color:var(--fg);text-decoration:none;font-size:13px;
border-left:2px solid transparent}
nav a:hover{background:var(--bg)}
nav a.on{background:var(--accent-soft);border-left-color:var(--accent);color:var(--accent);font-weight:600}
nav a .n{font-size:11px;color:var(--faint)}
nav a.on .n{color:var(--accent)}
aside .foot{margin-top:20px;padding-top:14px;border-top:1px solid var(--line);
font-size:11px;color:var(--faint);line-height:1.5}

/* main */
main{padding:26px 34px 90px;max-width:900px}
.topbar{display:flex;align-items:center;gap:10px;margin-bottom:22px;flex-wrap:wrap}
.topbar .switch{margin:0;width:210px}
.spacer{flex:1}
.btn{font:inherit;font-size:12.5px;padding:7px 13px;border:1px solid var(--line);
background:var(--panel);color:var(--fg);border-radius:8px;cursor:pointer;box-shadow:var(--shadow)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.copied{background:var(--ok-soft);border-color:var(--ok);color:var(--ok)}

/* h1 = the axis you grouped by, h2 = the other axis nested inside it */
.block{margin-bottom:46px}
.h1{font-size:27px;line-height:1.2;letter-spacing:-.02em;margin:0 0 4px;
display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.h1 .sub{font-size:13px;font-weight:400;color:var(--faint);letter-spacing:0}
.h1 .count{font-size:12px;font-weight:500;color:var(--muted);background:var(--bg);
border:1px solid var(--line);padding:2px 9px;border-radius:999px;margin-left:auto}
.h1.ax-project{color:var(--accent)}
.block>.h1{border-bottom:2px solid var(--fg);padding-bottom:10px;margin-bottom:20px}
.block>.h1.ax-project{border-bottom-color:var(--accent)}
.sub-block{margin:0 0 22px}
.h2{font-size:12px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted);
margin:0 0 10px;display:flex;align-items:center;gap:10px;font-weight:700}
.h2 .count{font-size:11px;color:var(--faint);font-weight:500}
.h2::after{content:"";flex:1;height:1px;background:var(--line);order:1}
.h2 .count{order:2}

.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
padding:17px 19px;margin-bottom:12px;box-shadow:var(--shadow)}
.card h3{margin:0 0 7px;font-size:16.5px;letter-spacing:-.01em}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.chip{font-size:11px;padding:2.5px 9px;border-radius:999px;background:var(--bg);
color:var(--muted);border:1px solid var(--line)}
.chip.k{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.chip.s-landed{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.summary{margin:0 0 13px;color:#3a3a37}
.sec{margin-top:12px}
.sec>.h{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;
color:var(--faint);margin-bottom:5px}
.sec ul{margin:0;padding-left:17px}
.sec li{margin:3px 0}
.sec.delivered>.h{color:var(--ok)}
.blocker{background:var(--warn-soft);border-radius:9px;padding:9px 12px;margin:6px 0}
.blocker .w{font-weight:600;color:var(--warn);font-size:13.5px}
.blocker .r{font-size:13.5px;margin-top:3px}
.blocker .r::before{content:"→ ";color:var(--warn)}
pre{background:#f3f3ef;border:1px solid var(--line);border-radius:8px;
padding:10px 12px;overflow-x:auto;margin:6px 0;font-size:12.5px;line-height:1.5}
.cap{font-size:12.5px;color:var(--muted);margin-top:8px}
.foot-meta{margin-top:13px;padding-top:9px;border-top:1px dashed var(--line);
font-size:11.5px;color:var(--faint)}
.ref{margin:5px 0}
.reftog{display:flex;gap:9px;align-items:center;width:100%;text-align:left;
font:inherit;font-size:12.5px;padding:6px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);cursor:pointer}
.reftog:hover{border-color:var(--accent)}
.reftog.open{border-color:var(--accent);background:var(--accent-soft)}
.reftog::before{content:"▸";color:var(--faint);font-size:10px}
.reftog.open::before{content:"▾";color:var(--accent)}
.reftog .path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:11.5px;color:var(--accent);white-space:nowrap}
.reftog .sha{font-family:ui-monospace,monospace;font-size:11px;color:var(--ok);
background:var(--ok-soft);padding:1px 6px;border-radius:4px}
.reftog .for{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.refbody{padding:2px 0 6px}
code{background:#f0f0ec;padding:1px 5px;border-radius:4px;font-size:12px}

/* timeline */
.tl{position:relative;padding-left:112px}
.tl::before{content:"";position:absolute;left:92px;top:6px;bottom:6px;width:2px;
background:linear-gradient(180deg,var(--line),var(--line) 70%,transparent)}
.tl-day{position:relative;margin-bottom:30px}
/* Static, not absolute: the day label needs its own row or it lands on top of
   the first beat's time in the same gutter. */
.tl-day>.d{display:block;margin:0 0 13px -112px;width:96px;text-align:right;
font-size:12px;font-weight:700;color:var(--fg)}
.tl-day>.d>span{display:block;font-size:10.5px;font-weight:500;color:var(--faint)}
.beat{position:relative;margin-bottom:9px}
.beat::before{content:"";position:absolute;left:-25px;top:8px;width:9px;height:9px;
border-radius:50%;background:var(--dot,var(--accent));box-shadow:0 0 0 3px var(--panel)}
.beat.first::before{width:13px;height:13px;left:-27px;top:6px}
.beat .body{border-left:3px solid var(--dot,var(--accent))}
.beat .t{position:absolute;left:-112px;width:84px;text-align:right;
font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums;padding-top:2px}
.beat .body{background:var(--panel);border:1px solid var(--line);border-radius:9px;
padding:9px 13px;box-shadow:var(--shadow)}
.beat .what{font-size:14px}
.beat .who{font-size:11px;color:var(--faint);margin-top:2px}
.empty{color:var(--muted);padding:26px 0}
@media(max-width:820px){
 .layout{grid-template-columns:1fr}
 aside{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}
 main{padding:20px 18px 60px}
 .tl{padding-left:64px}.tl::before{left:46px}
 .tl-day>.d,.beat .t{left:-64px;width:40px}
}
"""

_JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const ENTRIES = DATA.entries || [];
/* Projects are the top-level aggregate: that is how the work is actually
   owned, so it is what the page opens on. */
let groupBy = 'project', view = 'journal', active = null;

const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const lines = v => !v ? [] : (Array.isArray(v) ? v : [v]).map(i => {
  if (typeof i === 'string') return i;
  if (i && typeof i === 'object') {
    let t = i.what || i.summary || '';
    if (i.why) t += ' — ' + i.why;
    return t;
  }
  return '';
}).filter(Boolean);

const blockers = e => (e.blockers || []).filter(b => b && b.what && b.resolution);
const keyOf = e => groupBy === 'date' ? (e.date || 'undated')
               : groupBy === 'project' ? (e.project || 'unknown')
               : (e.kind || 'other');
const hhmm = ts => (ts || '').slice(11, 16);
const slug = k => 'g-' + String(k).replace(/[^a-z0-9]+/gi, '-').toLowerCase();

/* Stable colour per project, so a day's rail shows at a glance how many
   different things were touched. */
const PALETTE = ['#2f5d8a','#2c6e4b','#9a5b16','#7a3e8f','#b5452f','#1f7a8c','#5a5f2c'];
const dotColor = name => {
  let h = 0;
  for (const ch of String(name || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
};

/* The aggregation you pick becomes the h1; the other axis becomes the h2
   nested inside it. Group by date and you read days containing projects;
   group by project and you read projects containing days. */
const SECOND = { date: 'project', project: 'date', kind: 'project' };
const valueOf = (e, axis) => axis === 'date' ? (e.date || 'undated')
                          : axis === 'project' ? (e.project || 'unknown')
                          : (e.kind || 'other');

function sortKeys(keys, axis) {
  let out = keys.slice().sort();
  if (axis === 'date') out.reverse();
  return out.filter(k => k !== 'undated').concat(out.filter(k => k === 'undated'));
}

function groups() {
  const top = {};
  for (const e of ENTRIES) (top[valueOf(e, groupBy)] ||= []).push(e);
  const second = SECOND[groupBy];
  return sortKeys(Object.keys(top), groupBy).map(k => {
    const sub = {};
    for (const e of top[k]) (sub[valueOf(e, second)] ||= []).push(e);
    return [k, top[k], sortKeys(Object.keys(sub), second).map(s => [s, sub[s]])];
  });
}

function renderNav() {
  const gs = groups().map(([k, items]) => [k, items]);
  document.getElementById('navlabel').textContent =
    groupBy === 'date' ? 'Days' : groupBy === 'project' ? 'Projects' : 'Kinds';
  document.getElementById('nav').innerHTML = gs.map(([k, items]) =>
    `<a href="#${slug(k)}" data-key="${esc(k)}" class="${k === active ? 'on' : ''}">
       <span>${esc(k)}</span><span class="n">${items.length}</span></a>`).join('');
}

function card(e) {
  const secs = [['delivered','Delivered'],['decisions','Decisions']]
    .filter(([k]) => lines(e[k]).length)
    .map(([k, label]) => `<div class="sec ${k}"><div class="h">${label}</div>
      <ul>${lines(e[k]).map(l => `<li>${esc(l)}</li>`).join('')}</ul></div>`).join('');

  const bl = blockers(e);
  const blHtml = bl.length ? `<div class="sec"><div class="h">Blockers</div>
    ${bl.map(b => `<div class="blocker"><div class="w">${esc(b.what)}</div>
      <div class="r">${esc(b.resolution)}</div></div>`).join('')}</div>` : '';

  const sn = (e.snippets || []).filter(s => s && (s.code || s.caption));
  const snHtml = sn.length ? `<div class="sec"><div class="h">Detail</div>
    ${sn.map(s => `${s.caption ? `<div class="cap">${esc(s.caption)}</div>` : ''}
      ${s.code ? `<pre>${esc(s.code)}</pre>` : ''}`).join('')}</div>` : '';

  const refs = (e.references || []).filter(r => r && (r.code || r.commit));
  const refHtml = refs.length ? `<div class="sec"><div class="h">Code</div>
    ${refs.map((r, i) => `<div class="ref">
      <button class="reftog" data-ref="${esc(e.id)}-${i}">
        ${r.file ? `<span class="path">${esc(r.file.split('/').slice(-2).join('/'))}</span>` : ''}
        ${r.commit ? `<span class="sha">${esc(r.commit)}</span>` : ''}
        ${r.claim ? `<span class="for">${esc(String(r.claim).slice(0, 68))}</span>` : ''}
      </button>
      <div class="refbody" id="ref-${esc(e.id)}-${i}" hidden>
        ${r.subject ? `<div class="cap">${esc(r.subject)}</div>` : ''}
        ${r.code ? `<pre>${esc(r.code)}</pre>` : ''}
        ${r.file ? `<div class="cap">${esc(r.file)}</div>` : ''}
      </div></div>`).join('')}</div>` : '';

  const chips = [
    groupBy !== 'project' && e.project && `<span class="chip">${esc(e.project)}</span>`,
    e.kind && `<span class="chip k">${esc(e.kind)}</span>`,
    e.status && `<span class="chip s-${esc(e.status)}">${esc(e.status)}</span>`,
    groupBy !== 'date' && e.date && `<span class="chip">${esc(e.date)}</span>`,
  ].filter(Boolean).join('');

  const ss = (e.sessions || []).map(s => `<code>${esc(String(s).slice(0,8))}</code>`).join(' ');

  return `<article class="card">
    <h3>${esc(e.title || 'Untitled')}</h3>
    <div class="chips">${chips}</div>
    ${e.summary ? `<p class="summary">${esc(e.summary)}</p>` : ''}
    ${secs}${blHtml}${refHtml}${snHtml}
    ${ss ? `<div class="foot-meta">from ${ss}</div>` : ''}
  </article>`;
}

function prettyDay(k) {
  const d = new Date(k + 'T00:00:00');
  if (isNaN(d)) return esc(k);
  return d.toLocaleDateString(undefined,
    { weekday: 'long', day: 'numeric', month: 'long' });
}

function heading(key, axis, level, count) {
  const label = axis === 'date' && key !== 'undated' ? prettyDay(key) : esc(key);
  const sub = axis === 'date' && key !== 'undated' ? `<span class="sub">${esc(key)}</span>` : '';
  return level === 1
    ? `<h1 class="h1 ax-${axis}" id="${slug(key)}">${label}${sub}
         <span class="count">${count} item${count === 1 ? '' : 's'}</span></h1>`
    : `<h2 class="h2 ax-${axis}">${label}<span class="count">${count}</span></h2>`;
}

function renderJournal() {
  const gs = groups();
  if (!gs.length) return '<p class="empty">No entries.</p>';
  const second = SECOND[groupBy];
  return gs.map(([k, items, subs]) => `<section class="block">
    ${heading(k, groupBy, 1, items.length)}
    ${subs.map(([s, list]) => `<section class="sub-block">
        ${heading(s, second, 2, list.length)}
        ${list.map(card).join('')}</section>`).join('')}
  </section>`).join('');
}

function renderTimeline() {
  const beats = [];
  for (const e of ENTRIES)
    for (const b of e.timeline || [])
      if (b && b.ts) beats.push({ ...b, entry: e });
  beats.sort((a, z) => a.ts.localeCompare(z.ts));
  if (!beats.length) return '<p class="empty">No timeline recorded.</p>';

  const days = {};
  for (const b of beats) (days[b.ts.slice(0, 10)] ||= []).push(b);

  return `<div class="tl">` + Object.keys(days).sort().reverse().map(day => {
    const d = new Date(day + 'T00:00:00');
    const label = isNaN(d) ? day
      : d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
    const n = days[day].length;
    return `<div class="tl-day" id="${slug(day)}">
      <div class="d">${esc(label)}<span>${n} step${n === 1 ? '' : 's'}</span></div>
      ${days[day].map((b, i) => `<div class="beat ${i === 0 ? 'first' : ''}"
          style="--dot:${dotColor(b.entry.project)}">
        <div class="t">${esc(hhmm(b.ts))}</div>
        <div class="body"><div class="what">${esc(b.what)}</div>
          <div class="who">${esc(b.entry.project || '')}${b.entry.title ? ' · ' + esc(b.entry.title) : ''}</div>
        </div></div>`).join('')}
    </div>`;
  }).join('') + `</div>`;
}

function render() {
  renderNav();
  document.getElementById('body').innerHTML =
    view === 'timeline' ? renderTimeline() : renderJournal();
}

/* Plain text for pasting into WhatsApp: * * is WhatsApp's bold. */
function plainText() {
  const w = DATA.meta && DATA.meta.window || {};
  const out = [`*Work journal ${(w.since||'').slice(0,10)} - ${(w.until||'').slice(0,10)}*`, ''];

  if (view === 'timeline') {
    const beats = [];
    for (const e of ENTRIES) for (const b of e.timeline || []) if (b && b.ts) beats.push({...b, entry:e});
    beats.sort((a, z) => a.ts.localeCompare(z.ts));
    let day = '';
    for (const b of beats) {
      if (b.ts.slice(0,10) !== day) { day = b.ts.slice(0,10); out.push(`*${day}*`); }
      out.push(`${hhmm(b.ts)}  ${b.what}${b.entry.project ? ' (' + b.entry.project + ')' : ''}`);
    }
    out.push('');
    return out.join('\n');
  }

  for (const [k, items] of groups()) {
    out.push(`*${k}*`);
    for (const e of items) {
      out.push(`• ${e.title || 'Untitled'}`);
      if (e.summary) out.push(`  ${e.summary}`);
      for (const l of lines(e.delivered)) out.push(`  - ${l}`);
      for (const l of lines(e.decisions)) out.push(`  - ${l}`);
      for (const b of blockers(e)) out.push(`  ! ${b.what} -> ${b.resolution}`);
      out.push('');
    }
  }
  return out.join('\n');
}

async function copyPlain(button) {
  const text = plainText();
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    // file:// is not a secure context in some browsers; fall back.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) { window.prompt('Copy:', text); }
    ta.remove();
  }
  button.textContent = 'Copied';
  button.classList.add('copied');
  setTimeout(() => { button.textContent = 'Copy as plain text'; button.classList.remove('copied'); }, 1600);
}

document.addEventListener('click', ev => {
  const tog = ev.target.closest('.reftog');
  if (tog) {
    const body = document.getElementById('ref-' + tog.dataset.ref);
    if (body) { body.hidden = !body.hidden; tog.classList.toggle('open', !body.hidden); }
    return;
  }

  const nav = ev.target.closest('nav a');
  if (nav) { active = nav.dataset.key; renderNav(); return; }

  const copy = ev.target.closest('#copy');
  if (copy) { copyPlain(copy); return; }

  const b = ev.target.closest('button[data-group],button[data-view]');
  if (!b) return;
  if (b.dataset.group) { groupBy = b.dataset.group; active = null; }
  if (b.dataset.view) view = b.dataset.view;
  document.querySelectorAll('button[data-group]').forEach(x =>
    x.setAttribute('aria-pressed', String(x.dataset.group === groupBy)));
  document.querySelectorAll('button[data-view]').forEach(x =>
    x.setAttribute('aria-pressed', String(x.dataset.view === view)));
  render();
});

render();
"""


def render_html(composed: dict[str, Any], meta: dict[str, Any]) -> str:
    window = meta.get("window", {})
    entries = composed.get("entries") or []
    skipped = composed.get("skipped") or []
    since, until = window.get("since", "")[:10], window.get("until", "")[:10]

    payload = json.dumps(
        {"entries": entries, "skipped": skipped, "meta": meta}, ensure_ascii=False
    ).replace("</", "<\\/")

    beats = sum(len(e.get("timeline") or []) for e in entries)
    commits = sum(r.get("commits", 0) for r in meta.get("repos") or [])
    repo_note = f" · {commits} commits" if commits else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Journal · {html.escape(since)} → {html.escape(until)}</title>
<style>{_CSS}</style></head>
<body><div class="layout">
<aside>
  <h1>Work journal</h1>
  <div class="range">{html.escape(since)} → {html.escape(until)}</div>
  <div class="switch">
    <button data-group="project" aria-pressed="true">Project</button>
    <button data-group="date" aria-pressed="false">Date</button>
    <button data-group="kind" aria-pressed="false">Kind</button>
  </div>
  <div class="navlabel" id="navlabel">Projects</div>
  <nav id="nav"></nav>
  <div class="foot">
    {len(entries)} items · {len(meta.get('sessions') or [])} sessions · {beats} beats{repo_note}
  </div>
</aside>
<main>
  <div class="topbar">
    <div class="switch">
      <button data-view="journal" aria-pressed="true">Journal</button>
      <button data-view="timeline" aria-pressed="false">Timeline</button>
    </div>
    <span class="spacer"></span>
    <button class="btn" id="copy">Copy as plain text</button>
  </div>
  <div id="body"></div>
</main>
</div>
<script type="application/json" id="data">{payload}</script>
<script>{_JS}</script>
</body></html>"""
