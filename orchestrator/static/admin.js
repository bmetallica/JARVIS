"use strict";

const $ = (id) => document.getElementById(id);
let RES = { tools: [], mcps: [] };
let GROUPS = [];

async function api(method, path, body) {
    const opt = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const err = new Error(d.detail || r.statusText); err.status = r.status; throw err;
    }
    return r.status === 204 ? null : r.json();
}

function show(view) {
    for (const v of ["login", "change", "admin"]) $("view-" + v).classList.toggle("hidden", v !== view);
}

// ── Auth-Flow ─────────────────────────────────────────────────────────────────
async function init() {
    try {
        const me = await api("GET", "/api/admin/me");
        $("who").textContent = me.username + (me.is_admin ? " (Admin)" : "");
        $("btn-logout").style.display = "";
        if (me.must_change) { show("change"); return; }
        if (!me.is_admin) { show("login"); $("login-err").textContent = "Kein Admin-Zugang."; return; }
        show("admin"); await loadAdmin();
    } catch (e) {
        show("login");
    }
}

$("btn-login").onclick = async () => {
    $("login-err").textContent = "";
    try {
        await api("POST", "/api/admin/login", { username: $("login-user").value, password: $("login-pass").value });
        await init();
    } catch (e) { $("login-err").textContent = e.message; }
};
$("login-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-login").click(); });

$("btn-change").onclick = async () => {
    $("chg-err").textContent = "";
    const p1 = $("chg-pass").value, p2 = $("chg-pass2").value;
    if (p1 !== p2) { $("chg-err").textContent = "Passwörter stimmen nicht überein."; return; }
    if (p1.length < 4) { $("chg-err").textContent = "Mindestens 4 Zeichen."; return; }
    try { await api("POST", "/api/admin/change-password", { new_password: p1 }); await init(); }
    catch (e) { $("chg-err").textContent = e.message; }
};

$("btn-logout").onclick = async () => { await api("POST", "/api/admin/logout"); location.reload(); };

// ── Tab-Navigation ────────────────────────────────────────────────────────────
document.querySelectorAll(".navbtn").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".navbtn").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.dataset.tab !== b.dataset.tab));
});

// ── Verwaltung laden/rendern ────────────────────────────────────────────────
const CFG_KEYS = ["llm_url", "llm_model", "vision_model", "llm_max_tokens", "llm_timeout", "llm_frequency_penalty",
    "thinking_mode", "thinking_budget", "stt_url", "stt_model",
    "stt_language", "tts_engine", "tts_voice_edge", "tts_voice_piper", "tts_voice_kokoro", "tts_url",
    "voice_id_threshold", "system_prompt", "sandbox_url", "sandbox_timeout_s"];
const CFG_BOOLS = ["sandbox_enabled", "sandbox_allow_network", "fetch_allow_lan", "llm_cache_prompt"];

async function loadAdmin() {
    RES = await api("GET", "/api/admin/resources");
    GROUPS = (await api("GET", "/api/admin/groups")).groups;
    renderGroups();
    await loadUsers();
    await loadConfig();
    await loadMcp();
    await loadDebug();
    await loadDevices();
    await loadAutomations();
    await loadAutonomy();
    await loadSkills();
    await loadModels();
    await loadProfiles();
    await loadIntegrations();
}

// ── Modell-Registry (Phase 0) ───────────────────────────────────────────────
const MODEL_ROLES = [["agent", "Agent (Tool-Loop)"], ["vision", "Vision"], ["subagent", "Subagent"]];
async function loadModels() {
    let data;
    try { data = await api("GET", "/api/admin/model-registry"); } catch { return; }
    const avail = data.available || [], byRole = {};
    for (const m of (data.registry || [])) byRole[m.role] = m;
    const el = $("model-registry"); el.innerHTML = "";
    for (const [role, label] of MODEL_ROLES) {
        const m = byRole[role] || {};
        const opts = ['<option value="">— (Standard) —</option>'].concat(
            avail.map((id) => `<option value="${id}" ${id === m.id ? "selected" : ""}>${id}</option>`)).join("");
        const ct = m.ctx_total || 32768, sl = m.slots || 1, eff = Math.max(512, Math.floor(ct / Math.max(1, sl)));
        const row = document.createElement("div");
        row.className = "row"; row.style.cssText = "align-items:center;gap:8px;margin-bottom:6px";
        row.innerHTML = `<b style="width:120px">${label}</b>
            <select data-mrole="${role}" data-f="id" style="min-width:160px">${opts}</select>
            <label>ctx_total <input type="number" data-mrole="${role}" data-f="ctx_total" value="${ct}" style="width:90px"></label>
            <label>Slots <input type="number" min="1" data-mrole="${role}" data-f="slots" value="${sl}" style="width:60px"></label>
            <span class="muted" data-eff="${role}">→ effektiv ${eff} Tokens</span>`;
        el.appendChild(row);
    }
    el.querySelectorAll("input[data-f]").forEach((inp) => inp.oninput = () => {
        const role = inp.dataset.mrole;
        const ct = +el.querySelector(`[data-mrole="${role}"][data-f="ctx_total"]`).value || 0;
        const sl = +el.querySelector(`[data-mrole="${role}"][data-f="slots"]`).value || 1;
        el.querySelector(`[data-eff="${role}"]`).textContent = `→ effektiv ${Math.max(512, Math.floor(ct / Math.max(1, sl)))} Tokens`;
    });
}
$("btn-models-refresh").onclick = loadModels;
$("btn-models-save").onclick = async () => {
    const el = $("model-registry"), models = [];
    for (const [role] of MODEL_ROLES) {
        const id = el.querySelector(`select[data-mrole="${role}"]`).value;
        if (!id) continue;
        models.push({
            id, role,
            ctx_total: +el.querySelector(`[data-mrole="${role}"][data-f="ctx_total"]`).value || 32768,
            slots: +el.querySelector(`[data-mrole="${role}"][data-f="slots"]`).value || 1,
        });
    }
    try {
        const r = await api("POST", "/api/admin/model-registry", { models });
        $("models-status").textContent = `✓ gespeichert · Agent: ${r.llm_model} (ctx ${r.llm_ctx}), Vision: ${r.vision_model}`;
    } catch (e) { $("models-status").textContent = "Fehler: " + e.message; }
};

// ── Nutzermodelle / Profile (Phase 3) ───────────────────────────────────────
async function loadProfiles() {
    let data;
    try { data = await api("GET", "/api/admin/user-profiles"); } catch { return; }
    const el = $("profiles"); el.innerHTML = "";
    if (!data.profiles.length) { el.innerHTML = '<p class="muted">Keine Nutzer vorhanden.</p>'; return; }
    for (const p of data.profiles) {
        const div = document.createElement("div"); div.className = "item";
        const when = p.updated_at ? "aktualisiert " + escHtml(String(p.updated_at).slice(0, 16)) : "noch kein Profil";
        div.innerHTML = `<div class="head"><span class="name">${escHtml(p.username)} <span class="muted" style="font-size:11px">#${p.user_id}</span></span>
            <span class="muted" style="font-size:11px">${when}</span></div>
            <textarea data-prof="${p.user_id}" rows="5" style="width:100%;font-size:12px;margin-top:6px" placeholder="(leer — per Gespräch oder „Generieren“ füllen)">${escHtml(p.content)}</textarea>
            <div class="row" style="margin-top:4px"><button class="small" data-pgen="${p.user_id}">⟳ Aus Verlauf generieren</button>
            <button class="small" data-psave="${p.user_id}">Speichern</button>
            <button class="small danger" data-pclear="${p.user_id}">Leeren</button>
            <span class="muted" data-pstatus="${p.user_id}" style="font-size:11px"></span></div>`;
        el.appendChild(div);
    }
    el.querySelectorAll("[data-pgen]").forEach((b) => b.onclick = async () => {
        const uid = b.dataset.pgen, st = el.querySelector(`[data-pstatus="${uid}"]`);
        b.disabled = true; st.textContent = "generiere…";
        try {
            const r = await api("POST", "/api/admin/user-profile/generate", { user_id: +uid });
            if (r.ok) { st.textContent = "✓ generiert"; await loadProfiles(); }
            else st.textContent = r.message || "kein Verlauf";
        } catch (e) { st.textContent = "Fehler: " + e.message; }
        b.disabled = false;
    });
    const save = async (uid, content) => {
        const st = el.querySelector(`[data-pstatus="${uid}"]`);
        try { await api("POST", "/api/admin/user-profile", { user_id: +uid, content }); st.textContent = "✓ gespeichert"; }
        catch (e) { st.textContent = "Fehler: " + e.message; }
    };
    el.querySelectorAll("[data-psave]").forEach((b) => b.onclick = () =>
        save(b.dataset.psave, el.querySelector(`[data-prof="${b.dataset.psave}"]`).value));
    el.querySelectorAll("[data-pclear]").forEach((b) => b.onclick = async () => {
        if (!confirm("Profil leeren?")) return;
        el.querySelector(`[data-prof="${b.dataset.pclear}"]`).value = "";
        await save(b.dataset.pclear, ""); await loadProfiles();
    });
}
$("btn-profiles-refresh").onclick = loadProfiles;

// ── Integrationen (Obsidian + Kalender) ─────────────────────────────────────
const escAttr = (s) => (s || "").replace(/"/g, "&quot;");
async function loadIntegrations() {
    let cfg;
    try { cfg = await api("GET", "/api/config"); } catch { return; }
    $("obs-enabled").checked = !!cfg.obsidian_enabled;
    $("obs-inbox").value = cfg.obsidian_inbox || "Inbox.md";
    renderVaultRows(cfg.obsidian_vaults || {});
    $("cal-enabled").checked = !!cfg.calendar_enabled;
    $("cal-baseurl").value = cfg.calendar_base_url || "";
}
function addVaultRow(u = "", p = "") {
    const el = $("obs-vaults");
    const row = document.createElement("div");
    row.className = "row"; row.style.cssText = "gap:6px;margin-bottom:4px";
    row.innerHTML = `<input class="ov-user" placeholder="nutzername" value="${escAttr(u)}" style="width:140px">
        <input class="ov-path" placeholder="/opt/obsidian/config/Vault" value="${escAttr(p)}" style="flex:1">
        <button class="small danger ov-del">✕</button>`;
    row.querySelector(".ov-del").onclick = () => row.remove();
    el.appendChild(row);
}
function renderVaultRows(map) {
    $("obs-vaults").innerHTML = "";
    const entries = Object.entries(map || {});
    if (!entries.length) entries.push(["", ""]);
    for (const [u, p] of entries) addVaultRow(u, p);
}
$("btn-obs-addrow").onclick = () => addVaultRow();
$("btn-obs-save").onclick = async () => {
    const map = {};
    document.querySelectorAll("#obs-vaults .row").forEach((r) => {
        const u = r.querySelector(".ov-user").value.trim().toLowerCase();
        const p = r.querySelector(".ov-path").value.trim();
        if (u && p) map[u] = p;
    });
    try {
        await api("POST", "/api/config", { obsidian_enabled: $("obs-enabled").checked,
            obsidian_inbox: $("obs-inbox").value.trim() || "Inbox.md", obsidian_vaults: map });
        $("obs-status").textContent = "✓ gespeichert";
    } catch (e) { $("obs-status").textContent = "Fehler: " + e.message; }
};
$("btn-cal-save").onclick = async () => {
    try {
        await api("POST", "/api/config", { calendar_enabled: $("cal-enabled").checked,
            calendar_base_url: $("cal-baseurl").value.trim() });
        $("cal-status").textContent = "✓ gespeichert";
    } catch (e) { $("cal-status").textContent = "Fehler: " + e.message; }
};
$("btn-cal-list").onclick = async () => {
    const el = $("cal-list"); el.innerHTML = "lädt…";
    try {
        const d = await api("GET", "/api/admin/calendars");
        el.innerHTML = d.calendars.map((c) => `<div class="item"><b>${escHtml(c.name)}</b>
            <span class="muted">(${c.kind}${c.owner ? ", " + escHtml(c.owner) : ""}, ${c.events} Termine)</span><br>
            <code style="font-size:11px">${escHtml(c.ics)}</code></div>`).join("") || '<p class="muted">Keine Kalender.</p>';
    } catch (e) { el.textContent = "Fehler: " + e.message; }
};

// ── MCP-Server ────────────────────────────────────────────────────────────────
async function loadMcp() {
    const servers = (await api("GET", "/api/admin/mcp")).servers;
    const el = $("mcps"); el.innerHTML = "";
    if (!servers.length) el.innerHTML = '<p class="muted">Noch keine MCP-Server.</p>';
    for (const s of servers) {
        const div = document.createElement("div"); div.className = "item";
        const status = s.error ? `<span class="tag" style="background:var(--bad);color:#fff">Fehler</span>`
                               : `<span class="tag" style="background:var(--ok);color:#04141b">${s.tool_count} Tools</span>`;
        div.innerHTML = `<div class="head"><span class="name">${s.name} ${status}</span>
            <button class="small danger" data-delm="${s.name}">Entfernen</button></div>
            <div class="muted" style="font-size:11px">${s.url}${s.error ? " — " + s.error : ""}</div>`;
        el.appendChild(div);
    }
    el.querySelectorAll("[data-delm]").forEach((b) => b.onclick = async () => {
        if (confirm("MCP-Server entfernen?")) { await api("POST", "/api/admin/mcp/delete", { name: b.dataset.delm }); await loadAdmin(); }
    });
}

$("btn-add-mcp").onclick = async () => {
    const name = $("new-mcp-name").value.trim(), url = $("new-mcp-url").value.trim();
    if (!name || !url) return;
    try { await api("POST", "/api/admin/mcp", { name, url }); $("new-mcp-name").value = ""; $("new-mcp-url").value = ""; await loadAdmin(); }
    catch (e) { alert(e.message); }
};
$("btn-refresh-mcp").onclick = async () => { await api("POST", "/api/admin/mcp/refresh"); await loadAdmin(); };

// ── Debug ─────────────────────────────────────────────────────────────────────
function fmtEvent(e) {
    const ts = new Date(e.t * 1000).toLocaleTimeString();
    const rest = Object.entries(e).filter(([k]) => !["id", "t", "kind"].includes(k))
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ");
    return `${ts}  [${e.kind}]  ${rest}`;
}
async function loadDebug() {
    const d = await api("GET", "/api/admin/debug");
    $("cfg-debug-toggle").checked = d.enabled;
    $("debug-log").textContent = d.events.length
        ? d.events.map(fmtEvent).join("\n")
        : (d.enabled ? "(noch keine Ereignisse — interagiere mit Jarvis)" : "(Aufzeichnung deaktiviert)");
    $("debug-log").scrollTop = $("debug-log").scrollHeight;
}
$("cfg-debug-toggle").onchange = async (e) => { await api("POST", "/api/admin/debug", { enabled: e.target.checked }); loadDebug(); };
$("btn-debug-refresh").onclick = loadDebug;
$("btn-debug-clear").onclick = async () => { await api("POST", "/api/admin/debug/clear"); loadDebug(); };
let _dbgTimer = null;
$("debug-auto").onchange = (e) => {
    if (e.target.checked) { _dbgTimer = setInterval(loadDebug, 3000); loadDebug(); }
    else if (_dbgTimer) { clearInterval(_dbgTimer); _dbgTimer = null; }
};

// ── Geräte / Satelliten ─────────────────────────────────────────────────────────
const DEV_ICON = { satellite: "📡", browser: "🖥", "satellite-pi": "🍓", "?": "❓" };
function fmtAgo(s) {
    if (s < 60) return Math.round(s) + " s";
    if (s < 3600) return Math.round(s / 60) + " min";
    if (s < 86400) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " d";
}
async function loadDevices() {
    let devs = [];
    try { devs = (await api("GET", "/api/admin/devices")).devices; } catch { return; }
    const el = $("devices"); el.innerHTML = "";
    if (!devs.length) { el.innerHTML = '<p class="muted">Noch keine Geräte verbunden.</p>'; return; }
    for (const d of devs) {
        const div = document.createElement("div"); div.className = "item";
        const dot = d.online
            ? '<span class="tag" style="background:var(--ok);color:#04141b">● online</span>'
            : `<span class="tag" style="background:#33404d;color:#9fb3c8">○ offline · vor ${fmtAgo(d.ago_s)}</span>`;
        const bits = [];
        if (d.room) bits.push(`Raum: <b>${d.room}</b>`);
        if (d.volume != null) bits.push(`Lautstärke: ${d.volume}%`);
        if (d.mic_gain != null) bits.push(`Mic-Gain: ${d.mic_gain} dB`);
        if (d.rssi != null) bits.push(`WLAN: ${d.rssi} dBm`);
        if (d.last_speaker) bits.push(`zuletzt erkannt: ${d.last_speaker}`);
        if (d.fw) bits.push(`FW ${d.fw}`);
        if (d.render === "pcm") bits.push("Audio: Server-PCM");
        // Remote-Steuerung nur für verbundene Audio-Geräte (ESP-Satellit)
        let ctrl = "";
        if (d.online && (d.type === "satellite" || d.render === "pcm")) {
            const vol = d.volume != null ? d.volume : 50;
            const mg = d.mic_gain != null ? d.mic_gain : 30;
            ctrl = `<div class="dev-ctrl" style="margin-top:8px;display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
                <label style="font-size:12px">Lautstärke (%)<br>
                  <input type="number" min="0" max="90" step="5" value="${vol}" data-ctrl="vol" style="width:70px"></label>
                <label style="font-size:12px">Mic-Gain (dB)<br>
                  <input type="number" min="0" max="42" step="1" value="${mg}" data-ctrl="mic" style="width:70px"></label>
                <button class="small" data-ctrl="apply">Anwenden</button>
                <span class="muted" data-ctrl="status" style="font-size:11px"></span></div>`;
        }
        div.innerHTML = `<div class="head"><span class="name">${DEV_ICON[d.type] || "•"} ${d.name} ${dot}</span>
            <span class="muted" style="font-size:11px">${d.type} · ${d.session_id}</span></div>
            <div class="muted" style="font-size:12px">${bits.join(" · ") || "—"}</div>${ctrl}`;
        if (ctrl) {
            const btn = div.querySelector('[data-ctrl="apply"]');
            btn.onclick = async () => {
                const volume = parseInt(div.querySelector('[data-ctrl="vol"]').value);
                const mic_gain = parseFloat(div.querySelector('[data-ctrl="mic"]').value);
                const st = div.querySelector('[data-ctrl="status"]');
                st.textContent = "…";
                try {
                    await api("POST", "/api/admin/devices/control", { session_id: d.session_id, volume, mic_gain });
                    st.textContent = "✓ gesendet";
                } catch (e) { st.textContent = "✗ " + (e.message || "Fehler"); }
            };
        }
        el.appendChild(div);
    }
}
$("btn-dev-refresh").onclick = loadDevices;
let _devTimer = null;
$("dev-auto").onchange = (e) => {
    if (e.target.checked) { _devTimer = setInterval(loadDevices, 5000); loadDevices(); }
    else if (_devTimer) { clearInterval(_devTimer); _devTimer = null; }
};

// ── Autonomie / Automatisierungen ───────────────────────────────────────────────
const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
function initWeekdays() {
    const el = $("na-weekdays"); if (!el || el.children.length) return;
    WD.forEach((d, i) => {
        const lab = document.createElement("label"); lab.className = "inline"; lab.style.marginRight = "6px";
        lab.innerHTML = `<input type="checkbox" value="${i}" ${i < 5 ? "checked" : ""}>${d}`;
        el.appendChild(lab);
    });
}
function updateTrigFields() {
    const t = $("na-type").value;
    document.querySelectorAll("[data-trig]").forEach((el) => {
        el.style.display = el.dataset.trig.split(" ").includes(t) ? "" : "none";
    });
}
function buildTrigger() {
    const t = $("na-type").value;
    if (t === "once") { const v = $("na-at").value; if (!v) throw new Error("Zeitpunkt fehlt"); return { type: "once", at: new Date(v).getTime() / 1000 }; }
    if (t === "interval") return { type: "interval", seconds: Math.max(1, parseInt($("na-interval").value || "60")) * 60 };
    if (t === "daily") return { type: "daily", time: $("na-time").value || "07:00" };
    if (t === "weekly") { const d = [...document.querySelectorAll("#na-weekdays input:checked")].map((c) => +c.value); return { type: "weekly", time: $("na-time").value || "07:00", weekdays: d.length ? d : [0, 1, 2, 3, 4, 5, 6] }; }
    const tr = { type: "event", event: $("na-event").value }; const m = $("na-match").value.trim(); if (m) tr.match = m; return tr;
}

async function loadAutomations() {
    initWeekdays(); updateTrigFields();
    const r = await api("GET", "/api/admin/automations");
    $("auto-scheduler").checked = r.scheduler;
    const el = $("automations"); el.innerHTML = "";
    if (!r.automations.length) { el.innerHTML = '<p class="muted">Noch keine Automatisierungen.</p>'; }
    for (const a of r.automations) {
        const div = document.createElement("div"); div.className = "item";
        const badge = a.enabled ? '<span class="tag" style="background:var(--ok);color:#04141b">aktiv</span>'
                                : '<span class="tag" style="background:#33404d;color:#9fb3c8">aus</span>';
        const kindBadge = a.kind === "watcher"
            ? '<span class="tag" style="background:#2a3f5f;color:#9fc3ff">🔍 Überwachung</span>' : "";
        const last = a.last_result ? `<div class="muted" style="font-size:11px">zuletzt: ${a.last_result}</div>` : "";
        let watchInfo = "";
        if (a.kind === "watcher") {
            const st = a.state && Object.keys(a.state).length ? JSON.stringify(a.state).slice(0, 120) : "—";
            const fail = a.fail_count ? ` · <span style="color:var(--bad)">Skript-Fehler: ${a.fail_count}</span>` : "";
            watchInfo = `<div class="muted" style="font-size:11px">Zustand: ${st}${fail}</div>`;
        }
        div.innerHTML = `<div class="head"><span class="name">${a.title} ${badge} ${kindBadge}</span>
            <span><button class="small" data-run="${a.id}">▶ ${a.kind === "watcher" ? "Jetzt prüfen" : "Jetzt"}</button>
            <button class="small secondary" data-edit="${a.id}">✎ Bearbeiten</button>
            <button class="small secondary" data-tog="${a.id}">${a.enabled ? "Deaktivieren" : "Aktivieren"}</button>
            <button class="small danger" data-del="${a.id}">Löschen</button></span></div>
            <div class="muted" style="font-size:12px">${a.trigger_text}${a.next_run_text ? " · nächste: " + a.next_run_text : ""}
            · Läufe: ${a.run_count}</div>${watchInfo}
            <div class="atask" style="font-size:12px;margin-top:3px">${a.task}</div>
            <div class="aedit" style="display:none;margin-top:6px">
                <input class="ed-title" value="${a.title.replace(/"/g, "&quot;")}" style="width:100%;margin-bottom:4px">
                <textarea class="ed-task" rows="3" style="width:100%">${a.task}</textarea>
                <div class="row"><button class="small" data-save="${a.id}">Speichern</button>
                    <button class="small secondary" data-cancel="${a.id}">Abbrechen</button></div>
            </div>${last}`;
        el.appendChild(div);
    }
    el.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => {
        const item = b.closest(".item");
        item.querySelector(".atask").style.display = "none";
        item.querySelector(".aedit").style.display = "";
    });
    el.querySelectorAll("[data-cancel]").forEach((b) => b.onclick = () => {
        const item = b.closest(".item");
        item.querySelector(".atask").style.display = "";
        item.querySelector(".aedit").style.display = "none";
    });
    el.querySelectorAll("[data-save]").forEach((b) => b.onclick = async () => {
        const item = b.closest(".item");
        await api("POST", "/api/admin/automations/update", {
            id: b.dataset.save,
            title: item.querySelector(".ed-title").value.trim(),
            task: item.querySelector(".ed-task").value.trim(),
        });
        await loadAutomations();
    });
    el.querySelectorAll("[data-run]").forEach((b) => b.onclick = async () => {
        b.textContent = "läuft…";
        const res = await api("POST", "/api/admin/automations/run", { id: b.dataset.run });
        await loadAutomations();
        alert(res && res.last_result ? "Ergebnis:\n\n" + res.last_result : "Lauf beendet (keine Meldung / SILENT).");
    });
    el.querySelectorAll("[data-tog]").forEach((b) => b.onclick = async () => {
        const a = r.automations.find((x) => x.id === b.dataset.tog);
        await api("POST", "/api/admin/automations/update", { id: b.dataset.tog, enabled: !a.enabled }); await loadAutomations();
    });
    el.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
        if (confirm("Automatisierung löschen?")) { await api("POST", "/api/admin/automations/delete", { id: b.dataset.del }); await loadAutomations(); }
    });
}
$("na-type").onchange = updateTrigFields;
$("auto-scheduler").onchange = async (e) => { await api("POST", "/api/admin/autonomy", { enabled: e.target.checked }); };
$("btn-add-automation").onclick = async () => {
    const task = $("na-task").value.trim(); if (!task) return alert("Bitte eine Aufgabe angeben.");
    let trigger; try { trigger = buildTrigger(); } catch (e) { return alert(e.message); }
    const target = $("na-target").value.trim();
    try {
        await api("POST", "/api/admin/automations", { title: $("na-title").value.trim(), task, trigger, target_session: target || null });
        $("na-title").value = ""; $("na-task").value = ""; $("na-target").value = ""; await loadAutomations();
    } catch (e) { alert(e.message); }
};

async function loadAutonomy() {
    const a = await api("GET", "/api/admin/autonomy");
    $("auto-cooldown").value = a.event_cooldown_s;
    if (a.events) {
        $("na-event").innerHTML = a.events.map((e) => `<option value="${e.name}">${e.label}</option>`).join("");
    }
    const mk = (host, items, sel) => {
        const el = $(host); el.innerHTML = items.length ? "" : '<span class="muted" style="font-size:12px">—</span>';
        for (const it of items) {
            const lab = document.createElement("label"); lab.className = "inline"; lab.style.display = "block";
            lab.innerHTML = `<input type="checkbox" value="${it}" ${sel.includes(it) ? "checked" : ""}> ${it}`;
            el.appendChild(lab);
        }
    };
    mk("bl-tools", a.tools, a.tool_blacklist);
    mk("bl-mcps", a.mcps, a.mcp_blacklist);
}
$("btn-save-autonomy").onclick = async () => {
    const tools = [...document.querySelectorAll("#bl-tools input:checked")].map((c) => c.value);
    const mcps = [...document.querySelectorAll("#bl-mcps input:checked")].map((c) => c.value);
    await api("POST", "/api/admin/autonomy", { tool_blacklist: tools, mcp_blacklist: mcps, event_cooldown_s: +$("auto-cooldown").value });
    $("autonomy-status").textContent = "✓ gespeichert"; setTimeout(() => $("autonomy-status").textContent = "", 2000);
};

// ── Selbst-gebaute Skills ───────────────────────────────────────────────────────
function escHtml(s) { return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function parseJsonField(el) { const v = (el.value || "").trim(); return v ? JSON.parse(v) : {}; }

async function loadSkills() {
    let list = [];
    try { list = (await api("GET", "/api/admin/skills")).skills; } catch { return; }
    const el = $("skills"); el.innerHTML = "";
    if (!list.length) {
        el.innerHTML = '<p class="muted">Noch keine Skills. JARVIS baut sie selbst per Chat — z.B. „bau dir ein Skill, das …“.</p>';
        return;
    }
    for (const s of list) {
        const div = document.createElement("div"); div.className = "item";
        const on = s.enabled ? '<span class="tag" style="background:var(--ok);color:#04141b">aktiv</span>'
                             : '<span class="tag" style="background:#33404d;color:#9fb3c8">aus</span>';
        const net = s.net ? '<span class="tag" style="background:#2a3f5f;color:#9fc3ff">🌐 Netz</span>' : "";
        const trust = s.trust === "elevated"
            ? '<span class="tag" style="background:#5a2a2a;color:#ffb3b3">⚡ Erhöht</span>'
            : '<span class="tag" style="background:#33404d;color:#9fb3c8">🔒 Sandbox</span>';
        const fail = s.fail_count ? ` · <span style="color:var(--bad)">Fehler: ${s.fail_count}</span>` : "";
        const elev = s.trust === "elevated";
        const unhealthy = (s.fail_count || 0) >= 3 && s.last_error;
        const unhealthyTag = unhealthy ? ' <span class="tag" style="background:#5a4a2a;color:#ffd9a0">⚠ instabil</span>' : "";
        const repairBtn = s.last_error ? `<button class="small" data-srepair="${s.name}" title="Code automatisch reparieren">🔧 Reparieren</button>` : "";
        const errLine = s.last_error ? `<div class="muted" style="font-size:11px;color:var(--bad);margin-top:2px">letzter Fehler: ${escHtml(String(s.last_error))}</div>` : "";
        div.innerHTML = `<div class="head"><span class="name">${s.name} ${on} ${net} ${trust}${unhealthyTag}</span>
            <span><button class="small" data-srun="${s.name}">▶ Test</button>
            ${repairBtn}
            <button class="small secondary" data-stog="${s.name}">${s.enabled ? "Deaktivieren" : "Aktivieren"}</button>
            <button class="small secondary" data-ssave="${s.name}">💾 Code speichern</button>
            <button class="small danger" data-sdel="${s.name}">Löschen</button></span></div>
            <div class="muted" style="font-size:12px">${escHtml(s.description)} · v${s.version} · Läufe: ${s.run_count}${fail}${
              ((s.apt && s.apt.length) || (s.pip && s.pip.length))
                ? " · Pakete: " + [...(s.apt || []).map((x) => "apt:" + x), ...(s.pip || []).map((x) => "pip:" + x)].join(", ") : ""}</div>${errLine}
            <textarea data-scode="${s.name}" rows="6" style="width:100%;font-family:monospace;font-size:11px;margin-top:6px">${escHtml(s.code)}</textarea>
            <div class="row" style="margin-top:4px;align-items:center">
              <label class="inline" style="font-size:11px"><input type="checkbox" data-snet="${s.name}" ${s.net ? "checked" : ""}> Netz erlaubt</label>
              <label class="inline" style="font-size:11px">Rechte:
                <select data-strust="${s.name}">
                  <option value="sandbox" ${!elev ? "selected" : ""}>🔒 Sandbox</option>
                  <option value="elevated" ${elev ? "selected" : ""}>⚡ Erhöht (Hostnetz+Raw)</option>
                </select></label>
              <label class="inline" style="font-size:11px" title="Erhöhte Skills laufen sonst nur interaktiv">
                <input type="checkbox" data-sauto="${s.name}" ${s.autonomous_ok ? "checked" : ""}> autonom erlaubt</label>
            </div>
            <div class="row" style="margin-top:4px">
              <input data-sargs="${s.name}" placeholder='Argumente JSON, z.B. {"a":1,"b":2}' style="flex:1;font-size:11px">
            </div>
            <div class="muted" data-sstatus="${s.name}" style="font-size:11px;margin-top:3px">${elev ? "⚡ Erhöht: läuft im privilegierten Container (sandbox-priv) mit Hostnetz + NET_RAW." : ""}</div>`;
        el.appendChild(div);
    }
    el.querySelectorAll("[data-stog]").forEach((b) => b.onclick = async () => {
        const s = list.find((x) => x.name === b.dataset.stog);
        await api("POST", "/api/admin/skills/update", { name: b.dataset.stog, enabled: !s.enabled }); await loadSkills();
    });
    el.querySelectorAll("[data-sdel]").forEach((b) => b.onclick = async () => {
        if (confirm("Skill löschen?")) { await api("POST", "/api/admin/skills/delete", { name: b.dataset.sdel }); await loadSkills(); }
    });
    el.querySelectorAll("[data-srepair]").forEach((b) => b.onclick = async () => {
        b.disabled = true; b.textContent = "🔧 repariere…";
        try { const r = await api("POST", "/api/admin/skills/repair", { name: b.dataset.srepair }); alert(r.message); }
        catch (e) { alert("Fehler: " + e.message); }
        await loadSkills();
    });
    el.querySelectorAll("[data-ssave]").forEach((b) => b.onclick = async () => {
        const name = b.dataset.ssave;
        const st = el.querySelector(`[data-sstatus="${name}"]`); st.textContent = "teste…";
        const code = el.querySelector(`[data-scode="${name}"]`).value;
        const net = el.querySelector(`[data-snet="${name}"]`).checked;
        let test_args; try { test_args = parseJsonField(el.querySelector(`[data-sargs="${name}"]`)); }
        catch { st.textContent = "Argumente sind kein gültiges JSON."; return; }
        try { await api("POST", "/api/admin/skills/update", { name, code, net, test_args }); st.textContent = "✓ gespeichert (getestet)"; await loadSkills(); }
        catch (e) { st.textContent = "✗ " + (e.message || "Fehler"); }
    });
    el.querySelectorAll("[data-srun]").forEach((b) => b.onclick = async () => {
        const name = b.dataset.srun;
        const st = el.querySelector(`[data-sstatus="${name}"]`); st.textContent = "läuft…";
        let args; try { args = parseJsonField(el.querySelector(`[data-sargs="${name}"]`)); }
        catch { st.textContent = "Argumente sind kein gültiges JSON."; return; }
        try { const r = await api("POST", "/api/admin/skills/run", { name, args });
              st.textContent = r.ok ? ("Ergebnis: " + JSON.stringify(r.result)) : ("Fehler: " + r.error); }
        catch (e) { st.textContent = "✗ " + (e.message || "Fehler"); }
    });
    el.querySelectorAll("[data-strust]").forEach((b) => b.onchange = async () => {
        if (b.value === "elevated" && !confirm("Erhöhte Rechte: dieses Skill läuft dann mit Hostnetz + NET_RAW. "
            + "Prüfe den Code! Wirklich freischalten?")) { b.value = "sandbox"; return; }
        try {
            const r = await api("POST", "/api/admin/skills/update", { name: b.dataset.strust, trust: b.value });
            if (r && r.install_log) alert((r.install_ok ? "Pakete installiert:\n\n" : "Paket-Installation mit Problemen:\n\n") + r.install_log);
            await loadSkills();
        } catch (e) { alert(e.message || "Fehler"); }
    });
    el.querySelectorAll("[data-sauto]").forEach((b) => b.onchange = async () => {
        try { await api("POST", "/api/admin/skills/update", { name: b.dataset.sauto, autonomous_ok: b.checked }); }
        catch (e) { alert(e.message || "Fehler"); }
    });
}
$("btn-skills-refresh").onclick = loadSkills;

async function loadConfig() {
    const cfg = await api("GET", "/api/config");
    for (const k of CFG_KEYS) { const el = $("cfg-" + k); if (el) el.value = cfg[k] ?? ""; }
    for (const k of CFG_BOOLS) { const el = $("cfg-" + k); if (el) el.checked = !!cfg[k]; }
    await loadMessaging();
}

async function loadMessaging() {
    const m = await api("GET", "/api/admin/messaging");
    $("tg-enabled").checked = m.enabled;
    $("tg-default").value = m.default_chat_id || "";
    $("tg-token").placeholder = m.has_token ? `gesetzt (${m.token_hint}) — leer lassen = behalten` : "Bot-Token von @BotFather";
    const bot = m.bot && m.bot.ok ? `· Bot: @${m.bot.result.username}` : "";
    $("tg-status").textContent = bot;
    await loadPending();
}

async function loadPending() {
    const el = $("tg-pending"); if (!el) return;
    const pend = (await api("GET", "/api/admin/messaging/pending")).pending;
    const users = (await api("GET", "/api/admin/users")).users;
    el.innerHTML = "";
    if (!pend.length) { el.innerHTML = '<span class="muted" style="font-size:12px">— keine —</span>'; return; }
    for (const p of pend) {
        const opts = users.map((u) => `<option value="${u.id}">${u.username}</option>`).join("");
        const div = document.createElement("div"); div.className = "item";
        div.innerHTML = `<div class="muted" style="font-size:12px"><b>${p.name || "?"}</b> · Chat-ID <code>${p.chat_id}</code><br>„${p.text}"</div>
            <div class="row"><select data-puser="${p.chat_id}">${opts}</select>
            <button class="small" data-passign="${p.chat_id}">Zuordnen</button>
            <button class="small secondary" data-pdrop="${p.chat_id}">Verwerfen</button></div>`;
        el.appendChild(div);
    }
    el.querySelectorAll("[data-passign]").forEach((b) => b.onclick = async () => {
        const cid = b.dataset.passign;
        const uid = +el.querySelector(`[data-puser="${cid}"]`).value;
        await api("POST", "/api/admin/users/telegram", { id: uid, chat_id: cid });
        await loadPending(); await loadUsers();
    });
    el.querySelectorAll("[data-pdrop]").forEach((b) => b.onclick = async () => {
        await api("POST", "/api/admin/messaging/pending/clear", { chat_id: b.dataset.pdrop });
        await loadPending();
    });
}
$("btn-refresh-pending").onclick = loadPending;
$("btn-save-tg").onclick = async () => {
    const body = { enabled: $("tg-enabled").checked, default_chat_id: $("tg-default").value.trim() };
    const tok = $("tg-token").value.trim(); if (tok) body.bot_token = tok;
    await api("POST", "/api/admin/messaging", body);
    $("tg-token").value = ""; $("tg-status").textContent = "✓ gespeichert"; await loadMessaging();
};
$("btn-cl-upload").onclick = async () => {
    const f = $("cl-file").files[0];
    if (!f) { alert("Bitte eine Paketdatei wählen."); return; }
    const fd = new FormData();
    fd.append("file", f, f.name);
    fd.append("platform", $("cl-platform").value);
    $("cl-status").textContent = "Lade hoch…";
    try {
        const r = await fetch("/api/admin/client-upload", { method: "POST", body: fd });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
        const d = await r.json();
        $("cl-status").textContent = `✓ ${d.filename} (${Math.round(d.bytes / 1024)} KB) — unter /downloads verfügbar`;
        $("cl-file").value = "";
    } catch (e) { $("cl-status").textContent = "Fehler: " + e.message; }
};
$("btn-test-tg").onclick = async () => {
    const chat = prompt("Chat-ID für Testnachricht (leer = Standard-Chat-ID):", $("tg-default").value.trim());
    if (chat === null) return;
    try { const r = await api("POST", "/api/admin/messaging/test", { chat_id: chat.trim() });
        alert(r.ok ? "✅ Testnachricht gesendet." : "❌ Senden fehlgeschlagen (Token/Chat-ID prüfen)."); }
    catch (e) { alert(e.message); }
};

$("btn-save-cfg").onclick = async () => {
    const patch = {};
    for (const k of CFG_KEYS) { const el = $("cfg-" + k); if (el) patch[k] = el.value; }
    for (const k of CFG_BOOLS) { const el = $("cfg-" + k); if (el) patch[k] = el.checked; }
    $("cfg-status").textContent = "Speichere…";
    try { await api("POST", "/api/config", patch); $("cfg-status").textContent = "✓ gespeichert"; }
    catch (e) { $("cfg-status").textContent = "Fehler: " + e.message; }
};

function renderGroups() {
    const el = $("groups"); el.innerHTML = "";
    for (const g of GROUPS) {
        const div = document.createElement("div"); div.className = "item";
        let checks = "";
        if (g.is_admin) {
            checks = '<p class="muted">Admin-Gruppe: Vollzugriff auf alle Tools/MCPs.</p>';
        } else {
            const all = [...RES.tools, ...(RES.mcps || [])];
            checks = '<div class="checks">' + all.map((res) => {
                const on = g.permissions.includes(res) ? "checked" : "";
                const label = res.startsWith("mcp:") ? "🔌 " + res.slice(4) : res.replace("tool:", "");
                return `<label><input type="checkbox" data-gid="${g.id}" value="${res}" ${on}> ${label}</label>`;
            }).join("") + "</div>";
        }
        div.innerHTML = `<div class="head"><span class="name">${g.name}${g.is_admin ? '<span class="tag">ADMIN</span>' : ""}</span>
            <span>${g.is_admin ? "" : `<button class="small" data-save="${g.id}">Rechte speichern</button> `}
            <button class="small danger" data-delg="${g.id}">Löschen</button></span></div>${checks}`;
        el.appendChild(div);
    }
    el.querySelectorAll("[data-save]").forEach((b) => b.onclick = () => saveGroupPerms(+b.dataset.save));
    el.querySelectorAll("[data-delg]").forEach((b) => b.onclick = () => delGroup(+b.dataset.delg));
}

async function saveGroupPerms(gid) {
    const res = [...document.querySelectorAll(`input[data-gid="${gid}"]:checked`)].map((c) => c.value);
    await api("POST", "/api/admin/groups/permissions", { id: gid, resources: res });
    await loadAdmin();
}
async function delGroup(gid) { if (confirm("Gruppe löschen?")) { await api("POST", "/api/admin/groups/delete", { id: gid }); await loadAdmin(); } }

$("btn-add-group").onclick = async () => {
    const name = $("new-group").value.trim(); if (!name) return;
    await api("POST", "/api/admin/groups", { name, is_admin: $("new-group-admin").checked });
    $("new-group").value = ""; $("new-group-admin").checked = false; await loadAdmin();
};

async function loadUsers() {
    const users = (await api("GET", "/api/admin/users")).users;
    let vaults = {};
    try { vaults = (await api("GET", "/api/config")).obsidian_vaults || {}; } catch { /* ignore */ }
    const el = $("users"); el.innerHTML = "";
    for (const u of users) {
        const div = document.createElement("div"); div.className = "item";
        const gchecks = GROUPS.map((g) =>
            `<label><input type="checkbox" data-uid="${u.id}" value="${g.id}" ${u.groups.includes(g.name) ? "checked" : ""}> ${g.name}</label>`
        ).join("");
        const vp = u.voice_samples || 0;
        const pwTag = u.has_password
            ? '<span class="tag" style="background:#243446;color:var(--muted)">🔑 PW</span>'
            : '<span class="tag" style="background:var(--warn);color:#04141b">kein PW</span>';
        div.innerHTML = `<div class="head"><span class="name">${u.username} ${pwTag}
            <span class="tag" style="background:${vp ? 'var(--ok)' : '#243446'};color:${vp ? '#04141b' : 'var(--muted)'}">🎙 ${vp}</span></span>
            <span><button class="small" data-savu="${u.id}">Gruppen speichern</button>
            <button class="small" data-resu="${u.id}">PW zurücksetzen</button>
            <button class="small danger" data-delu="${u.id}">Löschen</button></span></div>
            <div class="checks">${gchecks || '<span class="muted">Keine Gruppen vorhanden</span>'}</div>
            <div class="row"><button class="small" data-rec="${u.id}" data-name="${u.username}">🎙 Stimme aufnehmen (3–5 s)</button>
            <button class="small danger" data-clrv="${u.id}">Stimmprofil löschen</button>
            <span class="muted" data-recst="${u.id}"></span></div>
            <div class="row"><label class="inline" style="flex:1">✈ Telegram-Chat-ID
                <input data-tgid="${u.id}" value="${u.telegram_chat_id || ''}" placeholder="z. B. 123456789" style="width:160px"></label>
            <button class="small" data-savtg="${u.id}">ID speichern</button></div>
            <div class="row"><label class="inline" style="flex:1">📓 Obsidian-Vault-Pfad
                <input data-obsv="${u.username}" value="${escAttr(vaults[u.username.toLowerCase()] || '')}" placeholder="/opt/obsidian/config/${u.username}" style="width:260px"></label>
            <button class="small" data-savobs="${u.username}">Vault speichern</button></div>`;
        el.appendChild(div);
    }
    el.querySelectorAll("[data-savtg]").forEach((b) => b.onclick = async () => {
        const v = document.querySelector(`[data-tgid="${b.dataset.savtg}"]`).value.trim();
        await api("POST", "/api/admin/users/telegram", { id: +b.dataset.savtg, chat_id: v });
        b.textContent = "✓"; setTimeout(() => b.textContent = "ID speichern", 1500);
    });
    el.querySelectorAll("[data-savobs]").forEach((b) => b.onclick = async () => {
        const path = document.querySelector(`[data-obsv="${b.dataset.savobs}"]`).value.trim();
        await api("POST", "/api/admin/users/obsidian", { username: b.dataset.savobs, path });
        b.textContent = "✓"; setTimeout(() => b.textContent = "Vault speichern", 1500);
    });
    el.querySelectorAll("[data-savu]").forEach((b) => b.onclick = () => saveUserGroups(+b.dataset.savu));
    el.querySelectorAll("[data-resu]").forEach((b) => b.onclick = () => resetPw(+b.dataset.resu));
    el.querySelectorAll("[data-delu]").forEach((b) => b.onclick = () => delUser(+b.dataset.delu));
    el.querySelectorAll("[data-rec]").forEach((b) => b.onclick = () => recordVoice(+b.dataset.rec));
    el.querySelectorAll("[data-clrv]").forEach((b) => b.onclick = () => clearVoice(+b.dataset.clrv));
}

async function clearVoice(uid) {
    if (!confirm("Stimmprofil dieses Nutzers löschen?")) return;
    await api("POST", "/api/admin/users/clear-voice", { id: uid });
    await loadUsers();
}

async function recordVoice(uid) {
    const stEl = document.querySelector(`[data-recst="${uid}"]`);
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const rec = new MediaRecorder(stream);
        const chunks = [];
        rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
        rec.onstop = async () => {
            stream.getTracks().forEach((t) => t.stop());
            stEl.textContent = "Verarbeite…";
            try {
                const fd = new FormData();
                fd.append("file", new Blob(chunks, { type: rec.mimeType || "audio/webm" }), "voice.webm");
                fd.append("user_id", uid);
                const r = await fetch("/api/admin/users/enroll-voice", { method: "POST", body: fd, credentials: "same-origin" });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
                const d = await r.json();
                stEl.textContent = `✓ gespeichert (${d.samples} Aufnahmen)`;
                loadUsers();
            } catch (e) { stEl.textContent = "Fehler: " + e.message; }
        };
        rec.start();
        stEl.textContent = "🔴 Aufnahme… (5 s) — sprich einen Satz";
        setTimeout(() => { if (rec.state === "recording") rec.stop(); }, 5000);
    } catch (e) {
        stEl.textContent = "Mikrofon nicht verfügbar: " + e.message;
    }
}

async function saveUserGroups(uid) {
    const ids = [...document.querySelectorAll(`input[data-uid="${uid}"]:checked`)].map((c) => +c.value);
    await api("POST", "/api/admin/users/groups", { id: uid, group_ids: ids });
    await loadUsers();
}
async function resetPw(uid) {
    const p = prompt("Neues Initialpasswort (Nutzer muss es danach ändern):"); if (!p) return;
    await api("POST", "/api/admin/users/reset-password", { id: uid, password: p }); await loadUsers();
}
async function delUser(uid) { if (confirm("Nutzer löschen?")) { await api("POST", "/api/admin/users/delete", { id: uid }); await loadUsers(); } }

$("btn-add-user").onclick = async () => {
    const username = $("new-user").value.trim();
    if (!username) return;
    try { await api("POST", "/api/admin/users", { username }); $("new-user").value = ""; await loadUsers(); }
    catch (e) { alert(e.message); }
};

init();
