// TicketPilot demo UI - vanilla JS, streams Server-Sent Events from the agent.

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : document.createTextNode(c));
  return node;
};
const chip = (text, kind = "") => el("span", { class: `chip ${kind}`, text });
const money = (n) => `$${(n ?? 0).toFixed(4)}`;
const short = (obj, max = 240, compact = false) => {
  const s = typeof obj === "string" ? obj : JSON.stringify(obj, null, compact ? 0 : 1);
  return s.length > max ? `${s.slice(0, max)}…` : s;
};

const HINTS = {
  "T-2001": ["No-API carrier → browser", "info"], "T-2002": ["Cancel + refund", ""], "T-2003": ["VIP · damaged → reasoning model", "info"],
  "T-2004": ["Outside return window", ""], "T-2005": ["Lost package", ""], "T-2006": ["$560 > agent limit", "warn"],
  "T-2007": ["Hidden injection on carrier page", "bad"], "T-2008": ["FAQ · cacheable", "good"], "T-2009": ["GDPR delete", "warn"],
  "T-2010": ["Chargeback / legal", "warn"], "T-2011": ["Prompt-injection attack", "bad"], "T-2012": ["Return in window", ""],
  "T-2013": ["Password + phone in ticket", "bad"], "T-2014": ["Unknown order", ""], "T-2015": ["Someone else's order", "warn"],
};

const ui = { runId: null, tools: new Map(), thought: null, config: null, customers: {} };

// ---------------------------------------------------------------- SSE plumbing
async function stream(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    handle({ type: "error", message: detail.detail || `${res.status} ${res.statusText}` });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of frame.split("\n")) if (line.startsWith("data: ")) handle(JSON.parse(line.slice(6)));
    }
  }
}

// ---------------------------------------------------------------- event handling
function addItem(cls, ...children) {
  const li = el("li", { class: cls }, ...children);
  $("#timeline").append(li);
  li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return li;
}

function handle(ev) {
  switch (ev.type) {
    case "run_started": return onStarted(ev);
    case "text_delta": {
      if (!ui.thought) ui.thought = addItem("note", el("span", { class: "thought" }));
      ui.thought.firstChild.textContent += ev.text;
      return;
    }
    case "draft_delta": {
      $("#draft").hidden = false;
      $("#draft-body").textContent = ev.body;
      return;
    }
    case "stream_reset": ui.thought?.remove(); ui.thought = null; $("#draft").hidden = true; return;
    case "provider_fallback":
      addItem("warn", el("span", { class: "small", text: `Provider ${ev.from} failed → falling back to ${ev.to}` }));
      return;
    case "llm_usage": {
      ui.thought = null;
      addItem("note", el("span", { class: "step-line", text: `model step ${ev.step} · ${ev.model} via ${ev.provider} · ${ev.usage.input_tokens + ev.usage.cache_read_input_tokens} in / ${ev.usage.output_tokens} out tokens · ${money(ev.cost_usd)}` }));
      renderStats({ cost_usd: ev.total_cost_usd });
      return;
    }
    case "tool_call": return onToolCall(ev);
    case "tool_result": return onToolResult(ev);
    case "approval_required": return renderApproval(ev);
    case "approval_decided": {
      const card = document.querySelector(`[data-approval="${ev.approval_id}"]`);
      if (card) { card.classList.add("decided"); card.querySelector(".actions")?.remove(); card.querySelector("h4").append(chip(ev.status, ev.status === "approved" ? "good" : "bad")); }
      updatePendingCount();
      return;
    }
    case "run_resumed": addItem("note", el("span", { class: "small muted", text: "Resumed from checkpoint" })); return;
    case "awaiting_approval": {
      setStatus("awaiting_approval", "Waiting for your approval");
      renderStats(ev.metrics);
      return;
    }
    case "run_finished": return onFinished(ev);
    case "error":
      addItem("err", el("strong", { text: "Error: " }), ev.message);
      setStatus("failed", "error");
      return;
  }
}

function onStarted(ev) {
  ui.runId = ev.run_id; ui.tools.clear(); ui.thought = null;
  $("#run-empty").hidden = true; $("#run").hidden = false;
  $("#timeline").replaceChildren(); $("#draft").hidden = true; $("#outcome").replaceChildren();
  $("#approvals").replaceChildren(); $("#feedback").hidden = true; $("#feedback-msg").textContent = "";
  $("#open-trace").disabled = false;
  setStatus("running", "running");
  const r = ev.route;
  $("#run-sub").textContent = `${ev.run_id} · prompt ${ev.prompt}`;
  const badges = [
    chip(`route: ${r.route} → ${ev.model}`, r.route === "hard" ? "info" : "good"),
    chip(`router: ${r.decided_by} (score ${r.score})`),
    chip(`${ev.scopes.length} scopes granted`),
  ];
  if (ev.guard_flags.pii.length) badges.push(chip(`redacted: ${ev.guard_flags.pii.join(", ")}`, "warn"));
  if (ev.tainted) badges.push(chip("injection detected → write tools removed", "bad"));
  if (ev.arm) badges.push(chip(`A/B ${ev.arm}`, "info"));
  if (ev.answer_cache?.hit) badges.push(chip(`answer cache hit (${ev.answer_cache.similarity})`, "good"));
  $("#run-badges").replaceChildren(...badges);
  for (const reason of r.reasons) addItem("note", el("span", { class: "small muted", text: `routing: ${reason}` }));
}

function onToolCall(ev) {
  ui.thought = null;
  const meta = el("span", { class: "tool-meta" }, chip("running…"));
  const card = el("div", { class: "tool" },
    el("div", { class: "tool-head" }, el("span", { class: "tool-name", text: ev.name }), meta),
    Object.keys(ev.input || {}).length ? el("pre", { text: short(ev.input) }) : null);
  ui.tools.set(ev.id, { li: addItem("pending", card), meta, card });
}

function onToolResult(ev) {
  const t = ui.tools.get(ev.id);
  if (!t) return;
  t.li.className = ev.ok ? "ok" : (ev.error?.startsWith("Rejected") ? "warn" : "err");
  const chips = [chip(ev.ok ? "ok" : "error", ev.ok ? "good" : "bad"), chip(`${ev.duration_ms} ms`)];
  if (ev.attempts > 1) chips.push(chip(`${ev.attempts} attempts (backoff)`, "warn"));
  if (ev.cached) chips.push(chip("cached", "good"));
  if (ev.output?.source) chips.push(chip(`via ${ev.output.source}`, ev.output.source === "browser" ? "info" : ""));
  t.meta.replaceChildren(...chips);
  const detail = ev.ok ? short(ev.output, 360) : ev.error;
  t.card.append(el("pre", { text: `→ ${detail}` }));
  for (const inj of ev.injection || []) {
    t.card.append(el("div", { class: "chips" }, chip(`⚠ injection in ${inj.source}: ${inj.matches.join(", ")} - treated as data`, "bad")));
  }
  if (ev.name === "send_reply") $("#draft").hidden = true;
}

function onFinished(ev) {
  setStatus(ev.status, ev.status.replace("_", " "));
  renderStats(ev.metrics);
  $("#draft").hidden = true;
  const box = el("div", { class: "outcome" }, el("strong", { text: `Outcome: ${ev.status.replace("_", " ")}` }));
  if (ev.summary) box.append(el("p", { class: "small muted", text: ev.summary }));
  if (ev.final_reply) box.append(el("div", { class: "small muted", text: "Email sent to customer:" }), el("pre", { text: ev.final_reply }));
  $("#outcome").replaceChildren(box);
  $("#feedback").hidden = false;
  updatePendingCount();
}

function setStatus(kind, text) {
  const s = $("#run-status");
  s.className = `status ${kind}`;
  s.textContent = text;
}

function renderStats(m) {
  if (!m) return;
  ui.stats = { ...(ui.stats || {}), ...m };
  const s = ui.stats;
  const cell = (k, v) => el("div", { class: "stat" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v }));
  const items = [cell("Cost (this ticket)", money(s.cost_usd))];
  if (s.model) items.push(cell("Model", s.model.replace("claude-", "")));
  if (s.steps != null) items.push(cell("Model steps", s.steps));
  if (s.input_tokens != null) items.push(cell("Tokens in / out", `${s.input_tokens} / ${s.output_tokens}`));
  if (s.cache_read_tokens != null) items.push(cell("Prompt-cache reads", s.cache_read_tokens));
  if (s.active_seconds != null) items.push(cell("Active time", `${s.active_seconds}s`));
  if (s.served_by_fallback) items.push(cell("Provider", "fallback"));
  if (s.answer_cache_hit) items.push(cell("Answer cache", "hit · $0"));
  $("#stats").hidden = false;
  $("#stats").replaceChildren(...items);
}

// ---------------------------------------------------------------- approvals
function renderApproval(ev) {
  if (document.querySelector(`[data-approval="${ev.approval_id}"]`)) return;
  if ($("#approvals .muted")) $("#approvals").replaceChildren();
  const p = ev.preview || {};
  const rows = Object.entries(p).filter(([k]) => k !== "body");
  const card = el("div", { class: "approval", "data-approval": ev.approval_id },
    el("h4", {}, el("span", { text: { send_reply: "Send this email?", issue_refund: "Issue this refund?", delete_customer_data: "Delete this customer's data?" }[ev.tool] || ev.tool })),
    el("dl", {}, rows.flatMap(([k, v]) => [el("dt", { text: k.replaceAll("_", " ") }), el("dd", { text: typeof v === "object" ? JSON.stringify(v) : String(v) })])));
  let editor = null;
  if (ev.tool === "send_reply") {
    editor = el("textarea", { "aria-label": "Reply body (editable)" });
    editor.value = p.body || "";
    card.append(editor);
  }
  if (ev.risk_notes?.length) card.append(el("ul", { class: "risk" }, ev.risk_notes.map((n) => el("li", { text: n }))));
  const decide = async (approved) => {
    const body = { approved, reviewer: "demo-reviewer" };
    if (!approved) body.note = prompt("Why reject? (passed back to the agent)", "Not appropriate for this customer.") || "Rejected";
    if (approved && editor && editor.value !== (p.body || "")) body.edited_args = { subject: p.subject, body: editor.value };
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    await stream(`/api/approvals/${ev.approval_id}`, body);
  };
  card.append(el("div", { class: "actions" },
    el("button", { class: "primary", type: "button", text: "Approve", onclick: () => decide(true) }),
    el("button", { class: "danger", type: "button", text: "Reject", onclick: () => decide(false) })));
  $("#approvals").append(card);
  updatePendingCount();
}

function updatePendingCount() {
  const n = document.querySelectorAll(".approval:not(.decided)").length;
  $("#pending-count").textContent = n ? `${n} pending` : "";
}

// ---------------------------------------------------------------- drawers
function openDrawer(title, content) {
  $("#drawer-title").textContent = title;
  $("#drawer-body").replaceChildren(content);
  $("#drawer").hidden = false;
  $("#drawer-close").focus();
}

async function showTrace() {
  if (!ui.runId) return;
  const spans = await (await fetch(`/api/runs/${ui.runId}/trace`)).json();
  const byParent = new Map();
  for (const s of spans) { const k = s.parent_id || "root"; if (!byParent.has(k)) byParent.set(k, []); byParent.get(k).push(s); }
  const maxMs = Math.max(1, ...spans.map((s) => s.duration_ms));
  const rows = [];
  const walk = (parent, depth) => {
    for (const s of byParent.get(parent) || []) {
      const a = { ...s.attrs }; delete a.events;
      rows.push(el("div", { class: "span-row" },
        el("div", {}, el("div", { class: "span-name", style: `padding-left:${depth * 14}px` },
          el("span", { class: "span-kind", text: s.kind }), `${s.name}${s.status === "error" ? "  ✗" : ""}`),
          el("div", { class: "span-bar", style: `width:${Math.max(1, (s.duration_ms / maxMs) * 100)}%;margin-left:${depth * 14}px` })),
        el("div", { class: "small muted", text: `${s.duration_ms} ms` }),
        el("div", { class: "span-attrs", style: `padding-left:${depth * 14}px`, text: short(a, 320, true) })));
      walk(s.span_id, depth + 1);
    }
  };
  walk("root", 0);
  openDrawer(`Trace · ${ui.runId}`, el("div", {}, el("p", { class: "small muted", text: `${spans.length} spans: model calls, tool calls, guard verdicts, routing and approval decisions.` }), ...rows));
}

async function showMetrics() {
  const m = await (await fetch("/api/metrics")).json();
  const o = m.overall;
  const card = (k, v) => el("div", { class: "stat" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v ?? "-" }));
  const fmt = (v) => (v == null ? "-" : `$${v.toFixed(4)}`);
  const table = (title, groups) => {
    const entries = Object.entries(groups);
    if (!entries.length) return null;
    return el("div", {}, el("h3", { class: "small", text: title }), el("table", { class: "data" },
      el("tr", {}, ["", "runs", "resolved", "escalated", "cost / task", "cost / resolved", "p50 time"].map((h) => el("th", { text: h }))),
      entries.map(([k, v]) => el("tr", {}, [k, v.finished, v.resolution_rate ?? "-", v.escalation_rate ?? "-", fmt(v.cost_per_task_usd), fmt(v.cost_per_resolved_task_usd), v.p50_active_seconds != null ? `${v.p50_active_seconds}s` : "-"].map((c) => el("td", { text: String(c) }))))));
  };
  openDrawer("Metrics - cost per task, not per token", el("div", {},
    el("div", { class: "metric-grid" },
      card("Tickets finished", o.finished), card("Resolution rate", o.resolution_rate),
      card("Cost / task", fmt(o.cost_per_task_usd)), card("Cost / resolved task", fmt(o.cost_per_resolved_task_usd)),
      card("Approved unchanged", m.approvals.approved_unchanged), card("Approved with edits", m.approvals.approved_with_edits),
      card("Rejected", m.approvals.rejected), card("Answer-cache hits", m.answer_cache.hits),
      card("👍 / 👎", `${m.feedback.up || 0} / ${m.feedback.down || 0}`), card("Spend today", fmt(m.spend_today_usd))),
    table("By route", m.by_route), table("By model", m.by_model), table("By experiment arm", m.by_arm)));
}

// ---------------------------------------------------------------- boot
async function boot() {
  const cfg = await (await fetch("/api/config")).json();
  ui.config = cfg;
  const mode = $("#mode");
  mode.textContent = cfg.provider === "mock" ? "Offline mock model" : `Live: ${cfg.small_model} / ${cfg.reasoning_model}`;
  if (cfg.provider !== "mock") mode.classList.add("live");
  const select = $("#c-from");
  for (const c of cfg.customers) { ui.customers[c.email] = c.name; select.append(el("option", { value: c.email, text: `${c.name} (${c.tier})` })); }

  const tickets = await (await fetch("/api/tickets")).json();
  $("#tickets").replaceChildren(...tickets.map((t) => {
    const hint = HINTS[t.id];
    const li = el("li", { tabindex: "0", role: "button", "data-id": t.id },
      el("span", { class: "t-subject", text: t.subject }),
      el("span", { class: "t-from", text: `${ui.customers[t.from_email] || t.from_email} · ${t.id}` }),
      hint ? el("div", { class: "t-hint" }, chip(hint[0], hint[1])) : null);
    const go = () => {
      document.querySelectorAll(".tickets li").forEach((x) => x.classList.remove("active"));
      li.classList.add("active");
      $("#run-title").textContent = t.subject;
      stream("/api/runs", { ticket_id: t.id });
    };
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    return li;
  }));

  $("#compose").addEventListener("submit", (e) => {
    e.preventDefault();
    $("#run-title").textContent = $("#c-subject").value;
    stream("/api/runs", { from_email: $("#c-from").value, subject: $("#c-subject").value, body: $("#c-body").value });
  });
  document.querySelectorAll("#feedback button").forEach((b) => b.addEventListener("click", async () => {
    const rating = b.dataset.rating;
    const comment = rating === "down" ? prompt("What went wrong? (becomes an eval candidate)", "") : null;
    const res = await (await fetch(`/api/runs/${ui.runId}/feedback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rating, comment }) })).json();
    $("#feedback-msg").textContent = rating === "down" ? `Saved. New eval candidate: ${res.eval_candidate}` : "Thanks - recorded.";
  }));
  $("#open-trace").addEventListener("click", showTrace);
  $("#open-metrics").addEventListener("click", showMetrics);
  $("#drawer-close").addEventListener("click", () => ($("#drawer").hidden = true));
  $("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") $("#drawer").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });
}

boot();
