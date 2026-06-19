"use strict";

async function load() {
    const el = document.getElementById("downloads");
    let items = [];
    try {
        items = (await (await fetch("/api/downloads")).json()).items;
    } catch (e) {
        el.textContent = "Fehler beim Laden: " + e.message;
        return;
    }
    el.innerHTML = "";
    for (const it of items) {
        const div = document.createElement("div");
        div.className = "item";
        const badge = it.available
            ? `<a class="dlbtn" href="${it.url}">⬇ Herunterladen</a>`
            : `<span class="tag" style="background:#243446;color:var(--muted)">in Vorbereitung</span>`;
        div.innerHTML = `<div class="head"><span class="name">${it.name}</span>${badge}</div>
            <div class="muted" style="font-size:12px;margin-top:6px">${it.desc}</div>
            ${it.available && it.id === "satellite-pi"
                ? '<div class="muted" style="font-size:11px;margin-top:8px">Installation: entpacken → <code>sudo ./install.sh</code> (fragt Orchestrator-URL + Raumname ab).</div>'
                : ""}`;
        el.appendChild(div);
    }
}
load();
