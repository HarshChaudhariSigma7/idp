// Sereno Volante web app. No build step, no framework: loads instantly on a conference-room laptop.
// All document-derived text is inserted with textContent (never innerHTML): it is untrusted input.

const $app = document.getElementById("app");
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
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const qty = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 3 });
function fmtVal(v, type) {
  if (v === null || v === undefined || v === "") return null;
  if (type === "number") return Number.isInteger(v) && Math.abs(v) < 100000 ? qty.format(v) : inr.format(v);
  if (type === "percent") return `${qty.format(v)}%`;
  if (type === "integer") return qty.format(v);
  if (type === "date") { const d = new Date(v + "T00:00:00"); return isNaN(d) ? v : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }); }
  return String(v);
}
function editVal(v, type) {
  if (v === null || v === undefined) return "";
  if (type === "date") { const [y, m, d] = String(v).split("-"); return d ? `${d}/${m}/${y}` : v; }
  return String(v);
}
function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts + (String(ts).endsWith("Z") ? "" : "Z"));
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
function dueText(ts) {
  if (!ts) return "";
  const ms = new Date(ts + "Z") - Date.now();
  const m = Math.round(Math.abs(ms) / 60000);
  const t = m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
  return ms < 0 ? `Overdue by ${t}` : `Due in ${t}`;
}
function toast(msg) {
  const t = h("div", { class: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}
function pill(status, label) { return h("span", { class: `pill ${status}` }, label); }
function can(p) { return ME && ME.permissions.includes(p); }

// ---------------------------------------------------------------- shell & routing
function shell(active, content, full = false) {
  const links = [
    ["dashboard", "Dashboard", can("dashboard.view")],
    ["documents", "Documents", can("document.upload") || can("document.view_all")],
    ["review", "Review", can("review.work")],
    ["match", "3-way match", can("match.run")],
    ["templates", "Templates", can("template.manage")],
    ["admin", "Admin", can("user.manage")],
    ["internal", "Internal metrics", can("metrics.internal")],
  ].filter(l => l[2]);
  const nav = h("nav", {}, links.map(([k, label]) => h("a", { href: `#/${k}`, class: active === k ? "on" : "" }, label,
    k === "review" && queueCount ? h("span", { class: "badge" }, queueCount) : null)));
  $app.replaceChildren(
    h("div", { class: "top" },
      h("div", { class: "logo" }, "Sereno Volante", h("small", {}, ME.company)),
      nav,
      h("div", { class: "me" }, h("span", {}, ME.name), h("button", { onclick: logout }, "Sign out"))),
    ME.demo ? h("div", { class: "demobar" }, "Demo workspace: synthetic documents with simulated AI readings and reviews. Numbers here are not accuracy data.") : null,
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
    h("h1", {}, "Sign in"), h("p", { class: "sub" }, "Sereno Volante document processing"),
    h("label", { class: "f" }, "Work email", email), h("label", { class: "f" }, "Password", pw),
    err, h("button", { class: "btn primary", type: "submit" }, "Continue"));
  $app.replaceChildren(h("div", { class: "login" }, form));
  email.focus();
}
async function renderMfa(enrolled) {
  const err = h("div", { class: "err" });
  const code = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "7", placeholder: "6-digit code" });
  const parts = [h("h1", {}, enrolled ? "Two-step verification" : "Set up two-step verification")];
  if (!enrolled) {
    const r = await api("/api/auth/mfa/enroll", { method: "POST", allow401: true });
    const qr = h("div", { class: "qr" });
    // server-generated SVG (no user content) parsed as XML, not HTML
    const doc = new DOMParser().parseFromString(r.qr_svg, "image/svg+xml");
    qr.append(document.importNode(doc.documentElement, true));
    parts.push(h("p", { class: "sub" }, "Scan with Google Authenticator, Microsoft Authenticator or similar. Required for every account that can see documents."), qr);
  } else parts.push(h("p", { class: "sub" }, "Enter the code from your authenticator app."));
  const form = h("form", { class: "card", onsubmit: async e => {
    e.preventDefault(); err.textContent = "";
    try { await api("/api/auth/mfa/verify", { method: "POST", json: { code: code.value }, allow401: true }); await boot(); }
    catch (x) { err.textContent = x.message; }
  } }, ...parts, h("label", { class: "f" }, "Code", code), err, h("button", { class: "btn primary", type: "submit" }, "Verify"));
  $app.replaceChildren(h("div", { class: "login" }, form));
  code.focus();
}

// ---------------------------------------------------------------- dashboard
function kpi(label, value, note, tone = "") {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value ?? "—"),
    h("div", { class: `note ${tone}` }, note || ""));
}
function trendChart(rows) {
  const W = 800, H = 220, P = { l: 40, r: 12, t: 12, b: 26 };
  const pts = rows.filter(r => r.accuracy !== null);
  if (pts.length < 1) return h("div", { class: "empty" }, "Trend appears after the first processed documents.");
  const lo = Math.max(0, Math.min(90, ...pts.map(r => Math.min(r.accuracy, r.straight_through ?? 100))) - 5);
  const x = i => P.l + (pts.length === 1 ? (W - P.l - P.r) / 2 : i * (W - P.l - P.r) / (pts.length - 1));
  const y = v => P.t + (100 - v) * (H - P.t - P.b) / (100 - lo);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": "Accuracy trend" });
  for (const v of [lo, (lo + 100) / 2, 100]) {
    g.append(svg("line", { x1: P.l, x2: W - P.r, y1: y(v), y2: y(v), class: "axis" }));
    g.append(svg("text", { x: 4, y: y(v) + 4, class: "lbl" }, `${Math.round(v)}%`));
  }
  const path = (key) => pts.map((r, i) => `${i ? "L" : "M"}${x(i)},${y(r[key] ?? 0)}`).join(" ");
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
  const tatTone = t.average_minutes === null ? "" : (t.average_minutes <= t.sla_minutes ? "good" : "bad");
  shell("dashboard", [
    h("div", { class: "row" }, h("div", {}, h("h1", {}, "Overview"), h("p", { class: "sub" }, `${ME.company} · last ${d.window_days} days`)),
      h("div", { class: "spacer" }), can("document.upload") ? h("a", { class: "btn primary", href: "#/documents" }, "Upload documents") : null),
    h("div", { class: "grid g4" },
      kpi("Processed today", d.today.processed, `${d.today.ready} ready to export · ${d.today.needed_review} needed review`),
      kpi("First-pass accuracy", a.first_pass_accuracy_pct !== null ? `${a.first_pass_accuracy_pct}%` : "—",
        a.fields_extracted ? `of ${qty.format(a.fields_extracted)} fields, no correction needed` : "No completed documents yet"),
      kpi("Average review time", t.average_minutes !== null ? fmtMinutes(t.average_minutes) : "—",
        t.on_time_pct !== null ? `${t.on_time_pct}% within the ${fmtMinutes(t.sla_minutes)} target` : `Target: ${fmtMinutes(t.sla_minutes)}`, tatTone),
      kpi("Data entry saved", d.time_saved_hours ? `${d.time_saved_hours} h` : "—", "Estimated manual keying avoided")),
    h("div", { class: "sectionhead" }, h("h2", {}, "Accuracy by document condition"),
      h("span", { class: "muted" }, "Share of fields correct first time, before any human touch. Every uncertain field still goes to a person.")),
    h("div", { class: "grid g4 conds" }, (d.conditions || []).map(conditionCard)),
    techStrip(d.technology || []),
    h("div", { class: "grid g2", style: { marginTop: "16px" } },
      h("div", { class: "card chart" },
        h("div", { class: "row" }, h("h2", {}, "Accuracy trend"), h("div", { class: "spacer" }),
          h("div", { class: "legend" }, h("span", {}, h("i"), "Fields correct first time"), h("span", {}, h("i", { class: "dash" }), "Documents with no review needed"))),
        trendChart(d.trend)),
      h("div", { class: "card" },
        h("h2", {}, "Review queue"),
        h("div", { class: "kpi" }, h("div", { class: "value" }, d.queue.open),
          h("div", { class: `note ${d.queue.overdue ? "bad" : "good"}` }, d.queue.overdue ? `${d.queue.overdue} past the ${fmtMinutes(t.sla_minutes)} target` : "Everything within target")),
        h("p", { class: "muted" }, "Every uncertain field is checked by a person before export. We never guess."),
        a.spot_check.documents ? h("p", {}, h("strong", {}, `Spot-check accuracy: ${a.spot_check.accuracy_pct}%`),
          h("span", { class: "muted" }, ` on ${a.spot_check.fields} auto-approved fields re-checked by hand (${a.spot_check.documents} documents)`)) : null,
        can("review.work") ? h("a", { class: "btn", href: "#/review" }, "Open queue") : null)),
    h("div", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "By document type"),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Type"), h("th", { class: "num" }, "Documents"), h("th", { class: "num" }, "Needed review"), h("th", { class: "num" }, "Straight-through"))),
        h("tbody", {}, Object.entries(d.by_type).map(([k, v]) => h("tr", {}, h("td", {}, ME.doc_types[k] || k), h("td", { class: "num" }, v.documents),
          h("td", { class: "num" }, v.needed_review), h("td", { class: "num" }, v.documents ? `${Math.round(100 * (v.documents - v.needed_review) / v.documents)}%` : "—")))))),
  ]);
}
function sparkline(points) {
  const pts = points.filter(p => p.accuracy !== null);
  const W = 240, H = 44, P = 4;
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark", role: "img", "aria-label": "Daily accuracy" });
  if (pts.length < 2) { g.append(svg("line", { x1: P, x2: W - P, y1: H / 2, y2: H / 2, class: "spark-empty" })); return g; }
  const lo = Math.min(...pts.map(p => p.accuracy)), hi = Math.max(...pts.map(p => p.accuracy));
  const span = Math.max(hi - lo, 2);
  const x = i => P + i * (W - 2 * P) / (pts.length - 1), y = v => H - P - (v - (hi - span)) * (H - 2 * P) / span;
  g.append(svg("path", { d: pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.accuracy)}`).join(" "), class: "spark-line" }));
  pts.forEach((p, i) => { const c = svg("circle", { cx: x(i), cy: y(p.accuracy), r: 7, class: "spark-hit" }); c.append(svg("title", {}, `${p.day}: ${p.accuracy}%`)); g.append(c); });
  const last = pts[pts.length - 1];
  g.append(svg("circle", { cx: x(pts.length - 1), cy: y(last.accuracy), r: 3, class: "spark-dot" }));
  return g;
}
function conditionCard(c) {
  const hero = c.accuracy_pct === null ? "—" : `${c.accuracy_pct}%`;
  const status = !c.fields ? h("span", { class: "chip neutral" }, "No documents yet")
    : c.measured ? h("span", { class: "chip ok" }, "✓ Measured")
    : h("span", { class: "chip neutral" }, `Building baseline · ${c.fields} of 200 fields`);
  const bench = c.benchmark ? h("div", { class: "bench" }, `Benchmark: ${c.benchmark.field_accuracy_pct}% on ${c.benchmark.documents} real labelled documents`)
    : h("div", { class: "bench muted" }, "Independent benchmark: pending");
  return h("div", { class: "card cond" },
    h("div", { class: "row" }, h("div", { class: "label" }, c.label), h("div", { class: "spacer" }), status),
    h("div", { class: "hero" }, hero),
    h("div", { class: "muted small" }, (c.key === "vernacular" ? "Hindi & regional scripts · " : "") +
      (c.fields ? `${qty.format(c.fields)} fields · ${c.documents} documents` : "Appears after the first completed documents")),
    sparkline(c.trend),
    h("div", { class: "sub2" },
      h("div", {}, h("div", { class: "v" }, c.caught_before_export_pct === null ? "—" : `${c.caught_before_export_pct}%`), h("div", { class: "k" }, "errors caught before export")),
      h("div", {}, h("div", { class: "v" }, c.no_review_docs_pct === null ? "—" : `${c.no_review_docs_pct}%`), h("div", { class: "k" }, "documents with no review"))),
    c.spot_check && c.spot_check.documents ? h("div", { class: "small" }, `Spot-check: ${c.spot_check.accuracy_pct}% on ${c.spot_check.fields} auto-approved fields`) : null,
    bench,
    h("div", { class: "tech" }, h("div", { class: "techlabel" }, "Technology"), h("ul", {}, c.technology.map(t => h("li", {}, t)))));
}
function techStrip(steps) {
  if (!steps.length) return null;
  return h("div", { class: "card techstrip" },
    h("div", { class: "row" }, h("h2", {}, "Under the hood"), h("span", { class: "muted" }, "What the system did for your documents in this period")),
    h("ol", { class: "steps" }, steps.map((st, i) => h("li", {},
      h("div", { class: "stepno" }, String(i + 1)),
      h("div", { class: "stepname" }, st.step),
      h("div", { class: "stepval" }, qty.format(st.value)),
      h("div", { class: "stepunit" }, st.unit),
      h("div", { class: "stepdetail" }, st.detail)))));
}
function fmtMinutes(m) { return m >= 60 ? `${Math.floor(m / 60)}h ${Math.round(m % 60)}m` : `${Math.round(m)} min`; }

// ---------------------------------------------------------------- documents
async function renderDocuments() {
  const list = h("tbody");
  const typeSel = h("select", {}, h("option", { value: "auto" }, "Detect automatically"),
    Object.entries(ME.doc_types).map(([k, v]) => h("option", { value: k }, v)));
  const fileIn = h("input", { type: "file", multiple: true, accept: ".pdf,.jpg,.jpeg,.png,.tif,.tiff,.webp", style: { display: "none" } });
  const drop = h("div", { class: "drop", onclick: () => fileIn.click() },
    h("strong", {}, "Drop invoices, LRs, POs, GRNs or contracts here"), h("div", {}, "PDF, scans or phone photos · up to 50 files at once"));
  const upload = async files => {
    if (!files.length) return;
    const form = new FormData();
    [...files].forEach(f => form.append("files", f));
    form.append("doc_type", typeSel.value);
    drop.classList.add("over");
    try {
      const r = await api("/api/documents", { method: "POST", form });
      const errs = r.results.filter(x => x.error);
      errs.forEach(e => toast(`${e.filename}: ${e.error}`));
      const dup = r.results.filter(x => x.duplicate).length;
      toast(`${r.results.length - errs.length} uploaded${dup ? ` (${dup} already uploaded before)` : ""}`);
      load();
    } catch (e) { toast(e.message); } finally { drop.classList.remove("over"); }
  };
  fileIn.addEventListener("change", () => upload(fileIn.files));
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", e => { e.preventDefault(); upload(e.dataTransfer.files); });
  const statusSel = h("select", { onchange: () => load() }, h("option", { value: "" }, "All statuses"),
    [["needs_review,unreadable", "Needs review"], ["ready", "Ready to export"], ["exported", "Exported"], ["processing,uploaded", "Processing"], ["failed", "Couldn't process"]]
      .map(([v, l]) => h("option", { value: v }, l)));
  let timer = null;
  async function load() {
    const r = await api(`/api/documents${statusSel.value ? `?status=${statusSel.value}` : ""}`);
    list.replaceChildren(...(r.documents.length ? r.documents.map(docRow) : [h("tr", {}, h("td", { colspan: 5, class: "empty" }, "No documents yet. Upload a few to get started."))]));
    clearTimeout(timer);
    if (r.documents.some(d => ["uploaded", "processing"].includes(d.status))) timer = setTimeout(load, 2500);
  }
  cleanup.push(() => clearTimeout(timer));
  const exportBtns = can("export.run") ? h("div", { class: "row" },
    h("select", { id: "exptype" }, Object.entries(ME.doc_types).map(([k, v]) => h("option", { value: k }, v))),
    h("button", { class: "btn", onclick: () => doExport("xlsx") }, "Export to Excel"),
    h("button", { class: "btn", onclick: () => doExport("csv") }, "Export CSV")) : null;
  shell("documents", [
    h("div", { class: "row" }, h("div", {}, h("h1", {}, "Documents"), h("p", { class: "sub" }, "Upload, track and export. Status is updated live.")), h("div", { class: "spacer" }), exportBtns),
    can("document.upload") && !ME.ai_ready ? h("div", { class: "banner warn", style: { marginBottom: "12px" } },
      "No Anthropic API key on this server yet: you can explore the sample documents, but new uploads can't be read. Add ANTHROPIC_API_KEY to .env and restart.") : null,
    can("document.upload") ? h("div", { class: "card", style: { marginBottom: "16px" } },
      h("div", { class: "row", style: { marginBottom: "12px" } }, h("label", { class: "f" }, "Document type", typeSel)), drop, fileIn) : null,
    h("div", { class: "card" }, h("div", { class: "row", style: { marginBottom: "8px" } }, h("h2", {}, "All documents"), h("div", { class: "spacer" }), statusSel),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", {}, "Type"), h("th", {}, "Status"), h("th", {}, "Uploaded"), h("th", {}, ""))), list)),
  ]);
  await load();
}
function docRow(d) {
  const open = () => { location.hash = (d.status === "needs_review" || d.status === "unreadable") ? `#/review/${d.id}` : `#/doc/${d.id}`; };
  return h("tr", { class: "click", onclick: open },
    h("td", {}, h("div", { style: { fontWeight: 600 } }, d.filename), h("div", { class: "msg" }, d.message || "")),
    h("td", {}, d.doc_type_label || "—"),
    h("td", {}, pill(d.status, d.status_label)),
    h("td", { class: "muted" }, when(d.created_at)),
    h("td", { class: "muted" }, d.status === "needs_review" ? dueText(d.review_due_at) : ""));
}
async function doExport(fmt) {
  const t = document.getElementById("exptype").value;
  const r = await api(`/api/exports?doc_type=${t}&fmt=${fmt}`, { raw: true });
  if (!r.ok) { const e = await r.json().catch(() => ({})); return toast(e.detail || "Nothing to export"); }
  const blob = await r.blob();
  const a = h("a", { href: URL.createObjectURL(blob), download: (r.headers.get("content-disposition") || "").split('filename="')[1]?.replace('"', "") || `export.${fmt}` });
  document.body.append(a); a.click(); a.remove();
  toast("Export downloaded");
}

// ---------------------------------------------------------------- review queue
async function renderQueue() {
  const q = await api("/api/review/queue");
  queueCount = q.documents.length;
  shell("review", [
    h("div", { class: "row" }, h("div", {}, h("h1", {}, "Review queue"), h("p", { class: "sub" }, `Oldest deadline first · target turnaround ${fmtMinutes(ME.sla_minutes)}`)),
      h("div", { class: "spacer" }), q.documents.length ? h("a", { class: "btn primary", href: `#/review/${q.documents[0].id}` }, "Start reviewing (Enter)") : null),
    h("div", { class: "card" }, q.documents.length ? h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Document"), h("th", {}, "What needs checking"), h("th", { class: "num" }, "Fields"), h("th", {}, "Deadline"))),
      h("tbody", {}, q.documents.map(d => h("tr", { class: "click", onclick: () => { location.hash = `#/review/${d.id}`; } },
        h("td", {}, h("div", { style: { fontWeight: 600 } }, d.filename), h("div", { class: "muted" }, d.doc_type_label || "")),
        h("td", { class: "msg" }, d.is_qa_sample ? "Spot check: confirm all fields (quality sample)" : (d.message || "")),
        h("td", { class: "num" }, d.fields_flagged),
        h("td", { class: d.overdue ? "bad-text" : "" }, dueText(d.review_due_at))))))
      : h("div", { class: "empty" }, "Queue is clear. Nice work.")),
  ]);
  const onKey = e => { if (e.key === "Enter" && q.documents.length) location.hash = `#/review/${q.documents[0].id}`; };
  document.addEventListener("keydown", onKey);
  cleanup.push(() => document.removeEventListener("keydown", onKey));
}

// ---------------------------------------------------------------- review screen
async function renderReview(id) {
  const doc = await api(`/api/documents/${id}`);
  const canReview = doc.can_review;
  let showAll = !canReview || doc.is_qa_sample || !doc.fields.some(f => f.needs_review);
  let active = 0, editing = false, zoomed = true, tStart = performance.now();
  let failedChecks = [];

  const boxesByField = new Map();
  const pagesEl = h("div", { class: "zoomwrap" });
  for (const p of doc.pages) {
    const page = h("div", { class: "page", "data-page": p.page_no });
    if (doc.available) {
      const img = h("img", { src: `/api/documents/${doc.id}/pages/${p.page_no}`, alt: `Page ${p.page_no}` });
      img.addEventListener("error", () => page.replaceChildren(h("div", { class: "gone" }, "This page image has been deleted under the retention policy.")));
      page.append(img);
    } else page.append(h("div", { class: "gone" }, "The original document was deleted under the retention policy. Extracted data remains."));
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
  const zoomBtn = h("button", { class: "btn small", onclick: () => { zoomed = !zoomed; focusBox(); } }, "Zoom to field (Z)");
  const viewer = h("div", { class: "viewer" }, stage, h("div", { class: "tools" }, zoomBtn,
    h("button", { class: "btn small", onclick: () => { zoomed = false; focusBox(); } }, "Whole page")));

  const fieldsEl = h("div", { class: "fields" });
  const bannerEl = h("div");
  const slaEl = h("div", { class: "sla" });
  const completeBtn = h("button", { class: "btn ok", onclick: () => complete(false) }, "Finish document");
  const head = h("div", { class: "head" },
    h("div", { class: "row" }, h("div", { class: "title" }, doc.filename), h("div", { class: "spacer" }), pill(doc.status, doc.status_label)),
    h("div", { class: "muted" }, [doc.doc_type_label, `${doc.page_count} page${doc.page_count === 1 ? "" : "s"}`,
      doc.languages && doc.languages.includes("hindi") ? "English + Hindi" : null, doc.handwriting ? "handwriting" : null].filter(Boolean).join(" · ")),
    bannerEl, slaEl);
  const allToggle = h("label", { class: "muted", style: { display: "flex", gap: "6px", alignItems: "center", fontSize: "13px" } },
    h("input", { type: "checkbox", onchange: e => { showAll = e.target.checked; active = 0; renderFields(); } }), "Show all fields (A)");
  if (showAll) allToggle.querySelector("input").checked = true;
  const foot = h("div", { class: "foot" },
    canReview ? h("div", { class: "row" }, allToggle, h("div", { class: "spacer" }), completeBtn) : h("div", { class: "muted" }, doc.reviewed_at ? `Reviewed ${when(doc.reviewed_at)}` : "View only"),
    canReview ? h("div", { class: "keys" }, h("span", {}, h("kbd", {}, "↓"), " ", h("kbd", {}, "↑"), " next / previous"), h("span", {}, h("kbd", {}, "Enter"), " confirm"),
      h("span", {}, h("kbd", {}, "E"), " or type to correct"), h("span", {}, h("kbd", {}, "Esc"), " cancel"), h("span", {}, h("kbd", {}, "Ctrl"), "+", h("kbd", {}, "Enter"), " finish & next")) : null);
  const side = h("div", { class: "side" }, head, fieldsEl, foot);
  shell("review", h("div", { class: "review" }, viewer, side), true);

  function visible() {
    return doc.fields.filter(f => showAll || f.needs_review);
  }
  function pendingCount() { return doc.fields.filter(f => f.needs_review && f.status === "pending").length; }
  function updateBanner() {
    const pend = pendingCount();
    bannerEl.replaceChildren();
    if (doc.is_qa_sample && canReview) bannerEl.append(h("div", { class: "banner info" }, "Quality spot check: this document was auto-approved. Please confirm every field; this measures our real accuracy."));
    if (failedChecks.length) bannerEl.append(h("div", { class: "banner bad" }, failedChecks[0].message));
    else if (pend) bannerEl.append(h("div", { class: "banner warn" }, `${pend} field${pend > 1 ? "s" : ""} to check · ${doc.message || ""}`));
    else if (canReview) bannerEl.append(h("div", { class: "banner ok" }, "All flagged fields checked. Press Ctrl+Enter to finish."));
    else bannerEl.append(h("div", { class: `banner ${doc.status === "ready" || doc.status === "exported" ? "ok" : "warn"}` }, doc.message || doc.status_label));
    slaEl.textContent = doc.review_due_at && !doc.reviewed_at ? dueText(doc.review_due_at) : "";
    slaEl.className = `sla ${doc.review_due_at && new Date(doc.review_due_at + "Z") < Date.now() ? "over" : ""}`;
    completeBtn.disabled = pend > 0;
  }
  function renderFields() {
    const list = visible();
    fieldsEl.replaceChildren();
    let lastGroup = null;
    list.forEach((f, i) => {
      const group = f.line_index !== null ? `Line item ${f.line_index + 1}` : ({ header: "Document", parties: "Parties", amounts: "Amounts & tax", payment: "Payment", custom: "Custom fields" }[f.group] || f.group);
      if (group !== lastGroup) { fieldsEl.append(h("div", { class: "group" }, group)); lastGroup = group; }
      const shown = fmtVal(f.value, f.type);
      const state = f.status === "confirmed" ? h("span", { class: "tag ok" }, "Confirmed") : f.status === "corrected" ? h("span", { class: "tag fix" }, "Corrected")
        : (f.needs_review ? h("span", { class: "tag todo" }, "Check") : null);
      const row = h("div", { class: `field ${i === active ? "active" : ""}`, onclick: () => { active = i; editing = false; renderFields(); } },
        h("div", { class: "lab" }, h("span", { class: `dotc ${["confirmed", "corrected"].includes(f.status) ? "high" : f.band}`, title: bandText(f.band) }), f.label.replace(/^Line \d+ · /, "")),
        h("div", { class: "state" }, state),
        editing && i === active ? null : h("div", { class: `val ${shown ? "" : "none"}` }, shown ?? "Not on document"),
        f.needs_review && f.status === "pending" && f.reason ? h("div", { class: "why" }, f.reason) : null,
        f.alt_value !== null && f.alt_value !== undefined && f.status === "pending" ? h("div", { class: "alt" }, `Other reading: ${fmtVal(f.alt_value, f.type)}`) : null);
      if (editing && i === active) {
        const inp = h("input", { type: "text", value: editVal(f.value, f.type), placeholder: f.type === "date" ? "DD/MM/YYYY" : "" });
        inp.addEventListener("keydown", async e => {
          if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); await act(f, "correct", inp.value); }
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); editing = false; renderFields(); }
        });
        row.append(inp);
        setTimeout(() => { inp.focus(); inp.select(); }, 0);
      }
      fieldsEl.append(row);
    });
    const act_el = fieldsEl.querySelector(".field.active");
    if (act_el) act_el.scrollIntoView({ block: "nearest" });
    updateBanner();
    focusBox();
  }
  function bandText(b) { return { high: "Checked and consistent", medium: "Probably right", low: "Needs a look" }[b]; }
  function focusBox() {
    boxesByField.forEach(b => b.classList.remove("active"));
    const f = visible()[active];
    const b = f && boxesByField.get(f.id);
    zoomBtn.textContent = zoomed ? "Zoomed to field (Z)" : "Zoom to field (Z)";
    if (!b) { pagesEl.style.transform = ""; return; }
    b.classList.add("active");
    const page = b.parentElement;
    if (!zoomed) { pagesEl.style.transform = ""; page.scrollIntoView({ block: "nearest" }); return; }
    const bw = (f.bbox.x1 - f.bbox.x0), scale = Math.min(3, Math.max(1.4, 0.35 / Math.max(bw, 0.05)));
    pagesEl.style.transform = `scale(${scale})`;
    requestAnimationFrame(() => {
      const r = b.getBoundingClientRect(), s = stage.getBoundingClientRect();
      stage.scrollLeft += (r.left + r.width / 2) - (s.left + s.width / 2);
      stage.scrollTop += (r.top + r.height / 2) - (s.top + s.height / 2);
    });
  }
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
    const idx = list.findIndex((f, i) => i > active && f.needs_review && f.status === "pending");
    if (idx >= 0) active = idx; else if (active < list.length - 1 && showAll) active++;
    tStart = performance.now();
    renderFields();
  }
  async function complete(override) {
    try {
      const r = await api(`/api/review/${doc.id}/complete`, { method: "POST", json: { override_failed_checks: override } });
      if (!r.completed) return confirmOverride(r.failed_checks);
      toast("Document finished");
      const q = await api("/api/review/queue");
      queueCount = q.documents.length;
      location.hash = q.documents.length ? `#/review/${q.documents[0].id}` : "#/review";
    } catch (e) { toast(e.message); }
  }
  function confirmOverride(checks) {
    const bg = h("div", { class: "modal-bg" }, h("div", { class: "modal" },
      h("h2", {}, "Figures still don't reconcile"),
      h("ul", {}, checks.map(c => h("li", {}, c.message))),
      h("p", { class: "muted" }, "If the document itself is printed this way, you can finish anyway. It will be marked as 'confirmed as printed'."),
      h("div", { class: "row" }, h("div", { class: "spacer" }), h("button", { class: "btn", onclick: () => bg.remove() }, "Go back"),
        h("button", { class: "btn primary", onclick: () => { bg.remove(); complete(true); } }, "Finish anyway"))));
    document.body.append(bg);
  }
  const onKey = e => {
    if (!canReview && !["ArrowDown", "ArrowUp", "j", "k", "z", "Z"].includes(e.key)) return;
    if (editing || document.querySelector(".modal-bg")) return;
    if (e.target.tagName === "INPUT" && e.target.type !== "checkbox") return;
    const list = visible(), f = list[active];
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); if (!pendingCount()) complete(false); return; }
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); active = Math.min(list.length - 1, active + 1); tStart = performance.now(); renderFields(); }
    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); active = Math.max(0, active - 1); tStart = performance.now(); renderFields(); }
    else if (e.key === "Enter" && f) { e.preventDefault(); act(f, "confirm"); }
    else if ((e.key === "e" || e.key === "E" || e.key === "F2") && f) { e.preventDefault(); editing = true; renderFields(); }
    else if (e.key === "z" || e.key === "Z") { zoomed = !zoomed; focusBox(); }
    else if (e.key === "a" || e.key === "A") { showAll = !showAll; allToggle.querySelector("input").checked = showAll; active = 0; renderFields(); }
    else if (f && e.key.length === 1 && /[0-9A-Za-z₹.,\-\/]/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      editing = true; renderFields();
      const inp = fieldsEl.querySelector(".field.active input");
      if (inp) { inp.value = e.key; e.preventDefault(); }
    }
  };
  document.addEventListener("keydown", onKey);
  cleanup.push(() => document.removeEventListener("keydown", onKey));
  const firstFlag = visible().findIndex(f => f.needs_review && f.status === "pending");
  active = Math.max(0, firstFlag);
  renderFields();
}

// ---------------------------------------------------------------- 3-way match
async function renderMatch(id) {
  if (id) return renderMatchResult(await api(`/api/match/${id}`));
  const docs = (await api("/api/documents?status=ready,exported")).documents;
  const pick = (type, multi) => {
    const opts = docs.filter(d => d.doc_type === type);
    return h("select", { multiple: multi, size: multi ? Math.min(6, Math.max(3, opts.length)) : null },
      multi ? null : h("option", { value: "" }, "Choose…"), opts.map(d => h("option", { value: d.id }, `${d.filename} · ${when(d.created_at)}`)));
  };
  const po = pick("po", false), grn = pick("grn", true), inv = pick("invoice", false);
  const err = h("div", { class: "err" });
  const past = (await api("/api/match")).matches;
  shell("match", [
    h("h1", {}, "3-way match"), h("p", { class: "sub" }, "Check what you were billed against what you ordered and what you actually received."),
    h("div", { class: "card grid g3" }, h("label", { class: "f" }, "Purchase order", po), h("label", { class: "f" }, "Goods receipt notes (Ctrl-click for several)", grn), h("label", { class: "f" }, "Invoice", inv)),
    h("div", { class: "row", style: { margin: "12px 0 24px" } }, h("button", { class: "btn primary", onclick: async () => {
      err.textContent = "";
      try {
        const r = await api("/api/match", { method: "POST", json: { po_id: po.value, invoice_id: inv.value, grn_ids: [...grn.selectedOptions].map(o => o.value) } });
        location.hash = `#/match/${r.id}`;
      } catch (e) { err.textContent = e.message; }
    } }, "Run match"), err),
    h("div", { class: "card" }, h("h2", {}, "Recent matches"), past.length ? h("table", { class: "t" }, h("tbody", {}, past.map(m => h("tr", { class: "click", onclick: () => { location.hash = `#/match/${m.id}`; } },
      h("td", {}, pill(m.status === "matched" ? "ready" : m.status === "mismatch" ? "failed" : "needs_review", m.status === "matched" ? "Matched" : m.status === "mismatch" ? "Mismatch" : "Incomplete")),
      h("td", {}, m.summary), h("td", { class: "muted" }, when(m.created_at)))))) : h("div", { class: "muted" }, "No matches yet.")),
  ]);
}
function renderMatchResult(r) {
  const n = v => (v === null || v === undefined ? "—" : qty.format(v));
  const money = v => (v === null || v === undefined ? "—" : `₹${inr.format(v)}`);
  shell("match", [
    h("div", { class: "row" }, h("h1", {}, "Match result"), h("div", { class: "spacer" }), h("a", { class: "btn", href: "#/match" }, "New match")),
    h("div", { class: `banner ${r.status === "matched" ? "ok" : r.status === "mismatch" ? "bad" : "warn"}`, style: { margin: "12px 0" } }, r.summary),
    r.issues.length ? h("div", { class: "card", style: { marginBottom: "16px" } }, h("h2", {}, "What doesn't match"),
      h("ul", {}, r.issues.map(i => h("li", { class: i.severity === "block" ? "bad-text" : "" }, i.message)))) : null,
    h("div", { class: "card" }, h("table", { class: "t" },
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
  const name = h("input", { type: "text", placeholder: "e.g. Patel Castings invoice" });
  const extra = h("input", { type: "text", placeholder: "Optional extra fields, comma separated (e.g. vendor code, batch no)" });
  const checks = h("div", { class: "checklist" });
  const err = h("div", { class: "err" });
  function fillDocs() {
    const list = docs.documents.filter(d => d.doc_type === typeSel.value);
    checks.replaceChildren(...(list.length ? list.map(d => h("label", {}, h("input", { type: "checkbox", value: d.id }), d.filename, h("span", { class: "muted" }, when(d.created_at))))
      : [h("div", { class: "muted" }, "Process and review a few documents of this type first.")]));
  }
  fillDocs();
  shell("templates", [
    h("h1", {}, "Templates"), h("p", { class: "sub" }, "Teach the system a new vendor format from 3 to 5 reviewed examples. No setup or coding needed."),
    h("div", { class: "grid g2" },
      h("div", { class: "card grid" }, h("h2", {}, "New template"),
        h("div", { class: "grid g2" }, h("label", { class: "f" }, "Document type", typeSel), h("label", { class: "f" }, "Name", name)),
        h("div", { class: "f" }, h("label", { class: "f" }, "Pick 3–5 reviewed examples from the same vendor"), checks),
        h("label", { class: "f" }, "Extra fields", extra), err,
        h("div", {}, h("button", { class: "btn primary", onclick: async () => {
          err.textContent = "";
          const ids = [...checks.querySelectorAll("input:checked")].map(i => i.value);
          const extra_fields = extra.value.split(",").map(s => s.trim()).filter(Boolean).map(s => ({ name: s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, ""), label: s }));
          try { await api("/api/templates", { method: "POST", json: { doc_type: typeSel.value, name: name.value || "Untitled", example_document_ids: ids, extra_fields } }); toast("Template created. Review the notes and activate it."); renderTemplates(); }
          catch (e) { err.textContent = e.message; }
        } }, "Create template"))),
      h("div", { class: "card" }, h("h2", {}, "How it works"),
        h("ol", { class: "muted" }, h("li", {}, "Upload and review a few documents from the vendor."), h("li", {}, "Pick 3–5 of them here."),
          h("li", {}, "We learn where each field sits and what reviewers had to fix."), h("li", {}, "Activate: future documents from that vendor use these notes automatically.")))),
    h("div", { class: "card", style: { marginTop: "16px" } }, h("h2", {}, "Your templates"),
      t.templates.length ? t.templates.map(tp => h("div", { style: { borderTop: "1px solid var(--line)", padding: "14px 0" } },
        h("div", { class: "row" }, h("strong", {}, tp.name), h("span", { class: "muted" }, `${ME.doc_types[tp.doc_type]} · ${tp.examples} examples`),
          pill(tp.status === "active" ? "ready" : "uploaded", tp.status), h("div", { class: "spacer" }),
          tp.status !== "active" ? h("button", { class: "btn small primary", onclick: async () => { try { await api(`/api/templates/${tp.id}`, { method: "POST", json: { status: "active" } }); toast("Activated"); renderTemplates(); } catch (e) { toast(e.message); } } }, "Activate")
            : h("button", { class: "btn small", onclick: async () => { await api(`/api/templates/${tp.id}`, { method: "POST", json: { status: "retired" } }); renderTemplates(); } }, "Retire")),
        tp.extra_fields.length ? h("div", { class: "muted" }, `Extra fields: ${tp.extra_fields.map(f => f.label).join(", ")}`) : null,
        h("div", { class: "hints", style: { marginTop: "8px" } }, tp.hints || "No stable layout notes found yet."))) : h("div", { class: "muted" }, "No templates yet.")),
  ]);
}

// ---------------------------------------------------------------- admin
async function renderAdmin() {
  const [u, a, sp] = await Promise.all([api("/api/admin/users"), api("/api/admin/audit?limit=100"), api("/api/subprocessors")]);
  const email = h("input", { type: "email" }), nm = h("input", { type: "text" }), pw = h("input", { type: "password", placeholder: "Min 12 characters" });
  const role = h("select", {}, Object.entries(u.roles).map(([k, v]) => h("option", { value: k, title: v }, k)));
  const err = h("div", { class: "err" });
  shell("admin", [
    h("h1", {}, "Admin"), h("p", { class: "sub" }, "Users, access, audit trail and subprocessors."),
    h("div", { class: "card", style: { marginBottom: "16px" } }, h("h2", {}, "Users"),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Name", "Email", "Role", "Two-step", "Last sign-in", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, u.users.map(x => h("tr", {}, h("td", {}, x.name), h("td", {}, x.email), h("td", {}, x.role),
          h("td", {}, x.mfa ? h("span", { class: "ok-text" }, "On") : h("span", { class: "muted" }, "Pending first sign-in")),
          h("td", { class: "muted" }, when(x.last_login_at)),
          h("td", {}, x.id === ME.id ? null : h("button", { class: "btn small", onclick: async () => { await api(`/api/admin/users/${x.id}`, { method: "POST", json: { active: !x.active } }); renderAdmin(); } }, x.active ? "Deactivate" : "Reactivate")))))),
      h("div", { class: "grid g4", style: { marginTop: "12px", alignItems: "end" } }, h("label", { class: "f" }, "Name", nm), h("label", { class: "f" }, "Email", email),
        h("label", { class: "f" }, "Role", role), h("label", { class: "f" }, "Temporary password", pw)),
      h("div", { class: "row", style: { marginTop: "12px" } }, h("button", { class: "btn primary", onclick: async () => {
        err.textContent = "";
        try { await api("/api/admin/users", { method: "POST", json: { name: nm.value, email: email.value, role: role.value, password: pw.value } }); toast("User added"); renderAdmin(); }
        catch (e) { err.textContent = e.message; }
      } }, "Add user"), err)),
    h("div", { class: "card", style: { marginBottom: "16px" } }, h("h2", {}, "Subprocessors"),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Name", "Purpose", "Data", "Location"].map(x => h("th", {}, x)))),
        h("tbody", {}, sp.subprocessors.map(s => h("tr", {}, h("td", {}, s.name), h("td", {}, s.purpose), h("td", {}, s.data), h("td", {}, s.location)))))),
    h("div", { class: "card" }, h("h2", {}, "Audit trail (latest 100)"), h("p", { class: "muted" }, "Tamper-evident log. Contains IDs and actions only, never document contents."),
      h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["When", "Event", "By", "Object", "Details"].map(x => h("th", {}, x)))),
        h("tbody", {}, a.events.map(e => h("tr", {}, h("td", { class: "muted" }, when(e.ts)), h("td", {}, e.event), h("td", { class: "muted" }, e.actor_id ? e.actor_id.slice(0, 8) : "system"),
          h("td", { class: "muted" }, e.object_id ? `${e.object_type} ${e.object_id.slice(0, 8)}` : ""), h("td", { class: "muted" }, Object.entries(e.meta).map(([k, v]) => `${k}=${v}`).join(" "))))))),
  ]);
}

// ---------------------------------------------------------------- internal metrics
async function renderInternal() {
  const m = await api("/api/internal/metrics?days=30");
  const sliceTable = (title, obj) => h("div", { class: "card" }, h("h2", {}, title), h("table", { class: "t" },
    h("thead", {}, h("tr", {}, ["Slice", "Docs", "Fields", "First-pass acc.", "Field review rate", "Spot-checked fields", "Escape rate"].map(x => h("th", { class: x === "Slice" ? "" : "num" }, x)))),
    h("tbody", {}, Object.entries(obj).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", { class: "num" }, v.documents), h("td", { class: "num" }, v.fields),
      h("td", { class: "num" }, v.first_pass_accuracy_pct ?? "—"), h("td", { class: "num" }, v.field_review_rate_pct ?? "—"),
      h("td", { class: "num" }, v.spot_check_fields), h("td", { class: "num" }, v.escape_rate_pct ?? "—"))))));
  const dr = m.demo_readiness;
  shell("internal", [
    h("h1", {}, "Internal metrics"), h("p", { class: "sub" }, `All tenants, aggregate only · last ${m.window_days} days · ${m.documents} documents`),
    h("div", { class: `banner ${dr.status === "ok" ? "info" : "bad"}`, style: { marginBottom: "16px" } },
      dr.status === "ok" ? `Demo gate from eval run ${when(dr.run_at)}: ${dr.cells.filter(c => c.demo_safe).map(c => c.cell).join(", ") || "no cell passed yet"}` : dr.message),
    h("div", { class: "grid g4", style: { marginBottom: "16px" } },
      kpi("Latency p50 / p95", m.latency_ms.p50 ? `${(m.latency_ms.p50 / 1000).toFixed(1)}s / ${(m.latency_ms.p95 / 1000).toFixed(1)}s` : "—", "Upload to result"),
      kpi("Model cost / doc", m.cost_usd_per_doc !== null ? `$${m.cost_usd_per_doc}` : "—", `${m.model_calls} calls · ${m.model_errors} errors`),
      kpi("Review p50 / p90", m.review.turnaround_p50_min !== null ? `${Math.round(m.review.turnaround_p50_min)} / ${Math.round(m.review.turnaround_p90_min)} min` : "—", m.review.on_time_pct !== null ? `${m.review.on_time_pct}% on time` : ""),
      kpi("Reviewer speed", m.review.seconds_per_field_p50 !== null ? `${m.review.seconds_per_field_p50}s` : "—", "median per field")),
    h("div", { class: "grid", style: { gap: "16px" } }, sliceTable("By document type", m.by_doc_type), sliceTable("By quality bucket", m.by_quality_bucket),
      sliceTable("By language", m.by_language), sliceTable("By handwriting", m.by_handwriting),
      h("div", { class: "card" }, h("h2", {}, "Calibration (reviewed fields)"), h("p", { class: "muted" }, "Error rate should fall as score rises. If not, re-run the backtest."),
        h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Score"), h("th", { class: "num" }, "Reviewed"), h("th", { class: "num" }, "Error rate %"))),
          h("tbody", {}, m.calibration.map(c => h("tr", {}, h("td", {}, c.score_bucket), h("td", { class: "num" }, c.reviewed), h("td", { class: "num" }, c.error_rate_pct ?? "—"))))))),
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
