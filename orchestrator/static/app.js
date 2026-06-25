"use strict";

const chatEl   = document.getElementById("chat");
const inputEl  = document.getElementById("input");
const sendBtn  = document.getElementById("btn-send");
const micBtn   = document.getElementById("btn-mic");
const ttsOn    = document.getElementById("tts-on");
const hintEl   = document.getElementById("hint");
const player   = document.getElementById("player");

let busy = false;          // Verlauf führt jetzt der Server pro Session
let currentAbort = null;   // AbortController des laufenden Streams (Barge-in #12)
let ttsStop = false;       // Signal: laufende Sprachausgabe sofort stoppen (Barge-in #12)

const hud = new JarvisHUD(document.getElementById("hud"));
hud.setState("IDLE");

// ── Session + WebSocket (quellen-bezogenes I/O-Routing) ──────────────────────
let sessionId = localStorage.getItem("jarvis_session_id") || null;
let ws = null;

function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
        ws.send(JSON.stringify({ type: "hello", session_id: sessionId, client_type: "browser" }));
    };
    ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "welcome") {
            sessionId = msg.session_id;
            localStorage.setItem("jarvis_session_id", sessionId);
        } else if (msg.type === "timer_alarm") {
            addMsg("system", "⏰ " + msg.message);
            enqueueTTS(msg.message);           // Alarm wird HIER (an der Quelle) ausgesprochen
        } else if (msg.type === "notify") {
            const pre = msg.automation ? `🤖 ${msg.automation}: ` : "🔔 ";
            addMsg("system", pre + msg.message);
            enqueueTTS(msg.message);           // proaktive Meldung (Automatisierung/Ereignis)
        } else if (msg.type === "attachment" && msg.data_uri) {
            const div = addMsg("assistant", msg.caption || "");   // Bild aus dem Workspace anzeigen
            const img = document.createElement("img");
            img.src = msg.data_uri;
            img.alt = msg.name || "Bild";
            img.style.cssText = "max-width:100%;border-radius:8px;margin-top:6px;display:block";
            div.appendChild(img);
            chatEl.scrollTop = chatEl.scrollHeight;
        }
    };
    ws.onclose = () => setTimeout(connectWS, 2000);   // Reconnect
    ws.onerror = () => { try { ws.close(); } catch {} };
}
connectWS();

// ── UI-Helfer ───────────────────────────────────────────────────────────────

function addMsg(role, text, meta) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    const who = role === "user" ? "Du" : role === "assistant" ? "Jarvis" : "System";
    const metaHtml = meta ? ` · <span class="meta">${meta}</span>` : "";
    div.innerHTML = `<span class="who">${who}${metaHtml}</span>`;
    div.appendChild(document.createTextNode(text));
    chatEl.appendChild(div);
    chatEl.scrollTop = chatEl.scrollHeight;
    return div;
}

function setHint(text, isError = false) {
    hintEl.textContent = text || "";
    hintEl.className = "hint" + (isError ? " error" : "");
}

function setBusy(b) {
    busy = b;
    sendBtn.disabled = b;
    inputEl.disabled = b;
}

// ── Chat-Flow ─────────────────────────────────────────────────────────────────

function parseSSE(raw) {
    let event = "message", data = "";
    for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    try { return { event, data: JSON.parse(data) }; } catch { return null; }
}

// Satzweise TTS-Warteschlange (Reihenfolge bleibt erhalten)
let ttsQueue = [], ttsPlaying = false;
function enqueueTTS(text) { ttsQueue.push(text); pumpTTS(); }
function playBlob(blob) {
    return new Promise((res) => {
        player.src = URL.createObjectURL(blob);
        player.onended = res; player.onerror = res;
        player.play().catch(res);
    });
}
async function pumpTTS() {
    if (ttsPlaying) return;
    ttsPlaying = true; hud.setState("SPEAKING");
    while (ttsQueue.length) {
        if (ttsStop) break;                      // Barge-in: Wiedergabe abbrechen
        const t = ttsQueue.shift();
        try {
            const res = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t }) });
            if (res.ok && !ttsStop) await playBlob(await res.blob());
        } catch { /* ignore */ }
    }
    ttsPlaying = false; ttsStop = false; hud.setState("IDLE");
}

// Barge-in (#12): laufende Antwort + Sprachausgabe sofort stoppen und den Turn serverseitig abbrechen.
function bargeIn() {
    ttsStop = true;
    ttsQueue = [];
    try { player.pause(); player.currentTime = 0; } catch { /* ignore */ }
    if (currentAbort) { try { currentAbort.abort(); } catch { /* ignore */ } }
    if (sessionId) {
        fetch("/api/chat/cancel", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId }),
        }).catch(() => {});
    }
    hud.setState("IDLE");
}

async function sendMessage(text, meta) {
    text = (text || "").trim();
    if (!text || busy) return;
    addMsg("user", text, meta);
    inputEl.value = "";
    setBusy(true);
    hud.setState("THINKING");
    setHint("Jarvis denkt nach…");

    const bubble = addMsg("assistant", "");
    const tts = ttsOn.checked;
    ttsStop = false;
    let full = "";
    currentAbort = new AbortController();
    try {
        const res = await fetch("/api/chat/stream", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text, session_id: sessionId }),
            signal: currentAbort.signal,
        });
        if (!res.ok || !res.body) throw new Error("Stream-Fehler " + res.status);
        const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let i;
            while ((i = buf.indexOf("\n\n")) >= 0) {
                const ev = parseSSE(buf.slice(0, i)); buf = buf.slice(i + 2);
                if (!ev) continue;
                if (ev.event === "sentence") {
                    full += (full ? " " : "") + ev.data.text;
                    bubble.lastChild.textContent = full;
                    chatEl.scrollTop = chatEl.scrollHeight;
                    if (tts) enqueueTTS(ev.data.text);
                    setHint("");
                } else if (ev.event === "status") {            // #11 Fortschritt anzeigen
                    setHint(ev.data.text);
                } else if (ev.event === "cancelled") {         // #12 Barge-in bestätigt
                    if (!full) bubble.lastChild.textContent = "(abgebrochen)";
                    setHint("Abgebrochen.");
                } else if (ev.event === "done") {
                    if (!full && ev.data.content) {
                        full = ev.data.content; bubble.lastChild.textContent = full;
                        if (tts) enqueueTTS(full);
                    }
                    setHint("");
                } else if (ev.event === "error") {
                    setHint("Fehler: " + ev.data.detail, true);
                }
            }
        }
        if (!full) { bubble.lastChild.textContent = "(keine Antwort)"; setHint("Leere Antwort — nochmal versuchen.", true); }
    } catch (e) {
        if (e.name === "AbortError") {           // Barge-in: kein Fehler, Nutzer hat unterbrochen
            if (!full) bubble.lastChild.textContent = "(abgebrochen)";
            setHint("Abgebrochen.");
        } else {
            setHint("Fehler: " + e.message, true);
        }
    } finally {
        currentAbort = null;
        setBusy(false);
        inputEl.focus();
        if (!tts) hud.setState("IDLE");     // bei TTS regelt pumpTTS den Zustand
    }
}

// ── Mikrofon → STT ──────────────────────────────────────────────────────────

let mediaRecorder = null;
let chunks = [];

async function toggleMic() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
        mediaRecorder.stop();
        return;
    }
    // Barge-in (#12): spricht/antwortet Jarvis noch, beim Mic-Klick sofort unterbrechen und zuhören.
    if (busy || ttsPlaying) bargeIn();
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream);
        chunks = [];
        mediaRecorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
        mediaRecorder.onstop = async () => {
            stream.getTracks().forEach((t) => t.stop());
            micBtn.classList.remove("recording");
            const blob = new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" });
            await transcribeAndSend(blob);
        };
        mediaRecorder.start();
        micBtn.classList.add("recording");
        hud.setState("LISTENING");
        setHint("Aufnahme läuft – nochmal klicken zum Stoppen.");
    } catch (e) {
        hud.setState("IDLE");
        setHint("Mikrofon nicht verfügbar: " + e.message, true);
    }
}

async function transcribeAndSend(blob) {
    setBusy(true);
    hud.setState("THINKING");
    setHint("Transkribiere…");
    try {
        const fd = new FormData();
        fd.append("file", blob, "audio.webm");
        if (sessionId) fd.append("session_id", sessionId);
        const res = await fetch("/api/stt", { method: "POST", body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
        const { text, speaker } = await res.json();
        if (speaker) lastSpeaker = speaker;
        setBusy(false);
        const meta = speaker
            ? `🎙 ${speaker.username} · ${Math.round(speaker.confidence * 100)} %`
            : "🎙 nicht erkannt";
        if (text) sendMessage(text, meta);
        else { hud.setState("IDLE"); setHint("Nichts erkannt."); }
    } catch (e) {
        setBusy(false);
        hud.setState("IDLE");
        setHint("STT-Fehler: " + e.message, true);
    }
}

// ── Health-Status ─────────────────────────────────────────────────────────────

async function refreshHealth() {
    try {
        const res = await fetch("/health");
        const data = await res.json();
        const s = data.services || {};
        for (const k of ["llm", "stt", "tts"]) {
            const dot = document.getElementById("dot-" + k);
            if (dot) dot.className = "dot " + (s[k] && s[k].ok ? "ok" : "bad");
        }
    } catch { /* ignore */ }
}

// ── Profil & Stimme (Selbstbedienung) ──────────────────────────────────────────
let lastSpeaker = null;     // zuletzt per Stimme erkannte Person
const profileEl = document.getElementById("profile");

function openProfile() {
    document.getElementById("prof-current").textContent = lastSpeaker ? lastSpeaker.username : "niemand";
    document.getElementById("prof-rec-status").textContent = "";
    document.getElementById("prof-create-status").textContent = "";
    profileEl.classList.remove("hidden");
}

async function recordVoiceProfile() {
    const st = document.getElementById("prof-rec-status");
    if (!lastSpeaker) { st.textContent = "Erst sprechen (oder Erst-Enrollment im Admin-Bereich)."; return; }
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const rec = new MediaRecorder(stream); const chunks = [];
        rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
        rec.onstop = async () => {
            stream.getTracks().forEach((t) => t.stop());
            st.textContent = "Verarbeite…";
            try {
                const fd = new FormData();
                fd.append("file", new Blob(chunks, { type: rec.mimeType || "audio/webm" }), "voice.webm");
                fd.append("session_id", sessionId || "");
                const r = await fetch("/api/voice/enroll-self", { method: "POST", body: fd });
                if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
                const d = await r.json();
                st.textContent = `✓ Stimmprofil für ${d.username} gespeichert (${d.samples} Aufnahmen).`;
            } catch (e) { st.textContent = "Fehler: " + e.message; }
        };
        rec.start(); st.textContent = "🔴 Aufnahme… (5 s) — sprich einen Satz";
        setTimeout(() => { if (rec.state === "recording") rec.stop(); }, 5000);
    } catch (e) { st.textContent = "Mikrofon nicht verfügbar: " + e.message; }
}

async function createUserBasic() {
    const st = document.getElementById("prof-create-status");
    const username = document.getElementById("prof-newuser").value.trim();
    if (!username) return;
    try {
        const r = await fetch("/api/users/create-basic", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, session_id: sessionId }),
        });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
        st.textContent = `✓ Nutzer „${username}“ angelegt (passwortlos).`;
        document.getElementById("prof-newuser").value = "";
    } catch (e) { st.textContent = "Fehler: " + e.message; }
}

// ── Events ──────────────────────────────────────────────────────────────────

sendBtn.addEventListener("click", () => sendMessage(inputEl.value));
inputEl.addEventListener("keydown", (e) => { if (e.key === "Enter") sendMessage(inputEl.value); });
micBtn.addEventListener("click", toggleMic);
document.getElementById("btn-profile").addEventListener("click", openProfile);
document.getElementById("btn-rec-voice").addEventListener("click", recordVoiceProfile);
document.getElementById("btn-create-user").addEventListener("click", createUserBasic);
document.getElementById("btn-prof-close").addEventListener("click", () => profileEl.classList.add("hidden"));

// Wissensbasis-Upload
const fileKnow = document.getElementById("file-knowledge");
document.getElementById("btn-knowledge").addEventListener("click", () => fileKnow.click());
fileKnow.addEventListener("change", async () => {
    const f = fileKnow.files[0];
    if (!f) return;
    setHint(`Lade „${f.name}“ in die Wissensbasis…`);
    try {
        const fd = new FormData();
        fd.append("file", f, f.name);
        const res = await fetch("/api/knowledge/upload", { method: "POST", body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
        const d = await res.json();
        addMsg("system", `📚 „${d.source}“ zur Wissensbasis hinzugefügt (${d.chunks} Abschnitte).`);
        setHint("");
    } catch (e) {
        setHint("Upload-Fehler: " + e.message, true);
    }
    fileKnow.value = "";
});

const fileVision = document.getElementById("file-vision");
document.getElementById("btn-vision").addEventListener("click", () => fileVision.click());
fileVision.addEventListener("change", async () => {
    const f = fileVision.files[0];
    if (!f) return;
    const question = inputEl.value.trim();
    addMsg("user", (question || "Bild zur Analyse") + ` 📷 (${f.name})`);
    inputEl.value = "";
    setHint("Analysiere Bild…");
    try {
        const fd = new FormData();
        fd.append("file", f, f.name);
        fd.append("question", question);
        fd.append("session_id", sessionId || "");
        const res = await fetch("/api/vision", { method: "POST", body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
        const d = await res.json();
        addMsg("assistant", d.answer);
        if (document.getElementById("tts-on").checked) enqueueTTS(d.answer);
        setHint("");
    } catch (e) {
        setHint("Bildanalyse-Fehler: " + e.message, true);
    }
    fileVision.value = "";
});

addMsg("system", "Jarvis ist online. Tippe oder klicke auf das Mikrofon.");
refreshHealth();
setInterval(refreshHealth, 15000);
