// SH-Jarvis Desktop-Client (Tauri v2) — Tray + Sidecar-Supervisor + Einstellungs-/Policy-UI.
// Die eigentliche Logik (WS, Aktionen, lokale Sicherheits-Policy) liegt im Python-Sidecar
// (deploy/client/jarvis-client.py). Diese App startet/überwacht den Sidecar und bietet die GUI.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, State};

fn default_python() -> String {
    if cfg!(windows) { "python".into() } else { "python3".into() }
}

#[derive(Serialize, Deserialize, Clone)]
struct Settings {
    server: String,
    name: String,
    allow_shell: bool,
    verify_tls: bool,
    python_cmd: String,
    script_path: String,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            server: "wss://192.168.66.224:8088".into(),
            name: hostname(),
            allow_shell: true,
            verify_tls: false,
            python_cmd: if cfg!(windows) { "python".into() } else { "python3".into() },
            script_path: String::new(),
        }
    }
}

fn hostname() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "Mein-PC".into())
}

struct AppState {
    config_dir: PathBuf,
    sidecar: Mutex<Option<Child>>,
    bundled_py: PathBuf,   // mitgelieferte jarvis-client.py (Ressource)
    bundled_bin: PathBuf,  // optional mitgelieferte PyInstaller-Binary (Ressource)
}

impl AppState {
    fn config_file(&self) -> PathBuf { self.config_dir.join("config.json") }
    fn policy_file(&self) -> PathBuf { self.config_dir.join("policy.json") }
    fn audit_file(&self) -> PathBuf { self.config_dir.join("audit.log") }

    fn load_settings(&self) -> Settings {
        fs::read_to_string(self.config_file())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }
    fn save_settings(&self, s: &Settings) -> Result<(), String> {
        fs::create_dir_all(&self.config_dir).map_err(|e| e.to_string())?;
        fs::write(self.config_file(), serde_json::to_string_pretty(s).unwrap())
            .map_err(|e| e.to_string())
    }
}

fn spawn_sidecar(state: &AppState) {
    // alten Prozess beenden
    if let Some(mut c) = state.sidecar.lock().unwrap().take() {
        let _ = c.kill();
    }
    let s = state.load_settings();

    // Zielprogramm bestimmen — NICHTS muss manuell konfiguriert werden:
    //   1) manueller Override aus den Einstellungen (script_path gesetzt)
    //   2) mitgelieferte PyInstaller-Binary (kein Python nötig)
    //   3) mitgeliefertes jarvis-client.py + Python
    let (program, script): (String, Option<String>) = if !s.script_path.trim().is_empty() {
        if s.python_cmd.trim().is_empty() {
            (s.script_path.clone(), None)
        } else {
            (s.python_cmd.clone(), Some(s.script_path.clone()))
        }
    } else if state.bundled_bin.exists() {
        (state.bundled_bin.to_string_lossy().into_owned(), None)
    } else if state.bundled_py.exists() {
        let py = if s.python_cmd.trim().is_empty() { default_python() } else { s.python_cmd.clone() };
        (py, Some(state.bundled_py.to_string_lossy().into_owned()))
    } else {
        eprintln!("[supervisor] Kein Sidecar gefunden (weder mitgeliefert noch konfiguriert).");
        return;
    };

    let mut cmd = Command::new(&program);
    if let Some(sc) = &script {
        cmd.arg(sc);
    }
    // Windows: Sidecar OHNE Konsolenfenster starten (läuft unsichtbar im Hintergrund).
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let child = cmd
        .env("JARVIS_SERVER", &s.server)
        .env("JARVIS_CLIENT_NAME", &s.name)
        .env("JARVIS_CLIENT_ALLOW_SHELL", if s.allow_shell { "1" } else { "0" })
        .env("JARVIS_VERIFY_TLS", if s.verify_tls { "1" } else { "0" })
        .env("JARVIS_CLIENT_POLICY", state.policy_file())
        .env("JARVIS_CLIENT_AUDIT", state.audit_file())
        .spawn();
    match child {
        Ok(c) => { *state.sidecar.lock().unwrap() = Some(c); }
        Err(e) => eprintln!("[supervisor] Sidecar-Start fehlgeschlagen: {e}"),
    }
}

// ── Commands (vom Frontend via invoke) ───────────────────────────────────────────
#[tauri::command]
fn get_settings(state: State<AppState>) -> Settings { state.load_settings() }

#[tauri::command]
fn save_settings(state: State<AppState>, settings: Settings) -> Result<(), String> {
    state.save_settings(&settings)?;
    spawn_sidecar(state.inner());
    Ok(())
}

#[tauri::command]
fn get_policy(state: State<AppState>) -> String {
    fs::read_to_string(state.policy_file())
        .unwrap_or_else(|_| "(Policy wird beim ersten Start des Sidecars erzeugt.)".into())
}

#[tauri::command]
fn save_policy(state: State<AppState>, text: String) -> Result<(), String> {
    serde_json::from_str::<serde_json::Value>(&text).map_err(|e| format!("Ungültiges JSON: {e}"))?;
    fs::create_dir_all(&state.config_dir).map_err(|e| e.to_string())?;
    fs::write(state.policy_file(), text).map_err(|e| e.to_string())?;
    spawn_sidecar(state.inner()); // Policy neu laden
    Ok(())
}

#[tauri::command]
fn read_audit(state: State<AppState>) -> String {
    let txt = fs::read_to_string(state.audit_file()).unwrap_or_default();
    txt.lines().rev().take(100).collect::<Vec<_>>().join("\n")
}

#[tauri::command]
fn status(state: State<AppState>) -> bool {
    let mut g = state.sidecar.lock().unwrap();
    if let Some(c) = g.as_mut() {
        matches!(c.try_wait(), Ok(None)) // None = läuft noch
    } else {
        false
    }
}

#[tauri::command]
fn restart_sidecar(state: State<AppState>) { spawn_sidecar(state.inner()); }

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let dir = app.path().app_config_dir().expect("config dir");
            fs::create_dir_all(&dir).ok();
            // Sidecar-Binary (Tauri externalBin) liegt NEBEN der App-Exe (Triple wird zur Laufzeit entfernt).
            let exe_dir = std::env::current_exe().ok()
                .and_then(|p| p.parent().map(|d| d.to_path_buf())).unwrap_or_default();
            let bundled_bin = exe_dir.join(if cfg!(windows) { "jarvis-client.exe" } else { "jarvis-client" });
            app.manage(AppState {
                config_dir: dir, sidecar: Mutex::new(None),
                bundled_py: PathBuf::new(), bundled_bin,
            });

            // Tray-Menü
            let open_i = MenuItem::with_id(app, "open", "Öffnen", true, None::<&str>)?;
            let restart_i = MenuItem::with_id(app, "restart", "Sidecar neu starten", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Beenden", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open_i, &restart_i, &quit_i])?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("SH-Jarvis Client")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => { if let Some(w) = app.get_webview_window("main") { let _ = w.show(); let _ = w.set_focus(); } }
                    "restart" => { spawn_sidecar(app.state::<AppState>().inner()); }
                    "quit" => {
                        if let Some(mut c) = app.state::<AppState>().sidecar.lock().unwrap().take() { let _ = c.kill(); }
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            // Sidecar wird mitgeliefert → direkt starten. Fenster nur beim ERSTSTART zeigen
            // (config.json fehlt noch), damit der Nutzer Orchestrator-URL/Name setzen kann.
            let state = app.state::<AppState>();
            let first_run = !state.config_file().exists();
            spawn_sidecar(state.inner());
            if first_run {
                if let Some(w) = app.get_webview_window("main") { let _ = w.show(); }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // Schließen → nur verstecken (App lebt im Tray weiter)
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_settings, save_settings, get_policy, save_policy, read_audit, status, restart_sidecar
        ])
        .run(tauri::generate_context!())
        .expect("Fehler beim Start der SH-Jarvis Desktop-App");
}
