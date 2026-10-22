"use strict";
// Environment portal. No framework and no raw-HTML insertion: all text goes through text nodes, so values
// typed by users (names, parameters) can never become markup.

const state = {
  token: sessionStorage.getItem("envp_token") || "", me: null, templates: [], limits: {},
  envs: [], pending: [], tab: "envs", selected: null, detail: null, logs: [], preview: null,
};
let pollTimer = null, previewTimer = null;

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v; // CSSOM is allowed under our strict CSP; a style="" attribute is not
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
const $ = (sel, root) => (root || document).querySelector(sel);
const money = (n) => "$" + Number(n).toFixed(n < 1 ? 4 : 2);

async function api(path, opts) {
  opts = opts || {};
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { Authorization: "Bearer " + state.token, "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) { const e = new Error(data.error || res.statusText); e.status = res.status; e.details = data.details; throw e; }
  return data;
}

function pillClass(status) {
  if (status === "ACTIVE" || status === "DESTROYED") return status === "ACTIVE" ? "ok" : "idle";
  if (status === "PROVISIONING" || status === "QUEUED" || status === "DESTROYING" || status === "DESTROY_QUEUED") return "info";
  if (status === "PENDING_APPROVAL") return "warn";
  if (status.endsWith("FAILED") || status === "REJECTED") return "bad";
  return "idle";
}
const pill = (s) => h("span", { class: "pill " + pillClass(s) }, s.replaceAll("_", " ").toLowerCase());

function timeLeft(epoch) {
  const s = Math.floor(epoch - Date.now() / 1000);
  if (s <= 0) return "expired";
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh}h ${String(mm).padStart(2, "0")}m` : `${mm}m ${String(ss).padStart(2, "0")}s`;
}
function expiryCell(env) {
  if (env.status === "ACTIVE" && env.expires_at_epoch) return h("span", { "data-expires": env.expires_at_epoch, class: "mono" }, timeLeft(env.expires_at_epoch));
  return h("span", { class: "hint" }, env.status === "PROVISIONING" || env.status === "QUEUED" ? "starts when ready" : "-");
}

// ---- login --------------------------------------------------------------------------------
function renderLogin(msg) {
  const input = h("input", { type: "password", placeholder: "paste your token", autocomplete: "off" });
  const go = async () => {
    state.token = input.value.trim();
    try { state.me = await api("/api/me"); sessionStorage.setItem("envp_token", state.token); await boot(); }
    catch (e) { renderLogin(e.status === 401 ? "That token was not accepted." : e.message); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  $("#app").replaceChildren(h("div", { class: "login card" },
    h("h2", null, "Environment Portal"),
    h("p", { class: "hint" }, "Sign in with the token printed by ./scripts/up.sh."),
    h("label", null, "Token"), input,
    msg && h("div", { class: "notice bad" }, msg),
    h("p", null, h("button", { class: "btn", onclick: go }, "Sign in"))));
  input.focus();
}

// ---- shell --------------------------------------------------------------------------------
function renderShell() {
  const isApprover = state.me.role === "approver" || state.me.role === "admin";
  const tabs = [["envs", "Environments"], ["new", "New environment"]];
  if (isApprover) tabs.push(["approvals", "Approvals"]);
  if (state.me.role === "admin") tabs.push(["admin", "Admin"]);
  const nav = h("nav", { role: "tablist" }, tabs.map(([id, label]) =>
    h("button", { role: "tab", "aria-selected": String(state.tab === id), onclick: () => { state.tab = id; renderShell(); renderTab(); } },
      label, id === "approvals" && state.pending.length ? h("span", { class: "badge" }, state.pending.length) : null)));
  $("#app").replaceChildren(h("div", { class: "wrap" },
    h("header", null, h("h1", null, "Environment Portal"),
      h("div", { class: "who" }, `${state.me.name} · team ${state.me.team} · ${state.me.role} `,
        h("button", { class: "btn secondary", onclick: logout }, "Sign out"))),
    nav, h("div", { id: "tab" })));
}
function logout() { sessionStorage.removeItem("envp_token"); clearInterval(pollTimer); state.token = ""; renderLogin(); }
function renderTab() {
  const t = $("#tab");
  if (!t) return;
  ({ envs: renderEnvs, new: renderNew, approvals: renderApprovals, admin: renderAdmin })[state.tab](t);
}

// ---- environments list + detail -------------------------------------------------------------
function renderEnvs(root) {
  root = root || $("#tab");
  const rows = state.envs.map((e) => h("tr", { class: "click" + (state.selected === e.id ? " sel" : ""), onclick: () => select(e.id) },
    h("td", null, h("strong", null, e.name), h("div", { class: "hint" }, `${e.template} · ${e.owner}`)),
    h("td", null, pill(e.status)), h("td", null, expiryCell(e)), h("td", { class: "mono" }, money(e.cost_usd))));
  const list = state.envs.length
    ? h("div", { class: "table-scroll" }, h("table", null, h("thead", null, h("tr", null, ["Environment", "Status", "Time left", "Cost"].map((x) => h("th", null, x)))), h("tbody", null, rows)))
    : h("div", { class: "empty" }, "No environments yet. Create one from the New environment tab.");
  root.replaceChildren(h("div", { class: "grid two" }, h("div", { class: "card" }, h("h2", null, "Environments"), list), h("div", { class: "card", id: "detail" })));
  renderDetail();
}
async function select(id) { state.selected = id; await refreshDetail(); renderEnvs(); }
async function refreshDetail() {
  if (!state.selected) return;
  try {
    state.detail = await api("/api/envs/" + state.selected);
    state.logs = (await api(`/api/envs/${state.selected}/logs`)).lines;
  } catch (e) { state.detail = null; }
}
function renderDetail() {
  const box = $("#detail");
  if (!box) return;
  const e = state.detail;
  if (!e || e.id !== state.selected) { box.replaceChildren(h("div", { class: "empty" }, "Select an environment to see details, events and logs.")); return; }
  const mine = e.owner === state.me.name || state.me.role === "admin";
  const act = (label, cls, fn, disabled) => h("button", { class: "btn " + cls, disabled: !!disabled, onclick: fn }, label);
  const actions = [];
  if (mine && e.status === "ACTIVE") {
    actions.push(act("+30 min", "secondary", () => doExtend(e.id, 30)), act("+60 min", "secondary", () => doExtend(e.id, 60)));
  }
  if (mine && ["ACTIVE", "QUEUED", "PENDING_APPROVAL", "DESTROY_FAILED", "PROVISION_FAILED"].includes(e.status)) {
    actions.push(act(e.status === "ACTIVE" ? "Destroy now" : "Cancel / destroy", "danger", () => doDestroy(e)));
  }
  const outs = Object.entries(e.outputs || {});
  box.replaceChildren(
    h("h2", null, e.name, " ", pill(e.status)),
    h("dl", { class: "kv" },
      h("dt", null, "ID"), h("dd", { class: "mono" }, e.id),
      h("dt", null, "Template"), h("dd", null, e.template),
      h("dt", null, "Owner"), h("dd", null, `${e.owner} (${e.team})`),
      h("dt", null, "Parameters"), h("dd", { class: "mono" }, JSON.stringify(e.params)),
      h("dt", null, "Lifetime"), h("dd", null, `${e.ttl_minutes} min requested, extended ${e.extensions}×`),
      h("dt", null, "Cost"), h("dd", null, `${money(e.cost_usd)} (${money(e.hourly_usd)}/h)`),
      e.approved_by && [h("dt", null, "Approved by"), h("dd", null, e.approved_by)],
      e.reason && [h("dt", null, "Note"), h("dd", null, e.reason)]),
    e.approval_reasons.length ? h("div", { class: "notice warn" }, "Approval reason: " + e.approval_reasons.join("; ")) : null,
    outs.length ? h("div", null, h("h2", { style: "margin-top:14px" }, "Outputs"), h("dl", { class: "kv" }, outs.map(([k, v]) => [h("dt", null, k), h("dd", { class: "mono" }, typeof v === "string" ? v : JSON.stringify(v))]))) : null,
    h("div", { class: "row-actions", style: "margin-top:12px" }, actions),
    h("h2", { style: "margin-top:16px" }, "Events"),
    h("ul", { class: "timeline" }, (e.events || []).slice().reverse().map((ev) => h("li", null, h("time", { class: "mono" }, ev.ts.slice(11, 19)), h("strong", null, ev.action.replaceAll("_", " ")), ev.detail ? " · " + ev.detail : "", h("span", { class: "hint" }, " · " + ev.actor)))),
    h("h2", { style: "margin-top:16px" }, "Logs"),
    h("pre", { class: "logs" }, state.logs.length ? state.logs.join("\n") : "(no logs yet)"));
}
async function doDestroy(e) {
  if (!confirm(`Destroy ${e.name}? This deletes its resources.`)) return;
  try { await api(`/api/envs/${e.id}/destroy`, { method: "POST" }); } catch (err) { alert(err.message); }
  await poll();
}
async function doExtend(id, minutes) {
  try { await api(`/api/envs/${id}/extend`, { method: "POST", body: { minutes } }); } catch (err) { alert(err.message); }
  await poll();
}

// ---- new environment ------------------------------------------------------------------------
function renderNew(root) {
  root = root || $("#tab");
  if (!state.templates.length) { root.replaceChildren(h("div", { class: "empty" }, "No templates available.")); return; }
  const tmplSel = h("select", { id: "f-template", onchange: () => { buildParams(); schedulePreview(); } },
    state.templates.map((t) => h("option", { value: t.name }, t.title)));
  const name = h("input", { type: "text", id: "f-name", placeholder: "e.g. checkout-fix", maxlength: "20", autocomplete: "off", oninput: schedulePreview });
  const ttl = h("input", { type: "number", id: "f-ttl", min: "1", oninput: schedulePreview });
  const submit = h("button", { class: "btn", id: "f-submit", disabled: true, onclick: submitNew }, "Request environment");
  root.replaceChildren(h("div", { class: "grid two" },
    h("form", { class: "card", onsubmit: (ev) => ev.preventDefault() },
      h("h2", null, "New environment"),
      h("label", null, "Template"), tmplSel, h("div", { class: "hint", id: "f-desc" }),
      h("label", null, "Name ", h("small", null, "(lowercase letters, digits, '-')")), name, h("div", { class: "field-err", id: "e-name" }),
      h("div", { id: "f-params" }),
      h("label", null, "Lifetime in minutes ", h("small", { id: "f-ttl-hint" })), ttl, h("div", { class: "field-err", id: "e-ttl_minutes" }),
      h("p", null, submit)),
    h("div", { class: "card", id: "f-preview" }, h("h2", null, "Preview"), h("div", { class: "hint" }, "Fill in the form to see the cost and whether approval is needed."))));
  buildParams();
  schedulePreview();
}
const curTemplate = () => state.templates.find((t) => t.name === $("#f-template").value);
function buildParams() {
  const t = curTemplate();
  $("#f-desc").textContent = t.description;
  $("#f-ttl").value = t.ttl.default; $("#f-ttl").min = t.ttl.min; $("#f-ttl").max = t.ttl.max;
  $("#f-ttl-hint").textContent = `(${t.ttl.min}-${t.ttl.max})`;
  $("#f-params").replaceChildren(...t.params.map((p) => {
    const id = "p-" + p.name;
    let input;
    if (p.type === "boolean") input = h("input", { type: "checkbox", id, checked: p.default === true, onchange: schedulePreview });
    else if (p.type === "enum") input = h("select", { id, onchange: schedulePreview }, p.values.map((v) => h("option", { value: String(v), selected: v === p.default }, String(v))));
    else if (p.type === "integer") input = h("input", { type: "number", id, value: p.default ?? "", min: p.min, max: p.max, oninput: schedulePreview });
    else input = h("input", { type: "text", id, value: p.default ?? "", maxlength: p.max_length || 64, oninput: schedulePreview });
    const lab = p.type === "boolean"
      ? h("label", { class: "check", for: id }, input, p.name.replaceAll("_", " "), h("small", null, " " + p.description))
      : [h("label", { for: id }, p.name.replaceAll("_", " "), " ", h("small", null, p.description)), input];
    return h("div", null, lab, h("div", { class: "field-err", id: "e-params." + p.name }));
  }));
}
function formBody() {
  const t = curTemplate(), params = {};
  for (const p of t.params) {
    const el = $("#p-" + p.name);
    if (p.type === "boolean") params[p.name] = el.checked;
    else if (p.type === "integer") params[p.name] = el.value === "" ? null : Number(el.value);
    else if (p.type === "enum") params[p.name] = p.values[el.selectedIndex];
    else params[p.name] = el.value;
  }
  const ttl = $("#f-ttl").value;
  return { name: $("#f-name").value, template: t.name, params, ttl_minutes: ttl === "" ? null : Number(ttl) };
}
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(runPreview, 250); }
async function runPreview() {
  if (state.tab !== "new" || !$("#f-template")) return;
  let d;
  try { d = await api("/api/estimate", { method: "POST", body: formBody() }); } catch (e) { return; }
  document.querySelectorAll(".field-err").forEach((n) => (n.textContent = ""));
  const general = [];
  for (const [k, v] of Object.entries(d.errors)) {
    const slot = document.getElementById("e-" + k);
    if (slot) slot.textContent = v; else general.push(v);
  }
  const nameEmpty = !$("#f-name").value;
  if (nameEmpty) { const s = document.getElementById("e-name"); if (s) s.textContent = ""; }
  const box = $("#f-preview"), t = curTemplate();
  const spent = d.budget && d.budget.limit_usd ? Math.min(100, (d.budget.used_usd + (d.cost_usd || 0)) / d.budget.limit_usd * 100) : 0;
  box.replaceChildren(h("h2", null, "Preview"),
    d.cost_usd !== undefined && d.ttl_minutes ? h("dl", { class: "kv" },
      h("dt", null, "Estimate"), h("dd", null, h("strong", null, money(d.cost_usd)), ` (${money(d.hourly_usd)}/h × ${d.ttl_minutes} min)`),
      h("dt", null, "Destroyed"), h("dd", null, `automatically ${d.ttl_minutes} min after it is ready`),
      d.budget && d.budget.limit_usd ? [h("dt", null, "Team budget"), h("dd", null, `${money(d.budget.used_usd)} used of ${money(d.budget.limit_usd)} this month`,
        h("div", { class: "bar" + (spent >= 100 ? " over" : "") }, h("i", { style: `width:${spent}%` })))] : null) : null,
    d.needs_approval ? h("div", { class: "notice warn" }, h("strong", null, "Needs approval. "), d.approval_reasons.join("; ")) : (d.ok ? h("div", { class: "notice ok" }, "Starts immediately, no approval needed.") : null),
    general.map((m) => h("div", { class: "notice bad" }, m)));
  const btn = $("#f-submit");
  btn.disabled = !d.ok || nameEmpty;
  btn.textContent = d.needs_approval ? "Request approval" : "Create environment";
  state.preview = d;
}
async function submitNew() {
  const btn = $("#f-submit"); btn.disabled = true;
  try {
    const env = await api("/api/envs", { method: "POST", body: formBody() });
    state.tab = "envs"; state.selected = env.id;
    await poll(); renderShell(); renderTab();
  } catch (e) {
    const msg = e.details ? Object.values(e.details).join(" ") : e.message;
    $("#f-preview").append(h("div", { class: "notice bad" }, msg)); btn.disabled = false;
  }
}

// ---- approvals + admin --------------------------------------------------------------------------
function renderApprovals(root) {
  root = root || $("#tab");
  const cards = state.pending.map((e) => {
    const own = e.owner === state.me.name;
    return h("div", { class: "card", style: "margin-bottom:12px" },
      h("h2", null, e.name, " ", pill(e.status)),
      h("dl", { class: "kv" }, h("dt", null, "Requested by"), h("dd", null, `${e.owner} (${e.team})`),
        h("dt", null, "Template"), h("dd", null, e.template), h("dt", null, "Parameters"), h("dd", { class: "mono" }, JSON.stringify(e.params)),
        h("dt", null, "Lifetime"), h("dd", null, e.ttl_minutes + " min"), h("dt", null, "Estimate"), h("dd", null, money(e.cost_usd))),
      h("div", { class: "notice warn" }, e.approval_reasons.join("; ")),
      own ? h("div", { class: "hint" }, "This is your own request; another approver must decide.") :
        h("div", { class: "row-actions" },
          h("button", { class: "btn", onclick: () => decide(e.id, "approve") }, "Approve"),
          h("button", { class: "btn danger", onclick: () => { const r = prompt("Reason for rejecting?") ; if (r !== null) decide(e.id, "reject", r); } }, "Reject")));
  });
  root.replaceChildren(...(cards.length ? cards : [h("div", { class: "empty" }, "Nothing is waiting for approval.")]));
}
async function decide(id, verb, reason) {
  try { await api(`/api/envs/${id}/${verb}`, { method: "POST", body: { reason } }); } catch (e) { alert(e.message); }
  await poll(); renderShell(); renderTab();
}
function renderAdmin(root) {
  root = root || $("#tab");
  const out = h("div", null), aws = h("input", { type: "checkbox", id: "scan-aws" });
  const scan = async () => {
    out.replaceChildren(h("div", { class: "hint" }, "Scanning..."));
    try {
      const d = await api("/api/orphans" + (aws.checked ? "?aws=1" : ""));
      out.replaceChildren(d.orphans.length ? h("table", null, h("thead", null, h("tr", null, ["Kind", "Environment", "Why", "Resource"].map((x) => h("th", null, x)))),
        h("tbody", null, d.orphans.map((o) => h("tr", null, h("td", null, o.kind), h("td", { class: "mono" }, o.env_id || "-"), h("td", null, o.why), h("td", { class: "mono" }, o.arn || (o.resources + " in state")))))) : h("div", { class: "notice ok" }, "No orphaned resources found."));
    } catch (e) { out.replaceChildren(h("div", { class: "notice bad" }, e.message)); }
  };
  root.replaceChildren(h("div", { class: "card" }, h("h2", null, "Orphan scan"),
    h("p", { class: "hint" }, "Finds resources that exist but whose environment is gone: they cost money and nobody owns them."),
    h("label", { class: "check", for: "scan-aws" }, aws, "Also scan AWS by tag (needs AWS credentials on the portal host)"),
    h("p", null, h("button", { class: "btn", onclick: scan }, "Run scan")), out));
}

// ---- polling and boot -----------------------------------------------------------------------------
async function poll() {
  try {
    const all = await api("/api/envs");
    state.envs = all.environments.filter((e) => !["REJECTED", "CANCELLED"].includes(e.status) || Date.now() / 1000 - (e.created_at_epoch || 0) < 3600);
    if (state.me.role !== "developer") {
      state.pending = (await api("/api/envs?status=PENDING_APPROVAL")).environments;
    }
    await refreshDetail();
  } catch (e) { if (e.status === 401) return logout(); }
  if (!$("#tab")) return; // first poll runs before the shell exists
  if (state.tab === "envs") renderEnvs();
  else if (state.tab === "approvals") renderApprovals();
  const badge = document.querySelector(".badge");
  if (state.me.role !== "developer" && (badge ? Number(badge.textContent) : 0) !== state.pending.length) renderShell(), renderTab();
}
setInterval(() => document.querySelectorAll("[data-expires]").forEach((n) => (n.textContent = timeLeft(Number(n.dataset.expires)))), 1000);

async function boot() {
  const t = await api("/api/templates");
  state.templates = t.templates; state.limits = t.limits;
  state.tab = "envs";
  await poll();
  renderShell(); renderTab();
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 4000);
}
(async () => {
  if (!state.token) return renderLogin();
  try { state.me = await api("/api/me"); await boot(); } catch (e) { renderLogin(); }
})();
