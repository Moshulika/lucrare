(function () {
"use strict";

const __c = window.__charts || {};
const COLOR = __c.COLOR || { accent: "#6ee7ff", accent2: "#b48bff", good: "#6efbb1", warn: "#ffc857", bad: "#ff7a90", grid: "rgba(255,255,255,0.06)", text: "rgba(230,236,255,0.7)" };
const KIND_COLORS = __c.KIND_COLORS || {};
const gridScale = __c.gridScale || ((o = {}) => o);
const destroyIfExists = __c.destroyIfExists || (() => {});
const fmtTokens = __c.fmtTokens || ((n) => String(n || 0));
const fmtMs = __c.fmtMs || ((n) => (n ? Math.round(n) + "ms" : "—"));
const fmtCost = __c.fmtCost || ((n) => (n ? "$" + Number(n).toFixed(4) : "$0"));

const PAGE_SIZE_PROJECTS = 10;
const PAGE_SIZE_SESSIONS = 10;
const PAGE_SIZE_TOOLS = 10;
const PAGE_SIZE_TURNS = 20;

const state = {
    project: null,
    session: null,
    sessionData: null,   // raw session JSON (events etc.)
    metrics: null,       // computed metrics
    projects: [],
    sessions: [],
    projectPage: 1,
    sessionPage: 1,
    toolPage: 1,
    turnPage: 1,
    subagentPage: 1,
};
const PAGE_SIZE_SUBAGENTS = 10;

window.addEventListener("error", (ev) => {
    const msg = ev.error ? (ev.error.stack || ev.error.message) : ev.message;
    showFatal("JS error: " + msg);
});

function showFatal(msg) {
    const empty = document.getElementById("empty");
    if (empty) {
        empty.classList.remove("hidden");
        empty.innerHTML = `<p style="color:var(--bad);white-space:pre-wrap;text-align:left;font-family:'JetBrains Mono',monospace;font-size:12px">${msg.replace(/</g, "&lt;")}</p>`;
    }
    const sys = document.getElementById("sysinfo");
    if (sys) sys.innerHTML = '<span class="muted" style="color:var(--bad)">JS error — see panel</span>';
}

async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url} → ${r.status}`);
    return r.json();
}

function el(tag, attrs = {}, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (v == null) continue;
        if (k === "class") e.className = v;
        else if (k === "html") e.innerHTML = v;
        else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v);
    }
    for (const c of children.flat()) {
        if (c == null) continue;
        e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
}

function setActive(ul, id) {
    [...ul.children].forEach((c) => c.classList.toggle("active", c.dataset.id === id));
}

function shortPath(p) {
    if (!p) return "?";
    const parts = p.split("/").filter(Boolean);
    if (parts.length <= 2) return p;
    return ".../" + parts.slice(-2).join("/");
}

function shortToolName(n) {
    if (!n) return "?";
    if (n.length <= 28) return n;
    return n.slice(0, 12) + "…" + n.slice(-13);
}

function jsonPretty(v) {
    try { return JSON.stringify(v, null, 2); }
    catch (_) { return String(v); }
}

// ---------- Pagination ----------
function paginate(arr, page, size) {
    const total = arr.length;
    const pages = Math.max(1, Math.ceil(total / size));
    const p = Math.min(Math.max(1, page), pages);
    const start = (p - 1) * size;
    return { items: arr.slice(start, start + size), page: p, pages, total };
}

function renderPager(elId, currentPage, pages, onChange) {
    const wrap = document.getElementById(elId);
    if (!wrap) return;
    wrap.innerHTML = "";
    if (pages <= 1) return;
    const prev = el("button", { onclick: () => onChange(currentPage - 1) }, "‹");
    prev.disabled = currentPage <= 1;
    const next = el("button", { onclick: () => onChange(currentPage + 1) }, "›");
    next.disabled = currentPage >= pages;
    wrap.appendChild(prev);
    wrap.appendChild(el("span", { class: "pginfo" }, `${currentPage}/${pages}`));
    wrap.appendChild(next);
}

// ---------- Modal ----------
let _scrollLockY = 0;

function lockScroll() {
    if (document.body.classList.contains("modal-open")) return;
    _scrollLockY = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.style.top = `-${_scrollLockY}px`;
    document.documentElement.classList.add("modal-open");
    document.body.classList.add("modal-open");
}

function unlockScroll() {
    if (!document.body.classList.contains("modal-open")) return;
    document.documentElement.classList.remove("modal-open");
    document.body.classList.remove("modal-open");
    document.body.style.top = "";
    window.scrollTo(0, _scrollLockY);
}

function openModal(title, bodyNode) {
    const modal = document.getElementById("modal");
    document.getElementById("modal-title").textContent = title;
    const body = document.getElementById("modal-body");
    body.innerHTML = "";
    body.appendChild(bodyNode);
    modal.classList.remove("hidden");
    lockScroll();
}
function closeModal() {
    document.getElementById("modal").classList.add("hidden");
    unlockScroll();
}

document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("modal-close")?.addEventListener("click", closeModal);
    document.querySelector(".modal-backdrop")?.addEventListener("click", closeModal);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeModal();
    });
});

// ---------- Tracing button ----------
async function loadTracing() {
    try {
        const t = await getJSON("/api/tracing");
        const btn = document.getElementById("tracing-btn");
        if (!btn) return;
        if (t.enabled && t.url) {
            btn.href = t.url;
            btn.title = `${t.label || t.backend}: ${t.host || t.url}`;
            document.getElementById("tracing-label").textContent = t.label || t.backend;
            btn.classList.remove("hidden");
        } else {
            btn.classList.add("hidden");
        }
    } catch (e) {
        console.warn("tracing config unavailable:", e);
    }
}

// ---------- Logs panel ----------
let _logsLastSig = "";
let _logsRefreshTimer = null;
let _logsAutoscroll = true;

function setLogsStatus(cls, label) {
    const pill = document.getElementById("logs-status");
    if (!pill) return;
    pill.classList.remove("live", "refreshing", "unavailable");
    if (cls) pill.classList.add(cls);
    pill.textContent = label;
}

async function loadLogs() {
    setLogsStatus("refreshing", "syncing");
    try {
        const data = await getJSON("/api/logs/tail?n=1000");
        renderLogs(data);
        _logsLastSig = `${data.path}|${data.mtime}|${data.size}`;
    } catch (e) {
        console.warn("logs fetch failed:", e);
        setLogsStatus("unavailable", "error");
    }
}

function renderLogs(data) {
    const pre = document.getElementById("logs-pre");
    const name = document.getElementById("logs-name");
    const foot = document.getElementById("logs-foot");
    if (!pre) return;

    if (!data.available) {
        pre.textContent = "";
        name.textContent = "";
        foot.textContent = data.path ? `expected at ${data.path}` : "";
        setLogsStatus("unavailable", "no log file");
        return;
    }

    name.textContent = data.filename;

    const wasNearBottom = isScrolledToBottom(pre);
    pre.textContent = (data.lines || []).join("");
    if (_logsAutoscroll && wasNearBottom) {
        pre.scrollTop = pre.scrollHeight;
    }

    const sizeKb = (data.size / 1024).toFixed(1) + "k";
    const lineCount = (data.lines || []).length;
    foot.textContent = `${lineCount} lines · ${sizeKb} · ${data.path}`;

    setLogsStatus(data.is_live ? "live" : null, data.is_live ? "live" : "latest");
}

function isScrolledToBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 30;
}

function scheduleLogsRefresh() {
    if (_logsRefreshTimer) clearTimeout(_logsRefreshTimer);
    _logsRefreshTimer = setTimeout(loadLogs, 250);
}

document.addEventListener("DOMContentLoaded", () => {
    const auto = document.getElementById("logs-autoscroll");
    if (auto) auto.addEventListener("change", () => { _logsAutoscroll = auto.checked; });
    document.getElementById("logs-refresh")?.addEventListener("click", loadLogs);
});

// ---------- Sysinfo ----------
async function loadSysinfo() {
    const s = await getJSON("/api/sysinfo");
    const box = document.getElementById("sysinfo");
    box.innerHTML = "";
    const items = [
        ["host", `${s.system} ${s.release}`],
        ["arch", s.machine],
        ["cpu", `${s.cpu_logical || "?"}t / ${s.cpu_physical || "?"}c`],
        ["ram", s.memory_total ? (s.memory_total / 1e9).toFixed(0) + "G" : "?"],
        ["py", s.python],
        ["rev", s.vibe_cli_rev || "—"],
    ];
    for (const [k, v] of items) {
        box.appendChild(el("span", {}, el("span", { class: "k" }, k + " "), v));
    }
}

// ---------- Projects ----------
async function loadProjects() {
    state.projects = await getJSON("/api/projects");
    state.projectPage = 1;
    document.getElementById("projects-count").textContent = `(${state.projects.length})`;
    renderProjects();
}

function renderProjects() {
    const ul = document.getElementById("projects");
    ul.innerHTML = "";
    if (!state.projects.length) {
        ul.appendChild(el("li", {}, el("span", { class: "muted" }, "No sessions found")));
        return;
    }
    const { items, page, pages } = paginate(state.projects, state.projectPage, PAGE_SIZE_PROJECTS);
    for (const p of items) {
        const li = el(
            "li",
            { "data-id": p.key, onclick: () => selectProject(p.key) },
            el("div", { class: "row1" }, shortPath(p.cwd)),
            el("div", { class: "row2" }, `${p.session_count} sessions`),
        );
        if (state.project === p.key) li.classList.add("active");
        ul.appendChild(li);
    }
    renderPager("projects-pager", page, pages, (np) => { state.projectPage = np; renderProjects(); });
}

async function selectProject(key) {
    state.project = key;
    state.session = null;
    state.sessionData = null;
    state.metrics = null;
    setActive(document.getElementById("projects"), key);
    document.getElementById("content").classList.add("hidden");
    document.getElementById("empty").classList.remove("hidden");
    await loadSessions(key);
}

// ---------- Sessions ----------
async function loadSessions(projectKey) {
    state.sessions = await getJSON(`/api/projects/${encodeURIComponent(projectKey)}/sessions`);
    state.sessionPage = 1;
    document.getElementById("sessions-count").textContent = `(${state.sessions.length})`;
    renderSessions();
}

function renderSessions() {
    const ul = document.getElementById("sessions");
    ul.innerHTML = "";
    if (!state.sessions.length) {
        ul.appendChild(el("li", {}, el("span", { class: "muted" }, "Empty project")));
        renderPager("sessions-pager", 1, 1, () => {});
        return;
    }
    const { items, page, pages } = paginate(state.sessions, state.sessionPage, PAGE_SIZE_SESSIONS);
    for (const s of items) {
        const preview = s.first_human_preview || "(no human turn)";
        const meta = `${s.provider}/${s.model}  ·  ${s.event_count} ev  ·  ${fmtTokens(s.input_total + s.output_total)} tok`;
        const li = el(
            "li",
            { "data-id": s.id, onclick: () => selectSession(state.project, s.id) },
            el("div", { class: "row1" }, preview),
            el("div", { class: "row2" }, meta),
        );
        if (state.session === s.id) li.classList.add("active");
        ul.appendChild(li);
    }
    renderPager("sessions-pager", page, pages, (np) => { state.sessionPage = np; renderSessions(); });
}

async function selectSession(projectKey, sessionId) {
    state.session = sessionId;
    setActive(document.getElementById("sessions"), sessionId);
    const detail = await getJSON(`/api/sessions/${encodeURIComponent(projectKey)}/${sessionId}`);
    const compaction = await getJSON(`/api/sessions/${encodeURIComponent(projectKey)}/${sessionId}/compaction`);
    state.sessionData = detail.session;
    state.metrics = detail.metrics;
    state.toolPage = 1;
    state.turnPage = 1;
    state.subagentPage = 1;
    document.getElementById("empty").classList.add("hidden");
    document.getElementById("content").classList.remove("hidden");
    render(compaction);
}

// ---------- Render ----------
function render(compaction) {
    const m = state.metrics;
    renderHeader(m);
    renderShares(m.tokens.shares);
    renderByKind(m.tokens.by_kind);
    renderContextWindow(m.context_window);
    renderContext(m.context_curve, m.header.context_limit);
    renderTurns(m.turns);
    renderTools(m.tool_calls);
    renderToolLatency(m.tool_calls);
    renderInference(m.latency);
    renderCostPerCall(m.latency, m.tokens);
    renderToolSources(m.tool_calls.by_source);
    renderCostBlock(m.tokens, m.header);
    renderCompactionBlock(compaction, m.compaction);
    renderParams(m.header);
    renderToolsList();
    renderTurnsTable();
    renderSubagents(m.subagents);
}

function renderSubagents(sa) {
    const card = document.getElementById("subagents-card");
    const summary = document.getElementById("subagents-summary");
    const list = document.getElementById("subagents-list");
    const countEl = document.getElementById("subagents-count");
    if (!card || !summary || !list || !countEl) return;
    const errored = (sa && sa.errored_spawns) || 0;
    const attempts = (sa && sa.spawn_attempts) || 0;
    // Show the card if we have successful spawns OR errored attempts — the
    // errored-only case is a diagnostic state worth surfacing (otherwise
    // "missing card" looks like a UI bug).
    if (!sa || (!sa.count && !errored)) {
        card.style.display = "none";
        return;
    }
    card.style.display = "";

    if (sa.count) {
        countEl.textContent = `(${sa.count}${sa.timed_out ? `, ${sa.timed_out} timed out` : ""}${errored ? `, ${errored} errored` : ""}, ${fmtCost(sa.cost && sa.cost.total)})`;
    } else {
        countEl.textContent = `(0 successful · ${errored} errored of ${attempts})`;
    }

    // ---- summary: aggregated per-type rollup ----
    summary.innerHTML = "";

    // Diagnostic banner for errored spawns — visible whenever the gap is
    // non-zero, regardless of how many successful spawns there were.
    if (errored > 0) {
        const banner = el(
            "div",
            {
                style:
                    "padding:8px 10px;margin-bottom:8px;border-radius:6px;" +
                    "background:rgba(245, 158, 11, 0.12);" +
                    "border-left:3px solid var(--warn);" +
                    "color:var(--muted);font-size:12px;line-height:1.4",
            },
            el(
                "span",
                { style: "color:var(--warn);font-weight:600" },
                `⚠ ${errored} spawn_subagent call(s) errored before recording subagent activity`,
            ),
            el("br", {}),
            "The tool was invoked but its body didn't reach the result-stashing step — usually a tool-framework error (envelope shape, signature mismatch). Check `tool_result` events with name `spawn_subagent` for the raw error text.",
        );
        summary.appendChild(banner);
    }
    if ((sa.by_type || []).length) {
        // Header row — 6-cell layout, empty .idx so columns line up with body rows.
        summary.appendChild(el("div", { class: "subagent-row", style: "color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:0.12em" },
            el("span", { class: "idx" }, ""),
            el("span", { class: "type" }, "by type"),
            el("span", { class: "preview" }, "count · timed-out"),
            el("span", { class: "nums" }, "mean dur"),
            el("span", { class: "tokens" }, "input · output"),
            el("span", { class: "end" }, "cost"),
        ));
        (sa.by_type || []).forEach((row) => {
            summary.appendChild(el("div", { class: "subagent-row" },
                el("span", { class: "idx" }, ""),
                el("span", { class: "type" },
                    el("span", { class: "pill builtin" }, "agent"),
                    " " + row.type),
                el("span", { class: "preview" }, `${row.count}${row.timed_out ? ` · ${row.timed_out} TO` : ""}`),
                el("span", { class: "nums" }, row.mean_ms ? fmtMs(row.mean_ms) : "—"),
                el("span", { class: "tokens" }, `${row.input_total} · ${row.output_total}`),
                el("span", { class: "end" }, fmtCost(row.cost && row.cost.total)),
            ));
        });
    }

    renderSubagentInvocations(sa.invocations || []);
    renderSubagentsFooter(sa);
}

function renderSubagentsFooter(sa) {
    const footer = document.getElementById("subagents-footer");
    if (!footer) return;
    const processed = (sa && sa.subagent_processed_total) || 0;
    const returned = (sa && sa.returned_to_orchestrator_total) || 0;
    const saved = (sa && sa.tokens_saved) || 0;
    if (!sa || !sa.count || processed <= 0) {
        footer.style.display = "none";
        footer.innerHTML = "";
        return;
    }
    // floor (not round) so 99.94% never displays as "100%" when something
    // did return to the orchestrator — only show 100% when returned == 0.
    const pct = processed > 0 ? Math.floor((saved / processed) * 100) : 0;
    footer.style.display = "";
    footer.style.cssText =
        "display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;" +
        "margin-top:10px;padding-top:10px;border-top:1px solid var(--border);" +
        "font-size:12px;color:var(--muted)";
    footer.innerHTML = "";
    const cell = (label, val, valStyle) =>
        el("div", { style: "display:flex;flex-direction:column;gap:2px" },
            el("span", { style: "text-transform:uppercase;letter-spacing:0.1em;font-size:10px" }, label),
            el("span", { style: "font-size:14px;color:var(--fg);font-weight:600;" + (valStyle || "") }, val),
        );
    footer.appendChild(cell("Subagent context", fmtTokens(processed) + " tok"));
    footer.appendChild(cell("Returned to orchestrator", fmtTokens(returned) + " tok"));
    footer.appendChild(cell(
        "Tokens saved",
        fmtTokens(saved) + " tok · " + pct + "%",
        "color:var(--good)",
    ));
}

function renderSubagentInvocations(invocations) {
    const list = document.getElementById("subagents-list");
    if (!list) return;
    list.innerHTML = "";
    if (!invocations.length) {
        renderPager("subagents-pager", 1, 1, () => {});
        return;
    }
    // Header
    list.appendChild(el("div", { class: "subagent-row", style: "color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:0.12em" },
        el("span", { class: "idx" }, "#"),
        el("span", { class: "type" }, "type"),
        el("span", { class: "preview" }, "prompt preview"),
        el("span", { class: "nums" }, "iter · tools · dur"),
        el("span", { class: "tokens" }, "in · out tok"),
        el("span", { class: "end" }, ""),
    ));
    const { items, page, pages } = paginate(invocations, state.subagentPage, PAGE_SIZE_SUBAGENTS);
    items.forEach((inv, idx) => {
        const absIdx = (page - 1) * PAGE_SIZE_SUBAGENTS + idx + 1;
        const promptPreview = (inv.prompt || "").replace(/\s+/g, " ").slice(0, 110);
        const statusFlag = inv.timed_out ? "TO " : (inv.error ? "ERR " : "");
        list.appendChild(el("div", { class: "subagent-row clickable", onclick: () => openSubagentModal(inv) },
            el("span", { class: "idx" }, String(absIdx)),
            el("span", { class: "type" },
                el("span", { class: "pill builtin" }, "agent"),
                " " + inv.type),
            el("span", { class: "preview" }, promptPreview || "—"),
            el("span", { class: "nums" }, `${inv.iterations}i · ${inv.tool_call_count}t · ${inv.duration_ms ? fmtMs(inv.duration_ms) : "—"}`),
            el("span", { class: "tokens" }, `${statusFlag}${inv.tokens.input} · ${inv.tokens.output}`),
            el("span", { class: "end chev" }, "›"),
        ));
    });
    renderPager("subagents-pager", page, pages, (np) => { state.subagentPage = np; renderSubagentInvocations(invocations); });
}

function openSubagentModal(inv) {
    const body = el("div", {});
    const flags = [];
    if (inv.timed_out) flags.push("TIMED OUT");
    if (inv.error) flags.push(`ERROR: ${inv.error}`);
    body.appendChild(el("div", { class: "meta" },
        el("span", {}, "type: " + inv.type),
        el("span", {}, "depth: " + inv.depth),
        el("span", {}, "iterations: " + inv.iterations),
        el("span", {}, "tool calls: " + inv.tool_call_count),
        el("span", {}, "duration: " + (inv.duration_ms ? fmtMs(inv.duration_ms) : "—")),
        el("span", {}, "ts: " + (inv.ts || "—")),
        el("span", {}, "tool_call_id: " + (inv.tool_call_id || "—")),
        ...(flags.length ? [el("span", { style: "color:var(--warn);font-weight:600" }, flags.join(" · "))] : []),
    ));
    body.appendChild(el("section", {},
        el("h4", {}, "Prompt (orchestrator → subagent)"),
        el("pre", {}, inv.prompt || "(no prompt recorded)"),
    ));
    body.appendChild(el("section", {},
        el("h4", {}, "Final report (subagent → orchestrator)"),
        el("pre", {}, inv.output || "(no output)"),
    ));
    const t = inv.tokens || {};
    const c = inv.cost || {};
    body.appendChild(el("section", {},
        el("h4", {}, "Tokens · cost"),
        el("pre", {},
            `input        ${t.input || 0}\n` +
            `output       ${t.output || 0}\n` +
            `cache_read   ${t.cache_read || 0}\n` +
            `cache_write  ${t.cache_write || 0}\n` +
            `\n` +
            `cost (input + output, parent provider/model)\n` +
            `  total      ${fmtCost(c.total)}\n` +
            (c.free ? "  (free tier)\n" : "")
        ),
    ));
    openModal(`Subagent · ${inv.type}`, body);
}

function renderHeader(m) {
    const h = m.header;
    const t = m.tokens;
    const pricing = t.price_per_million;
    const grid = document.getElementById("header-grid");
    const stats = [
        ["session", h.short_id, "compact"],
        ["provider/model", `${h.provider} / ${h.model}`, "compact"],
        ["turns", h.turns],
        ["events", m.events_summary.total],
        ["input tokens", fmtTokens(t.input_total)],
        ["output tokens", fmtTokens(t.output_total)],
        ["cache reads", fmtTokens(t.cache_read_total)],
        ["peak ctx", fmtTokens(h.peak_size) + (h.context_limit ? ` / ${fmtTokens(h.context_limit)}` : "")],
        ["peak util", h.context_limit ? (h.peak_utilization * 100).toFixed(1) + "%" : "—"],
        ["duration", fmtMs(h.duration_seconds * 1000)],
        ["est. cost", fmtCost(t.cost.total) + (t.free ? "  free" : ""), t.free ? "free" : ""],
        ["$/1M", (pricing.input || pricing.output) ? `in ${pricing.input}  out ${pricing.output}` : "—", "compact"],
    ];
    grid.innerHTML = "";
    for (const [label, val, cls] of stats) {
        grid.appendChild(el("div", { class: "stat " + (cls || "") },
            el("div", { class: "label" }, label),
            el("div", { class: "val" }, String(val)),
        ));
    }
}

function renderShares(shares) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-shares");
    const labels = Object.keys(shares).filter((k) => shares[k] > 0);
    const data = labels.map((k) => shares[k] * 100);
    const colors = labels.map((k) => KIND_COLORS[k] || KIND_COLORS[k.replace("tools", "tool_call").replace("assistant", "assistant_text")] || COLOR.accent);
    new Chart(document.getElementById("chart-shares"), {
        type: "doughnut",
        data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: "rgba(255,255,255,0.08)", borderWidth: 1 }] },
        options: {
            maintainAspectRatio: false,
            cutout: "62%",
            plugins: {
                tooltip: { callbacks: { label: (c) => `${c.label}: ${c.parsed.toFixed(1)}%` } },
            },
        },
    });
}

function renderByKind(byKind) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-by-kind");
    const entries = Object.entries(byKind).sort((a, b) => b[1] - a[1]);
    new Chart(document.getElementById("chart-by-kind"), {
        type: "bar",
        data: {
            labels: entries.map((e) => e[0]),
            datasets: [{ data: entries.map((e) => e[1]), backgroundColor: entries.map((e) => KIND_COLORS[e[0]] || COLOR.accent), borderRadius: 4 }],
        },
        options: {
            maintainAspectRatio: false,
            indexAxis: "y",
            plugins: { legend: { display: false } },
            scales: {
                x: gridScale({ ticks: { color: COLOR.text, callback: (v) => fmtTokens(v) } }),
                y: gridScale({ grid: { display: false } }),
            },
        },
    });
}

function renderContextWindow(cw) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-context-window");
    const legend = document.getElementById("cw-legend");
    const summary = document.getElementById("cw-summary");
    if (!legend || !summary) return;
    legend.innerHTML = "";

    // Truly unknown limit (no session-recorded value AND model not in the
    // catalog) — show a placeholder rather than an empty pie.
    if (!cw || !cw.limit) {
        summary.textContent = "context limit unknown for this model";
        const canvas = document.getElementById("chart-context-window");
        if (canvas) {
            const ctx = canvas.getContext("2d");
            ctx && ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
        return;
    }

    // Friendly category → palette mapping. Reuses KIND_COLORS for visual
    // consistency with the shares doughnut so users learn one palette.
    const CAT_COLOR = {
        "System prompt": KIND_COLORS.system_prompt,
        "User messages": KIND_COLORS.human,
        "Assistant responses": KIND_COLORS.assistant_text,
        "Thinking": KIND_COLORS.assistant_thinking,
        "Tool I/O": KIND_COLORS.tool_call,
        "Subagents": "#7e9bff",
        "Compaction summaries": KIND_COLORS.compaction,
    };
    const OUTPUT_COLOR = "rgba(180, 139, 255, 0.45)";
    const COMPACTION_BUFFER_COLOR = "rgba(255, 200, 87, 0.55)";
    const FREE_COLOR = "rgba(255, 255, 255, 0.08)";

    const labels = [];
    const data = [];
    const colors = [];

    Object.entries(cw.categories || {}).forEach(([label, tok]) => {
        labels.push(label);
        data.push(tok);
        colors.push(CAT_COLOR[label] || COLOR.accent);
    });

    const outputLabel = "Output reservation";
    if (cw.max_output > 0) {
        labels.push(outputLabel);
        data.push(cw.max_output);
        colors.push(OUTPUT_COLOR);
    }
    const bufferLabel = `Auto-compact buffer (>${Math.round((cw.threshold || 0) * 100)}%)`;
    if (cw.compaction_buffer > 0) {
        labels.push(bufferLabel);
        data.push(cw.compaction_buffer);
        colors.push(COMPACTION_BUFFER_COLOR);
    }
    if (cw.free > 0) {
        labels.push("Free");
        data.push(cw.free);
        colors.push(FREE_COLOR);
    }

    const usedPct = cw.limit ? ((cw.used_total / cw.limit) * 100).toFixed(1) : "0";
    const limitHint = cw.limit_source === "catalog" ? " (catalog)" : "";
    summary.textContent = `${fmtTokens(cw.used_total)} / ${fmtTokens(cw.limit)} used (${usedPct}%)${limitHint}`;

    new Chart(document.getElementById("chart-context-window"), {
        type: "doughnut",
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: colors,
                borderColor: "rgba(255,255,255,0.08)",
                borderWidth: 1,
            }],
        },
        options: {
            maintainAspectRatio: false,
            cutout: "58%",
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (c) => {
                            const pct = cw.limit ? ((c.parsed / cw.limit) * 100).toFixed(1) : "0";
                            return `${c.label}: ${fmtTokens(c.parsed)} (${pct}%)`;
                        },
                    },
                },
            },
        },
    });

    // Custom legend (right side of the pie) with tokens + % of total limit.
    // First reservation slice (output / buffer / free) gets a divider so the
    // eye can separate "used" rows from "reserved/free" rows.
    const firstReservationIdx = labels.findIndex(
        (l) => l === outputLabel || l === bufferLabel || l === "Free",
    );
    labels.forEach((label, i) => {
        const tok = data[i];
        const pct = cw.limit ? ((tok / cw.limit) * 100).toFixed(1) : "0";
        legend.appendChild(el(
            "li",
            i === firstReservationIdx ? { class: "cw-divider" } : {},
            el("span", { class: "sw", style: `background:${colors[i]}` }),
            el("span", { class: "lbl" }, label),
            el("span", { class: "tok" }, fmtTokens(tok)),
            el("span", { class: "pct" }, pct + "%"),
        ));
    });
}

function renderContext(curve, limit) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-context");
    const labels = curve.map((_, i) => i);
    const data = curve.map((p) => p.context_size_after);
    const compactionIdxs = curve.map((p, i) => (p.kind === "compaction" ? i : null)).filter((x) => x != null);

    const datasets = [{
        label: "context size",
        data, borderColor: COLOR.accent, backgroundColor: "rgba(110, 231, 255, 0.10)",
        fill: true, stepped: true, pointRadius: 0, tension: 0,
    }];
    if (limit) {
        datasets.push({
            label: `limit (${fmtTokens(limit)})`,
            data: data.map(() => limit), borderColor: COLOR.bad, borderDash: [4, 4], pointRadius: 0, fill: false,
        });
    }
    if (compactionIdxs.length) {
        datasets.push({
            type: "bar", label: "compaction",
            data: labels.map((i) => (compactionIdxs.includes(i) ? Math.max(...data) : null)),
            backgroundColor: "rgba(255, 122, 144, 0.45)", barThickness: 2,
        });
    }

    new Chart(document.getElementById("chart-context"), {
        type: "line",
        data: { labels, datasets },
        options: {
            maintainAspectRatio: false,
            interaction: { mode: "nearest", intersect: false },
            scales: {
                x: gridScale({ title: { display: true, text: "event #", color: COLOR.text } }),
                y: gridScale({ ticks: { color: COLOR.text, callback: (v) => fmtTokens(v) } }),
            },
            plugins: {
                tooltip: {
                    callbacks: {
                        title: (items) => {
                            const idx = items[0].dataIndex;
                            return `event ${idx} · ${curve[idx].kind}`;
                        },
                        label: (item) => fmtTokens(item.parsed.y) + " tokens",
                    },
                },
            },
        },
    });
}

function renderTurns(turns) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-turns");
    const labels = turns.map((_, i) => `T${i + 1}`);
    const kinds = ["human", "assistant_thinking", "assistant_text", "tool_call", "tool_result"];
    const datasets = kinds.map((k) => ({
        label: k,
        data: turns.map((t) => t.tokens_by_kind[k] || 0),
        backgroundColor: KIND_COLORS[k],
        borderRadius: 3,
    }));
    new Chart(document.getElementById("chart-turns"), {
        type: "bar",
        data: { labels, datasets },
        options: {
            maintainAspectRatio: false,
            scales: {
                x: gridScale({ stacked: true }),
                y: gridScale({ stacked: true, ticks: { callback: (v) => fmtTokens(v) } }),
            },
        },
    });
}

function renderTools(tc) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-tools");
    if (!tc.by_name.length) return;
    new Chart(document.getElementById("chart-tools"), {
        type: "bar",
        data: {
            labels: tc.by_name.map((t) => shortToolName(t.name)),
            datasets: [{ data: tc.by_name.map((t) => t.count), backgroundColor: COLOR.good, borderRadius: 3 }],
        },
        options: {
            maintainAspectRatio: false,
            indexAxis: "y",
            plugins: { legend: { display: false } },
            scales: { x: gridScale({ ticks: { precision: 0 } }), y: gridScale({ grid: { display: false } }) },
        },
    });
}

function renderToolLatency(tc) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-tool-latency");
    if (!tc.by_name.length || tc.by_name.every((t) => !t.p50_ms)) return;
    new Chart(document.getElementById("chart-tool-latency"), {
        type: "bar",
        data: {
            labels: tc.by_name.map((t) => shortToolName(t.name)),
            datasets: [
                { label: "p50", data: tc.by_name.map((t) => t.p50_ms), backgroundColor: COLOR.accent, borderRadius: 3 },
                { label: "p95", data: tc.by_name.map((t) => t.p95_ms), backgroundColor: COLOR.accent2, borderRadius: 3 },
            ],
        },
        options: {
            maintainAspectRatio: false,
            indexAxis: "y",
            scales: {
                x: gridScale({ ticks: { callback: (v) => fmtMs(v) } }),
                y: gridScale({ grid: { display: false } }),
            },
        },
    });
}

function renderInference(lat) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-inference");
    const samples = lat.samples;
    if (!samples.length) return;
    const labels = samples.map((_, i) => i + 1);
    const dur = samples.map((s) => s.duration_ms || 0);
    const tps = samples.map((s) => s.tokens_per_sec || 0);
    new Chart(document.getElementById("chart-inference"), {
        type: "bar",
        data: {
            labels,
            datasets: [
                { label: "duration (ms)", type: "bar", data: dur, backgroundColor: COLOR.accent2, yAxisID: "y", borderRadius: 3 },
                { label: "tokens/sec", type: "line", data: tps, borderColor: COLOR.good, backgroundColor: "rgba(110, 251, 177, 0.15)", yAxisID: "y2", pointRadius: 2, tension: 0.25 },
            ],
        },
        options: {
            maintainAspectRatio: false,
            scales: {
                x: gridScale({ title: { display: true, text: "LLM call #", color: COLOR.text } }),
                y: gridScale({ position: "left", ticks: { callback: (v) => fmtMs(v) } }),
                y2: gridScale({ position: "right", grid: { display: false }, ticks: { callback: (v) => v.toFixed(0) + " tps" } }),
            },
        },
    });
}

function renderCostPerCall(latency, tokens) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-cost");
    const samples = latency.samples;
    if (!samples.length) return;
    const pricing = tokens.price_per_million;
    const free = tokens.free;
    const inCost = samples.map((s) => (s.input_tokens * pricing.input) / 1e6);
    const outCost = samples.map((s) => (s.output_tokens * pricing.output) / 1e6);
    const labels = samples.map((_, i) => i + 1);
    new Chart(document.getElementById("chart-cost"), {
        type: "bar",
        data: {
            labels,
            datasets: [
                { label: "input $", data: inCost, backgroundColor: COLOR.accent, stack: "c", borderRadius: 3 },
                { label: "output $", data: outCost, backgroundColor: COLOR.accent2, stack: "c", borderRadius: 3 },
            ],
        },
        options: {
            maintainAspectRatio: false,
            plugins: {
                title: free ? { display: true, text: "Free / open-source — $0", color: COLOR.good, font: { size: 12 } } : { display: false },
                tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtCost(c.parsed.y)}` } },
            },
            scales: {
                x: gridScale({ stacked: true, title: { display: true, text: "LLM call #", color: COLOR.text } }),
                y: gridScale({ stacked: true, ticks: { callback: (v) => fmtCost(v) } }),
            },
        },
    });
}

function renderToolSources(bySource) {
    if (typeof Chart === "undefined") return;
    destroyIfExists("chart-tool-sources");
    const entries = Object.entries(bySource || {});
    const wrap = document.getElementById("chart-tool-sources").parentElement;
    // Clean any prior empty-state message
    wrap.querySelectorAll("p.muted").forEach((p) => p.remove());
    if (!entries.length) {
        wrap.appendChild(el("p", { class: "muted", style: "text-align:center;margin-top:12px" }, "No tool calls in this session."));
        return;
    }
    const palette = [COLOR.good, COLOR.accent, COLOR.accent2, COLOR.warn, COLOR.bad];
    new Chart(document.getElementById("chart-tool-sources"), {
        type: "doughnut",
        data: {
            labels: entries.map((e) => e[0]),
            datasets: [{
                data: entries.map((e) => e[1]),
                backgroundColor: entries.map((_, i) => palette[i % palette.length]),
                borderColor: "rgba(255,255,255,0.08)", borderWidth: 1,
            }],
        },
        options: { maintainAspectRatio: false, cutout: "62%" },
    });
}

function renderCostBlock(tokens, header) {
    const block = document.getElementById("cost-block");
    const cost = tokens.cost;
    block.innerHTML = "";
    const pillRow = el("div", { style: "margin-bottom:10px" });
    if (tokens.free) pillRow.appendChild(el("span", { class: "pill free" }, "free / open-source"));
    pillRow.appendChild(el("span", { class: "pill" }, `${header.provider}/${header.model}`));
    block.appendChild(pillRow);
    const tbl = el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "bucket"), el("th", {}, "tokens"), el("th", {}, "cost"))),
        el("tbody", {},
            row("input", tokens.input_total, cost.input),
            row("output", tokens.output_total, cost.output),
            row("cache read", tokens.cache_read_total, cost.cache_read),
            row("total", tokens.input_total + tokens.output_total + tokens.cache_read_total, cost.total, true),
        ),
    );
    block.appendChild(tbl);
    function row(label, n, c, bold) {
        return el("tr", {},
            el("td", { style: bold ? "font-weight:600" : "" }, label),
            el("td", {}, fmtTokens(n)),
            el("td", { style: bold ? "font-weight:600" : "" }, fmtCost(c)),
        );
    }
}

function renderCompactionBlock(payload, summary) {
    const block = document.getElementById("compaction-block");
    block.innerHTML = "";
    if (!summary.any_compaction && !payload.snapshots.length) {
        block.appendChild(el("p", { class: "muted" }, "No compaction occurred in this session."));
        return;
    }
    if (summary.threshold_passes.length) {
        block.appendChild(el("div", {}, el("strong", {}, `${summary.threshold_passes.length} threshold pass(es)`)));
        for (const p of summary.threshold_passes) {
            block.appendChild(el("div", { class: "muted" },
                `${fmtTokens(p.before)} → ${fmtTokens(p.after)} (-${fmtTokens(p.freed)}, ${p.removed_events} events)`));
        }
    }
    if (Object.keys(summary.eager_stages).length) {
        const stages = Object.entries(summary.eager_stages).map(
            ([name, info]) => `${name}: ${info.count}× saved ${fmtTokens(Math.round((info.original_bytes_total || 0) / 4))}`,
        );
        block.appendChild(el("div", { style: "margin-top:8px" }, el("strong", {}, "Eager stages")));
        block.appendChild(el("div", { class: "muted" }, stages.join(" · ")));
    }
    if (payload.diffs.length) {
        block.appendChild(el("div", { style: "margin-top:8px" }, el("strong", {}, "Pre-compaction backups")));
        const tbl = el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, "file"), el("th", {}, "events Δ"), el("th", {}, "input Δ"))),
            el("tbody", {}, ...payload.diffs.map((d) => el("tr", {},
                el("td", {}, d.filename),
                el("td", {}, `${d.events_then} → ${d.events_now} (-${d.events_removed})`),
                el("td", {}, `${fmtTokens(d.input_then)} → ${fmtTokens(d.input_now)}`),
            ))),
        );
        block.appendChild(tbl);
    }
}

function renderParams(header) {
    const block = document.getElementById("params-block");
    block.innerHTML = "";
    const params = header.model_params || {};
    if (!Object.keys(params).length) {
        block.appendChild(el("p", { class: "muted" },
            "No model_params recorded — this session pre-dates the schema extension. New sessions will populate temperature, effort, mode, and connected MCP servers."));
        return;
    }
    const tbl = el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "param"), el("th", {}, "value"))),
        el("tbody", {}, ...Object.entries(params).map(([k, v]) => el("tr", {},
            el("td", {}, k),
            el("td", {}, Array.isArray(v) ? v.join(", ") : String(v)),
        ))),
    );
    block.appendChild(tbl);
}

// ---------- Tools used (clickable list) ----------
function collectToolEvents() {
    const events = (state.sessionData?.events) || [];
    const calls = [];
    const resultsByCallId = {};
    for (const e of events) {
        if (e.kind === "tool_result") {
            const id = (e.meta || {}).tool_call_id;
            if (id) resultsByCallId[id] = e;
        }
    }
    for (const e of events) {
        if (e.kind === "tool_call") {
            const meta = e.meta || {};
            const id = meta.tool_call_id || "";
            const result = id ? resultsByCallId[id] : null;
            const ts1 = Date.parse(e.ts || "");
            const ts2 = result ? Date.parse(result.ts || "") : 0;
            const dur = (ts1 && ts2) ? Math.max(0, ts2 - ts1) : null;
            const name = meta.name || "?";
            const source = meta.source || (name.includes("__") ? "mcp:" + name.split("__", 1)[0] : "builtin");
            const resultMeta = (result && result.meta) || {};
            calls.push({
                call: e,
                result,
                name,
                source,
                duration_ms: dur,
                resultBytes: result ? jsonPretty(result.content).length : 0,
                argsPreview: (typeof e.content === "string") ? e.content.slice(0, 80) : jsonPretty(e.content).slice(0, 80),
                preconditionDenied: !!resultMeta.precondition_denied,
                guard: resultMeta.guard || null,
                denialReason: resultMeta.denial_reason || null,
            });
        }
    }
    return calls;
}

function renderToolsList() {
    const all = collectToolEvents();
    document.getElementById("tools-count").textContent = `(${all.length})`;
    const block = document.getElementById("tools-list");
    block.innerHTML = "";
    if (!all.length) {
        block.appendChild(el("p", { class: "muted" }, "No tool calls in this session."));
        renderPager("tools-pager", 1, 1, () => {});
        return;
    }
    // Header row
    block.appendChild(el("div", { class: "tool-row", style: "color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:0.12em;cursor:default" },
        el("span", { class: "idx" }, "#"),
        el("span", { class: "name" }, "tool / source"),
        el("span", { class: "preview" }, "args preview"),
        el("span", { class: "dur" }, "duration"),
        el("span", { class: "size" }, "result"),
        el("span", { class: "chev" }, ""),
    ));
    const { items, page, pages } = paginate(all, state.toolPage, PAGE_SIZE_TOOLS);
    items.forEach((tc, idx) => {
        const absIdx = (page - 1) * PAGE_SIZE_TOOLS + idx + 1;
        const sourceCls = tc.source === "builtin" ? "pill builtin" : "pill mcp";
        // Build the name cell — source pill, tool name, plus a "rejected"
        // pill + (i) tooltip when a tool_guard precondition denied the call.
        const nameChildren = [
            el("span", { class: sourceCls }, tc.source),
            " " + tc.name,
        ];
        if (tc.preconditionDenied) {
            nameChildren.push(" ");
            nameChildren.push(el(
                "span",
                { class: "pill bad", title: `Guard: ${tc.guard || "?"}` },
                "rejected",
            ));
            nameChildren.push(" ");
            nameChildren.push(el(
                "span",
                {
                    class: "info-dot",
                    title: tc.denialReason || "Tool precondition denied.",
                },
                "ⓘ",
            ));
        }
        block.appendChild(el("div", { class: "tool-row", onclick: () => openToolModal(tc) },
            el("span", { class: "idx" }, String(absIdx)),
            el("span", { class: "name" }, ...nameChildren),
            el("span", { class: "preview" }, tc.argsPreview),
            el("span", { class: "dur" }, tc.duration_ms != null ? fmtMs(tc.duration_ms) : "—"),
            el("span", { class: "size" }, tc.resultBytes ? (tc.resultBytes > 1024 ? (tc.resultBytes / 1024).toFixed(1) + "k" : tc.resultBytes + "b") : "—"),
            el("span", { class: "chev" }, "›"),
        ));
    });
    renderPager("tools-pager", page, pages, (np) => { state.toolPage = np; renderToolsList(); });
}

function openToolModal(tc) {
    const body = el("div", {});
    body.appendChild(el("div", { class: "meta" },
        el("span", {}, "name: " + tc.name),
        el("span", {}, "source: " + tc.source),
        el("span", {}, "duration: " + (tc.duration_ms != null ? fmtMs(tc.duration_ms) : "—")),
        el("span", {}, "ts: " + (tc.call.ts || "")),
        el("span", {}, "tool_call_id: " + ((tc.call.meta || {}).tool_call_id || "—")),
        ...(tc.preconditionDenied ? [
            el("span", { style: "color:var(--bad);font-weight:600" },
                `rejected by guard: ${tc.guard || "?"}`),
        ] : []),
    ));
    if (tc.preconditionDenied) {
        body.appendChild(el("section", {},
            el("h4", {}, "Guard denial"),
            el("p", { class: "muted", style: "margin:4px 0" },
                "This call was short-circuited by a deterministic precondition. " +
                "The error string below was returned to the LLM as the tool result so it can self-correct."),
            el("pre", {}, tc.denialReason || "(no reason recorded)"),
        ));
    }
    body.appendChild(el("section", {},
        el("h4", {}, "Input args"),
        el("pre", {}, jsonPretty(tc.call.content)),
    ));
    body.appendChild(el("section", {},
        el("h4", {}, "Tool result"),
        el("pre", {}, tc.result ? (typeof tc.result.content === "string" ? tc.result.content : jsonPretty(tc.result.content)) : "(no result recorded)"),
    ));
    openModal(`Tool call · ${tc.name}`, body);
}

// ---------- Turn anatomy (paginated, clickable) ----------
function renderTurnsTable() {
    const turns = state.metrics.turns || [];
    document.getElementById("turns-count").textContent = `(${turns.length})`;
    const wrap = document.getElementById("turns-table");
    wrap.innerHTML = "";
    if (!turns.length) {
        wrap.appendChild(el("p", { class: "muted" }, "No turns yet."));
        renderPager("turns-pager", 1, 1, () => {});
        return;
    }
    const { items, page, pages } = paginate(turns, state.turnPage, PAGE_SIZE_TURNS);
    const tbl = el("table", {},
        el("thead", {}, el("tr", {},
            el("th", {}, "#"), el("th", {}, "preview"), el("th", {}, "tools"),
            el("th", {}, "tokens"), el("th", {}, "duration"))),
        el("tbody", {}, ...items.map((t, idx) => {
            const absIdx = (page - 1) * PAGE_SIZE_TURNS + idx + 1;
            const totalTok = Object.values(t.tokens_by_kind || {}).reduce((a, b) => a + b, 0);
            return el("tr", { class: "clickable", onclick: () => openTurnModal(t, absIdx) },
                el("td", {}, String(absIdx)),
                el("td", {}, t.human_preview || "—"),
                el("td", {}, String(t.tool_calls)),
                el("td", {}, fmtTokens(totalTok)),
                el("td", {}, fmtMs(t.duration_seconds * 1000)));
        })),
    );
    wrap.appendChild(tbl);
    renderPager("turns-pager", page, pages, (np) => { state.turnPage = np; renderTurnsTable(); });
}

function openTurnModal(turn, idx) {
    const events = ((state.sessionData?.events) || []).filter((e) => e.turn_id === turn.turn_id);
    const body = el("div", {});
    body.appendChild(el("div", { class: "meta" },
        el("span", {}, `turn #${idx}`),
        el("span", {}, "events: " + events.length),
        el("span", {}, "tool calls: " + (turn.tool_calls || 0)),
        el("span", {}, "duration: " + fmtMs(turn.duration_seconds * 1000)),
        el("span", {}, "first ts: " + (turn.first_ts || "")),
    ));

    for (const e of events) {
        const sec = el("section", {});
        const tok = e.tokens || {};
        const dur = e.duration_ms;
        const ttft = e.time_to_first_token_ms;
        const headBits = [
            e.kind,
            e.ts ? e.ts.split("T")[1].replace("Z", "") : "",
            tok.input ? `in ${fmtTokens(tok.input)}` : null,
            tok.output ? `out ${fmtTokens(tok.output)}` : null,
            dur ? `dur ${fmtMs(dur)}` : null,
            ttft ? `ttft ${fmtMs(ttft)}` : null,
            (e.meta || {}).name ? `tool ${(e.meta || {}).name}` : null,
        ].filter(Boolean).join(" · ");
        sec.appendChild(el("h4", {}, headBits));
        const content = e.content;
        const text = (typeof content === "string") ? content : jsonPretty(content);
        sec.appendChild(el("pre", {}, text || "(empty)"));
        body.appendChild(sec);
    }

    openModal(`Turn ${idx}`, body);
}

// ---------- Live updates (SSE) ----------
let _eventSource = null;
let _refreshTimer = null;

function setLiveStatus(cls, label, title) {
    const pill = document.getElementById("live");
    if (!pill) return;
    pill.classList.remove("connected", "refreshing", "disconnected");
    if (cls) pill.classList.add(cls);
    pill.querySelector(".live-label").textContent = label;
    if (title) pill.title = title;
}

async function refreshAll(reason) {
    try {
        setLiveStatus("refreshing", "syncing", "Refreshing — " + (reason || "change detected"));
        // Always refresh projects list (counts may shift, new project may appear).
        await loadProjects();
        // If we have a project selected, refresh its sessions.
        if (state.project) {
            await loadSessions(state.project);
        }
        // If a session is open and still exists, reload its detail.
        if (state.project && state.session) {
            const stillThere = state.sessions.some((s) => s.id === state.session);
            if (stillThere) {
                const detail = await getJSON(`/api/sessions/${encodeURIComponent(state.project)}/${state.session}`);
                const compaction = await getJSON(`/api/sessions/${encodeURIComponent(state.project)}/${state.session}/compaction`);
                state.sessionData = detail.session;
                state.metrics = detail.metrics;
                render(compaction);
            } else {
                // Session was deleted/gone — reset.
                state.session = null;
                state.sessionData = null;
                state.metrics = null;
                document.getElementById("content").classList.add("hidden");
                document.getElementById("empty").classList.remove("hidden");
            }
        }
        setLiveStatus("connected", "live", "Live updates: connected");
    } catch (e) {
        console.error("refresh failed:", e);
        setLiveStatus("connected", "live", "Live updates: connected (last refresh failed)");
    }
}

function scheduleRefresh(reason) {
    if (_refreshTimer) clearTimeout(_refreshTimer);
    _refreshTimer = setTimeout(() => refreshAll(reason), 250);
}

function connectSSE() {
    if (typeof EventSource === "undefined") {
        setLiveStatus("disconnected", "no sse", "Browser doesn't support EventSource");
        return;
    }
    if (_eventSource) {
        try { _eventSource.close(); } catch (_) {}
    }
    setLiveStatus(null, "…", "Live updates: connecting…");
    const es = new EventSource("/api/events");
    _eventSource = es;

    es.addEventListener("hello", () => {
        setLiveStatus("connected", "live", "Live updates: connected");
    });
    es.addEventListener("change", (ev) => {
        let data = {};
        try { data = JSON.parse(ev.data || "{}"); } catch (_) {}
        const n = (data.changes || []).length;
        scheduleRefresh(n ? `${n} file(s) changed` : "change");
    });
    es.addEventListener("logs_change", () => {
        scheduleLogsRefresh();
    });
    es.addEventListener("open", () => {
        setLiveStatus("connected", "live", "Live updates: connected");
    });
    es.addEventListener("error", () => {
        // EventSource auto-reconnects; reflect status meanwhile.
        if (es.readyState === EventSource.CLOSED) {
            setLiveStatus("disconnected", "offline", "Live updates: disconnected — retrying…");
            // Manually retry after a delay (browsers also retry, this is belt-and-suspenders).
            setTimeout(connectSSE, 3000);
        } else {
            setLiveStatus("disconnected", "retrying", "Live updates: reconnecting…");
        }
    });
}

// ---------- Boot ----------
(async function main() {
    try {
        await Promise.all([loadSysinfo(), loadProjects(), loadTracing(), loadLogs()]);
        connectSSE();
    } catch (e) {
        console.error(e);
        showFatal("Error: " + e.message);
    }
})();

})();  // close outer IIFE
