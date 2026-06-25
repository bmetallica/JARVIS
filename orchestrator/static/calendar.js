"use strict";
// JARVIS-Kalender (nutzerseitig). Identität kommt serverseitig aus der Session (zuletzt erkannte Stimme).
(function () {
    const $ = (id) => document.getElementById(id);
    const modal = $("calendar");
    const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
        "Oktober", "November", "Dezember"];
    const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

    let view = { y: 0, m: 0 };
    let data = { events: [], writable: [], subscriptions: [], ical_link: "", username: null };
    let selected = null;      // "YYYY-MM-DD"
    let editing = null;       // event-id beim Bearbeiten

    const sid = () => localStorage.getItem("jarvis_session_id") || "";
    const pad = (n) => String(n).padStart(2, "0");
    const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

    async function apiGet(start, end) {
        const r = await fetch(`/api/me/calendar?session_id=${encodeURIComponent(sid())}&start=${start}&end=${end}`);
        if (!r.ok) { const d = await r.json().catch(() => ({})); const e = new Error(d.detail || r.statusText); e.status = r.status; throw e; }
        return r.json();
    }
    async function apiAction(action, args) {
        const r = await fetch("/api/me/calendar/action", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sid(), action, args }),
        });
        if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || r.statusText); }
        return r.json();
    }

    function open() {
        const now = new Date();
        view = { y: now.getFullYear(), m: now.getMonth() };
        selected = ymd(now);
        modal.classList.remove("hidden");
        loadMonth();
    }
    function close() { modal.classList.add("hidden"); }

    async function loadMonth() {
        $("cal-month").textContent = `${MONTHS[view.m]} ${view.y}`;
        const first = new Date(view.y, view.m, 1);
        const startDay = (first.getDay() + 6) % 7;           // Montag = 0
        const gridStart = new Date(view.y, view.m, 1 - startDay);
        const gridEnd = new Date(gridStart); gridEnd.setDate(gridStart.getDate() + 42);
        try {
            data = await apiGet(ymd(gridStart), ymd(gridEnd));
            $("cal-msg").textContent = ""; $("cal-msg").className = "hint";
            $("cal-user").textContent = data.username ? "· " + data.username : "";
        } catch (e) {
            $("cal-grid").innerHTML = ""; $("cal-day").innerHTML = "";
            $("cal-msg").className = "hint error";
            $("cal-msg").textContent = e.status === 401
                ? "Jarvis weiß noch nicht, wer du bist — sprich einmal kurz (Mikrofon), dann öffne den Kalender erneut."
                : "Fehler: " + e.message;
            return;
        }
        renderGrid(gridStart);
        renderExtras();
        if (selected) renderDay(selected);
    }

    function eventsByDay() {
        const map = {};
        for (const ev of data.events) {
            const day = (ev.start || "").slice(0, 10);
            (map[day] = map[day] || []).push(ev);
        }
        for (const k in map) map[k].sort((a, b) => (a.start || "").localeCompare(b.start || ""));
        return map;
    }

    function renderGrid(gridStart) {
        const grid = $("cal-grid"); grid.innerHTML = "";
        for (const w of WD) { const h = document.createElement("div"); h.className = "cal-wd"; h.textContent = w; grid.appendChild(h); }
        const byDay = eventsByDay();
        const today = ymd(new Date());
        for (let i = 0; i < 42; i++) {
            const d = new Date(gridStart); d.setDate(gridStart.getDate() + i);
            const ds = ymd(d);
            const cell = document.createElement("div");
            const wknd = (d.getDay() === 0 || d.getDay() === 6);
            cell.className = "cal-cell" + (d.getMonth() !== view.m ? " other" : "") + (ds === today ? " today" : "")
                + (ds === selected ? " selected" : "") + (wknd ? " weekend" : "");
            let html = `<span class="cal-num">${d.getDate()}</span>`;
            const evs = byDay[ds] || [];
            for (const ev of evs.slice(0, 3)) {
                const t = ev.all_day ? "" : (ev.start || "").slice(11, 16) + " ";
                html += `<div class="cal-chip ${ev.can_edit ? "" : "ext"}">${esc(t + ev.title)}</div>`;
            }
            if (evs.length > 3) html += `<div class="cal-more">+${evs.length - 3} mehr</div>`;
            cell.innerHTML = html;
            cell.onclick = () => { selected = ds; editing = null; loadMonth(); };
            grid.appendChild(cell);
        }
    }

    function calOptions() {
        // Selector-Werte für add/update: 'own' | 'common' | <username>
        return data.writable.map((c) => {
            let val = c.kind === "shared" ? "common" : (c.access === "owner" ? "own" : c.name.replace(/^Kalender\s+/, ""));
            return `<option value="${esc(val)}">${esc(c.name)}</option>`;
        }).join("") || '<option value="own">Mein Kalender</option>';
    }

    function renderDay(ds) {
        const box = $("cal-day");
        const byDay = eventsByDay();
        const evs = byDay[ds] || [];
        const dObj = new Date(ds + "T00:00");
        const head = dObj.toLocaleDateString("de-DE", { weekday: "long", day: "2-digit", month: "long", year: "numeric" });
        let html = `<h3>${head}</h3>`;
        if (!evs.length) html += `<p class="muted" style="font-size:12px">Keine Termine.</p>`;
        for (const ev of evs) {
            const t = ev.all_day ? "ganztägig" : (ev.start || "").slice(11, 16) + (ev.end ? "–" + ev.end.slice(11, 16) : "");
            const loc = ev.location ? ` · ${esc(ev.location)}` : "";
            const rec = ev.rrule ? " ↻" : "";
            const btns = ev.can_edit
                ? `<span><button class="small secondary" data-edit="${ev.id}">✎</button>
                   <button class="small" data-del="${ev.id}" style="background:var(--bad);color:#fff">✕</button></span>`
                : `<span class="ev-meta">[${esc(ev.calendar)}]</span>`;
            html += `<div class="cal-ev"><span><span class="ev-time">${t}</span> ${esc(ev.title)}
                <span class="ev-meta">${loc}${rec}</span></span>${btns}</div>`;
        }
        // Formular (Anlegen/Bearbeiten)
        const editEv = editing ? data.events.find((e) => e.id === editing) : null;
        html += `<div class="cal-form">
            <div class="cal-row" style="margin-bottom:6px">
                <input id="cf-title" placeholder="Titel" style="flex:1" value="${editEv ? esc(editEv.title) : ""}">
                <select id="cf-cal">${calOptions()}</select>
            </div>
            <div class="cal-row" style="margin-bottom:6px">
                <input id="cf-date" type="date" value="${editEv ? (editEv.start || "").slice(0, 10) : ds}">
                <input id="cf-start" type="time" value="${editEv && !editEv.all_day ? (editEv.start || "").slice(11, 16) : "09:00"}">
                <input id="cf-end" type="time" value="${editEv && editEv.end ? (editEv.end || "").slice(11, 16) : "10:00"}">
                <label class="muted" style="font-size:12px"><input type="checkbox" id="cf-allday" ${editEv && editEv.all_day ? "checked" : ""}> ganztägig</label>
            </div>
            <div class="cal-row">
                <input id="cf-loc" placeholder="Ort (optional)" style="flex:1" value="${editEv ? esc(editEv.location || "") : ""}">
                <select id="cf-rec">
                    <option value="none">einmalig</option><option value="daily">täglich</option>
                    <option value="weekly">wöchentlich</option><option value="monthly">monatlich</option>
                    <option value="yearly">jährlich</option></select>
                <button id="cf-save">${editEv ? "Aktualisieren" : "Hinzufügen"}</button>
                ${editEv ? '<button id="cf-cancel" class="secondary small">Abbrechen</button>' : ""}
            </div>
            <div id="cf-msg" class="hint"></div>
        </div>`;
        box.innerHTML = html;

        box.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
            if (!confirm("Termin austragen?")) return;
            try { await apiAction("delete_event", { event_id: +b.dataset.del }); editing = null; await loadMonth(); }
            catch (e) { $("cf-msg").textContent = "Fehler: " + e.message; }
        });
        box.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => { editing = +b.dataset.edit; renderDay(ds); });
        const cancel = $("cf-cancel"); if (cancel) cancel.onclick = () => { editing = null; renderDay(ds); };
        $("cf-save").onclick = saveEvent;
    }

    async function saveEvent() {
        const allday = $("cf-allday").checked;
        const date = $("cf-date").value;
        if (!$("cf-title").value.trim() || !date) { $("cf-msg").textContent = "Titel und Datum nötig."; return; }
        const args = {
            title: $("cf-title").value.trim(), calendar: $("cf-cal").value,
            location: $("cf-loc").value.trim(), all_day: allday,
            recurrence: $("cf-rec").value,
            start: allday ? date : `${date}T${$("cf-start").value || "09:00"}`,
            end: allday ? null : `${date}T${$("cf-end").value || "10:00"}`,
        };
        try {
            if (editing) { args.event_id = editing; await apiAction("update_event", args); editing = null; }
            else { await apiAction("add_event", args); }
            await loadMonth();
        } catch (e) { $("cf-msg").textContent = "Fehler: " + e.message; }
    }

    function renderExtras() {
        $("cal-ical").value = data.ical_link || "";
        const subs = $("cal-subs");
        subs.innerHTML = (data.subscriptions || []).map((s) =>
            `<div class="cal-sub"><span>${esc(s.name)} <span class="muted">(${s.events} Termine${s.last_error ? ", ⚠ Fehler" : ""})</span></span>
             <button class="small secondary" data-unsub="${esc(s.name)}">Entfernen</button></div>`).join("")
            || '<p class="muted" style="font-size:12px">Keine externen Kalender abonniert.</p>';
        subs.querySelectorAll("[data-unsub]").forEach((b) => b.onclick = async () => {
            try { await apiAction("unsubscribe_calendar", { name: b.dataset.unsub }); await loadMonth(); } catch (e) { alert(e.message); }
        });
    }

    // ── Events verdrahten ───────────────────────────────────────────────────
    document.addEventListener("click", (e) => {
        if (e.target && (e.target.id === "btn-calendar" || e.target.id === "btn-open-calendar")) {
            const prof = $("profile"); if (prof) prof.classList.add("hidden");
            open();
        }
    });
    $("cal-close").onclick = close;
    $("cal-prev").onclick = () => { view.m--; if (view.m < 0) { view.m = 11; view.y--; } selected = null; editing = null; loadMonth(); };
    $("cal-next").onclick = () => { view.m++; if (view.m > 11) { view.m = 0; view.y++; } selected = null; editing = null; loadMonth(); };
    $("cal-subbtn").onclick = async () => {
        const url = $("cal-suburl").value.trim(); if (!url) return;
        $("cal-subbtn").disabled = true;
        try { const r = await apiAction("subscribe_calendar", { url, name: $("cal-subname").value.trim() });
            $("cal-suburl").value = ""; $("cal-subname").value = ""; alert(r.message); await loadMonth(); }
        catch (e) { alert("Fehler: " + e.message); }
        $("cal-subbtn").disabled = false;
    };
    $("cal-icalcopy").onclick = () => { $("cal-ical").select(); try { navigator.clipboard.writeText($("cal-ical").value); } catch {} $("cal-icalcopy").textContent = "✓"; setTimeout(() => $("cal-icalcopy").textContent = "Kopieren", 1200); };
})();
