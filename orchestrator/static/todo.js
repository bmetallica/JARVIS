"use strict";
// JARVIS To-do (nutzerseitig, Identität serverseitig aus der Session/Stimme).
(function () {
    const $ = (id) => document.getElementById(id);
    const modal = $("todo");
    const sid = () => localStorage.getItem("jarvis_session_id") || "";
    const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    let items = [];

    async function apiGet() {
        const r = await fetch(`/api/me/todos?session_id=${encodeURIComponent(sid())}`);
        if (!r.ok) { const d = await r.json().catch(() => ({})); const e = new Error(d.detail || r.statusText); e.status = r.status; throw e; }
        return r.json();
    }
    async function post(path, body) {
        const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sid(), ...body }) });
        if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || r.statusText); }
        return r.json();
    }

    async function load() {
        try {
            const d = await apiGet();
            $("todo-msg").textContent = ""; $("todo-msg").className = "hint";
            $("todo-user").textContent = d.username ? "· " + d.username : "";
            $("todo-link").value = d.link || "";
            items = d.items || [];
            render();
        } catch (e) {
            $("todo-list").innerHTML = ""; $("todo-msg").className = "hint error";
            $("todo-msg").textContent = e.status === 401
                ? "Jarvis weiß noch nicht, wer du bist — sprich einmal kurz (Mikrofon), dann öffne die To-do erneut."
                : "Fehler: " + e.message;
        }
    }

    function render() {
        const el = $("todo-list");
        const open = items.filter((t) => !t.done), done = items.filter((t) => t.done);
        if (!items.length) { el.innerHTML = '<p class="muted" style="font-size:13px">Noch nichts auf der Liste.</p>'; return; }
        const row = (t) => `<div class="todo-item ${t.done ? "done" : ""}">
            <div class="todo-box" data-tg="${t.id}">${t.done ? "✓" : ""}</div>
            <span class="todo-text">${esc(t.text)}</span>
            ${t.due_date ? `<span class="todo-badge">📅 ${t.due_date}</span>` : ""}
            <button class="todo-del" data-del="${t.id}" title="Löschen">✕</button></div>`;
        el.innerHTML = open.map(row).join("")
            + (done.length ? `<p class="muted" style="font-size:11px;margin:10px 0 2px">Erledigt</p>` + done.map(row).join("") : "");
        el.querySelectorAll("[data-tg]").forEach((b) => b.onclick = async () => {
            const t = items.find((x) => x.id === +b.dataset.tg);
            try { await post("/api/me/todos/toggle", { id: t.id, done: !t.done }); await load(); }
            catch (e) { $("todo-msg").textContent = "Fehler: " + e.message; }
        });
        el.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
            try { await post("/api/me/todos/remove", { id: +b.dataset.del }); await load(); }
            catch (e) { $("todo-msg").textContent = "Fehler: " + e.message; }
        });
    }

    async function add() {
        const text = $("todo-new").value.trim();
        if (!text) return;
        try {
            await post("/api/me/todos/add", { text, due: $("todo-due").value || null });
            $("todo-new").value = ""; $("todo-due").value = ""; await load();
        } catch (e) { $("todo-msg").textContent = "Fehler: " + e.message; }
    }

    document.addEventListener("click", (e) => {
        if (e.target && (e.target.id === "btn-todo" || e.target.id === "btn-open-todo")) {
            const prof = $("profile"); if (prof) prof.classList.add("hidden");
            modal.classList.remove("hidden"); load();
        }
    });
    $("todo-close").onclick = () => modal.classList.add("hidden");
    $("todo-add").onclick = add;
    $("todo-new").addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    $("todo-linkcopy").onclick = () => { $("todo-link").select(); try { navigator.clipboard.writeText($("todo-link").value); } catch {} $("todo-linkcopy").textContent = "✓"; setTimeout(() => $("todo-linkcopy").textContent = "Kopieren", 1200); };
})();
