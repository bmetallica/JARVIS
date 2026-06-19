const { invoke } = window.__TAURI__.core;
const $ = (id) => document.getElementById(id);

// Tabs
document.querySelectorAll("nav .tab").forEach((b) => b.onclick = () => {
  document.querySelectorAll("nav .tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll("main .panel").forEach((p) =>
    p.classList.toggle("hidden", p.dataset.tab !== b.dataset.tab));
  if (b.dataset.tab === "policy") loadPolicy();
  if (b.dataset.tab === "audit") loadAudit();
});

const SET_FIELDS = ["server", "name", "python_cmd", "script_path"];
const SET_BOOLS = ["allow_shell", "verify_tls"];

async function loadSettings() {
  const s = await invoke("get_settings");
  for (const k of SET_FIELDS) $(k).value = s[k] ?? "";
  for (const k of SET_BOOLS) $(k).checked = !!s[k];
}
$("save-settings").onclick = async () => {
  const s = {};
  for (const k of SET_FIELDS) s[k] = $(k).value.trim();
  for (const k of SET_BOOLS) s[k] = $(k).checked;
  try {
    await invoke("save_settings", { settings: s });
    $("settings-status").textContent = "✓ gespeichert, Sidecar neu gestartet";
  } catch (e) { $("settings-status").textContent = "Fehler: " + e; }
  setTimeout(refreshStatus, 800);
};

async function loadPolicy() { $("policy").value = await invoke("get_policy"); }
$("reload-policy").onclick = loadPolicy;
$("save-policy").onclick = async () => {
  try {
    await invoke("save_policy", { text: $("policy").value });
    $("policy-status").textContent = "✓ gespeichert";
  } catch (e) { $("policy-status").textContent = "" + e; }
};

async function loadAudit() { $("audit").textContent = (await invoke("read_audit")) || "(noch keine Einträge)"; }
$("reload-audit").onclick = loadAudit;

async function refreshStatus() {
  const ok = await invoke("status");
  const el = $("status");
  el.textContent = ok ? "● Sidecar läuft" : "○ Sidecar gestoppt";
  el.className = "status " + (ok ? "ok" : "off");
}

loadSettings();
refreshStatus();
setInterval(refreshStatus, 4000);
