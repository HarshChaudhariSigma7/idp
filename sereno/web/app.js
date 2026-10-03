// Sereno Volante web app. No build step, no framework: loads instantly on any laptop.
// All document-derived text is inserted with textContent (never innerHTML): it is untrusted input.

const $app = document.getElementById("app");
const MOD = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "⌘" : "Ctrl";
let ME = null;
let queueCount = 0;
let cleanup = [];

// ---------------------------------------------------------------- helpers
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function svg(tag, attrs = {}, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", credentials: "same-origin", headers: { "x-sereno": "1" } };
  if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["content-type"] = "application/json"; }
  if (opts.form) init.body = opts.form;
  const r = await fetch(path, init);
  if (r.status === 401 && !opts.allow401) { ME = null; renderLogin(); throw new Error("signed out"); }
  if (opts.raw) return r;
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || "Something went wrong"), { status: r.status, data });
  return data;
}
function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { return null; }
}
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const qty = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 3 });
const has = v => v !== null && v !== undefined && v !== "";
function fmtVal(v, type) {
  if (!has(v)) return null;
  if (type === "number") return Number.isInteger(v) && Math.abs(v) < 100000 ? qty.format(v) : inr.format(v);
  if (type === "percent") return `${qty.format(v)}%`;
  if (type === "integer") return qty.format(v);
  if (type === "date") { const d = new Date(v + "T00:00:00"); return isNaN(d) ? v : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }); }
  return String(v);
}
function editVal(v, type) {
  if (!has(v)) return "";
  if (type === "date") { const [y, m, d] = String(v).split("-"); return d ? `${d}/${m}/${y}` : v; }
  return String(v);
}
function when(ts) {
  if (!ts) return "";
  const d = new Date(ts + (String(ts).endsWith("Z") ? "" : "Z"));
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
function dueText(ts) {
  if (!ts) return "";
  const ms = new Date(ts + "Z") - Date.now();
  const m = Math.round(Math.abs(ms) / 60000);
  const t = m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
  return ms < 0 ? `${t} overdue` : `Due in ${t}`;
}
function fmtMinutes(m) { return m >= 60 ? `${Math.floor(m / 60)}h ${Math.round(m % 60)}m` : `${Math.round(m)} min`; }
const pct = v => (v === null || v === undefined ? "—" : `${v}%`);
function toast(msg, ms = 2600) {
  let box = document.querySelector(".toasts");
  if (!box) { box = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(box); }
  const t = h("div", { class: "toast" }, msg);
  box.append(t);
  setTimeout(() => t.remove(), ms);
}
function pill(status, label) { return h("span", { class: `pill ${status}` }, label); }
function can(p) { return ME && ME.permissions.includes(p); }
function head(title, ...right) { return h("div", { class: "pagehead" }, h("h1", {}, title), h("div", { class: "spacer" }), ...right); }

// ---------------------------------------------------------------- shell & routing
function shell(active, content, full = false) {
  const links = [
    ["dashboard", "Overview", can("dashboard.view")],
    ["documents", "Documents", can("document.upload") || can("document.view_all")],
    ["review", "Review", can("review.work")],
    ["match", "Match", can("match.run")],
    ["templates", "Templates", can("template.manage")],
    ["admin", "Admin", can("user.manage")],
    ["internal", "Internal", can("metrics.internal")],
  ].filter(l => l[2]);
  const nav = h("nav", {}, links.map(([k, label]) => h("a", { href: `#/${k}`, class: active === k ? "on" : "" }, label,
    k === "review" && queueCount ? h("span", { class: "badge" }, queueCount) : null)));
  $app.replaceChildren(
    h("header", { class: "top" },
      h("div", { class: "logo" }, "Sereno", ME.demo ? h("span", { class: "demo", title: "Synthetic documents with simulated readings. Not accuracy data." }, "Demo") : null),
      nav,
      h("div", { class: "me" }, h("span", { title: ME.email }, ME.company), h("button", { onclick: logout }, "Sign out"))),
    h("main", { class: full ? "full" : "" }, content));
}
async function logout() { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); ME = null; renderLogin(); }

function home() {
  if (can("dashboard.view")) return "#/dashboard";
  if (ME.role === "uploader") return "#/documents";
  if (can("review.work")) return "#/review";
  if (can("metrics.internal")) return "#/internal";
  return "#/documents";
}

async function route() {
  cleanup.forEach(f => f()); cleanup = [];
  if (!ME) return;
  const [, page, id] = (location.hash || home()).split("/");
  if (can("review.work")) api("/api/review/queue").then(q => { queueCount = q.documents.length; }).catch(() => {});
  try {
    if (page === "dashboard") return await renderDashboard();
    if (page === "documents") return await renderDocuments();
    if (page === "review" && id) return await renderReview(id);
    if (page === "review") return await renderQueue();
    if (page === "doc") return await renderReview(id);
    if (page === "match") return await renderMatch(id);
    if (page === "templates") return await renderTemplates();
    if (page === "admin") return await renderAdmin();
    if (page === "internal") return await renderInternal();
    location.hash = home();
  } catch (e) { if (e.message !== "signed out") shell(page, h("div", { class: "card err" }, e.message)); }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- login + MFA
function renderLogin() {
  cleanup.forEach(f => f()); cleanup = [];
  const err = h("div", { class: "err" });
  const email = h("input", { type: "email", autocomplete: "username", required: true });
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
  const form = h("form", { class: "card", onsubmit: async e => {
    e.preventDefault(); err.textContent = "";
    try {
      const r = await api("/api/auth/login", { method: "POST", json: { email: email.value, password: pw.value }, allow401: true });
      renderMfa(r.mfa_enrolled);
    } catch (x) { err.textContent = x.message; }
  } },
    h("h1", {}, "Sereno Volante"),
    h("label", { class: "f" }, "Email", email), h("label", { class: "f" }, "Password", pw),
    err, h("button", { class: "btn primary", type: "submit" }, "Sign in"));
  $app.replaceChildren(h("div", { class: "login" }, form));
  email.focus();
}
async function renderMfa(enrolled) {
  const err = h("div", { class: "err" });
  const code = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "7", placeholder: "123456" });
  const parts = [h("h1", {}, enrolled ? "Verification code" : "Set up two-step sign-in")];
  if (!enrolled) {
    const r = await api("/api/auth/mfa/enroll", { method: "POST", allow401: true });
    const qr = h("div", { class: "qr" });
    // server-generated SVG (no user content) parsed as XML, not HTML
    const doc = new DOMParser().parseFromString(r.qr_svg, "image/svg+xml");
    qr.append(document.importNode(doc.documentElement, true));
    parts.push(h("p", { class: "sub" }, "Scan with any authenticator app."), qr);
  }
  const form = h("form", { class: "card", onsubmit: async e => {
    e.preventDefault(); err.textContent = "";
    try { await api("/api/auth/mfa/verify", { method: "POST", json: { code: code.value }, allow401: true }); await boot(); }
    catch (x) { err.textContent = x.message; }
  } }, ...parts, h("label", { class: "f" }, "6-digit code", code), err, h("button", { class: "btn primary", type: "submit" }, "Verify"));
  $app.replaceChildren(h("div", { class: "login" }, form));
  code.focus();
}

// ---------------------------------------------------------------- dashboard
function kpi(label, value, note, tone = "") {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value ?? "—"),
    note ? h("div", { class: `note ${tone}` }, note) : null);
}
function trendChart(rows) {
  const W = 800, H = 200, P = { l: 40, r: 12, t: 12, b: 26 };
  const pts = rows.filter(r => r.accuracy !== null);
  if (pts.length < 2) return h("div", { class: "empty" }, "Shows from the second day of use");
  const lo = Math.max(0, Math.min(90, ...pts.map(r => Math.min(r.accuracy, r.straight_through ?? 100))) - 5);
  const x = i => P.l + (pts.length === 1 ? (W - P.l - P.r) / 2 : i * (W - P.l - P.r) / (pts.length - 1));
  const y = v => P.t + (100 - v) * (H - P.t - P.b) / (100 - lo);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": "Accuracy trend" });
  for (const v of [lo, (lo + 100) / 2, 100]) {
    g.append(svg("line", { x1: P.l, x2: W - P.r, y1: y(v), y2: y(v), class: "axis" }));
    g.append(svg("text", { x: 4, y: y(v) + 4, class: "lbl" }, `${Math.round(v)}%`));
  }
  const path = key => pts.map((r, i) => `${i ? "L" : "M"}${x(i)},${y(r[key] ?? 0)}`).join(" ");
  g.append(svg("path", { d: path("straight_through"), class: "l2" }));
  g.append(svg("path", { d: path("accuracy"), class: "l1" }));
  pts.forEach((r, i) => {
    const c = svg("circle", { cx: x(i), cy: y(r.accuracy), r: 3.5, class: "dot" });
    c.append(svg("title", {}, `${r.day}: ${r.accuracy}% accurate, ${r.documents} documents`));
    g.append(c);
  });
  const step = Math.max(1, Math.ceil(pts.length / 8));
  pts.forEach((r, i) => { if (i % step === 0) g.append(svg("text", { x: x(i) - 16, y: H - 6, class: "lbl" }, r.day.slice(5))); });
  return g;
}
async function renderDashboard() {
  const d = await api("/api/dashboard?days=30");
  const t = d.turnaround, a = d.accuracy;
  const conds = (d.conditions || []).filter(c => c.key === "overall" || c.fields);
  const anyBench = conds.some(c => c.benchmark), anySpot = conds.some(c => c.spot_check && c.spot_check.documents);
  shell("dashboard", [
    head("Overview", h("span", { class: "muted" }, `Last ${d.window_days} days`),
      can("document.upload") ? h("a", { class: "btn primary", href: "#/documents" }, "Upload") : null),
    h("div", { class: "grid g4" },
      kpi("Processed today", d.today.processed, d.today.processed ? `${d.today.processed - d.today.needed_review} needed no review` : null),
      kpi("Accuracy", a.first_pass_accuracy_pct !== null ? `${a.first_pass_accuracy_pct}%` : "—",
        a.fields_extracted ? `${qty.format(a.fields_extracted)} fields, no correction` : null),
      kpi("Review time", t.average_minutes !== null ? fmtMinutes(t.average_minutes) : "—",
        t.on_time_pct !== null ? `${t.on_time_pct}% within ${fmtMinutes(t.sla_minutes)}` : null,
        t.average_minutes === null ? "" : (t.average_minutes <= t.sla_minutes ? "good" : "bad")),
      kpi("Hours saved", d.time_saved_hours || "—", "manual keying avoided")),
    h("div", { class: "grid g2" },
      h("div", { class: "card chart" },
        h("div", { class: "row" }, h("h2", {}, "Trend"), h("div", { class: "spacer" }),
          h("div", { class: "legend" }, h("span", {}, h("i"), "Fields correct"), h("span", {}, h("i", { class: "dash" }), "No review needed"))),
        trendChart(d.trend)),
      h("div", { class: "card kpi" },
        h("div", { class: "label" }, "Review queue"),
        h("div", { class: "value" }, d.queue.open),
        h("div", { class: `note ${d.queue.overdue ? "bad" : "good"}` }, d.queue.overdue ? `${d.queue.overdue} overdue` : "All within target"),
        a.spot_check.documents ? h("p", { class: "muted small" }, `Spot checks: ${a.spot_check.accuracy_pct}% of ${a.spot_check.fields} auto-approved fields correct`) : null,
        can("review.work") && d.queue.open ? h("a", { class: "btn", href: "#/review" }, "Open queue") : null)),
    h("div", { class: "grid g2 eq" },
      h("div", { class: "card" }, h("h2", {}, "By condition"),
        h("table", { class: "t" },
          h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { class: "num" }, "Accuracy"), h("th", { class: "num" }, "Errors caught"),
            h("th", { class: "num" }, "No review"), anySpot ? h("th", { class: "num" }, "Spot check") : null,
            anyBench ? h("th", { class: "num" }, "Benchmark") : null, h("th", { class: "num" }, "Fields"))),
          h("tbody", {}, conds.map(c => h("tr", {},
            h("td", {}, c.label),
            h("td", { class: `num ${c.measured ? "strong" : "muted"}`, title: c.measured ? null : "Under 200 fields: early figure" }, pct(c.accuracy_pct)),
            h("td", { class: "num" }, pct(c.caught_before_export_pct)),
            h("td", { class: "num" }, pct(c.no_review_docs_pct)),
            anySpot ? h("td", { class: "num" }, c.spot_check && c.spot_check.documents ? pct(c.spot_check.accuracy_pct) : "—") : null,
            anyBench ? h("td", { class: "num" }, c.benchmark ? pct(c.benchmark.field_accuracy_pct) : "—") : null,
            h("td", { class: "num muted" }, qty.format(c.fields))))))),
      h("div", { class: "card" }, h("h2", {}, "By type"),
        Object.keys(d.by_type).length ? h("table", { class: "t" },
          h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { class: "num" }, "Documents"), h("th", { class: "num" }, "No review"))),
          h("tbody", {}, Object.entries(d.by_type).map(([k, v]) => h("tr", {}, h("td", {}, ME.doc_types[k] || k), h("td", { class: "num" }, v.documents),
            h("td", { class: "num" }, v.documents ? `${Math.round(100 * (v.documents - v.needed_review) / v.documents)}%` : "—")))))
          : h("div", { class: "empty" }, "No documents yet"))),
  ]);
}

// ---------------------------------------------------------------- documents
async function renderDocuments() {
  const list = h("tbody");
  const typeSel = h("select", { "aria-label": "Document type", onchange: () => store("sv.type", typeSel.value) },
    h("option", { value: "auto" }, "Auto-detect type"),
    Object.entries(ME.doc_types).map(([k, v]) => h("option", { value: k }, v)));
  const saved = store("sv.type");
  if (saved && [...typeSel.options].some(o => o.value === saved)) typeSel.value = saved;
  const fileIn = h("input", { type: "file", multiple: true, accept: ".pdf,.jpg,.jpeg,.png,.tif,.tiff,.webp", hidden: true });
  const drop = h("div", { class: "drop", tabindex: "0", role: "button", onclick: () => fileIn.click(),
    onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } } },
    h("strong", {}, "Drop files or click to upload"), h("span", { class: "muted" }, "PDF, JPG, PNG, TIFF"));
  const upload = async files => {
    if (!files.length) return;
    const form = new FormData();
    [...files].forEach(f => form.append("files", f));
    form.append("doc_type", typeSel.value);
    drop.classList.add("over");
    try {
      const r = await api("/api/documents", { method: "POST", form });
      const errs = r.results.filter(x => x.error);
      const byMsg = new Map();
      errs.forEach(e => byMsg.set(e.error, [...(byMsg.get(e.error) || []), e.filename]));
      byMsg.forEach((names, msg) => toast(names.length > 1 ? `${names.length} files: ${msg}` : `${names[0]}: ${msg}`, 7000));
      const ok = r.results.length - errs.length;
      if (ok) toast(`${ok} uploaded`);
      load();
    } catch (e) { toast(e.message); } finally { drop.classList.remove("over"); fileIn.value = ""; }
  };
  fileIn.addEventListener("change", () => upload(fileIn.files));
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", e => { e.preventDefault(); upload(e.dataTransfer.files); });
  const statusSel = h("select", { "aria-label": "Status", onchange: () => load() }, h("option", { value: "" }, "All"),
    [["needs_review,unreadable", "Needs review"], ["ready", "Ready"], ["exported", "Exported"], ["processing,uploaded", "Processing"], ["failed", "Failed"]]
      .map(([v, l]) => h("option", { value: v }, l)));
  let timer = null;
  async function load() {
    const r = await api(`/api/documents${statusSel.value ? `?status=${statusSel.value}` : ""}`);
    list.replaceChildren(...(r.documents.length ? r.documents.map(docRow) : [h("tr", {}, h("td", { colspan: 4, class: "empty" }, "No documents"))]));
    clearTimeout(timer);
    if (r.documents.some(d => ["uploaded", "processing"].includes(d.status))) timer = setTimeout(load, 2000);
  }
  cleanup.push(() => clearTimeout(timer));
  const expType = h("select", { "aria-label": "Export type" }, Object.entries(ME.doc_types).map(([k, v]) => h("option", { value: k }, v)));
  const exportBtns = can("export.run") ? h("div", { class: "row tight" }, expType,
    h("button", { class: "btn", onclick: () => doExport(expType.value, "xlsx") }, "Excel"),
    h("button", { class: "btn", onclick: () => doExport(expType.value, "csv") }, "CSV")) : null;
  shell("documents", [
    head("Documents", exportBtns),
    can("document.upload") && !ME.ai_ready ? h("div", { class: "banner warn" }, "No AI key: uploads can't be read. Add ANTHROPIC_API_KEY to .env and restart.") : null,
    can("document.upload") ? h("div", { class: "upload" }, drop, typeSel, fileIn) : null,
    h("div", { class: "card flush" },
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", { class: "hide-sm" }, "Type"),
        h("th", {}, statusSel), h("th", { class: "num hide-sm" }, "Uploaded"))), list)),
  ]);
  await load();
}
function docRow(d) {
  const open = () => { location.hash = (d.status === "needs_review" || d.status === "unreadable") ? `#/review/${d.id}` : `#/doc/${d.id}`; };
  const msg = d.status === "needs_review" ? [d.message, dueText(d.review_due_at)].filter(Boolean).join(" · ") : (d.status === "failed" || d.status === "unreadable" ? d.message : "");
  return h("tr", { class: "click", onclick: open },
    h("td", {}, h("div", { class: "fname" }, d.filename), msg ? h("div", { class: "msg" }, msg) : null),
    h("td", { class: "hide-sm" }, d.doc_type_label || ""),
    h("td", {}, pill(d.status, d.status_label)),
    h("td", { class: "num muted hide-sm" }, when(d.created_at)));
}
async function doExport(t, fmt) {
  const r = await api(`/api/exports?doc_type=${t}&fmt=${fmt}`, { raw: true });
  if (!r.ok) { const e = await r.json().catch(() => ({})); return toast(e.detail || "Nothing to export"); }
  const blob = await r.blob();
  const a = h("a", { href: URL.createObjectURL(blob), download: (r.headers.get("content-disposition") || "").split('filename="')[1]?.replace('"', "") || `export.${fmt}` });
  document.body.append(a); a.click(); a.remove();
  toast("Exported");
}

// ---------------------------------------------------------------- review queue
async function renderQueue() {
  const q = await api("/api/review/queue");
  queueCount = q.documents.length;
  shell("review", [
    head("Review", q.documents.length ? h("a", { class: "btn primary", href: `#/review/${q.documents[0].id}` }, "Start ", h("kbd", {}, "↵")) : null),
    h("div", { class: "card flush" }, q.documents.length ? h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Document"), h("th", { class: "hide-sm" }, "To check"), h("th", { class: "num" }, "Fields"), h("th", { class: "num" }, "Due"))),
      h("tbody", {}, q.documents.map(d => h("tr", { class: "click", onclick: () => { location.hash = `#/review/${d.id}`; } },
        h("td", {}, h("div", { class: "fname" }, d.filename), h("div", { class: "msg" }, d.doc_type_label || "")),
        h("td", { class: "msg hide-sm" }, d.is_qa_sample ? "Spot check: every field" : (d.message || "")),
        h("td", { class: "num" }, d.is_qa_sample ? d.fields_total : d.fields_flagged),
        h("td", { class: `num ${d.overdue ? "bad-text" : "muted"}` }, dueText(d.review_due_at))))))
      : h("div", { class: "empty" }, "All clear")),
  ]);
  const onKey = e => { if (e.key === "Enter" && q.documents.length) location.hash = `#/review/${q.documents[0].id}`; };
  document.addEventListener("keydown", onKey);
  cleanup.push(() => document.removeEventListener("keydown", onKey));
}

// ---------------------------------------------------------------- review screen
async function renderReview(id) {
  const doc = await api(`/api/documents/${id}`);
  const canReview = doc.can_review;
  const spot = doc.is_qa_sample && canReview;
  const todo = f => canReview && (spot ? !["confirmed", "corrected"].includes(f.status) : f.needs_review && f.status === "pending");
  let showAll = !canReview || spot || !doc.fields.some(f => f.needs_review);
  let active = 0, editing = false, zoomed = true, tStart = performance.now();
  let failedChecks = [];

  const boxesByField = new Map();
  const pagesEl = h("div", { class: "zoomwrap" });
  for (const p of doc.pages) {
    const page = h("div", { class: "page", "data-page": p.page_no });
    if (doc.available) {
      const img = h("img", { src: `/api/documents/${doc.id}/pages/${p.page_no}`, alt: `Page ${p.page_no}` });
      img.addEventListener("error", () => page.replaceChildren(h("div", { class: "gone" }, "Page image deleted (retention policy)")));
      page.append(img);
    } else page.append(h("div", { class: "gone" }, "Original deleted (retention policy). Extracted data remains."));
    pagesEl.append(page);
  }
  for (const f of doc.fields) {
    if (!f.bbox || !f.page) continue;
    const page = pagesEl.querySelector(`[data-page="${f.page}"]`);
    if (!page) continue;
    const b = h("div", { class: `box ${f.needs_review && f.status === "pending" ? "flag" : ""}` });
    Object.assign(b.style, { left: `${f.bbox.x0 * 100}%`, top: `${f.bbox.y0 * 100}%`, width: `${(f.bbox.x1 - f.bbox.x0) * 100}%`, height: `${(f.bbox.y1 - f.bbox.y0) * 100}%` });
    page.append(b);
    boxesByField.set(f.id, b);
  }
  const stage = h("div", { class: "stage" }, pagesEl);
  const zoomBtn = h("button", { class: "btn small", onclick: () => { zoomed = !zoomed; focusBox(); } });
  const viewer = h("div", { class: "viewer" }, stage, h("div", { class: "tools" }, zoomBtn));

  const fieldsEl = h("div", { class: "fields" });
  const bannerEl = h("div", { class: "bannerslot" });
  const finishBtn = h("button", { class: "btn ok", onclick: () => complete(false) }, "Finish ", h("kbd", {}, `${MOD}↵`));
  const notes = (doc.checks || []).filter(c => c.status === "note");
  const meta = [doc.doc_type_label, `${doc.page_count} page${doc.page_count === 1 ? "" : "s"}`,
    doc.languages && doc.languages.some(l => !["english", "other"].includes(l)) ? doc.languages.filter(l => l !== "other").map(l => l[0].toUpperCase() + l.slice(1)).join(" + ") : null,
    doc.handwriting ? "Handwriting" : null].filter(Boolean).join(" · ");
  const dueEl = h("span", { class: "due" });
  const top = h("div", { class: "head" },
    h("div", { class: "row" }, h("div", { class: "title" }, doc.filename), h("div", { class: "spacer" }), pill(doc.status, doc.status_label)),
    h("div", { class: "muted small" }, meta, dueEl),
    bannerEl,
    notes.length ? h("details", { class: "notes" }, h("summary", {}, `Notes (${notes.length})`), h("ul", {}, notes.map(c => h("li", {}, c.message)))) : null);
  const allToggle = h("input", { type: "checkbox", onchange: e => { showAll = e.target.checked; active = 0; renderFields(); } });
  allToggle.checked = showAll;
  const keysEl = h("div", { class: "keys" });
  const foot = h("div", { class: "foot" },
    canReview ? h("div", { class: "row" }, h("label", { class: "check" }, allToggle, "All fields ", h("kbd", {}, "A")), h("div", { class: "spacer" }), finishBtn)
      : h("div", { class: "muted small" }, doc.reviewed_at ? `Reviewed ${when(doc.reviewed_at)}` : "View only"),
    canReview ? keysEl : null);
  shell("review", h("div", { class: "review" }, viewer, h("div", { class: "side" }, top, fieldsEl, foot)), true);

  function visible() { return doc.fields.filter(f => showAll || f.needs_review); }
  function pendingCount() { return doc.fields.filter(todo).length; }
  // what the reviewer can pick with one key: [1] the value shown, [2] the other reading, [3] a suggestion
  function options(f) {
    if (!todo(f)) return [];
    const seen = new Set([JSON.stringify(f.value)]);
    const out = [];
    for (const [v, why] of [[f.alt_value, "other reading"], [f.suggested_value, f.suggestion_reason || "suggested"]]) {
      if (!has(v) || seen.has(JSON.stringify(v))) continue;
      seen.add(JSON.stringify(v));
      out.push({ value: v, why });
    }
    return out.length ? [{ value: f.value, why: null, keep: true }, ...out] : [];
  }
  function updateChrome() {
    const pend = pendingCount();
    bannerEl.replaceChildren();
    if (failedChecks.length) bannerEl.append(h("div", { class: "banner bad" }, failedChecks[0].message));
    else if (spot && pend) bannerEl.append(h("div", { class: "banner info" }, `Spot check: confirm all ${pend} fields`));
    else if (pend) bannerEl.append(h("div", { class: "banner warn" }, `${pend} to check`));
    else if (canReview) bannerEl.append(h("div", { class: "banner ok" }, "All checked"));
    else if (doc.message) bannerEl.append(h("div", { class: `banner ${["ready", "exported"].includes(doc.status) ? "ok" : "warn"}` }, doc.message));
    const due = doc.review_due_at && !doc.reviewed_at ? dueText(doc.review_due_at) : "";
    dueEl.textContent = due ? ` · ${due}` : "";
    dueEl.classList.toggle("bad-text", due.endsWith("overdue"));
    finishBtn.disabled = pend > 0;
    const f = visible()[active], opts = f ? options(f) : [];
    keysEl.replaceChildren(...[["↑↓", "move"], f && todo(f) ? ["↵", "confirm"] : null, opts.length ? [`1–${opts.length}`, "pick"] : null,
      f && todo(f) ? ["type", "to fix"] : null, ["Z", "zoom"]].filter(Boolean).map(([k, v]) => h("span", {}, h("kbd", {}, k), ` ${v}`)));
    zoomBtn.replaceChildren(zoomed ? "Whole page " : "Zoom ", h("kbd", {}, "Z"));
  }
  function renderFields() {
    const list = visible();
    fieldsEl.replaceChildren();
    let lastGroup = null;
    list.forEach((f, i) => {
      const group = f.line_index !== null ? `Line ${f.line_index + 1}` : ({ header: "Document", parties: "Parties", amounts: "Amounts", payment: "Payment", custom: "Custom" }[f.group] || f.group);
      if (group !== lastGroup) { fieldsEl.append(h("div", { class: "group" }, group)); lastGroup = group; }
      const isActive = i === active, opts = options(f), shown = fmtVal(f.value, f.type);
      const state = f.status === "confirmed" ? h("span", { class: "tag ok" }, "✓") : f.status === "corrected" ? h("span", { class: "tag fix" }, "Fixed") : null;
      const row = h("div", { class: `field ${isActive ? "active" : ""} ${todo(f) ? "todo" : ""}`, onclick: () => { active = i; editing = false; renderFields(); } },
        h("div", { class: "lab" }, h("span", { class: `dotc ${["confirmed", "corrected"].includes(f.status) ? "high" : f.band}` }), f.label.replace(/^Line \d+ · /, "")),
        h("div", { class: "state" }, state),
        editing && isActive ? null : opts.length ? null : h("div", { class: `val ${shown ? "" : "none"}`, title: canReview ? "Click to edit (F2)" : null,
          onclick: e => { if (isActive && canReview) { e.stopPropagation(); editing = true; renderFields(); } } }, shown ?? "Not on document"),
        todo(f) && f.reason ? h("div", { class: "why" }, f.reason) : null);
      if (opts.length && !(editing && isActive)) {
        row.append(h("div", { class: "opts" }, opts.map((o, k) => h("button", { class: `opt ${o.keep ? "keep" : ""}`, title: o.why || "as read",
          onclick: e => { e.stopPropagation(); active = i; choose(f, o); } },
          h("kbd", {}, String(k + 1)), h("span", { class: "v" }, fmtVal(o.value, f.type) ?? "Blank"), o.why ? h("span", { class: "w" }, o.why) : null))));
      }
      if (isActive && !todo(f) && (f.evidence || []).length) row.append(h("div", { class: "evid" }, f.evidence.join(" · ")));
      if (editing && isActive) {
        const inp = h("input", { type: "text", value: editVal(f.value, f.type), placeholder: f.type === "date" ? "DD/MM/YYYY" : "", "aria-label": f.label });
        inp.addEventListener("keydown", async e => {
          if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); await act(f, "correct", inp.value); }
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); editing = false; renderFields(); }
        });
        row.append(inp);
        setTimeout(() => { inp.focus(); if (inp.value.length > 1) inp.select(); }, 0);
      }
      fieldsEl.append(row);
    });
    const el = fieldsEl.querySelector(".field.active");
    if (el) el.scrollIntoView({ block: "nearest" });
    updateChrome();
    focusBox();
  }
  function focusBox() {
    boxesByField.forEach(b => b.classList.remove("active"));
    const f = visible()[active];
    const b = f && boxesByField.get(f.id);
    zoomBtn.replaceChildren(zoomed ? "Whole page " : "Zoom ", h("kbd", {}, "Z"));
    if (!b) { pagesEl.style.transform = ""; return; }
    b.classList.add("active");
    const page = b.parentElement;
    if (!zoomed) { pagesEl.style.transform = ""; page.scrollIntoView({ block: "nearest" }); return; }
    const bw = f.bbox.x1 - f.bbox.x0, scale = Math.min(3, Math.max(1.4, 0.35 / Math.max(bw, 0.05)));
    pagesEl.style.transform = `scale(${scale})`;
    requestAnimationFrame(() => {
      const r = b.getBoundingClientRect(), s = stage.getBoundingClientRect();
      stage.scrollLeft += (r.left + r.width / 2) - (s.left + s.width / 2);
      stage.scrollTop += (r.top + r.height / 2) - (s.top + s.height / 2);
    });
  }
  function choose(f, o) { return o.keep ? act(f, "confirm") : act(f, "correct", editVal(o.value, f.type)); }
  async function act(f, action, value) {
    const ms = Math.round(performance.now() - tStart);
    try {
      const r = await api(`/api/review/${doc.id}/fields/${f.id}`, { method: "POST", json: { action, value, time_ms: ms } });
      Object.assign(f, r.field);
      failedChecks = r.failed_checks.filter(c => c.severity === "error");
      editing = false;
      const b = boxesByField.get(f.id); if (b) b.classList.remove("flag");
      next();
    } catch (e) { toast(e.message); }
  }
  function next() {
    const list = visible();
    const idx = list.findIndex((f, i) => i > active && todo(f));
    const first = list.findIndex(todo);
    if (idx >= 0) active = idx; else if (first >= 0) active = first; else if (active < list.length - 1 && showAll) active++;
    tStart = performance.now();
    renderFields();
  }
  async function complete(override) {
    try {
      const r = await api(`/api/review/${doc.id}/complete`, { method: "POST", json: { override_failed_checks: override } });
      if (!r.completed) return confirmOverride(r.failed_checks);
      toast("Done");
      const q = await api("/api/review/queue");
      queueCount = q.documents.length;
      location.hash = q.documents.length ? `#/review/${q.documents[0].id}` : "#/review";
    } catch (e) { toast(e.message); }
  }
  function confirmOverride(checks) {
    const bg = h("div", { class: "modal-bg" }, h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
      h("h2", {}, "Figures still don't reconcile"),
      h("ul", {}, checks.map(c => h("li", {}, c.message))),
      h("p", { class: "muted" }, "Finish anyway only if the document is printed this way."),
      h("div", { class: "row" }, h("div", { class: "spacer" }), h("button", { class: "btn", onclick: () => bg.remove() }, "Back"),
        h("button", { class: "btn primary", onclick: () => { bg.remove(); complete(true); } }, "Confirm as printed"))));
    document.body.append(bg);
    bg.querySelector(".btn.primary").focus();
  }
  const onKey = e => {
    if (editing || document.querySelector(".modal-bg")) return;
    if (e.target.tagName === "INPUT" && e.target.type !== "checkbox") return;
    const list = visible(), f = list[active];
    if (!canReview && !["ArrowDown", "ArrowUp", "j", "k", "z", "Z"].includes(e.key)) return;
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); if (!pendingCount()) complete(false); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const opts = f ? options(f) : [];
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); active = Math.min(list.length - 1, active + 1); tStart = performance.now(); renderFields(); }
    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); active = Math.max(0, active - 1); tStart = performance.now(); renderFields(); }
    else if (e.key === "Enter" && f && todo(f)) { e.preventDefault(); act(f, "confirm"); }
    else if (opts.length && /^[1-9]$/.test(e.key) && +e.key <= opts.length) { e.preventDefault(); choose(f, opts[+e.key - 1]); }
    else if ((e.key === "s" || e.key === "S") && f && todo(f) && has(f.suggested_value)) { e.preventDefault(); act(f, "correct", editVal(f.suggested_value, f.type)); }
    else if (e.key === "z" || e.key === "Z") { zoomed = !zoomed; focusBox(); updateChrome(); }
    else if (e.key === "a" || e.key === "A") { showAll = !showAll; allToggle.checked = showAll; active = 0; renderFields(); }
    else if (e.key === "F2" && f && canReview) { e.preventDefault(); editing = true; renderFields(); }
    else if (f && canReview && e.key.length === 1 && /[0-9A-Za-z₹.,\-\/]/.test(e.key) && !/^[ajksz]$/i.test(e.key)) {
      editing = true; renderFields();
      const inp = fieldsEl.querySelector(".field.active input");
      if (inp) { inp.value = e.key; e.preventDefault(); }
    }
  };
  document.addEventListener("keydown", onKey);
  cleanup.push(() => document.removeEventListener("keydown", onKey));
  active = Math.max(0, visible().findIndex(todo));
  renderFields();
}

// ---------------------------------------------------------------- 3-way match
async function renderMatch(id) {
  if (id) return renderMatchResult(await api(`/api/match/${id}`));
  const docs = (await api("/api/documents?status=ready,exported")).documents;
  const pick = (type, multi) => {
    const opts = docs.filter(d => d.doc_type === type);
    return h("select", { multiple: multi, size: multi ? Math.min(6, Math.max(3, opts.length)) : null },
      multi ? null : h("option", { value: "" }, "Choose"), opts.map(d => h("option", { value: d.id }, `${d.filename} · ${when(d.created_at)}`)));
  };
  const po = pick("po", false), grn = pick("grn", true), inv = pick("invoice", false);
  const err = h("div", { class: "err" });
  const past = (await api("/api/match")).matches;
  shell("match", [
    head("3-way match"),
    h("div", { class: "card grid g3" }, h("label", { class: "f" }, "Purchase order", po), h("label", { class: "f" }, `Goods receipts (${MOD}-click for several)`, grn), h("label", { class: "f" }, "Invoice", inv)),
    h("div", { class: "row", style: { margin: "12px 0 24px" } }, h("button", { class: "btn primary", onclick: async () => {
      err.textContent = "";
      try {
        const r = await api("/api/match", { method: "POST", json: { po_id: po.value, invoice_id: inv.value, grn_ids: [...grn.selectedOptions].map(o => o.value) } });
        location.hash = `#/match/${r.id}`;
      } catch (e) { err.textContent = e.message; }
    } }, "Match"), err),
    past.length ? h("div", { class: "card flush" }, h("table", { class: "t" }, h("tbody", {}, past.map(m => h("tr", { class: "click", onclick: () => { location.hash = `#/match/${m.id}`; } },
      h("td", {}, pill(m.status === "matched" ? "ready" : m.status === "mismatch" ? "failed" : "needs_review", m.status === "matched" ? "Matched" : m.status === "mismatch" ? "Mismatch" : "Incomplete")),
      h("td", {}, m.summary), h("td", { class: "num muted" }, when(m.created_at))))))) : null,
  ]);
}
function renderMatchResult(r) {
  const n = v => (v === null || v === undefined ? "—" : qty.format(v));
  const money = v => (v === null || v === undefined ? "—" : `₹${inr.format(v)}`);
  shell("match", [
    head("Match result", h("a", { class: "btn", href: "#/match" }, "New match")),
    h("div", { class: `banner ${r.status === "matched" ? "ok" : r.status === "mismatch" ? "bad" : "warn"}` }, r.summary),
    r.issues.length ? h("div", { class: "card" }, h("ul", { class: "issues" }, r.issues.map(i => h("li", { class: i.severity === "block" ? "bad-text" : "" }, i.message)))) : null,
    h("div", { class: "card flush" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, ["Item", "Ordered", "Received", "Accepted", "Billed", "PO rate", "Invoice rate", "GST PO / inv", ""].map(x => h("th", { class: x === "Item" || !x ? "" : "num" }, x)))),
      h("tbody", {}, r.lines.map(l => h("tr", {},
        h("td", {}, l.description || "—"), h("td", { class: "num" }, n(l.qty_ordered)), h("td", { class: "num" }, n(l.qty_received)),
        h("td", { class: "num" }, n(l.qty_accepted)), h("td", { class: "num" }, n(l.qty_invoiced)), h("td", { class: "num" }, money(l.rate_po)),
        h("td", { class: "num" }, money(l.rate_invoice)), h("td", { class: "num" }, `${n(l.gst_po)}% / ${n(l.gst_invoice)}%`),
        h("td", {}, l.status === "ok" ? h("span", { class: "ok-text" }, "OK") : h("span", { class: "bad-text" }, l.status === "not_on_po" ? "Not on PO" : "Mismatch"))))))),
  ]);
}

// ---------------------------------------------------------------- templates
async function renderTemplates() {
  const [t, docs] = await Promise.all([api("/api/templates"), api("/api/documents?status=ready,exported")]);
  const typeSel = h("select", { onchange: () => fillDocs() }, Object.entries(ME.doc_types).map(([k, v]) => h("option", { value: k }, v)));
  const name = h("input", { type: "text", placeholder: "Patel Castings invoice" });
  const extra = h("input", { type: "text", placeholder: "vendor code, batch no" });
  const checks = h("div", { class: "checklist" });
  const err = h("div", { class: "err" });
  function fillDocs() {
    const list = docs.documents.filter(d => d.doc_type === typeSel.value);
    checks.replaceChildren(...(list.length ? list.map(d => h("label", {}, h("input", { type: "checkbox", value: d.id }), d.filename, h("span", { class: "muted" }, when(d.created_at))))
      : [h("div", { class: "muted" }, "No reviewed documents of this type yet")]));
  }
  fillDocs();
  shell("templates", [
    head("Templates"),
    h("div", { class: "card grid" }, h("h2", {}, "Learn a vendor's layout from 3–5 reviewed documents"),
      h("div", { class: "grid g2 eq" }, h("label", { class: "f" }, "Type", typeSel), h("label", { class: "f" }, "Name", name)),
      h("div", { class: "f" }, h("div", { class: "flabel" }, "Examples"), checks),
      h("label", { class: "f" }, "Extra fields (optional, comma separated)", extra), err,
      h("div", {}, h("button", { class: "btn primary", onclick: async () => {
        err.textContent = "";
        const ids = [...checks.querySelectorAll("input:checked")].map(i => i.value);
        const extra_fields = extra.value.split(",").map(s => s.trim()).filter(Boolean).map(s => ({ name: s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, ""), label: s }));
        try { await api("/api/templates", { method: "POST", json: { doc_type: typeSel.value, name: name.value || "Untitled", example_document_ids: ids, extra_fields } }); toast("Created. Activate it below."); renderTemplates(); }
        catch (e) { err.textContent = e.message; }
      } }, "Create"))),
    t.templates.length ? h("div", { class: "card" },
      t.templates.map(tp => h("div", { class: "tpl" },
        h("div", { class: "row" }, h("strong", {}, tp.name), h("span", { class: "muted" }, `${ME.doc_types[tp.doc_type]} · ${tp.examples} examples`),
          pill(tp.status === "active" ? "ready" : "uploaded", tp.status), h("div", { class: "spacer" }),
          tp.status !== "active" ? h("button", { class: "btn small primary", onclick: async () => { try { await api(`/api/templates/${tp.id}`, { method: "POST", json: { status: "active" } }); toast("Activated"); renderTemplates(); } catch (e) { toast(e.message); } } }, "Activate")
            : h("button", { class: "btn small", onclick: async () => { await api(`/api/templates/${tp.id}`, { method: "POST", json: { status: "retired" } }); renderTemplates(); } }, "Retire")),
        tp.extra_fields.length ? h("div", { class: "muted small" }, `Extra fields: ${tp.extra_fields.map(f => f.label).join(", ")}`) : null,
        tp.hints ? h("details", {}, h("summary", { class: "muted small" }, "Layout notes"), h("div", { class: "hints" }, tp.hints)) : null))) : null,
  ]);
}

// ---------------------------------------------------------------- admin
async function renderAdmin() {
  const [u, a, sp, co] = await Promise.all([api("/api/admin/users"), api("/api/admin/audit?limit=50"), api("/api/subprocessors"),
    api("/api/admin/company")]);
  const gst = h("textarea", { rows: 3, placeholder: "27AAPFU0939F1ZV" });
  gst.value = (co.own_gstins || []).join("\n");
  const gerr = h("div", { class: "err" });
  const email = h("input", { type: "email" }), nm = h("input", { type: "text" }), pw = h("input", { type: "password", placeholder: "12+ characters" });
  const role = h("select", {}, Object.entries(u.roles).map(([k, v]) => h("option", { value: k, title: v }, k)));
  const err = h("div", { class: "err" });
  shell("admin", [
    head("Admin"),
    h("div", { class: "card" }, h("h2", {}, "Users"),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Name", "Email", "Role", "Two-step", "Last sign-in", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, u.users.map(x => h("tr", {}, h("td", {}, x.name), h("td", {}, x.email), h("td", {}, x.role),
          h("td", {}, x.mfa ? h("span", { class: "ok-text" }, "On") : h("span", { class: "muted" }, "Pending")),
          h("td", { class: "muted" }, when(x.last_login_at)),
          h("td", { class: "num" }, x.id === ME.id ? null : h("button", { class: "btn small", onclick: async () => { await api(`/api/admin/users/${x.id}`, { method: "POST", json: { active: !x.active } }); renderAdmin(); } }, x.active ? "Deactivate" : "Reactivate")))))),
      h("div", { class: "grid g5" }, h("label", { class: "f" }, "Name", nm), h("label", { class: "f" }, "Email", email),
        h("label", { class: "f" }, "Role", role), h("label", { class: "f" }, "Temporary password", pw),
        h("button", { class: "btn primary", onclick: async () => {
          err.textContent = "";
          try { await api("/api/admin/users", { method: "POST", json: { name: nm.value, email: email.value, role: role.value, password: pw.value } }); toast("User added"); renderAdmin(); }
          catch (e) { err.textContent = e.message; }
        } }, "Add user")), err),
    h("div", { class: "card" }, h("h2", {}, "Company GSTINs"),
      h("p", { class: "muted small" }, "Checks every invoice is billed to you and fixes misread buyer GSTINs." +
        ((co.learned_gstins || []).length ? ` Learned: ${co.learned_gstins.join(", ")}` : "")),
      gst, h("div", { class: "row" }, h("button", { class: "btn primary", onclick: async () => {
        gerr.textContent = "";
        try { await api("/api/admin/company", { method: "POST", json: { own_gstins: gst.value.split(/[\s,]+/).filter(Boolean) } }); toast("Saved"); }
        catch (e) { gerr.textContent = e.message; }
      } }, "Save"), gerr)),
    h("div", { class: "card" }, h("h2", {}, "Subprocessors"),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Name", "Purpose", "Data", "Location"].map(x => h("th", {}, x)))),
        h("tbody", {}, sp.subprocessors.map(s => h("tr", {}, h("td", {}, s.name), h("td", {}, s.purpose), h("td", {}, s.data), h("td", {}, s.location)))))),
    h("details", { class: "card" }, h("summary", {}, h("h2", { class: "inline" }, "Audit trail"), h("span", { class: "muted small" }, " Tamper-evident. IDs and actions only, never document contents.")),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["When", "Event", "By", "Object", "Details"].map(x => h("th", {}, x)))),
        h("tbody", {}, a.events.map(e => h("tr", {}, h("td", { class: "muted" }, when(e.ts)), h("td", {}, e.event), h("td", { class: "muted" }, e.actor_id ? e.actor_id.slice(0, 8) : "system"),
          h("td", { class: "muted" }, e.object_id ? `${e.object_type} ${e.object_id.slice(0, 8)}` : ""), h("td", { class: "muted" }, Object.entries(e.meta).map(([k, v]) => `${k}=${v}`).join(" "))))))),
  ]);
}

// ---------------------------------------------------------------- internal metrics
async function renderInternal() {
  const m = await api("/api/internal/metrics?days=30");
  const sliceTable = (title, obj) => h("div", { class: "card" }, h("h2", {}, title), h("table", { class: "t" },
    h("thead", {}, h("tr", {}, ["", "Docs", "Fields", "Accuracy", "Review rate", "Spot-checked", "Escape rate"].map(x => h("th", { class: x ? "num" : "" }, x)))),
    h("tbody", {}, Object.entries(obj).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", { class: "num" }, v.documents), h("td", { class: "num" }, v.fields),
      h("td", { class: "num" }, pct(v.first_pass_accuracy_pct)), h("td", { class: "num" }, pct(v.field_review_rate_pct)),
      h("td", { class: "num" }, v.spot_check_fields), h("td", { class: "num" }, pct(v.escape_rate_pct)))))));
  const dr = m.demo_readiness, ef = m.efficiency || {};
  shell("internal", [
    head("Internal", h("span", { class: "muted" }, `All tenants · ${m.window_days} days · ${m.documents} documents`)),
    h("div", { class: `banner ${dr.status === "ok" ? "info" : "bad"}` },
      dr.status === "ok" ? `Eval gate ${when(dr.run_at)}: ${dr.cells.filter(c => c.demo_safe).map(c => c.cell).join(", ") || "no cell passed"}` : dr.message),
    h("div", { class: "grid g4" },
      kpi("AI calls / document", ef.calls_per_doc ?? "—", ef.one_call_pct !== null && ef.one_call_pct !== undefined ? `${ef.one_call_pct}% needed one call` : null),
      kpi("Proven without AI", pct(ef.fields_proven_pct), "QR, arithmetic, PDF text, records"),
      kpi("Re-read zoomed", pct(ef.fields_reread_pct), "of fields"),
      kpi("Cost / document", m.cost_usd_per_doc !== null ? `$${m.cost_usd_per_doc}` : "—", `${m.model_calls} calls · ${m.model_errors} errors`)),
    h("div", { class: "grid g4" },
      kpi("Latency p50 / p95", m.latency_ms.p50 ? `${(m.latency_ms.p50 / 1000).toFixed(1)}s / ${(m.latency_ms.p95 / 1000).toFixed(1)}s` : "—"),
      kpi("Review p50 / p90", m.review.turnaround_p50_min !== null ? `${Math.round(m.review.turnaround_p50_min)} / ${Math.round(m.review.turnaround_p90_min)} min` : "—", m.review.on_time_pct !== null ? `${m.review.on_time_pct}% on time` : null),
      kpi("Seconds / field", m.review.seconds_per_field_p50 ?? "—", "median review")),
    sliceTable("By type", m.by_doc_type), sliceTable("By quality", m.by_quality_bucket),
    sliceTable("By language", m.by_language), sliceTable("By handwriting", m.by_handwriting),
    h("div", { class: "card" }, h("h2", {}, "Calibration"), h("p", { class: "muted small" }, "Error rate must fall as the score rises; otherwise run the backtest."),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Score"), h("th", { class: "num" }, "Reviewed"), h("th", { class: "num" }, "Error rate"))),
        h("tbody", {}, m.calibration.map(c => h("tr", {}, h("td", {}, c.score_bucket), h("td", { class: "num" }, c.reviewed), h("td", { class: "num" }, pct(c.error_rate_pct))))))),
  ]);
}

// ---------------------------------------------------------------- boot
async function boot() {
  try { ME = await api("/api/me", { allow401: true }); }
  catch { ME = null; }
  if (!ME || ME.detail) { ME = null; return renderLogin(); }
  if (!location.hash) location.hash = home(); else route();
}
boot();
