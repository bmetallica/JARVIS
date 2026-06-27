/*
 * jarvis-client.js — schlanker SDK-Wrapper um das JARVIS Plugin-Gateway (/api/v1/*).
 * Läuft im Browser (PWA) wie in Node. Vollständige Spezifikation: ../../pluginsystem.md.
 *
 *   const jv = new JarvisClient("https://192.168.66.224:8088", "jvp_adhs_…", "Vater");
 *   await jv.storagePatch("tamagotchi", "state", { $inc: { xp: 50 } }, "shared");
 */
class JarvisClient {
  constructor(host, apiKey, user = null) {
    this.base = host.replace(/\/$/, "") + "/api/v1";
    this.wsBase = this.base.replace(/^http/, "ws");
    this.key = apiKey;
    this.user = user;
  }

  _headers(extra = {}) {
    const h = { "Authorization": `Bearer ${this.key}`, ...extra };
    if (this.user) h["X-JARVIS-User"] = this.user;
    return h;
  }

  async _json(path, { method = "GET", body = null } = {}) {
    const opt = { method, headers: this._headers(body ? { "Content-Type": "application/json" } : {}) };
    if (body) opt.body = JSON.stringify(body);
    const r = await fetch(this.base + path, opt);
    if (!r.ok) throw new Error(`${method} ${path} → ${r.status}: ${await r.text()}`);
    return r.json();
  }

  // ── Identität ───────────────────────────────────────────────────────────────
  me() { return this._json("/me"); }

  // ── Inference ───────────────────────────────────────────────────────────────
  chat(messages, { system_prompt, think } = {}) {
    return this._json("/inference/chat", { method: "POST", body: { messages, system_prompt, think } });
  }
  structure(text, schema) {
    return this._json("/inference/structure", { method: "POST", body: { text, schema } });
  }
  agent(task, allow_tools) {
    return this._json("/inference/agent", { method: "POST", body: { task, allow_tools } });
  }
  // SSE-Streaming (satzweise) — onSentence(text) je Satz, gibt finalen Text zurück
  async chatStream(messages, onSentence, { system_prompt, think } = {}) {
    const r = await fetch(this.base + "/inference/chat", {
      method: "POST", headers: this._headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ messages, system_prompt, think, stream: true }),
    });
    const reader = r.body.getReader(); const dec = new TextDecoder();
    let buf = "", final = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n"); buf = parts.pop();
      for (const p of parts) {
        const ev = p.match(/event: (\w+)/)?.[1]; const data = p.match(/data: (.*)/s)?.[1];
        if (!data) continue; const o = JSON.parse(data);
        if (ev === "sentence") { onSentence?.(o.text); final += o.text + " "; }
        if (ev === "done") final = o.content || final;
      }
    }
    return final.trim();
  }

  // ── Vision ──────────────────────────────────────────────────────────────────
  visionAnalyze(imageDataUrl, question) {
    return this._json("/vision/analyze", { method: "POST", body: { image: imageDataUrl, question } });
  }
  visionOcr(imageDataUrl) { return this._json("/vision/ocr", { method: "POST", body: { image: imageDataUrl } }); }
  visionClassify(imageDataUrl, labels) {
    return this._json("/vision/classify", { method: "POST", body: { image: imageDataUrl, labels } });
  }

  // ── Audio ───────────────────────────────────────────────────────────────────
  async stt(fileOrBlob, language = "") {
    const fd = new FormData(); fd.append("file", fileOrBlob); if (language) fd.append("language", language);
    const r = await fetch(this.base + "/audio/stt", { method: "POST", headers: this._headers(), body: fd });
    if (!r.ok) throw new Error("stt " + r.status); return r.json();
  }
  async tts(text, { voice } = {}) {                       // → Blob (audio)
    const r = await fetch(this.base + "/audio/tts", {
      method: "POST", headers: this._headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ text, voice }) });
    if (!r.ok) throw new Error("tts " + r.status); return r.blob();
  }

  // ── RAG ─────────────────────────────────────────────────────────────────────
  ragInsert(collection, content, metadata = {}) {
    return this._json("/rag/insert", { method: "POST", body: { collection, content, metadata } });
  }
  ragQuery(collection, query, limit = 5) {
    return this._json("/rag/query", { method: "POST", body: { collection, query, limit } });
  }

  // ── Storage (scope: "user" | "shared") ───────────────────────────────────────
  storageGet(coll, key, scope = "user") { return this._json(`/storage/${coll}/${key}?scope=${scope}`); }
  storagePut(coll, key, value, scope = "user") {
    return this._json(`/storage/${coll}/${key}?scope=${scope}`, { method: "PUT", body: { value } });
  }
  storagePatch(coll, key, patch, scope = "user") {
    return this._json(`/storage/${coll}/${key}?scope=${scope}`, { method: "PATCH", body: patch });
  }
  storageDelete(coll, key, scope = "user") {
    return this._json(`/storage/${coll}/${key}?scope=${scope}`, { method: "DELETE" });
  }
  storageList(coll, { prefix = "", scope = "user" } = {}) {
    return this._json(`/storage/${coll}?scope=${scope}&prefix=${encodeURIComponent(prefix)}`);
  }

  // ── Notify / Tools / Scheduler ───────────────────────────────────────────────
  notify(text, { channels = ["auto"], speak = false, kind = "reminder", meta = {} } = {}) {
    return this._json("/channels/notify", { method: "POST", body: { text, channels, speak, kind, meta } });
  }
  invokeTool(name, args = {}) {
    return this._json(`/tools/${name}/invoke`, { method: "POST", body: { args } });
  }
  scheduleJob(title, schedule, action) {
    return this._json("/scheduler/jobs", { method: "POST", body: { title, schedule, action } });
  }
  listJobs() { return this._json("/scheduler/jobs"); }

  // ── Event-WebSocket ──────────────────────────────────────────────────────────
  // Browser: globales WebSocket. Node: `npm i ws` und WS via opts.WebSocket injizieren.
  connectEvents(topics, onEvent, opts = {}) {
    const WS = opts.WebSocket || (typeof WebSocket !== "undefined" ? WebSocket : globalThis.WebSocket);
    const ws = new WS(`${this.wsBase}/ws?token=${this.key}`);
    ws.onopen = () => ws.send(JSON.stringify({ op: "subscribe", topics }));
    ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.op === "event") onEvent(m.topic, m.payload); };
    return {
      ws,
      publish: (topic, payload) => ws.send(JSON.stringify({ op: "publish", topic, payload })),
      close: () => ws.close(),
    };
  }
}

if (typeof module !== "undefined") module.exports = { JarvisClient };
