/* Musicarr web UI — dependency-free SPA */
"use strict";
const API = "/api/v1";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const state = { meta: null, profiles: [], mprofiles: [], roots: [], settings: {}, artists: null };
const store = {
  get(k, d) { try { const v = localStorage.getItem("musicarr." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("musicarr." + k, JSON.stringify(v)); } catch {} },
};

async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(API + path, o);
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}
const post = (p, b) => api(p, { method: "POST", body: b || {} });
const put = (p, b) => api(p, { method: "PUT", body: b || {} });
const del = p => api(p, { method: "DELETE" });

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind; t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "error" ? 7000 : 3500);
}
async function run(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast(e.message, "error"); throw e; }
}
const size = n => { n = n || 0; for (const u of ["B","KB","MB","GB","TB"]) { if (n < 1024) return n.toFixed(u === "B" ? 0 : 1) + " " + u; n /= 1024; } return n.toFixed(1) + " PB"; };
const year = d => (d || "").slice(0, 4) || "TBA";
const ago = iso => { if (!iso) return "—"; const s = (Date.now() - new Date(iso)) / 1000; if (s < 60) return "just now"; if (s < 3600) return Math.floor(s/60) + "m ago"; if (s < 86400) return Math.floor(s/3600) + "h ago"; return Math.floor(s/86400) + "d ago"; };
const fmtDate = iso => iso ? new Date(iso).toLocaleString() : "—";
const initials = n => (n || "?").replace(/^the /i, "").split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();
function qualityLabel(q) {
  if (!q) return "";
  const c = /FLAC|ALAC|WAV|APE|WavPack/.test(q) ? "green" : /320|V0|AAC-256|AAC-320|Opus|Q10/.test(q) ? "blue" : q === "Unknown" ? "gray" : "orange";
  return `<span class="label ${c}">${esc(q)}</span>`;
}
const protoLabel = p => `<span class="label ${p === "torrent" ? "green" : p === "usenet" ? "blue" : "purple"}">${esc(p)}</span>`;
function progressBar(have, total, lg) {
  const pct = total ? Math.round(have / total * 100) : 0;
  const cls = !total ? "" : pct >= 100 ? "" : pct > 0 ? "partial" : "none";
  return `<div class="progress ${cls} ${lg ? "lg" : ""}"><div style="width:${total ? Math.max(pct, 2) : 0}%"></div>${lg ? `<span>${have}/${total}</span>` : ""}</div>`;
}

/* ---------------- modal ---------------- */
function modal({ title, body = "", footer = "", wide = false, onClose }) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal ${wide ? "wide" : ""}"><header><span>${title}</span><button class="icon-btn x">✕</button></header>
    <div class="body">${body}</div>${footer ? `<footer>${footer}</footer>` : ""}</div>`;
  const close = () => { back.remove(); onClose && onClose(); };
  back.addEventListener("mousedown", e => { if (e.target === back) close(); });
  $(".x", back).onclick = close;
  $("#modalRoot").appendChild(back);
  back.close = close;
  return back;
}
function confirmBox(title, text, okLabel = "Yes", danger = true, extra = "") {
  return new Promise(res => {
    const m = modal({ title, body: `<p>${text}</p>${extra}`, footer: `<button class="btn cancel">Cancel</button><button class="btn ${danger ? "danger" : "primary"} ok">${okLabel}</button>`, onClose: () => res(null) });
    $(".cancel", m).onclick = () => { m.remove(); res(null); };
    $(".ok", m).onclick = () => { const vals = {}; $$("input", m).forEach(i => vals[i.name] = i.type === "checkbox" ? i.checked : i.value); m.remove(); res(vals); };
  });
}

/* ---------------- dynamic forms ---------------- */
// field: {key, label, type: text|password|number|bool|select|multi|chips|textarea|folder, options, help, placeholder}
function renderFields(fields, vals) {
  return fields.map(f => {
    const v = getPath(vals, f.key) ?? f.default ?? "";
    let input;
    switch (f.type) {
      case "bool": input = `<label class="switch"><input type="checkbox" data-k="${f.key}" ${v === true || v === 1 || v === "1" ? "checked" : ""}><span></span></label>`; break;
      case "select": input = `<select data-k="${f.key}">${f.options.map(o => { const [ov, ol] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(ov)}" ${String(ov) === String(v) ? "selected" : ""}>${esc(ol)}</option>`; }).join("")}</select>`; break;
      case "chips": { const sel = new Set(v || []); input = `<div class="chips" data-k="${f.key}" data-chips>${f.options.map(o => `<span class="chip ${sel.has(o) ? "on" : ""}" data-v="${esc(o)}">${esc(o)}</span>`).join("")}</div>`; break; }
      case "textarea": input = `<textarea data-k="${f.key}" placeholder="${esc(f.placeholder || "")}">${esc(v)}</textarea>`; break;
      case "folder": input = `<div class="row"><input class="grow" type="text" data-k="${f.key}" value="${esc(v)}" placeholder="${esc(f.placeholder || "")}"><button class="btn sm" type="button" data-browse="${f.key}">Browse</button></div>`; break;
      default: input = `<input type="${f.type || "text"}" data-k="${f.key}" value="${esc(v)}" placeholder="${esc(f.placeholder || "")}">`;
    }
    return `<div class="field"><label>${esc(f.label)}</label><div>${input}${f.help ? `<div class="help">${f.help}</div>` : ""}</div></div>`;
  }).join("");
}
function bindFields(root) {
  $$("[data-chips] .chip", root).forEach(c => c.onclick = () => c.classList.toggle("on"));
  $$("[data-browse]", root).forEach(b => b.onclick = async () => {
    const inp = $(`[data-k="${b.dataset.browse}"]`, root);
    const p = await folderPicker(inp.value || "/");
    if (p) inp.value = p;
  });
}
function readFields(root, fields) {
  const out = {};
  for (const f of fields) {
    const el = $(`[data-k="${f.key}"]`, root);
    if (!el) continue;
    let v;
    if (f.type === "bool") v = el.checked;
    else if (f.type === "number") v = el.value === "" ? null : Number(el.value);
    else if (f.type === "chips") v = $$(".chip.on", el).map(c => c.dataset.v);
    else v = el.value;
    setPath(out, f.key, v);
  }
  return out;
}
function getPath(o, k) { return k.split(".").reduce((a, p) => a == null ? a : a[p], o); }
function setPath(o, k, v) { const ps = k.split("."); let c = o; ps.slice(0, -1).forEach(p => c = c[p] = c[p] || {}); c[ps.at(-1)] = v; }

function folderPicker(start) {
  return new Promise(res => {
    let cur = start;
    const m = modal({ title: "Choose folder", body: `<div class="row" style="margin-bottom:8px"><input type="text" id="fsPath" class="grow"><button class="btn sm" id="fsGo">Go</button></div><div class="fs-list" id="fsList"></div>`,
      footer: `<button class="btn cancel">Cancel</button><button class="btn primary ok">Select</button>`, onClose: () => res(null) });
    const load = async p => {
      const d = await api("/filesystem?path=" + encodeURIComponent(p));
      cur = d.path; $("#fsPath", m).value = cur;
      $("#fsList", m).innerHTML = (d.error ? `<div class="dim">${esc(d.error)}</div>` : "") +
        `<div data-p="${esc(d.parent)}">⬑ ..</div>` + d.dirs.map(n => `<div data-p="${esc((cur.replace(/\/$/, "")) + "/" + n)}">📁 ${esc(n)}</div>`).join("") +
        d.files.slice(0, 60).map(n => `<div class="dim" style="cursor:default">♪ ${esc(n)}</div>`).join("");
      $$("[data-p]", m).forEach(el => el.onclick = () => load(el.dataset.p));
    };
    $("#fsGo", m).onclick = () => load($("#fsPath", m).value);
    $(".cancel", m).onclick = () => { m.remove(); res(null); };
    $(".ok", m).onclick = () => { m.remove(); res($("#fsPath", m).value); };
    load(cur).catch(() => load("/"));
  });
}

/* ---------------- shared data ---------------- */
async function loadShared(force) {
  if (state.meta && !force) return;
  const [meta, profiles, mprofiles, roots, settings] = await Promise.all([
    api("/meta"), api("/res/quality_profiles"), api("/res/metadata_profiles"), api("/res/root_folders"), api("/settings")]);
  Object.assign(state, { meta, profiles, mprofiles, roots, settings });
}
const profName = id => (state.profiles.find(p => p.id === id) || {}).name || "—";

/* ---------------- router ---------------- */
const routes = [];
const route = (re, fn) => routes.push([re, fn]);
let pollTimer = null;
async function router() {
  clearInterval(pollTimer);
  document.body.classList.remove("nav-open");
  $("#modalRoot").innerHTML = "";
  const h = location.hash.slice(1) || "/artists";
  const sec = h.split("/")[1];
  $$(".sidebar>a").forEach(a => a.classList.toggle("active", a.dataset.nav === (["add", "import", "artist", "editor"].includes(sec) ? "artists" : sec)));
  $$(".sidebar .sub").forEach(s => s.classList.toggle("open", s.dataset.sub === (["add", "import", "artist", "editor"].includes(sec) ? "artists" : sec)));
  $$(".sidebar .sub a").forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + h));
  const view = $("#view");
  view.innerHTML = `<div class="loading"><span class="spinner"></span></div>`;
  window.scrollTo(0, 0);
  try {
    await loadShared();
    for (const [re, fn] of routes) {
      const m = h.match(re);
      if (m) return await fn(view, ...m.slice(1));
    }
    view.innerHTML = `<div class="empty"><div class="big">?</div>Page not found</div>`;
  } catch (e) {
    view.innerHTML = `<div class="empty"><div class="big">!</div>${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", router);

function toolbar(buttons, right = "") {
  return `<div class="toolbar">${buttons.map(b => b === "|" ? `<span style="width:1px;height:32px;background:var(--line);margin:0 6px"></span>` :
    `<button class="tb ${b.on ? "on" : ""}" data-act="${b.act}" title="${esc(b.title || b.label)}"><b>${b.icon}</b>${esc(b.label)}</button>`).join("")}<span class="spacer"></span>${right}</div>`;
}
function bindToolbar(root, handlers) {
  $$(".toolbar [data-act]", root).forEach(b => b.onclick = () => handlers[b.dataset.act] && handlers[b.dataset.act](b));
}
async function command(name) {
  const c = await run(() => post("/commands", { name }));
  toast(`${name} started`);
  return c;
}

/* ================= ARTISTS ================= */
route(/^\/artists$/, async view => {
  const artists = await api("/artists");
  state.artists = artists;
  let mode = store.get("artistView", "posters"), sort = store.get("artistSort", "name"), filter = store.get("artistFilter", "all");
  const sorters = {
    name: (a, b) => (a.sort_name || a.name).localeCompare(b.sort_name || b.name),
    added: (a, b) => (b.added || "").localeCompare(a.added || ""),
    size: (a, b) => b.size - a.size,
    missing: (a, b) => (b.track_total - b.track_have) - (a.track_total - a.track_have),
    next: (a, b) => ((a.next_album || {}).d || "9").localeCompare((b.next_album || {}).d || "9"),
  };
  const filters = {
    all: () => true, monitored: a => a.monitored, unmonitored: a => !a.monitored,
    missing: a => a.track_have < a.track_total, complete: a => a.track_total && a.track_have >= a.track_total,
  };
  const draw = () => {
    const list = artists.filter(filters[filter]).sort(sorters[sort]);
    const tb = toolbar([
      { act: "refresh", icon: "⟳", label: "Update All" }, { act: "rss", icon: "⌁", label: "RSS Sync" },
      { act: "missing", icon: "⌕", label: "Search Missing" }, "|",
      { act: "add", icon: "+", label: "Add New" }, { act: "import", icon: "⇲", label: "Import" }, "|",
      { act: "posters", icon: "▦", label: "Posters", on: mode === "posters" }, { act: "table", icon: "☰", label: "Table", on: mode === "table" },
    ], `<select id="sortSel" style="width:auto">${Object.keys(sorters).map(k => `<option ${k === sort ? "selected" : ""} value="${k}">Sort: ${k}</option>`).join("")}</select>
        <select id="filterSel" style="width:auto;margin-left:6px">${Object.keys(filters).map(k => `<option ${k === filter ? "selected" : ""} value="${k}">Filter: ${k}</option>`).join("")}</select>`);
    let body;
    if (!artists.length) {
      body = `<div class="empty"><div class="big">♪</div><p>Your library is empty.</p><p><a class="btn primary" href="#/add">Add an artist</a> <a class="btn" href="#/import">Import existing library</a></p></div>`;
    } else if (mode === "posters") {
      body = `<div class="page"><div class="posters">${list.map(a => `
        <div class="poster" data-id="${a.id}">
          ${a.monitored ? "" : `<span class="label gray unmon">Unmonitored</span>`}
          <div class="img" style="${a.image ? `background-image:url('${esc(a.image)}')` : ""}">${a.image ? "" : esc(initials(a.name))}</div>
          <div class="meta"><div class="name" title="${esc(a.name)}">${esc(a.name)}</div>
          <div class="sub">${a.albums} albums · ${size(a.size)}</div>
          ${progressBar(a.track_have, a.track_total)}</div></div>`).join("")}</div>
        <p class="dim small" style="margin-top:16px">${list.length} artists · ${artists.reduce((s, a) => s + a.track_have, 0)} tracks · ${size(artists.reduce((s, a) => s + a.size, 0))}</p></div>`;
    } else {
      body = `<div class="page table-wrap"><table><thead><tr><th></th><th>Artist</th><th>Type</th><th>Quality Profile</th><th>Albums</th><th>Next Album</th><th>Last Album</th><th>Size</th><th style="width:160px">Tracks</th></tr></thead><tbody>
        ${list.map(a => `<tr class="clickable" data-id="${a.id}"><td>${a.monitored ? "<span class='ok'>●</span>" : "<span class='dim'>○</span>"}</td>
          <td><b>${esc(a.name)}</b> ${a.disambiguation ? `<span class="dim small">(${esc(a.disambiguation)})</span>` : ""}</td>
          <td class="muted">${esc(a.artist_type || "")}</td><td>${esc(profName(a.quality_profile_id))}</td><td>${a.albums}</td>
          <td class="small">${a.next_album ? esc(a.next_album.title) + " <span class='dim'>" + esc(a.next_album.d) + "</span>" : "—"}</td>
          <td class="small">${a.last_album ? esc(a.last_album.title) + " <span class='dim'>" + year(a.last_album.d) + "</span>" : "—"}</td>
          <td class="num">${size(a.size)}</td><td>${progressBar(a.track_have, a.track_total, true)}</td></tr>`).join("")}</tbody></table></div>`;
    }
    view.innerHTML = tb + body;
    bindToolbar(view, {
      refresh: () => command("Refresh Artists"), rss: () => command("RSS Sync"), missing: () => command("Missing Album Search"),
      add: () => location.hash = "#/add", import: () => location.hash = "#/import",
      posters: () => { mode = "posters"; store.set("artistView", mode); draw(); }, table: () => { mode = "table"; store.set("artistView", mode); draw(); },
    });
    $("#sortSel").onchange = e => { sort = e.target.value; store.set("artistSort", sort); draw(); };
    $("#filterSel").onchange = e => { filter = e.target.value; store.set("artistFilter", filter); draw(); };
    $$("[data-id]", view).forEach(el => el.onclick = () => location.hash = "#/artist/" + el.dataset.id);
  };
  draw();
});

/* ================= ADD NEW ================= */
route(/^\/add(?:\/(.*))?$/, async (view, term) => {
  term = decodeURIComponent(term || "");
  view.innerHTML = `<div class="page"><h1>Add New Artist</h1>
    <div class="row"><input id="addTerm" type="search" class="grow" placeholder="Artist name, or a MusicBrainz artist ID" value="${esc(term)}" style="font-size:16px;padding:11px 14px">
    <select id="addMode" style="width:auto"><option value="artist">Artists</option><option value="album">Albums</option></select></div>
    <p class="dim small">Tip: search by album to find an artist from one of their records. Data comes from MusicBrainz.</p>
    <div id="addResults"></div></div>`;
  const doSearch = async () => {
    const t = $("#addTerm").value.trim(); if (!t) return;
    history.replaceState(null, "", "#/add/" + encodeURIComponent(t));
    const box = $("#addResults");
    box.innerHTML = `<div class="loading"><span class="spinner"></span></div>`;
    try {
      if ($("#addMode").value === "album") {
        const res = await api("/lookup/albums?term=" + encodeURIComponent(t));
        box.innerHTML = res.length ? `<div class="cards">${res.map((r, i) => `<div class="card click" data-i="${i}" style="display:flex;gap:12px">
          <img src="${esc(r.cover)}" onerror="this.style.visibility='hidden'" style="width:80px;height:80px;border-radius:6px;object-fit:cover;background:#262a34">
          <div><div class="card-title">${esc(r.title)}</div><div class="muted">${esc(r.artist_name)}</div>
          <div class="small dim">${esc(r.type)} ${r.secondary_types.join(", ")} · ${year(r.date)}</div>
          ${r.artist_existing ? `<span class="label green">In library</span>` : ""}</div></div>`).join("")}</div>` : `<div class="empty">No results</div>`;
        $$("[data-i]", box).forEach(el => el.onclick = () => {
          const r = res[el.dataset.i];
          if (r.existing) location.hash = "#/artist/" + r.existing.artist_id;
          else addArtistModal({ mbid: r.artist_mbid, name: r.artist_name });
        });
      } else {
        const res = await api("/lookup/artists?term=" + encodeURIComponent(t));
        box.innerHTML = res.length ? `<div class="cards">${res.map((r, i) => `<div class="card click" data-i="${i}">
          <div class="card-title">${esc(r.name)} ${r.existing ? `<span class="label green">In library</span>` : ""}</div>
          <div class="muted small">${esc(r.disambiguation || "")}</div>
          <div class="small dim" style="margin-top:6px">${[r.type, r.country, r.begin && "since " + r.begin.slice(0, 4)].filter(Boolean).map(esc).join(" · ")}</div>
          <div class="chips" style="margin-top:8px">${r.tags.map(x => `<span class="chip">${esc(x)}</span>`).join("")}</div></div>`).join("")}</div>` : `<div class="empty">No results</div>`;
        $$("[data-i]", box).forEach(el => el.onclick = () => {
          const r = res[el.dataset.i];
          if (r.existing) { const a = (state.artists || []).find(x => x.mbid === r.mbid); location.hash = a ? "#/artist/" + a.id : "#/artists"; }
          else addArtistModal(r);
        });
      }
    } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  };
  let tmr;
  $("#addTerm").oninput = () => { clearTimeout(tmr); tmr = setTimeout(doSearch, 600); };
  $("#addTerm").onkeydown = e => { if (e.key === "Enter") { clearTimeout(tmr); doSearch(); } };
  $("#addMode").onchange = doSearch;
  $("#addTerm").focus();
  if (term) doSearch();
});

function addOptionsFields(defaults = {}) {
  return [
    { key: "root_folder", label: "Root Folder", type: "select", options: state.roots.map(r => r.path), default: defaults.root_folder || (state.roots[0] || {}).path },
    { key: "monitor", label: "Monitor", type: "select", default: defaults.monitor || "all", options: [["all", "All Albums"], ["future", "Future Albums"], ["missing", "Missing Albums"], ["existing", "Existing Albums"], ["latest", "Latest Album"], ["first", "First Album"], ["none", "None"]] },
    { key: "monitor_new", label: "Monitor New Albums", type: "select", default: "all", options: [["all", "All new albums"], ["none", "None"]] },
    { key: "quality_profile_id", label: "Quality Profile", type: "select", options: state.profiles.map(p => [p.id, p.name]) },
    { key: "metadata_profile_id", label: "Metadata Profile", type: "select", options: state.mprofiles.map(p => [p.id, p.name]), help: "Which release types (albums, EPs, singles, live…) to track" },
    { key: "search", label: "Start search for missing albums", type: "bool", default: state.settings.search_on_add },
  ];
}
function addArtistModal(a) {
  if (!state.roots.length) { toast("Add a root folder first (Settings → Media Management)", "warn"); location.hash = "#/settings/media"; return; }
  const fields = addOptionsFields();
  const m = modal({ title: `Add ${esc(a.name)}`, body: (a.disambiguation ? `<p class="muted">${esc(a.disambiguation)}</p>` : "") + renderFields(fields, {}),
    footer: `<button class="btn primary add">Add ${esc(a.name)}</button>` });
  bindFields(m);
  $(".add", m).onclick = async () => {
    const v = readFields(m, fields);
    v.quality_profile_id = Number(v.quality_profile_id); v.metadata_profile_id = Number(v.metadata_profile_id);
    $(".add", m).disabled = true;
    try {
      const r = await post("/artists", { mbid: a.mbid, ...v });
      m.remove(); toast(`Added ${a.name} — fetching albums…`);
      location.hash = "#/artist/" + r.id;
    } catch (e) { toast(e.message, "error"); $(".add", m).disabled = false; }
  };
}

/* ================= LIBRARY IMPORT ================= */
route(/^\/import$/, async view => {
  const folders = await api("/library/unmapped");
  view.innerHTML = `<div class="page"><h1>Library Import</h1>
    <p class="muted">Folders in your root folders that aren't linked to an artist yet. Musicarr matches each one on MusicBrainz, then links your existing files without moving them.</p>
    ${!state.roots.length ? `<div class="health error">No root folders configured. <a class="btn sm" href="#/settings/media">Add one</a></div>` : ""}
    ${folders.length ? `<div class="row" style="margin:10px 0"><span class="muted">Defaults:</span>
      <select id="impMonitor" style="width:auto"><option value="existing">Monitor existing albums</option><option value="all">Monitor all</option><option value="missing">Monitor missing</option><option value="none">Monitor none</option></select>
      <select id="impProfile" style="width:auto">${state.profiles.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
      <label class="row small"><input type="checkbox" id="impSearch"> Search for missing after import</label>
      <span class="grow"></span><button class="btn" id="matchAll">Match all</button><button class="btn primary" id="impGo">Import selected</button></div>
    <div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="impAll"></th><th>Folder</th><th>MusicBrainz match</th></tr></thead><tbody>
    ${folders.map((f, i) => `<tr data-i="${i}"><td><input type="checkbox" class="imp-sel"></td><td><b>${esc(f.name)}</b><div class="dim small">${esc(f.path)}</div></td>
      <td class="match"><button class="btn sm do-match">Find match</button></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><div class="big">✓</div>Nothing to import — every folder is linked.</div>`}</div>`;
  if (!folders.length) return;
  const matches = {};
  const doMatch = async i => {
    const cell = $(`tr[data-i="${i}"] .match`, view);
    cell.innerHTML = `<span class="spinner"></span>`;
    try {
      const res = await api("/lookup/artists?term=" + encodeURIComponent(folders[i].name));
      if (!res.length) { cell.innerHTML = `<span class="rej">No match</span> <button class="btn sm do-match">Retry</button>`; return; }
      cell.innerHTML = `<select class="m-sel">${res.slice(0, 8).map(r => `<option value="${r.mbid}" ${r.existing ? "disabled" : ""}>${esc(r.name)}${r.disambiguation ? " (" + esc(r.disambiguation) + ")" : ""}${r.existing ? " — in library" : ""}</option>`).join("")}</select>`;
      matches[i] = $(".m-sel", cell);
      $(`tr[data-i="${i}"] .imp-sel`, view).checked = !res[0].existing;
    } catch (e) { cell.innerHTML = `<span class="rej">${esc(e.message)}</span>`; }
  };
  view.addEventListener("click", e => { if (e.target.classList.contains("do-match")) doMatch(+e.target.closest("tr").dataset.i); });
  $("#matchAll").onclick = async () => { for (let i = 0; i < folders.length; i++) if (!matches[i]) await doMatch(i); };
  $("#impAll").onchange = e => $$(".imp-sel", view).forEach(c => c.checked = e.target.checked);
  $("#impGo").onclick = async () => {
    const items = [];
    $$("tr[data-i]", view).forEach(tr => {
      const i = +tr.dataset.i;
      if ($(".imp-sel", tr).checked && matches[i]) items.push({ mbid: matches[i].value, path: folders[i].path, root_folder: folders[i].root_folder,
        monitor: $("#impMonitor").value, quality_profile_id: +$("#impProfile").value, search: $("#impSearch").checked });
    });
    if (!items.length) return toast("Select matched folders first", "warn");
    await run(() => post("/library/import", { items }), `Importing ${items.length} artist(s)…`);
    location.hash = "#/artists";
  };
});

/* ================= MASS EDITOR ================= */
route(/^\/editor$/, async view => {
  const artists = (await api("/artists")).sort((a, b) => (a.sort_name || a.name).localeCompare(b.sort_name || b.name));
  view.innerHTML = `<div class="page"><h1>Mass Editor</h1><div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="edAll"></th><th>Artist</th><th>Monitored</th><th>Quality Profile</th><th>Root Folder</th><th>Path</th></tr></thead><tbody>
    ${artists.map(a => `<tr><td><input type="checkbox" class="ed" value="${a.id}"></td><td>${esc(a.name)}</td><td>${a.monitored ? "Yes" : "No"}</td><td>${esc(profName(a.quality_profile_id))}</td><td class="small">${esc(a.root_folder || "")}</td><td class="small dim">${esc(a.path || "")}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="card" style="position:sticky;bottom:0;margin-top:12px"><div class="row">
      <select id="edMon" style="width:auto"><option value="">Monitored: no change</option><option value="1">Monitored</option><option value="0">Unmonitored</option></select>
      <select id="edProf" style="width:auto"><option value="">Quality: no change</option>${state.profiles.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
      <select id="edMeta" style="width:auto"><option value="">Metadata: no change</option>${state.mprofiles.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
      <select id="edRoot" style="width:auto"><option value="">Root folder: no change</option>${state.roots.map(r => `<option>${esc(r.path)}</option>`).join("")}</select>
      <span class="grow"></span><button class="btn danger" id="edDel">Delete</button><button class="btn primary" id="edSave">Apply</button></div></div></div>`;
  $("#edAll").onchange = e => $$(".ed").forEach(c => c.checked = e.target.checked);
  const ids = () => $$(".ed:checked").map(c => +c.value);
  $("#edSave").onclick = async () => {
    const b = { ids: ids() };
    if (!b.ids.length) return toast("Nothing selected", "warn");
    if ($("#edMon").value) b.monitored = +$("#edMon").value;
    if ($("#edProf").value) b.quality_profile_id = +$("#edProf").value;
    if ($("#edMeta").value) b.metadata_profile_id = +$("#edMeta").value;
    if ($("#edRoot").value) b.root_folder = $("#edRoot").value;
    await run(() => put("/artists/editor", b), "Saved"); router();
  };
  $("#edDel").onclick = async () => {
    const sel = ids(); if (!sel.length) return;
    const r = await confirmBox("Delete artists", `Remove ${sel.length} artist(s) from Musicarr?`, "Delete", true, `<label class="row"><input type="checkbox" name="files"> Also delete their files from disk</label>`);
    if (!r) return;
    for (const id of sel) await del(`/artists/${id}?delete_files=${r.files}`);
    toast("Deleted"); router();
  };
});

/* ================= ARTIST DETAIL ================= */
route(/^\/artist\/(\d+)$/, async (view, id) => {
  const draw = async () => {
    const a = await api("/artists/" + id);
    const groups = {};
    for (const al of a.albums) {
      const g = al.secondary_types && al.secondary_types.length ? al.secondary_types[0] : al.album_type || "Other";
      (groups[g] = groups[g] || []).push(al);
    }
    const order = ["Album", "EP", "Single", "Live", "Compilation", "Soundtrack", "Remix", "DJ-mix", "Mixtape/Street", "Demo", "Broadcast", "Other"];
    const gkeys = Object.keys(groups).sort((x, y) => (order.indexOf(x) + 99) % 99 - (order.indexOf(y) + 99) % 99);
    const have = a.albums.reduce((s, x) => s + x.track_have, 0), total = a.albums.reduce((s, x) => s + (x.monitored ? x.track_total : 0), 0);
    const sz = a.albums.reduce((s, x) => s + x.size, 0);
    const collapsed = store.get("collapsed", {});
    view.innerHTML = toolbar([
      { act: "refresh", icon: "⟳", label: "Refresh & Scan" }, { act: "search", icon: "⌕", label: "Search Monitored" },
      { act: "manual", icon: "⇲", label: "Manual Import" }, "|", { act: "edit", icon: "✎", label: "Edit" }, { act: "delete", icon: "🗑", label: "Delete" },
    ]) + `<div class="hero"><div class="bg" style="background-image:url('${esc(a.image || "")}')"></div>
      <div class="art" style="background-image:url('${esc(a.image || "")}')"></div>
      <div class="grow"><div class="row"><button class="monitor ${a.monitored ? "on" : ""}" id="artMon" title="Toggle monitoring">${a.monitored ? "★" : "☆"}</button>
        <h1>${esc(a.name)}</h1>${a.disambiguation ? `<span class="muted">(${esc(a.disambiguation)})</span>` : ""}</div>
        <div class="facts"><span class="label gray">📁 ${esc(a.path)}</span><span class="label blue">${esc(profName(a.quality_profile_id))}</span>
          <span class="label gray">${size(sz)}</span><span class="label ${a.monitored ? "green" : "gray"}">${a.monitored ? "Monitored" : "Unmonitored"}</span>
          ${a.artist_type ? `<span class="label gray">${esc(a.artist_type)}</span>` : ""}${a.country ? `<span class="label gray">${esc(a.country)}</span>` : ""}
          <span class="label gray">New albums: ${a.monitor_new === "all" ? "monitored" : "ignored"}</span></div>
        <div style="max-width:420px;margin-bottom:10px">${progressBar(have, total, true)}</div>
        <div class="chips" style="margin-bottom:10px">${(a.genres || []).map(g => `<span class="chip">${esc(g)}</span>`).join("")}</div>
        <div class="overview">${a.overview ? esc(a.overview) : `<span class="dim">No biography. Add a Last.fm API key in Settings → General to fetch artist bios.</span>`}</div>
        <div class="row small" style="margin-top:10px">${(a.links || []).filter(l => /wikipedia|official|discogs|allmusic|bandcamp|last\.fm|spotify/i.test(l.url + l.type)).slice(0, 6).map(l => `<a class="label gray" target="_blank" rel="noopener" href="${esc(l.url)}">${esc(l.type)}</a>`).join("")}
          <a class="label gray" target="_blank" rel="noopener" href="https://musicbrainz.org/artist/${a.mbid}">MusicBrainz</a></div>
      </div></div>
      <div style="padding-top:14px">${a.albums.length ? gkeys.map(g => `<div class="album-group">
        <div class="head" data-g="${esc(g)}"><button class="monitor grp-mon ${groups[g].every(x => x.monitored) ? "on" : ""}" data-g="${esc(g)}" title="Toggle all">${groups[g].every(x => x.monitored) ? "★" : "☆"}</button>
          ${esc(g)}s <span class="dim small">${groups[g].length}</span><span class="grow"></span><span class="dim">${collapsed[g] ? "▸" : "▾"}</span></div>
        <div class="${collapsed[g] ? "hidden" : ""}">${groups[g].map(al => albumRow(al)).join("")}</div></div>`).join("")
        : `<div class="empty"><span class="spinner"></span><p>Fetching albums from MusicBrainz… (about 1 second per album)</p></div>`}</div>`;
    bindToolbar(view, {
      refresh: async () => { await run(() => post(`/artists/${id}/refresh`), "Refreshing…"); setTimeout(draw, 4000); },
      search: () => run(() => post(`/artists/${id}/search`), "Searching monitored albums…"),
      manual: () => manualImport(null, a),
      edit: () => editArtist(a, draw),
      delete: async () => {
        const r = await confirmBox(`Delete ${esc(a.name)}`, "Remove this artist from Musicarr?", "Delete", true,
          `<label class="row"><input type="checkbox" name="files"> Delete artist folder and all files</label><label class="row"><input type="checkbox" name="exclude"> Prevent import lists from re-adding</label>`);
        if (!r) return;
        await run(() => del(`/artists/${id}?delete_files=${r.files}&exclude=${r.exclude}`), "Deleted");
        location.hash = "#/artists";
      },
    });
    $("#artMon").onclick = async () => { await put("/artists/" + id, { monitored: a.monitored ? 0 : 1 }); draw(); };
    $$(".album-group .head", view).forEach(h => h.onclick = e => {
      if (e.target.closest(".grp-mon")) return;
      const c = store.get("collapsed", {}); c[h.dataset.g] = !c[h.dataset.g]; store.set("collapsed", c);
      h.nextElementSibling.classList.toggle("hidden");
    });
    $$(".grp-mon", view).forEach(b => b.onclick = async () => {
      const g = groups[b.dataset.g]; const on = !g.every(x => x.monitored);
      await put("/albums/monitor", { ids: g.map(x => x.id), monitored: on }); draw();
    });
    bindAlbumRows(view, a, draw);
    if (!a.albums.length) setTimeout(() => { if (location.hash === "#/artist/" + id) draw(); }, 5000);
  };
  await draw();
  pollTimer = setInterval(async () => {
    const cmds = await api("/commands").catch(() => []);
    if (cmds.some(c => c.status === "running" && /Refresh|Add Artist|Import Artist|Rescan/.test(c.name))) view._dirty = true;
    else if (view._dirty) { view._dirty = false; draw(); }
  }, 4000);
});

function albumRow(al) {
  const status = al.queued ? `<span class="label blue" title="${esc(al.queued.title)}">⇣ ${esc(al.queued.status)} ${Math.round(al.queued.progress || 0)}%</span>`
    : !al.release_date || al.release_date > new Date().toISOString().slice(0, 10) ? `<span class="label purple">Unreleased</span>`
    : al.track_total && al.track_have >= al.track_total ? qualityLabel(al.quality) : al.track_have ? `${qualityLabel(al.quality)} <span class="label orange">Partial</span>`
    : al.monitored ? `<span class="label red">Missing</span>` : `<span class="label gray">Unmonitored</span>`;
  return `<div class="album-row" data-al="${al.id}">
    <button class="monitor al-mon ${al.monitored ? "on" : ""}">${al.monitored ? "★" : "☆"}</button>
    <div class="cover" style="background-image:url('${esc(al.cover || "")}')"></div>
    <div><div class="title al-open">${esc(al.title)}</div><div class="small dim">${esc(al.release_date || "TBA")} · ${al.track_total} tracks${al.size ? " · " + size(al.size) : ""}</div></div>
    <div class="hide-sm">${progressBar(al.track_have, al.track_total, true)}</div>
    <div>${status}</div>
    <div class="hide-sm small dim">${al.last_search ? "searched " + ago(al.last_search) : ""}</div>
    <div class="row" style="justify-content:flex-end;gap:2px;flex-wrap:nowrap">
      <button class="icon-btn al-auto" title="Automatic search">⌕</button>
      <button class="icon-btn al-int" title="Interactive search">☰</button>
      <button class="icon-btn al-man" title="Manual import">⇲</button>
    </div></div>`;
}
function bindAlbumRows(root, artist, redraw) {
  $$(".album-row", root).forEach(r => {
    const id = +r.dataset.al;
    const al = artist.albums.find(x => x.id === id);
    $(".al-mon", r).onclick = async () => { await put("/albums/" + id, { monitored: al.monitored ? 0 : 1 }); redraw(); };
    $(".al-auto", r).onclick = () => run(() => post(`/albums/${id}/search`), `Searching for ${al.title}…`);
    $(".al-int", r).onclick = () => interactiveSearch(id, al.title);
    $(".al-man", r).onclick = () => manualImport(id, artist);
    $(".al-open", r).onclick = () => albumDetails(id, redraw);
  });
}

function editArtist(a, done) {
  const fields = [
    { key: "monitored", label: "Monitored", type: "bool" },
    { key: "monitor_new", label: "Monitor New Albums", type: "select", options: [["all", "All new albums"], ["none", "None"]] },
    { key: "quality_profile_id", label: "Quality Profile", type: "select", options: state.profiles.map(p => [p.id, p.name]) },
    { key: "metadata_profile_id", label: "Metadata Profile", type: "select", options: state.mprofiles.map(p => [p.id, p.name]) },
    { key: "path", label: "Path", type: "folder" },
  ];
  const m = modal({ title: "Edit " + esc(a.name), body: renderFields(fields, a), footer: `<button class="btn primary save">Save</button>` });
  bindFields(m);
  $(".save", m).onclick = async () => {
    const v = readFields(m, fields);
    v.quality_profile_id = +v.quality_profile_id; v.metadata_profile_id = +v.metadata_profile_id; v.monitored = v.monitored ? 1 : 0;
    await run(() => put("/artists/" + a.id, v), "Saved"); m.remove(); done();
  };
}

async function albumDetails(id, redraw) {
  const al = await api("/albums/" + id);
  const discs = [...new Set(al.tracks.map(t => t.disc))];
  const m = modal({ wide: true, title: `${esc(al.artist.name)} — ${esc(al.title)}`, body: `
    <div class="row" style="align-items:flex-start;gap:18px">
      <img src="${esc(al.cover)}" onerror="this.style.visibility='hidden'" style="width:180px;height:180px;border-radius:8px;object-fit:cover;background:#262a34">
      <div class="grow"><div class="row" style="margin-bottom:8px"><span class="label gray">${esc(al.album_type)}</span>${(al.secondary_types || []).map(s => `<span class="label gray">${esc(s)}</span>`).join("")}
        <span class="label gray">${esc(al.release_date || "TBA")}</span>${qualityLabel(al.quality)}<span class="label ${al.monitored ? "green" : "gray"}">${al.monitored ? "Monitored" : "Unmonitored"}</span></div>
        <div class="field"><label>Release used for tracklist</label><div><select id="relSel"><option>Loading releases…</option></select><div class="help">Pick a specific edition (deluxe, remaster, a different track count…). Tracks re-match after changing.</div></div></div>
        <div class="field"><label>Folder</label><div class="small">${esc(al.path || "—")}</div></div>
        <div class="row"><a class="btn sm" target="_blank" rel="noopener" href="https://musicbrainz.org/release-group/${al.mbid}">MusicBrainz</a>
          <button class="btn sm" id="delFiles">Delete files</button></div></div></div>
    <div class="tabs"><a class="on" data-t="tracks">Tracks</a><a data-t="history">History</a></div>
    <div data-p="tracks"><table class="track-table"><thead><tr><th>#</th><th>Title</th><th>Length</th><th>Quality</th><th>File</th></tr></thead><tbody>
      ${al.tracks.map(t => `<tr><td class="dim">${discs.length > 1 ? t.disc + "-" : ""}${t.position}</td><td>${esc(t.title)}${t.artist_credit && t.artist_credit !== al.artist.name ? `<div class="dim small">${esc(t.artist_credit)}</div>` : ""}</td>
        <td class="dim">${t.length_ms ? Math.floor(t.length_ms / 60000) + ":" + String(Math.floor(t.length_ms / 1000) % 60).padStart(2, "0") : ""}</td>
        <td>${t.file_path ? qualityLabel(t.quality) : `<span class="label red">Missing</span>`}</td><td class="small dim"><span class="ell" title="${esc(t.file_path || "")}">${esc((t.file_path || "").split("/").pop())}</span></td></tr>`).join("")}</tbody></table></div>
    <div data-p="history" class="hidden">${historyTable(al.history, false)}</div>`,
    footer: `<button class="btn" id="mInt">Interactive Search</button><button class="btn primary" id="mAuto">Search</button>` });
  $$(".tabs a", m).forEach(t => t.onclick = () => { $$(".tabs a", m).forEach(x => x.classList.toggle("on", x === t)); $$("[data-p]", m).forEach(p => p.classList.toggle("hidden", p.dataset.p !== t.dataset.t)); });
  $("#mAuto", m).onclick = () => run(() => post(`/albums/${id}/search`), "Searching…");
  $("#mInt", m).onclick = () => { m.remove(); interactiveSearch(id, al.title); };
  $("#delFiles", m).onclick = async () => {
    if (!await confirmBox("Delete files", `Delete all files for ${esc(al.title)}? (Moves to the recycle bin if one is set.)`)) return;
    await run(() => del(`/albums/${id}/files`), "Files deleted"); m.remove(); redraw && redraw();
  };
  api(`/albums/${id}/releases`).then(rels => {
    const sel = $("#relSel", m);
    sel.innerHTML = rels.map(r => `<option value="${r.mbid}" ${r.mbid === al.release_mbid ? "selected" : ""}>${esc(r.title)} — ${esc(r.format)} · ${r.tracks} tracks · ${esc(r.country || "")} ${esc(r.date || "")} ${r.disambiguation ? "(" + esc(r.disambiguation) + ")" : ""} ${r.status !== "Official" ? "[" + esc(r.status) + "]" : ""}</option>`).join("");
    sel.onchange = async () => { await run(() => put("/albums/" + id, { release_mbid: sel.value }), "Tracklist updated"); m.remove(); albumDetails(id, redraw); };
  }).catch(() => $("#relSel", m).innerHTML = "<option>Unavailable</option>");
}

/* ---------- interactive search ---------- */
async function interactiveSearch(id, title) {
  const m = modal({ wide: true, title: "Interactive Search — " + esc(title), body: `<div class="loading"><span class="spinner"></span><p class="muted">Searching indexers and Soulseek… (Soulseek takes ~30 seconds)</p></div>` });
  let res;
  try { res = await api(`/albums/${id}/interactive`); } catch (e) { $(".body", m).innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  let showRejected = false, proto = "all";
  const draw = () => {
    const list = res.filter(r => (showRejected || r.approved) && (proto === "all" || r.protocol === proto));
    $(".body", m).innerHTML = `<div class="row" style="margin-bottom:10px">
      <select id="isProto" style="width:auto">${["all", "torrent", "usenet", "soulseek"].map(p => `<option ${p === proto ? "selected" : ""}>${p}</option>`).join("")}</select>
      <label class="row small"><input type="checkbox" id="isRej" ${showRejected ? "checked" : ""}> Show rejected (${res.filter(r => !r.approved).length})</label>
      <span class="grow"></span><span class="dim small">${list.length} of ${res.length} releases</span></div>
      ${list.length ? `<div class="table-wrap"><table><thead><tr><th>Source</th><th>Title</th><th>Quality</th><th class="num">Size</th><th class="num">Peers / Match</th><th class="num">Age</th><th></th><th></th></tr></thead><tbody>
      ${list.map(r => `<tr><td>${protoLabel(r.protocol)}<div class="dim small">${esc(r.indexer || "")}</div></td>
        <td><span class="ell" title="${esc(r.title)}">${r.info_url ? `<a href="${esc(r.info_url)}" target="_blank" rel="noopener">${esc(r.title)}</a>` : esc(r.title)}</span>
          ${r.protocol === "soulseek" ? `<div class="dim small">${r.file_count} files · ${r.free_slot ? "free slot" : "queue " + r.queue_length} · ${size(r.upload_speed)}/s</div>` : ""}</td>
        <td>${qualityLabel(r.quality)}</td><td class="num">${size(r.size)}</td>
        <td class="num">${r.protocol === "torrent" ? `<span class="${(r.seeders || 0) > 0 ? "ok" : "rej"}">${r.seeders ?? "?"}</span> / ${r.leechers ?? "?"}` : r.protocol === "soulseek" ? (r.track_match ?? "?") + "%" : r.grabs != null ? r.grabs + " grabs" : ""}</td>
        <td class="num dim">${r.protocol === "soulseek" ? "" : r.age_days + "d"}</td>
        <td>${r.approved ? `<span class="ok" title="Approved">✔</span>` : `<span class="rej" title="${esc(r.rejections.join("\n"))}">✖</span>`}</td>
        <td><button class="btn sm ${r.approved ? "primary" : ""}" data-grab="${res.indexOf(r)}">⇣</button></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">No ${showRejected ? "" : "approved "}releases found${res.length ? " — tick “Show rejected” to see why" : ""}.</div>`}`;
    $("#isRej", m).onchange = e => { showRejected = e.target.checked; draw(); };
    $("#isProto", m).onchange = e => { proto = e.target.value; draw(); };
    $$("[data-grab]", m).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { await post(`/albums/${id}/grab`, res[+b.dataset.grab]); b.textContent = "✔"; toast("Sent to download client"); }
      catch (e) { toast(e.message, "error"); b.disabled = false; }
    });
  };
  draw();
}

/* ---------- manual import ---------- */
async function manualImport(albumId, artist, folder, queueId) {
  folder = folder || await folderPicker("/downloads");
  if (!folder) return;
  if (!albumId) {
    const opts = artist.albums.map(a => `<option value="${a.id}">${esc(a.title)} (${year(a.release_date)})</option>`).join("");
    const m = modal({ title: "Which album is this?", body: `<p class="dim small">${esc(folder)}</p><select id="miAl">${opts}</select>`, footer: `<button class="btn primary ok">Next</button>` });
    $(".ok", m).onclick = () => { const v = +$("#miAl", m).value; m.remove(); manualImport(v, artist, folder, queueId); };
    return;
  }
  const pv = await run(() => api(`/albums/${albumId}/preview?folder=` + encodeURIComponent(folder)));
  const fileOpts = sel => `<option value="">— skip —</option>` + pv.files.map(f => `<option value="${esc(f.path)}" ${f.path === sel ? "selected" : ""}>${esc(f.path.slice(folder.length + 1) || f.path)} [${esc(f.quality)}]</option>`).join("");
  const byTrack = {}; pv.files.forEach(f => { if (f.track_id) byTrack[f.track_id] = f.path; });
  const m = modal({ wide: true, title: "Manual Import", body: `<p class="dim small">${esc(folder)} — ${pv.files.length} audio files, ${Object.keys(byTrack).length}/${pv.tracks.length} auto-matched</p>
    <table><thead><tr><th>#</th><th>Track</th><th>File</th></tr></thead><tbody>${pv.tracks.map(t => `<tr><td class="dim">${t.disc}-${t.position}</td><td>${esc(t.title)}</td>
      <td><select data-t="${t.id}">${fileOpts(byTrack[t.id])}</select></td></tr>`).join("")}</tbody></table>`,
    footer: `<select id="miMode" style="width:auto" class="left"><option value="">Mode: default (${esc(state.settings.import_mode)})</option><option value="move">Move</option><option value="copy">Copy</option><option value="hardlink">Hardlink</option></select>
      <button class="btn primary go">Import</button>` });
  $(".go", m).onclick = async () => {
    const overrides = {}; $$("[data-t]", m).forEach(s => { if (s.value) overrides[s.dataset.t] = s.value; });
    $(".go", m).disabled = true;
    try {
      const r = await post(`/albums/${albumId}/import`, { folder, overrides, force: true, mode: $("#miMode", m).value || null, queue_id: queueId });
      toast(`Imported ${r.imported}/${r.total} tracks`); m.remove(); router();
    } catch (e) { toast(e.message, "error"); $(".go", m).disabled = false; }
  };
}

/* ================= CALENDAR ================= */
route(/^\/calendar$/, async view => {
  let cur = new Date(); cur.setDate(1);
  let mode = store.get("calMode", "month");
  const draw = async () => {
    const start = new Date(cur); start.setDate(1 - start.getDay());
    const end = new Date(start); end.setDate(start.getDate() + 41);
    const iso = d => d.toISOString().slice(0, 10);
    const evs = mode === "month" ? await api(`/calendar?start=${iso(start)}&end=${iso(end)}`) : await api(`/calendar?start=${iso(new Date(Date.now() - 7 * 864e5))}&end=9999`);
    const today = iso(new Date());
    const evClass = e => (e.track_total && e.track_have >= e.track_total ? "have" : e.release_date <= today && e.monitored ? "missing" : "") + (e.monitored ? "" : " unmon");
    let body;
    if (mode === "month") {
      const days = [];
      for (let i = 0; i < 42; i++) { const d = new Date(start); d.setDate(start.getDate() + i); days.push(d); }
      body = `<div class="cal">${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map(d => `<div class="dow">${d}</div>`).join("")}
        ${days.map(d => { const k = iso(d); return `<div class="day ${d.getMonth() !== cur.getMonth() ? "out" : ""} ${k === today ? "today" : ""}"><div class="d">${d.getDate()}</div>
          ${evs.filter(e => e.release_date === k).map(e => `<div class="ev ${evClass(e)}" data-a="${e.artist_id}" title="${esc(e.artist_name)} — ${esc(e.title)}"><b>${esc(e.artist_name)}</b> ${esc(e.title)}</div>`).join("")}</div>`; }).join("")}</div>`;
    } else {
      body = evs.length ? `<table><tbody>${evs.map(e => `<tr class="clickable" data-a="${e.artist_id}"><td style="width:110px">${esc(e.release_date)}</td><td><b>${esc(e.artist_name)}</b> — ${esc(e.title)}</td><td>${esc(e.album_type || "")}</td><td><span class="label ${evClass(e).includes("have") ? "green" : evClass(e).includes("missing") ? "red" : "blue"}">${evClass(e).includes("have") ? "Downloaded" : evClass(e).includes("missing") ? "Missing" : "Upcoming"}</span></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Nothing upcoming</div>`;
    }
    view.innerHTML = toolbar([{ act: "prev", icon: "‹", label: "Prev" }, { act: "today", icon: "●", label: "Today" }, { act: "next", icon: "›", label: "Next" }, "|",
      { act: "month", icon: "▦", label: "Month", on: mode === "month" }, { act: "agenda", icon: "☰", label: "Agenda", on: mode === "agenda" }, "|", { act: "ics", icon: "⇪", label: "iCal" }],
      `<b>${mode === "month" ? cur.toLocaleString(undefined, { month: "long", year: "numeric" }) : "Upcoming"}</b>`) + `<div class="page">${body}</div>`;
    bindToolbar(view, {
      prev: () => { cur.setMonth(cur.getMonth() - 1); draw(); }, next: () => { cur.setMonth(cur.getMonth() + 1); draw(); },
      today: () => { cur = new Date(); cur.setDate(1); draw(); },
      month: () => { mode = "month"; store.set("calMode", mode); draw(); }, agenda: () => { mode = "agenda"; store.set("calMode", mode); draw(); },
      ics: () => { const u = `${location.origin}/feed/calendar.ics?apikey=${state.settings.api_key}`; navigator.clipboard?.writeText(u); toast("iCal feed URL copied"); },
    });
    $$("[data-a]", view).forEach(e => e.onclick = () => location.hash = "#/artist/" + e.dataset.a);
  };
  draw();
});

/* ================= ACTIVITY ================= */
route(/^\/activity\/queue$/, async view => {
  const draw = async () => {
    const q = await api("/queue");
    view.innerHTML = toolbar([{ act: "refresh", icon: "⟳", label: "Refresh" }, { act: "check", icon: "⇣", label: "Check Now" }]) +
      `<div class="page">${q.length ? `<div class="table-wrap"><table><thead><tr><th>Status</th><th>Artist</th><th>Album</th><th>Release</th><th>Quality</th><th>Protocol</th><th>Client</th><th class="num">Size</th><th style="width:140px">Progress</th><th></th></tr></thead><tbody>
      ${q.map(i => `<tr><td>${{ importBlocked: `<span class="label orange" title="${esc(i.message)}">Import blocked</span>`, failed: `<span class="label red" title="${esc(i.message)}">Failed</span>`, imported: `<span class="label green">Imported</span>`, importing: `<span class="label blue">Importing</span>`, stalled: `<span class="label orange">Stalled</span>` }[i.status] || `<span class="label gray">${esc(i.status)}</span>`}</td>
        <td><a href="#/artist/${i.artist_id}">${esc(i.artist_name || "")}</a></td><td>${esc(i.album_title || "")}</td>
        <td><span class="ell small" title="${esc(i.title)}">${esc(i.title)}</span>${i.message && !["imported"].includes(i.status) ? `<div class="small ${i.status === "importBlocked" || i.status === "failed" ? "rej" : "dim"}">${esc(i.message)}</div>` : ""}</td>
        <td>${qualityLabel(i.quality)}</td><td>${protoLabel(i.protocol)}</td><td class="small">${esc(i.client_name || "")}</td><td class="num">${size(i.size)}</td>
        <td>${progressBar(Math.round(i.progress || 0), 100, true)}</td>
        <td class="num" style="white-space:nowrap">${i.status === "importBlocked" ? `<button class="icon-btn" data-man="${i.id}" title="Manual import">⇲</button><button class="icon-btn" data-retry="${i.id}" title="Retry import">⟳</button>` : ""}
          <button class="icon-btn" data-rm="${i.id}" title="Remove">✕</button></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><div class="big">⇣</div>Queue is empty</div>`}</div>`;
    bindToolbar(view, { refresh: draw, check: async () => { await command("Check Downloads"); setTimeout(draw, 2000); } });
    $$("[data-rm]", view).forEach(b => b.onclick = async () => {
      const r = await confirmBox("Remove from queue", "Remove this download?", "Remove", true,
        `<label class="row"><input type="checkbox" name="remove" checked> Remove from download client</label><label class="row"><input type="checkbox" name="blocklist"> Blocklist this release</label><label class="row"><input type="checkbox" name="search"> Search for a replacement (with blocklist)</label>`);
      if (!r) return;
      await run(() => del(`/queue/${b.dataset.rm}?remove=${r.remove}&blocklist=${r.blocklist}&search=${r.search}`), "Removed"); draw();
    });
    $$("[data-retry]", view).forEach(b => b.onclick = async () => { await post(`/queue/${b.dataset.retry}/retry`); toast("Retrying import"); setTimeout(draw, 3000); });
    $$("[data-man]", view).forEach(b => b.onclick = async () => {
      const i = q.find(x => x.id === +b.dataset.man);
      const a = await api("/artists/" + i.artist_id);
      manualImport(i.album_id, a, i.output_path, i.id);
    });
  };
  await draw();
  pollTimer = setInterval(draw, 5000);
});

function historyTable(items, withArtist = true) {
  const ev = { grabbed: ["blue", "Grabbed"], imported: ["green", "Imported"], upgraded: ["green", "Upgraded"], downloadFailed: ["red", "Failed"],
    artistAdded: ["purple", "Artist added"], fileDeleted: ["orange", "Deleted"] };
  if (!items.length) return `<div class="empty">No history</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>Event</th>${withArtist ? "<th>Artist</th><th>Album</th>" : ""}<th>Source</th><th>Quality</th><th>Date</th><th></th></tr></thead><tbody>
    ${items.map(h => { const [c, l] = ev[h.event] || ["gray", h.event]; return `<tr><td><span class="label ${c}">${l}</span></td>
      ${withArtist ? `<td>${h.artist_id ? `<a href="#/artist/${h.artist_id}">${esc(h.artist_name || "")}</a>` : ""}</td><td>${esc(h.album_title || "")}</td>` : ""}
      <td><span class="ell small" title="${esc(JSON.stringify(h.data))}">${esc(h.source_title || "")}</span>${h.data && h.data.reason ? `<div class="small rej">${esc(h.data.reason)}</div>` : ""}</td>
      <td>${qualityLabel(h.quality)}</td><td class="small dim" title="${esc(h.date)}">${ago(h.date)}</td>
      <td>${h.event === "grabbed" || h.event === "imported" ? `<button class="icon-btn" data-fail="${h.id}" title="Mark as failed (blocklist & search again)">⚑</button>` : ""}</td></tr>`; }).join("")}</tbody></table></div>`;
}
route(/^\/activity\/history$/, async view => {
  let page = 1, event = "";
  const draw = async () => {
    const r = await api(`/history?page=${page}&event=${event}`);
    const pages = Math.max(1, Math.ceil(r.total / r.size));
    view.innerHTML = toolbar([{ act: "refresh", icon: "⟳", label: "Refresh" }], `<select id="hEv" style="width:auto">${[["", "All events"], ["grabbed", "Grabbed"], ["imported", "Imported"], ["upgraded", "Upgraded"], ["downloadFailed", "Failed"], ["artistAdded", "Artist added"]].map(([v, l]) => `<option value="${v}" ${v === event ? "selected" : ""}>${l}</option>`).join("")}</select>`) +
      `<div class="page">${historyTable(r.items)}<div class="row" style="justify-content:center;margin-top:14px"><button class="btn sm" id="hPrev" ${page <= 1 ? "disabled" : ""}>‹</button><span class="muted">Page ${page} of ${pages} · ${r.total} records</span><button class="btn sm" id="hNext" ${page >= pages ? "disabled" : ""}>›</button></div></div>`;
    bindToolbar(view, { refresh: draw });
    $("#hEv").onchange = e => { event = e.target.value; page = 1; draw(); };
    $("#hPrev").onclick = () => { page--; draw(); }; $("#hNext").onclick = () => { page++; draw(); };
    $$("[data-fail]", view).forEach(b => b.onclick = async () => { if (await confirmBox("Mark as failed", "Blocklist this release and search for another?", "Mark failed")) { await run(() => post(`/history/${b.dataset.fail}/failed`), "Blocklisted, searching again"); } });
  };
  draw();
});
route(/^\/activity\/blocklist$/, async view => {
  const draw = async () => {
    const b = await api("/blocklist");
    view.innerHTML = toolbar([{ act: "clear", icon: "🗑", label: "Clear" }]) + `<div class="page">${b.length ? `<table><thead><tr><th>Artist</th><th>Album</th><th>Release</th><th>Protocol</th><th>Reason</th><th>Date</th><th></th></tr></thead><tbody>
      ${b.map(x => `<tr><td>${esc(x.artist_name || "")}</td><td>${esc(x.album_title || "")}</td><td><span class="ell small">${esc(x.source_title)}</span></td><td>${protoLabel(x.protocol || "")}</td><td class="small">${esc(x.reason || "")}</td><td class="small dim">${ago(x.date)}</td><td><button class="icon-btn" data-d="${x.id}">✕</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Blocklist is empty</div>`}</div>`;
    bindToolbar(view, { clear: async () => { if (await confirmBox("Clear blocklist", "Remove every blocklisted release?")) { await del("/blocklist/all"); draw(); } } });
    $$("[data-d]", view).forEach(x => x.onclick = async () => { await del("/blocklist/" + x.dataset.d); draw(); });
  };
  draw();
});

/* ================= WANTED ================= */
route(/^\/wanted\/(missing|cutoff)$/, async (view, kind) => {
  const items = await api("/wanted/" + kind);
  view.innerHTML = toolbar([{ act: "sel", icon: "⌕", label: "Search Selected" }, { act: "all", icon: "⌕", label: kind === "missing" ? "Search All Missing" : "Search All Cutoff" }, "|", { act: "unmon", icon: "☆", label: "Unmonitor Selected" }]) +
    `<div class="page"><h1>${kind === "missing" ? "Missing Albums" : "Cutoff Unmet"}</h1>${items.length ? `<div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="wAll"></th><th>Artist</th><th>Album</th><th>Type</th><th>Release Date</th>${kind === "cutoff" ? "<th>Have</th><th>Cutoff</th>" : ""}<th>Last Search</th><th></th></tr></thead><tbody>
    ${items.map(a => `<tr><td><input type="checkbox" class="w" value="${a.id}"></td><td><a href="#/artist/${a.artist_id}">${esc(a.artist_name)}</a></td><td>${esc(a.title)}</td><td class="muted">${esc(a.album_type || "")}</td><td>${esc(a.release_date || "")}</td>
      ${kind === "cutoff" ? `<td>${qualityLabel(a.current_quality)}</td><td>${qualityLabel(a.cutoff)}</td>` : ""}<td class="small dim">${a.last_search ? ago(a.last_search) : "never"}</td>
      <td class="num"><button class="icon-btn" data-s="${a.id}" title="Search">⌕</button><button class="icon-btn" data-i="${a.id}" data-t="${esc(a.title)}" title="Interactive">☰</button></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty"><div class="big">✓</div>Nothing ${kind === "missing" ? "missing" : "below cutoff"}</div>`}</div>`;
  const sel = () => $$(".w:checked").map(c => +c.value);
  bindToolbar(view, {
    sel: () => { const ids = sel(); if (!ids.length) return toast("Nothing selected", "warn"); run(() => post("/albums/search", { ids }), `Searching ${ids.length} album(s)`); },
    all: () => command(kind === "missing" ? "Missing Album Search" : "Cutoff Unmet Search"),
    unmon: async () => { const ids = sel(); if (!ids.length) return; await put("/albums/monitor", { ids, monitored: false }); router(); },
  });
  $("#wAll") && ($("#wAll").onchange = e => $$(".w").forEach(c => c.checked = e.target.checked));
  $$("[data-s]", view).forEach(b => b.onclick = () => run(() => post(`/albums/${b.dataset.s}/search`), "Searching…"));
  $$("[data-i]", view).forEach(b => b.onclick = () => interactiveSearch(+b.dataset.i, b.dataset.t));
});

/* ================= SETTINGS ================= */
function settingsPage(view, title, inner) { view.innerHTML = `<div class="page"><h1>${title}</h1>${inner}</div>`; }
async function saveSettings(root, fields) {
  const v = readFields(root, fields);
  state.settings = await run(() => put("/settings", v), "Settings saved");
}

route(/^\/settings\/media$/, async view => {
  const fields = [
    { key: "naming_enabled", label: "Rename Tracks", type: "bool", help: "Off = keep the original file names" },
    { key: "artist_folder_format", label: "Artist Folder Format" },
    { key: "album_folder_format", label: "Album Folder Format" },
    { key: "track_format", label: "Track Format" },
    { key: "multi_disc_track_format", label: "Multi-disc Track Format" },
    { key: "import_mode", label: "Import Mode", type: "select", options: [["move", "Move (Usenet/Soulseek); torrents are copied so they keep seeding"], ["copy", "Copy"], ["hardlink", "Hardlink (falls back to copy)"]] },
    { key: "write_tags", label: "Write Tags", type: "bool", help: "Writes clean MusicBrainz tags (Picard-compatible) so Navidrome/Plex/Jellyfin group albums correctly" },
    { key: "embed_cover", label: "Embed Cover Art", type: "bool" },
    { key: "save_cover_jpg", label: "Save cover.jpg", type: "bool" },
    { key: "min_track_match_pct", label: "Minimum Track Match %", type: "number", help: "Automatic imports below this are blocked for manual import" },
    { key: "delete_empty_folders", label: "Delete Empty Download Folders", type: "bool" },
    { key: "recycle_bin", label: "Recycle Bin", type: "folder", help: "Replaced/deleted files go here instead of being deleted. Empty = delete" },
    { key: "unmonitor_deleted", label: "Unmonitor Deleted Albums", type: "bool", help: "If files disappear from disk, unmonitor the album instead of re-downloading" },
  ];
  const roots = await api("/res/root_folders");
  const maps = await api("/res/remote_path_mappings");
  const clients = await api("/res/download_clients");
  settingsPage(view, "Media Management", `
    <h2>Root Folders</h2>
    <table><thead><tr><th>Path</th><th>Free Space</th><th>Unmapped Folders</th><th></th></tr></thead><tbody>
    ${roots.map(r => `<tr><td>${esc(r.path)} ${r.missing ? `<span class="label red">Missing</span>` : ""}</td><td>${r.free != null ? size(r.free) + " / " + size(r.total) : "—"}</td><td>${r.unmapped ? `<a href="#/import">${r.unmapped}</a>` : 0}</td><td><button class="icon-btn" data-delroot="${r.id}">✕</button></td></tr>`).join("")}
    </tbody></table><button class="btn" id="addRoot" style="margin-top:8px">+ Add Root Folder</button>
    <h2>Naming</h2><div class="card">${renderFields(fields.slice(0, 5), state.settings)}
    <div class="help" style="margin-top:8px">Tokens: ${state.meta.naming_tokens.map(t => `<code>${esc(t)}</code>`).join(" ")}</div>
    <div id="namePrev" class="small muted" style="margin-top:10px"></div></div>
    <h2>Importing</h2><div class="card">${renderFields(fields.slice(5), state.settings)}</div>
    <h2>Remote Path Mappings</h2><p class="dim small">Use when a download client reports paths that differ from what Musicarr sees (e.g. client in another container).</p>
    <table><thead><tr><th>Client</th><th>Remote Path</th><th>Local Path</th><th></th></tr></thead><tbody>
    ${maps.map(x => `<tr><td>${esc((clients.find(c => c.id === x.client_id) || {}).name || "Any")}</td><td>${esc(x.remote_path)}</td><td>${esc(x.local_path)}</td><td><button class="icon-btn" data-delmap="${x.id}">✕</button></td></tr>`).join("")}
    </tbody></table><button class="btn" id="addMap" style="margin-top:8px">+ Add Mapping</button>
    <div style="margin-top:20px"><button class="btn primary" id="saveMedia">Save Changes</button></div>`);
  bindFields(view);
  const prev = async () => { const p = await api("/naming/preview"); $("#namePrev").innerHTML = `Example: <code>${esc(p.album_path)}/${esc(p.track)}</code><br>Multi-disc: <code>${esc(p.multi_disc_track)}</code>`; };
  prev();
  $("#saveMedia").onclick = async () => { await saveSettings(view, fields); prev(); };
  $("#addRoot").onclick = async () => { const p = await folderPicker("/"); if (p) { await run(() => post("/res/root_folders", { path: p }), "Root folder added"); await loadShared(true); router(); } };
  $$("[data-delroot]").forEach(b => b.onclick = async () => { await del("/res/root_folders/" + b.dataset.delroot); await loadShared(true); router(); });
  $("#addMap").onclick = () => editResource("remote_path_mappings", {}, [
    { key: "client_id", label: "Download Client", type: "select", options: [["", "Any"], ...clients.map(c => [c.id, c.name])] },
    { key: "remote_path", label: "Remote Path", placeholder: "/data/downloads" }, { key: "local_path", label: "Local Path", type: "folder", placeholder: "/downloads" }], router);
  $$("[data-delmap]").forEach(b => b.onclick = async () => { await del("/res/remote_path_mappings/" + b.dataset.delmap); router(); });
});

async function editResource(res, item, fields, done, opts = {}) {
  const isNew = !item.id;
  const m = modal({ title: (isNew ? "Add " : "Edit ") + (opts.title || ""), body: (opts.intro || "") + renderFields(fields, item),
    footer: `${!isNew ? `<button class="btn danger left delete">Delete</button>` : ""}${opts.test ? `<button class="btn test">Test</button>` : ""}<button class="btn primary save">Save</button>` });
  bindFields(m);
  const collect = () => {
    const v = readFields(m, fields);
    for (const f of fields) if (f.type === "bool") setPath(v, f.key, getPath(v, f.key) ? 1 : 0);
    if (v.client_id === "") v.client_id = null; else if (v.client_id) v.client_id = +v.client_id;
    if (typeof v.protocol_order === "string") v.protocol_order = JSON.parse(v.protocol_order);
    for (const k of ["quality_profile_id", "metadata_profile_id", "priority", "min_seeders"]) if (v[k] !== undefined && v[k] !== null && v[k] !== "") v[k] = +v[k];
    return Object.assign({}, item, v);
  };
  if (opts.test) $(".test", m).onclick = async () => {
    const b = $(".test", m); b.disabled = true; b.innerHTML = `<span class="spinner" style="width:14px;height:14px"></span>`;
    try { const r = await post(`/res/${res}/test`, collect()); toast(r.message, r.ok ? "" : "error"); } catch (e) { toast(e.message, "error"); }
    b.disabled = false; b.textContent = "Test";
  };
  $(".save", m).onclick = async () => {
    const v = collect();
    try { if (isNew) await post(`/res/${res}`, v); else await put(`/res/${res}/${item.id}`, v); m.remove(); toast("Saved"); await loadShared(true); done(); }
    catch (e) { toast(e.message, "error"); }
  };
  if (!isNew) $(".delete", m).onclick = async () => { if (await confirmBox("Delete", `Delete ${esc(item.name || "item")}?`)) { await del(`/res/${res}/${item.id}`); m.remove(); await loadShared(true); done(); } };
}

route(/^\/settings\/profiles$/, async view => {
  const [qp, mp] = [await api("/res/quality_profiles"), await api("/res/metadata_profiles")];
  settingsPage(view, "Profiles", `<h2>Quality Profiles</h2><div class="cards">${qp.map(p => `<div class="card click" data-q="${p.id}"><div class="card-title">${esc(p.name)}</div>
      <div class="chips">${p.allowed.slice(0, 8).map(q => qualityLabel(q)).join(" ")}${p.allowed.length > 8 ? ` <span class="dim small">+${p.allowed.length - 8}</span>` : ""}</div>
      <div class="small dim" style="margin-top:8px">Cutoff: ${esc(p.cutoff)} · Upgrades ${p.upgrade_allowed ? "on" : "off"} · ${esc((p.protocol_order || []).join(" › "))}</div></div>`).join("")}<div class="card add" id="addQ">+</div></div>
    <h2>Metadata Profiles</h2><div class="cards">${mp.map(p => `<div class="card click" data-m="${p.id}"><div class="card-title">${esc(p.name)}</div>
      <div class="small">${esc(p.primary_types.join(", "))}</div><div class="small dim">${esc(p.secondary_types.join(", ") || "No secondary types")}</div><div class="small dim">${esc(p.release_statuses.join(", "))}</div></div>`).join("")}<div class="card add" id="addM">+</div></div>`);
  const qFields = () => [
    { key: "name", label: "Name" },
    { key: "allowed", label: "Allowed Qualities", type: "chips", options: state.meta.qualities, help: "Ranked best → worst in this order" },
    { key: "cutoff", label: "Upgrade Until", type: "select", options: state.meta.qualities },
    { key: "upgrade_allowed", label: "Upgrades Allowed", type: "bool" },
    { key: "protocol_order", label: "Protocol Preference", type: "select", options: [['["usenet","torrent","soulseek"]', "Usenet › Torrent › Soulseek"], ['["torrent","usenet","soulseek"]', "Torrent › Usenet › Soulseek"], ['["soulseek","usenet","torrent"]', "Soulseek › Usenet › Torrent"], ['["soulseek","torrent","usenet"]', "Soulseek › Torrent › Usenet"]] },
    { key: "preferred_words", label: "Preferred Words", help: "Comma separated, e.g. <code>web, remaster</code>. Ranked higher within the same quality" },
    { key: "required_words", label: "Must Contain", help: "Comma separated — at least one must match" },
    { key: "ignored_words", label: "Must Not Contain", help: "Comma separated, e.g. <code>karaoke, instrumental, 8D</code>" },
    { key: "min_seeders", label: "Minimum Seeders", type: "number" },
  ];
  const openQ = p => {
    const item = { ...p, protocol_order: JSON.stringify(p.protocol_order || ["usenet", "torrent", "soulseek"]) };
    editResource("quality_profiles", item, qFields(), router, { title: "Quality Profile" });
  };
  $$("[data-q]").forEach(c => c.onclick = () => openQ(qp.find(p => p.id === +c.dataset.q)));
  $("#addQ").onclick = () => openQ({ name: "", allowed: ["FLAC", "MP3-320"], cutoff: "FLAC", upgrade_allowed: 1, min_seeders: 1 });
  const mFields = [{ key: "name", label: "Name" }, { key: "primary_types", label: "Primary Types", type: "chips", options: state.meta.primary_types },
    { key: "secondary_types", label: "Secondary Types", type: "chips", options: state.meta.secondary_types, help: "Release groups with any unselected secondary type are skipped" },
    { key: "release_statuses", label: "Release Statuses", type: "chips", options: state.meta.release_statuses }];
  $$("[data-m]").forEach(c => c.onclick = () => editResource("metadata_profiles", mp.find(p => p.id === +c.dataset.m), mFields, router, { title: "Metadata Profile" }));
  $("#addM").onclick = () => editResource("metadata_profiles", { primary_types: ["Album"], secondary_types: [], release_statuses: ["Official"] }, mFields, router, { title: "Metadata Profile" });
});

route(/^\/settings\/indexers$/, async view => {
  const ix = await api("/res/indexers");
  settingsPage(view, "Indexers", `<p class="muted">Add Torznab (torrent) and Newznab (Usenet) feeds — from Prowlarr, Jackett, NZBHydra2 or directly from your indexers. Soulseek is set up under Download Clients (slskd).</p>
    <div class="cards">${ix.map(i => `<div class="card click" data-i="${i.id}"><div class="card-title">${esc(i.name)} ${protoLabel(i.kind === "torznab" ? "torrent" : "usenet")}</div>
      <div class="small dim">${esc(i.url)}</div><div class="row small" style="margin-top:8px">${i.enabled ? `<span class="label green">Enabled</span>` : `<span class="label gray">Disabled</span>`}${i.rss ? `<span class="label gray">RSS</span>` : ""}${i.search ? `<span class="label gray">Search</span>` : ""}<span class="label gray">Priority ${i.priority}</span></div></div>`).join("")}
    <div class="card add" id="addI">+</div></div>`);
  const fields = [
    { key: "name", label: "Name" }, { key: "kind", label: "Type", type: "select", options: [["torznab", "Torznab (torrents)"], ["newznab", "Newznab (Usenet)"]] },
    { key: "url", label: "URL", placeholder: "http://prowlarr:9696/1/", help: "Prowlarr: copy the indexer's Torznab/Newznab feed URL. <code>/api</code> is added automatically" },
    { key: "api_key", label: "API Key", type: "password" }, { key: "categories", label: "Categories", default: "3000,3010,3040", help: "3000 Audio, 3010 MP3, 3040 Lossless" },
    { key: "priority", label: "Priority", type: "number", default: 25, help: "1 (highest) – 50. Breaks ties between equal releases" },
    { key: "min_seeders", label: "Minimum Seeders", type: "number", default: 1 },
    { key: "enabled", label: "Enabled", type: "bool", default: true }, { key: "rss", label: "Enable RSS", type: "bool", default: true }, { key: "search", label: "Enable Search", type: "bool", default: true },
  ];
  $$("[data-i]").forEach(c => c.onclick = () => editResource("indexers", ix.find(x => x.id === +c.dataset.i), fields, router, { title: "Indexer", test: true }));
  $("#addI").onclick = () => editResource("indexers", { enabled: 1, rss: 1, search: 1, priority: 25, min_seeders: 1, kind: "torznab", categories: "3000,3010,3040" }, fields, router, { title: "Indexer", test: true });
});

const CLIENT_FIELDS = {
  qbittorrent: [["url", "URL", "http://qbittorrent:8080"], ["username", "Username"], ["password", "Password", "", "password"], ["category", "Category", "music"], ["download_path", "Download Folder", "/downloads/torrents", "text", "Where the client saves music, as the client sees it (its own container path). Leave empty to use the client’s default."]],
  transmission: [["url", "URL", "http://transmission:9091"], ["username", "Username"], ["password", "Password", "", "password"], ["category", "Label", "music"], ["download_path", "Download Folder", "/downloads/torrents", "text", "Where the client saves music, as the client sees it (its own container path). Leave empty to use the client’s default."]],
  deluge: [["url", "URL", "http://deluge:8112"], ["password", "Web UI Password", "", "password"], ["category", "Label", "music"], ["download_path", "Download Folder", "/downloads/torrents", "text", "Where the client saves music, as the client sees it (its own container path). Leave empty to use the client’s default."]],
  sabnzbd: [["url", "URL", "http://sabnzbd:8080"], ["api_key", "API Key", "", "password"], ["category", "Category", "music", "text", "Usenet clients pick the folder from the category — set the <b>music</b> category’s folder in the client (e.g. /downloads/usenet)."]],
  nzbget: [["url", "URL", "http://nzbget:6789"], ["username", "Username"], ["password", "Password", "", "password"], ["category", "Category", "music", "text", "Usenet clients pick the folder from the category — set the <b>music</b> category’s folder in the client (e.g. /downloads/usenet)."]],
  slskd: [["url", "slskd URL", "http://slskd:5030"], ["api_key", "API Key", "", "password"], ["download_path", "slskd Downloads Folder (as Musicarr sees it)", "/downloads/soulseek"]],
  torrent_blackhole: [["download_path", "Torrent Watch Folder"], ["extra.completed_path", "Completed Downloads Folder"]],
  usenet_blackhole: [["download_path", "NZB Watch Folder"], ["extra.completed_path", "Completed Downloads Folder"]],
};
const CLIENT_NAMES = { qbittorrent: "qBittorrent", transmission: "Transmission", deluge: "Deluge", sabnzbd: "SABnzbd", nzbget: "NZBGet", slskd: "Soulseek (slskd)", torrent_blackhole: "Torrent Blackhole", usenet_blackhole: "Usenet Blackhole" };

route(/^\/settings\/clients$/, async view => {
  const cl = await api("/res/download_clients");
  settingsPage(view, "Download Clients", `<div class="cards">${cl.map(c => `<div class="card click" data-c="${c.id}"><div class="card-title">${esc(c.name)} ${protoLabel(c.protocol)}</div>
      <div class="small dim">${esc(CLIENT_NAMES[c.kind])} · ${esc(c.url || c.download_path)}</div>${c.url && c.download_path ? `<div class="small dim">📁 ${esc(c.download_path)}</div>` : c.kind === "sabnzbd" || c.kind === "nzbget" ? `<div class="small dim">📁 from category “${esc(c.category || "music")}”</div>` : ""}<div class="row small" style="margin-top:8px">${c.enabled ? `<span class="label green">Enabled</span>` : `<span class="label gray">Disabled</span>`}<span class="label gray">Priority ${c.priority}</span></div></div>`).join("")}
    <div class="card add" id="addC">+</div></div>
    <h2>Failed Download Handling</h2><div class="card" id="fdh">${renderFields(fdhFields, state.settings)}<button class="btn primary" id="saveFdh" style="margin-top:10px">Save</button></div>`);
  bindFields($("#fdh"));
  $("#saveFdh").onclick = () => saveSettings($("#fdh"), fdhFields);
  const open = (c) => {
    if (!c.kind) {
      const m = modal({ title: "Add Download Client", body: `<div class="cards">${Object.entries(CLIENT_NAMES).map(([k, n]) => `<div class="card click" data-k="${k}"><div class="card-title">${esc(n)}</div>${protoLabel(state.meta.download_client_kinds[k])}</div>`).join("")}</div>` });
      $$("[data-k]", m).forEach(x => x.onclick = () => { m.remove(); if (x.dataset.k === "slskd") { location.hash = "#/settings/soulseek"; return; } open({ kind: x.dataset.k, name: CLIENT_NAMES[x.dataset.k], enabled: 1, priority: 1, remove_completed: 1, category: "music" }); });
      return;
    }
    const fields = [{ key: "name", label: "Name" }, ...CLIENT_FIELDS[c.kind].map(([k, l, ph, t, help]) => ({ key: k, label: l, placeholder: ph, help, type: t || (k.includes("path") ? "folder" : "text") })),
      { key: "priority", label: "Priority", type: "number", help: "Lower = preferred when several clients share a protocol" },
      { key: "remove_completed", label: "Remove Completed", type: "bool", help: "Remove from the client after import (torrents keep seeding — handle ratio in the client)" },
      { key: "enabled", label: "Enabled", type: "bool" }];
    const intro = c.kind === "slskd" ? `<p class="small muted">Musicarr drives Soulseek through <a href="https://github.com/slskd/slskd" target="_blank" rel="noopener">slskd</a>. Enable the API in slskd (<code>web.authentication.api_keys</code>) and mount slskd's downloads folder into Musicarr. Searches look for complete album folders and score them against the MusicBrainz tracklist.</p>` : "";
    editResource("download_clients", c, fields, router, { title: CLIENT_NAMES[c.kind], test: true, intro });
  };
  $$("[data-c]").forEach(x => x.onclick = () => { const c = cl.find(c => c.id === +x.dataset.c); if (c.kind === "slskd") location.hash = "#/settings/soulseek"; else open(c); });
  $("#addC").onclick = () => open({});
});
/* ---------- Soulseek ---------- */
route(/^\/settings\/soulseek$/, async view => {
  const st = await api("/soulseek");
  const conn = st.client || {};
  const connFields = [
    { key: "url", label: "slskd URL", placeholder: "http://slskd:5030", help: "Musicarr talks to Soulseek through slskd, which runs next to it in the same compose file" },
    { key: "username", label: "slskd Web Username", default: "slskd" },
    { key: "password", label: "slskd Web Password", type: "password", placeholder: conn.has_password ? "•••••••• (unchanged)" : "slskd" },
    { key: "download_path", label: "Downloads Folder (as Musicarr sees it)", type: "folder", default: "/downloads/soulseek" },
  ];
  const s = st.settings || {};
  const acct = [
    { key: "soulseek_username", label: "Soulseek Username", help: "Any unused name — Soulseek creates the account the first time it logs in. Pick something unique." },
    { key: "soulseek_password", label: "Soulseek Password", type: "password", placeholder: s.has_soulseek_password ? "•••••••• (unchanged)" : "" },
    { key: "description", label: "Profile Description", placeholder: "Shown to other users" },
    { key: "listen_port", label: "Listen Port", type: "number", default: 50300, help: "Forward this TCP port on your router to the server for faster, more reliable transfers" },
  ];
  const share = [
    { key: "share_dirs", label: "Shared Folders", type: "textarea", default: "/music", help: "One folder per line, as slskd sees them. Many Soulseek users won't upload to people who share nothing — sharing your library (read-only) gets you better results." },
    { key: "upload_slots", label: "Upload Slots", type: "number", placeholder: "20" },
    { key: "upload_speed_limit", label: "Upload Limit (KiB/s)", type: "number", placeholder: "unlimited" },
    { key: "download_slots", label: "Download Slots", type: "number", placeholder: "unlimited" },
    { key: "download_speed_limit", label: "Download Limit (KiB/s)", type: "number", placeholder: "unlimited" },
  ];
  const dirs = [
    { key: "downloads_dir", label: "slskd Downloads Folder", default: "/downloads/soulseek", help: "Inside the slskd container. Keep it on the same shared downloads mount as Musicarr." },
    { key: "incomplete_dir", label: "slskd Incomplete Folder", default: "/app/incomplete", help: "Partial files stay here, inside slskd’s own folder, until they finish" },
  ];
  const searchF = [
    { key: "soulseek_search_timeout_sec", label: "Search Time (seconds)", type: "number" },
    { key: "soulseek_min_upload_speed_kbps", label: "Minimum Peer Speed (KB/s)", type: "number", help: "Skip peers slower than this. 0 = any" },
    { key: "soulseek_max_queue", label: "Max Peer Queue", type: "number", help: "Skip peers with more queued uploads than this" },
    { key: "allow_partial_soulseek", label: "Allow Incomplete Folders", type: "bool", help: "Accept folders with fewer files than the tracklist" },
  ];
  const stateLabel = !st.configured ? `<span class="label gray">Not set up</span>` : !st.reachable ? `<span class="label red">slskd unreachable</span>`
    : st.logged_in ? `<span class="label green">Logged in</span>` : st.connected ? `<span class="label orange">Connected, not logged in</span>` : `<span class="label red">${esc(st.state || "Disconnected")}</span>`;
  const manageable = st.configured && st.reachable && st.remote_config;
  settingsPage(view, "Soulseek", `
    <div class="card"><div class="row"><b>Status</b> ${stateLabel}
      ${st.version ? `<span class="dim small">slskd ${esc(st.version)}</span>` : ""}${st.shares != null ? `<span class="dim small">· sharing ${st.shares} files</span>` : ""}
      <span class="grow"></span>
      ${st.reachable ? `<button class="btn sm" id="slReconnect">Reconnect</button>${st.connected ? `<button class="btn sm" id="slDisconnect">Disconnect</button>` : `<button class="btn sm primary" id="slConnect">Connect</button>`}` : ""}
      <button class="btn sm" id="slRefresh">⟳</button></div>
      ${st.error ? `<div class="small rej" style="margin-top:8px">${esc(st.error)}</div>` : ""}
      ${st.reachable && !st.remote_config ? `<div class="health warning" style="margin-top:10px">⚠ ${esc(st.config_error || "slskd isn't allowing remote configuration")}</div>` : ""}
      ${st.reachable && st.remote_config && !s.soulseek_username ? `<div class="health warning" style="margin-top:10px">Pick a Soulseek username and password below to get online.</div>` : ""}
    </div>
    <h2>slskd Connection</h2><div class="card" id="slConn">${renderFields(connFields, conn)}<button class="btn" id="slSaveConn" style="margin-top:10px">Save Connection</button></div>
    ${manageable ? `
    <h2>Soulseek Account</h2><div class="card" id="slAcct">${renderFields(acct, s)}</div>
    <h2>Sharing &amp; Limits</h2><div class="card" id="slShare">${renderFields(share, s)}</div>
    <h2>Folders</h2><div class="card" id="slDirs">${renderFields(dirs, s)}</div>` : ""}
    <h2>Search</h2><div class="card" id="slSearch">${renderFields(searchF, state.settings)}</div>
    <div style="margin-top:16px"><button class="btn primary" id="slSave">Save Changes</button></div>`);
  bindFields(view);
  $("#slRefresh").onclick = router;
  const act = a => async () => { await run(() => post("/soulseek/" + a), a === "disconnect" ? "Disconnected" : "Connecting…"); setTimeout(router, 2500); };
  if ($("#slConnect")) $("#slConnect").onclick = act("connect");
  if ($("#slDisconnect")) $("#slDisconnect").onclick = act("disconnect");
  if ($("#slReconnect")) $("#slReconnect").onclick = act("reconnect");
  $("#slSaveConn").onclick = async () => {
    const v = readFields($("#slConn"), connFields);
    if (!v.url) return toast("Enter the slskd URL", "warn");
    await run(() => put("/soulseek/connection", v), "Connection saved"); await loadShared(true); router();
  };
  $("#slSave").onclick = async () => {
    const b = $("#slSave"); b.disabled = true;
    const v = readFields($("#slSearch"), searchF);
    if (manageable) Object.assign(v, readFields($("#slAcct"), acct), readFields($("#slShare"), share), readFields($("#slDirs"), dirs));
    try {
      const r = await put("/soulseek", v);
      toast(r.reconnected ? "Saved — reconnecting to Soulseek…" : "Saved");
      await loadShared(true); setTimeout(router, r.reconnected ? 3000 : 300);
    } catch (e) { toast(e.message, "error"); b.disabled = false; }
  };
});

const fdhFields = [
  { key: "failed_download_handling", label: "Failed Download Handling", type: "bool", help: "Blocklist failed/stalled downloads and remove them from the client" },
  { key: "redownload_failed", label: "Redownload", type: "bool", help: "Automatically search for a different release when one fails" },
];

const LIST_FIELDS = {
  listenbrainz_top: [["config.username", "ListenBrainz Username"], ["config.range", "Range", "all_time", "select", ["week", "month", "quarter", "half_yearly", "year", "all_time"]], ["config.count", "Artists", "25", "number"]],
  listenbrainz_similar: [["config.username", "ListenBrainz Username"], ["config.count", "Artists", "25", "number"]],
  lastfm_user: [["config.username", "Last.fm Username"], ["config.period", "Period", "overall", "select", ["7day", "1month", "3month", "6month", "12month", "overall"]], ["config.count", "Artists", "25", "number"]],
  musicbrainz_collection: [["config.collection_id", "Collection ID (MBID)"]],
  mbid_list: [["config.mbids", "Artist MBIDs (one per line)", "", "textarea"]],
};
const LIST_NAMES = { listenbrainz_top: "ListenBrainz — Top Artists", listenbrainz_similar: "ListenBrainz — Similar Artists (experimental)", lastfm_user: "Last.fm — Top Artists", musicbrainz_collection: "MusicBrainz Collection", mbid_list: "Custom MBID List" };

route(/^\/settings\/lists$/, async view => {
  const [lists, excl] = [await api("/res/import_lists"), await api("/res/import_exclusions")];
  settingsPage(view, "Import Lists", `<p class="muted">Automatically add artists from your listening history. Synced every 12 hours (System → Tasks to run now).</p>
    <div class="cards">${lists.map(l => `<div class="card click" data-l="${l.id}"><div class="card-title">${esc(l.name)}</div><div class="small dim">${esc(LIST_NAMES[l.kind])}</div>
      <div class="small dim">Last sync: ${l.last_sync ? ago(l.last_sync) : "never"}</div></div>`).join("")}<div class="card add" id="addL">+</div></div>
    <h2>Import Exclusions</h2>${excl.length ? `<table><tbody>${excl.map(e => `<tr><td>${esc(e.name)}</td><td class="dim small">${esc(e.mbid)}</td><td><button class="icon-btn" data-x="${e.id}">✕</button></td></tr>`).join("")}</tbody></table>` : `<p class="dim">None</p>`}`);
  const open = l => {
    if (!l.kind) {
      const m = modal({ title: "Add Import List", body: `<div class="cards">${Object.entries(LIST_NAMES).map(([k, n]) => `<div class="card click" data-k="${k}"><div class="card-title">${esc(n)}</div></div>`).join("")}</div>` });
      $$("[data-k]", m).forEach(x => x.onclick = () => { m.remove(); open({ kind: x.dataset.k, name: LIST_NAMES[x.dataset.k].split(" (")[0], enabled: 1, monitor: "all", search_on_add: 1, config: {} }); });
      return;
    }
    const fields = [{ key: "name", label: "Name" }, ...LIST_FIELDS[l.kind].map(([k, lab, d, t, opts]) => ({ key: k, label: lab, default: d, type: t || "text", options: opts })),
      ...addOptionsFields().filter(f => f.key !== "search" && f.key !== "monitor_new"),
      { key: "search_on_add", label: "Search on Add", type: "bool" }, { key: "enabled", label: "Enabled", type: "bool" }];
    editResource("import_lists", { ...l, quality_profile_id: l.quality_profile_id || (state.profiles[0] || {}).id, metadata_profile_id: l.metadata_profile_id || (state.mprofiles[0] || {}).id }, fields, router, { title: "Import List" });
  };
  $$("[data-l]").forEach(x => x.onclick = () => open(lists.find(l => l.id === +x.dataset.l)));
  $("#addL").onclick = () => open({});
  $$("[data-x]").forEach(b => b.onclick = async () => { await del("/res/import_exclusions/" + b.dataset.x); router(); });
});

const NOTIF_FIELDS = {
  discord: [["config.webhook_url", "Webhook URL"]],
  ntfy: [["config.server", "Server", "https://ntfy.sh"], ["config.topic", "Topic"], ["config.token", "Access Token (optional)", "", "password"]],
  gotify: [["config.server", "Server URL"], ["config.token", "App Token", "", "password"], ["config.priority", "Priority", "5", "number"]],
  pushover: [["config.app_token", "App Token", "", "password"], ["config.user_key", "User Key", "", "password"]],
  telegram: [["config.bot_token", "Bot Token", "", "password"], ["config.chat_id", "Chat ID"]],
  webhook: [["config.url", "URL"], ["config.method", "Method", "POST", "select", ["POST", "PUT"]]],
  navidrome: [["config.url", "Navidrome URL", "http://navidrome:4533"], ["config.username", "Username"], ["config.password", "Password", "", "password"]],
  jellyfin: [["config.url", "Jellyfin URL", "http://jellyfin:8096"], ["config.api_key", "API Key", "", "password"]],
  plex: [["config.url", "Plex URL", "http://plex:32400"], ["config.token", "X-Plex-Token", "", "password"], ["config.section_id", "Music Library Section ID (optional)"]],
};
const NOTIF_NAMES = { discord: "Discord", ntfy: "ntfy", gotify: "Gotify", pushover: "Pushover", telegram: "Telegram", webhook: "Webhook", navidrome: "Navidrome (library scan)", jellyfin: "Jellyfin (library refresh)", plex: "Plex (library refresh)" };

route(/^\/settings\/connect$/, async view => {
  const ns = await api("/res/notifications");
  settingsPage(view, "Connect", `<p class="muted">Notifications, plus media servers that get a library scan after each import.</p><div class="cards">${ns.map(n => `<div class="card click" data-n="${n.id}"><div class="card-title">${esc(n.name)}</div>
    <div class="small dim">${esc(NOTIF_NAMES[n.kind])}</div><div class="row small" style="margin-top:8px">${["grab", "import", "upgrade", "failure"].filter(e => n["on_" + e]).map(e => `<span class="label gray">On ${e}</span>`).join("")}</div></div>`).join("")}
    <div class="card add" id="addN">+</div></div>`);
  const open = n => {
    if (!n.kind) {
      const m = modal({ title: "Add Connection", body: `<div class="cards">${Object.entries(NOTIF_NAMES).map(([k, nm]) => `<div class="card click" data-k="${k}"><div class="card-title">${esc(nm)}</div></div>`).join("")}</div>` });
      $$("[data-k]", m).forEach(x => x.onclick = () => { m.remove(); open({ kind: x.dataset.k, name: NOTIF_NAMES[x.dataset.k].split(" (")[0], enabled: 1, on_grab: 1, on_import: 1, on_upgrade: 1, on_failure: 1, config: {} }); });
      return;
    }
    const fields = [{ key: "name", label: "Name" }, ...NOTIF_FIELDS[n.kind].map(([k, l, d, t, o]) => ({ key: k, label: l, default: d, type: t || "text", options: o })),
      { key: "on_grab", label: "On Grab", type: "bool" }, { key: "on_import", label: "On Import", type: "bool" }, { key: "on_upgrade", label: "On Upgrade", type: "bool" },
      { key: "on_failure", label: "On Failure", type: "bool" }, { key: "enabled", label: "Enabled", type: "bool" }];
    editResource("notifications", n, fields, router, { title: NOTIF_NAMES[n.kind], test: true });
  };
  $$("[data-n]").forEach(x => x.onclick = () => open(ns.find(n => n.id === +x.dataset.n)));
  $("#addN").onclick = () => open({});
});

route(/^\/settings\/general$/, async view => {
  const fields = [
    { key: "auth_username", label: "Username", help: "Set a username and password to require login for the web UI (HTTP Basic). Leave empty when behind an authenticating proxy." },
    { key: "auth_password", label: "Password", type: "password" },
    { key: "api_key", label: "API Key", help: "Use as the <code>X-Api-Key</code> header or <code>?apikey=</code>" },
    { key: "lastfm_api_key", label: "Last.fm API Key", help: "Optional: artist biographies and Last.fm import lists" },
    { key: "fanart_api_key", label: "fanart.tv API Key", help: "Optional: proper artist photos (otherwise the first album cover is used)" },
    { key: "search_on_add", label: "Search When Adding Artists", type: "bool" },
    { key: "rss_interval_min", label: "RSS Sync Interval (minutes)", type: "number", help: "0 disables" },
    { key: "missing_search_interval_hours", label: "Missing Search Interval (hours)", type: "number", help: "Searches up to 50 missing albums per run, least recently searched first. 0 disables" },
    { key: "refresh_interval_hours", label: "Artist Refresh Interval (hours)", type: "number", help: "Checks MusicBrainz for new releases" },
    { key: "library_scan_interval_hours", label: "Disk Scan Interval (hours)", type: "number" },
  ];
  settingsPage(view, "General", `<div class="card">${renderFields(fields, state.settings)}</div><div style="margin-top:14px" class="row"><button class="btn primary" id="saveG">Save</button><button class="btn" id="regen">Regenerate API Key</button></div>`);
  $("#saveG").onclick = () => saveSettings(view, fields);
  $("#regen").onclick = async () => { const k = [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, "0")).join(""); $(`[data-k="api_key"]`).value = k; };
});

/* ================= SYSTEM ================= */
route(/^\/system\/status$/, async view => {
  const [st, health, stats] = await Promise.all([api("/system/status"), api("/health"), api("/stats")]);
  view.innerHTML = `<div class="page"><h1>Status</h1>
    <h2>Health</h2>${health.length ? health.map(h => `<div class="health ${h.level}">${h.level === "error" ? "⛔" : "⚠"} ${esc(h.message)}</div>`).join("") : `<div class="health">✅ No issues</div>`}
    <h2>Library</h2><div class="stat-tiles">
      <div class="tile"><div class="v">${stats.artists}</div><div class="k">Artists</div></div>
      <div class="tile"><div class="v">${stats.albums}</div><div class="k">Albums (${stats.monitored_albums} monitored)</div></div>
      <div class="tile"><div class="v">${stats.tracks_have}</div><div class="k">of ${stats.tracks_total} tracks on disk</div></div>
      <div class="tile"><div class="v">${size(stats.size)}</div><div class="k">Library size</div></div>
      <div class="tile"><div class="v">${stats.missing}</div><div class="k">Missing albums</div></div>
      <div class="tile"><div class="v">${stats.queue}</div><div class="k">Downloading</div></div></div>
    ${stats.qualities.length ? `<h2>Quality Breakdown</h2><div class="card">${stats.qualities.map(q => `<div class="row" style="margin:4px 0"><div style="width:120px">${qualityLabel(q.quality)}</div><div class="grow progress" style="margin:0;height:10px"><div style="width:${(q.c / stats.tracks_have * 100).toFixed(1)}%"></div></div><div class="dim small" style="width:70px;text-align:right">${q.c}</div></div>`).join("")}</div>` : ""}
    <h2>About</h2><div class="card"><div class="field"><label>Version</label><div>${esc(st.version)}</div></div>
      <div class="field"><label>Uptime</label><div>${Math.floor(st.uptime / 3600)}h ${Math.floor(st.uptime % 3600 / 60)}m</div></div>
      <div class="field"><label>Config</label><div>${esc(st.config)}</div></div><div class="field"><label>Database</label><div>${esc(st.database)}</div></div>
      <div class="field"><label>Metadata</label><div>MusicBrainz · Cover Art Archive${state.settings.lastfm_api_key ? " · Last.fm" : ""}${state.settings.fanart_api_key ? " · fanart.tv" : ""}</div></div></div></div>`;
});
route(/^\/system\/tasks$/, async view => {
  const draw = async () => {
    const [ts, cmds] = await Promise.all([api("/tasks"), api("/commands")]);
    const iv = s => !s ? "manual" : s < 3600 ? Math.round(s / 60) + " min" : s < 86400 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d";
    view.innerHTML = `<div class="page"><h1>Scheduled</h1><table><thead><tr><th>Task</th><th>Interval</th><th>Last Run</th><th>Next Run</th><th></th></tr></thead><tbody>
      ${ts.map(t => `<tr><td>${esc(t.name)}</td><td>${iv(t.interval)}</td><td class="small">${t.last ? ago(new Date(t.last * 1000).toISOString()) : "—"}</td><td class="small">${t.next ? new Date(t.next * 1000).toLocaleString() : "—"}</td>
        <td><button class="icon-btn" data-run="${esc(t.name)}" title="Run now">${t.running ? `<span class="spinner" style="width:14px;height:14px"></span>` : "▶"}</button></td></tr>`).join("")}</tbody></table>
      <h1 style="margin-top:28px">Queue</h1>${cmds.length ? `<table><thead><tr><th>Command</th><th>Status</th><th>Queued</th><th>Duration</th><th>Message</th></tr></thead><tbody>
      ${cmds.slice(0, 50).map(c => `<tr><td>${esc(c.name)}</td><td><span class="label ${c.status === "completed" ? "green" : c.status === "failed" ? "red" : c.status === "running" ? "blue" : "gray"}">${c.status}</span></td>
        <td class="small dim">${ago(c.queued)}</td><td class="small dim">${c.started && c.ended ? Math.round((new Date(c.ended) - new Date(c.started)) / 1000) + "s" : ""}</td><td class="small">${esc(c.message)}</td></tr>`).join("")}</tbody></table>` : `<p class="dim">Nothing has run yet</p>`}</div>`;
    $$("[data-run]", view).forEach(b => b.onclick = async () => { await command(b.dataset.run); setTimeout(draw, 800); });
  };
  await draw();
  pollTimer = setInterval(draw, 4000);
});
route(/^\/system\/logs$/, async view => {
  let level = "";
  const draw = async () => {
    const logs = await api("/logs" + (level ? "?level=" + level : ""));
    view.innerHTML = toolbar([{ act: "refresh", icon: "⟳", label: "Refresh" }], `<select id="lvl" style="width:auto">${["", "INFO", "WARNING", "ERROR", "DEBUG"].map(l => `<option value="${l}" ${l === level ? "selected" : ""}>${l || "All levels"}</option>`).join("")}</select>`) +
      `<div class="page table-wrap"><table><tbody>${logs.map(l => `<tr class="log-row"><td class="dim" style="white-space:nowrap">${esc(l.time)}</td><td class="lvl-${l.level}">${l.level}</td><td class="dim">${esc(l.logger)}</td><td style="white-space:pre-wrap">${esc(l.message)}</td></tr>`).join("")}</tbody></table>${logs.length ? "" : `<div class="empty">No log entries</div>`}</div>`;
    bindToolbar(view, { refresh: draw });
    $("#lvl").onchange = e => { level = e.target.value; draw(); };
  };
  draw();
});

/* ================= global ================= */
$("#menuToggle").onclick = () => document.body.classList.toggle("nav-open");
(() => {
  const inp = $("#globalSearch"), box = $("#globalResults");
  inp.oninput = async () => {
    const t = inp.value.trim().toLowerCase();
    if (!t) return box.classList.add("hidden");
    if (!state.artists) state.artists = await api("/artists");
    const hits = state.artists.filter(a => a.name.toLowerCase().includes(t)).slice(0, 8);
    box.innerHTML = hits.map(a => `<a href="#/artist/${a.id}"><img src="${esc(a.image || "")}" onerror="this.style.visibility='hidden'">${esc(a.name)}</a>`).join("") +
      `<a href="#/add/${encodeURIComponent(inp.value.trim())}" class="muted">+ Search MusicBrainz for “${esc(inp.value.trim())}”</a>`;
    box.classList.remove("hidden");
  };
  inp.onkeydown = e => { if (e.key === "Enter") { const a = $("a", box); if (a) location.hash = a.getAttribute("href"); } if (e.key === "Escape") box.classList.add("hidden"); };
  document.addEventListener("click", e => { if (!e.target.closest(".global-search")) box.classList.add("hidden"); else if (e.target.closest("a")) { box.classList.add("hidden"); inp.value = ""; } });
})();
async function pollBadges() {
  try {
    const [q, h] = await Promise.all([api("/queue"), api("/health")]);
    const n = q.filter(i => !["imported", "failed"].includes(i.status)).length;
    $("#queueCount").textContent = n || "";
    $("#healthCount").textContent = h.length || "";
  } catch {}
}
pollBadges(); setInterval(pollBadges, 15000);
router();
