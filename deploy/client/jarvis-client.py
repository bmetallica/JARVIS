#!/usr/bin/env python3
"""
SH-Jarvis Client-Agent (Thin-Client / Python-Sidecar).

Verbindet sich mit dem Orchestrator (`/ws/client`), meldet seine Capabilities und führt
vom Server angeforderte Aktionen LOKAL auf dem Nutzer-Rechner aus (Programme starten,
Fenster, Medien/Lautstärke, Dateien, Zwischenablage, Systeminfos).

Dies ist das Laufzeit-Fundament; die Tauri-Tray-GUI wird später drumherum gebaut.
Plattform: Linux zuerst (Best-Effort, externe Tools wmctrl/playerctl/pactl/xclip), Windows/macOS
in Vorbereitung (plattformspezifische Zweige unten).

Start:
    JARVIS_SERVER=wss://192.168.66.224:8088 JARVIS_CLIENT_NAME="Arbeits-PC" python3 jarvis-client.py
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import ssl
import subprocess
import uuid

import websockets

SERVER = os.environ.get("JARVIS_SERVER", "wss://192.168.66.224:8088").rstrip("/")
NAME = os.environ.get("JARVIS_CLIENT_NAME", platform.node() or "Client")
ALLOW_SHELL = os.environ.get("JARVIS_CLIENT_ALLOW_SHELL", "1") != "0"
VERIFY_TLS = os.environ.get("JARVIS_VERIFY_TLS", "0") == "1"
_DIR = os.path.dirname(os.path.abspath(__file__))
_SID_FILE = os.path.join(_DIR, ".client_id")
POLICY_FILE = os.environ.get("JARVIS_CLIENT_POLICY", os.path.join(_DIR, "policy.json"))
AUDIT_FILE = os.environ.get("JARVIS_CLIENT_AUDIT", os.path.join(_DIR, "audit.log"))
IS_LINUX, IS_WIN, IS_MAC = (platform.system() == s for s in ("Linux", "Windows", "Darwin"))

ALL_ACTIONS = [
    # Programme / Skripte
    "app.launch", "app.close", "shell.run", "open.url", "open.path",
    # Fenster / Desktop / Eingabe
    "window.list", "window.focus", "window.close", "window.minimize", "window.maximize",
    "screenshot", "input.type", "input.hotkey", "notify",
    # Medien / Lautstärke / System
    "media.play_pause", "media.next", "media.prev", "media.stop",
    "media.volume", "volume.up", "volume.down", "volume.mute",
    "system.info", "process.list", "system.lock", "system.suspend", "system.shutdown", "system.restart",
    # Dateien / Zwischenablage
    "fs.read", "fs.write", "fs.append", "fs.list", "fs.mkdir", "fs.move", "fs.copy", "fs.delete",
    "clipboard.get", "clipboard.set",
]

# ── Lokale Sicherheits-Policy (der Client ist die letzte Instanz) ─────────────────
# Entscheidung je Aktion: "allow" (sofort) · "ask" (Bestätigung nötig) · "deny" (gesperrt).
_DEFAULT_POLICY = {
    "default": "ask",
    "actions": {
        # anzeigen / lesen / unkritisch → allow
        "system.info": "allow", "fs.list": "allow", "window.list": "allow",
        "clipboard.get": "allow", "clipboard.set": "allow", "notify": "allow",
        "media.volume": "allow", "volume.up": "allow", "volume.down": "allow", "volume.mute": "allow",
        "media.play_pause": "allow", "media.next": "allow", "media.prev": "allow", "media.stop": "allow",
        "window.focus": "allow", "window.minimize": "allow", "window.maximize": "allow",
        "screenshot": "allow", "process.list": "allow",
        # mit Wirkung → Bestätigung
        "fs.read": "ask", "fs.write": "ask", "fs.append": "ask", "fs.mkdir": "ask",
        "fs.move": "ask", "fs.copy": "ask",
        "app.launch": "ask", "app.close": "ask", "open.url": "ask", "open.path": "ask",
        "shell.run": "ask", "system.lock": "ask", "system.suspend": "ask",
        "window.close": "ask", "input.type": "ask", "input.hotkey": "ask",
        # destruktiv → standardmäßig gesperrt (Nutzer kann in policy.json auf "ask" stellen)
        "fs.delete": "deny", "system.shutdown": "deny", "system.restart": "deny",
    },
    "fs_read_roots": [],      # leer = keine Pfadbeschränkung; sonst nur unterhalb dieser Verzeichnisse
    "fs_write_roots": [],
    "deny": [],               # hart gesperrte Aktionen (werden gar nicht erst angeboten)
}


def load_policy() -> dict:
    pol = json.loads(json.dumps(_DEFAULT_POLICY))   # tiefe Kopie
    try:
        user = json.load(open(POLICY_FILE))
        pol.update({k: user[k] for k in ("default", "fs_read_roots", "fs_write_roots", "deny") if k in user})
        pol["actions"].update(user.get("actions", {}))
    except FileNotFoundError:
        json.dump(_DEFAULT_POLICY, open(POLICY_FILE, "w"), indent=2)   # Vorlage anlegen
        print(f"[client] Policy-Vorlage angelegt: {POLICY_FILE}")
    except Exception as e:
        print(f"[client] Policy nicht lesbar ({e}) — nutze sichere Defaults.")
    if not ALLOW_SHELL:
        pol.setdefault("deny", []).append("shell.run")
    return pol


POLICY = load_policy()


def _under(path: str, roots: list) -> bool:
    if not roots:
        return True
    rp = os.path.realpath(os.path.expanduser(path))
    return any(rp == os.path.realpath(os.path.expanduser(r)) or
               rp.startswith(os.path.realpath(os.path.expanduser(r)) + os.sep) for r in roots)


def decide(action: str, p: dict) -> str:
    if action in POLICY.get("deny", []):
        return "deny"
    d = POLICY["actions"].get(action, POLICY.get("default", "ask"))
    if action == "fs.read" and not _under(p.get("path", ""), POLICY.get("fs_read_roots", [])):
        return "deny"
    if action in ("fs.write", "fs.append", "fs.mkdir", "fs.delete") \
            and not _under(p.get("path", ""), POLICY.get("fs_write_roots", [])):
        return "deny"
    if action in ("fs.move", "fs.copy") and not _under(p.get("dest", ""), POLICY.get("fs_write_roots", [])):
        return "deny"
    return d


def confirm(action: str, p: dict) -> bool:
    """Bestätigung einholen (zenity/kdialog/osascript/MessageBox/Terminal). Ohne Kanal → ablehnen."""
    msg = f"JARVIS möchte auf diesem Rechner ausführen:\n\n{action}\n{json.dumps(p, ensure_ascii=False)[:300]}\n\nErlauben?"
    try:
        if IS_LINUX and shutil.which("zenity"):
            return _run(["zenity", "--question", "--title=JARVIS", f"--text={msg}", "--timeout=60"], 70)[0] == 0
        if IS_LINUX and shutil.which("kdialog"):
            return _run(["kdialog", "--yesno", msg], 70)[0] == 0
        if IS_MAC:
            rc, out, _ = _run(["osascript", "-e",
                               f'display dialog {json.dumps(msg)} buttons {{"Ablehnen","Erlauben"}} default button "Ablehnen"'], 70)
            return "Erlauben" in out
        if IS_WIN:
            import ctypes
            return ctypes.windll.user32.MessageBoxW(0, msg, "JARVIS", 0x4 | 0x30) == 6  # MB_YESNO|ICON → IDYES=6
    except Exception:
        pass
    if os.isatty(0):
        try:
            return input(f"[JARVIS] {action} {p} erlauben? [j/N] ").strip().lower() in ("j", "ja", "y", "yes")
        except Exception:
            return False
    return False        # fail-safe: keine Möglichkeit zu fragen → ablehnen


def audit(action: str, p: dict, decision: str, ok=None) -> None:
    try:
        import datetime
        line = (f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{action}\t{decision}"
                f"\tok={ok}\t{json.dumps(p, ensure_ascii=False)[:200]}\n")
        open(AUDIT_FILE, "a").write(line)
    except Exception:
        pass


def _session_id() -> str:
    try:
        return open(_SID_FILE).read().strip() or uuid.uuid4().hex[:12]
    except FileNotFoundError:
        sid = "pc" + uuid.uuid4().hex[:10]
        open(_SID_FILE, "w").write(sid)
        return sid


def _have(*tools: str) -> bool:
    return all(shutil.which(t) for t in tools)


_NO_WINDOW = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW → keine aufpoppenden Konsolen auf Windows


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _ps(script: str, timeout: int = 25) -> tuple[int, str, str]:
    """PowerShell-Skript ausführen (Windows) — ohne Konsolenfenster."""
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


# ── Windows-Bordmittel (Win32 via ctypes + PowerShell), keine Zusatzpakete ────────
if IS_WIN:
    import ctypes
    from ctypes import wintypes
    _u32 = ctypes.windll.user32

    def _vk(code: int) -> None:                         # virtuelle Taste drücken (down+up)
        _u32.keybd_event(code, 0, 0, 0)
        _u32.keybd_event(code, 0, 2, 0)

    def _win_windows() -> list:                         # sichtbare Fenster mit Titel
        out = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(h, _l):
            if _u32.IsWindowVisible(h):
                n = _u32.GetWindowTextLengthW(h)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _u32.GetWindowTextW(h, buf, n + 1)
                    if buf.value.strip():
                        out.append((h, buf.value))
            return True
        _u32.EnumWindows(_cb, 0)
        return out

    def _win_find(title: str):
        t = (title or "").lower()
        for h, name in _win_windows():
            if t in name.lower():
                return h
        return None

    # Core-Audio (absolute Lautstärke / Mute) — dependency-frei via Add-Type
    _AUDIO_PS = r'''
$ErrorActionPreference="Stop"
if (-not ("AudioCtl" -as [type])) { Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAEV{int n1();int n2();int gc(out int c);int SetMasterVolumeLevel(float l,System.Guid g);
int SetMasterVolumeLevelScalar(float l,System.Guid g);int gml(out float l);int gmls(out float l);
int scvl(uint n,float l,System.Guid g);int scvls(uint n,float l,System.Guid g);int gcvl(uint n,out float l);
int gcvls(uint n,out float l);int SetMute([MarshalAs(UnmanagedType.Bool)]bool m,System.Guid g);int GetMute(out bool m);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMD{int Activate(ref System.Guid id,int c,System.IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDE{int n1();int GetDefaultAudioEndpoint(int f,int r,out IMMD e);}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDE{}
public class AudioCtl{
 static IAEV V(){var e=(IMMDE)(new MMDE());IMMD d;e.GetDefaultAudioEndpoint(0,1,out d);
  System.Guid i=typeof(IAEV).GUID;object o;d.Activate(ref i,1,System.IntPtr.Zero,out o);return (IAEV)o;}
 public static void SetVol(float v){V().SetMasterVolumeLevelScalar(v,System.Guid.Empty);}
 public static void SetMute(bool m){V().SetMute(m,System.Guid.Empty);}}
"@ }
'''


# ── Capabilities = alle Aktionen, die die Policy NICHT hart sperrt ────────────────
def capabilities() -> list[str]:
    deny = set(POLICY.get("deny", []))
    return [a for a in ALL_ACTIONS if a not in deny and POLICY["actions"].get(a, POLICY["default"]) != "deny"]


def gated_act(action: str, p: dict) -> dict:
    """Lokale Sicherheitsprüfung VOR der Ausführung — der Client ist die letzte Instanz.
    deny → ablehnen; ask → Nutzer bestätigen lassen; allow → ausführen. Alles ins Audit-Log."""
    d = decide(action, p)
    if d == "deny":
        audit(action, p, "deny")
        return {"ok": False, "error": "Von der lokalen Sicherheitsrichtlinie verweigert."}
    if d == "ask":
        if not confirm(action, p):
            audit(action, p, "ask→abgelehnt")
            return {"ok": False, "error": "Vom Nutzer am Gerät abgelehnt (oder keine Bestätigung möglich)."}
        audit(action, p, "ask→erlaubt")
    else:
        audit(action, p, "allow")
    res = act(action, p)
    audit(action, p, d + "/ausgeführt", res.get("ok"))
    return res


# ── Aktions-Handler ───────────────────────────────────────────────────────────
def act(action: str, p: dict) -> dict:
    try:
        if action == "app.launch":
            name = p.get("name") or p.get("path")
            if not name:
                return {"ok": False, "error": "Kein Programmname."}
            args = p.get("args") or []
            if IS_WIN:
                os.startfile(name)  # type: ignore[attr-defined]
            elif IS_MAC:
                subprocess.Popen(["open", "-a", name, *args])
            else:
                opener = shutil.which(name)
                subprocess.Popen([opener, *args] if opener else ["xdg-open", name])
            return {"ok": True, "result": f"{name} gestartet."}

        if action == "app.close":
            name = p.get("name", "")
            if not name:
                return {"ok": False, "error": "Kein Programmname."}
            if IS_WIN:
                _run(["taskkill", "/IM", name if name.endswith(".exe") else name + ".exe", "/F"])
            elif IS_MAC:
                _run(["pkill", "-x", name])
            else:
                _run(["pkill", "-f", name])
            return {"ok": True, "result": f"{name} beendet (sofern es lief)."}

        if action == "shell.run":
            if not ALLOW_SHELL:
                return {"ok": False, "error": "Shell-Aktionen sind auf diesem Client deaktiviert."}
            cmd = p.get("command", "")
            if not cmd:
                return {"ok": False, "error": "Kein Befehl."}
            rc, out, err = _run(["bash", "-lc", cmd] if not IS_WIN else ["cmd", "/c", cmd], 30)
            return {"ok": rc == 0, "result": (out or err)[:3000], "error": None if rc == 0 else err[:500]}

        if action == "open.url":
            import webbrowser
            url = p.get("url", "")
            if not url.startswith("http"):
                return {"ok": False, "error": "Ungültige URL."}
            webbrowser.open(url)
            return {"ok": True, "result": f"{url} geöffnet."}
        if action == "open.path":
            path = os.path.expanduser(p.get("path", ""))
            if not os.path.exists(path):
                return {"ok": False, "error": "Pfad existiert nicht."}
            if IS_WIN:
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                _run([("open" if IS_MAC else "xdg-open"), path])
            return {"ok": True, "result": f"{path} geöffnet."}

        if action == "screenshot":
            return _screenshot()
        if action == "input.type":
            return _input_type(p.get("text", ""))
        if action == "input.hotkey":
            return _input_hotkey(p.get("keys", ""))
        if action == "notify":
            return _notify(p.get("title", "JARVIS"), p.get("message") or p.get("text", ""))

        if action == "fs.read":
            with open(os.path.expanduser(p["path"]), "r", errors="replace") as f:
                return {"ok": True, "result": f.read(8000)}
        if action == "fs.write":
            path = os.path.expanduser(p["path"])
            with open(path, "w") as f:
                f.write(p.get("content", ""))
            return {"ok": True, "result": f"{path} geschrieben."}
        if action == "fs.append":
            with open(os.path.expanduser(p["path"]), "a") as f:
                f.write(p.get("content", ""))
            return {"ok": True, "result": "Angehängt."}
        if action == "fs.list":
            d = os.path.expanduser(p.get("path", "."))
            return {"ok": True, "result": ", ".join(sorted(os.listdir(d))[:200])}
        if action == "fs.mkdir":
            os.makedirs(os.path.expanduser(p["path"]), exist_ok=True)
            return {"ok": True, "result": "Verzeichnis angelegt."}
        if action in ("fs.move", "fs.copy"):
            import shutil as _sh
            src = os.path.expanduser(p.get("src", "")); dest = os.path.expanduser(p.get("dest", ""))
            if not src or not dest:
                return {"ok": False, "error": "src und dest erforderlich."}
            if action == "fs.move":
                _sh.move(src, dest)
            elif os.path.isdir(src):
                _sh.copytree(src, dest)
            else:
                _sh.copy2(src, dest)
            return {"ok": True, "result": f"{src} → {dest}"}
        if action == "fs.delete":
            path = os.path.expanduser(p["path"])
            if os.path.isdir(path):
                import shutil as _sh
                _sh.rmtree(path)
            else:
                os.remove(path)
            return {"ok": True, "result": f"{path} gelöscht."}

        if action == "clipboard.get":
            return {"ok": True, "result": _clipboard_get()}
        if action == "clipboard.set":
            _clipboard_set(p.get("text", ""))
            return {"ok": True, "result": "Zwischenablage gesetzt."}

        if action == "media.volume":
            return _set_volume(int(p.get("level", 50)))
        if action in ("volume.up", "volume.down"):
            return _volume_step(action == "volume.up")
        if action == "volume.mute":
            return _mute(bool(p.get("on", True)))
        if action in ("media.play_pause", "media.next", "media.prev", "media.stop"):
            return _media({"media.play_pause": "play-pause", "media.next": "next",
                           "media.prev": "previous", "media.stop": "stop"}[action])

        if action == "window.list":
            if IS_WIN:
                titles = [t for _h, t in _win_windows()]
                return {"ok": True, "result": "; ".join(titles[:40])}
            if IS_LINUX and _have("wmctrl"):
                _, out, _ = _run(["wmctrl", "-l"])
                titles = [ln.split(None, 3)[-1] for ln in out.splitlines() if ln]
                return {"ok": True, "result": "; ".join(titles[:40])}
            return {"ok": False, "error": "Fensterliste nicht verfügbar (wmctrl fehlt?)."}
        if action == "window.focus":
            title = p.get("title", "")
            if IS_WIN:
                h = _win_find(title)
                if not h:
                    return {"ok": False, "error": f"Kein Fenster mit „{title}“."}
                _u32.ShowWindow(h, 9); _u32.SetForegroundWindow(h)   # SW_RESTORE + Vordergrund
                return {"ok": True, "result": "Fokussiert."}
            if IS_LINUX and _have("wmctrl"):
                rc, _, err = _run(["wmctrl", "-a", title])
                return {"ok": rc == 0, "result": "Fokussiert.", "error": err or None}
            return {"ok": False, "error": "window.focus nicht verfügbar."}
        if action in ("window.close", "window.minimize", "window.maximize"):
            title = p.get("title", "")
            if IS_WIN:
                h = _win_find(title)
                if not h:
                    return {"ok": False, "error": f"Kein Fenster mit „{title}“."}
                if action == "window.close":
                    _u32.PostMessageW(h, 0x0010, 0, 0)               # WM_CLOSE
                else:
                    _u32.ShowWindow(h, 6 if action == "window.minimize" else 3)  # SW_MINIMIZE / SW_MAXIMIZE
                return {"ok": True, "result": action}
            if IS_LINUX and _have("wmctrl"):
                if action == "window.close":
                    rc = _run(["wmctrl", "-c", title])[0]
                else:
                    state = "add,hidden" if action == "window.minimize" else "add,maximized_vert,maximized_horz"
                    rc = _run(["wmctrl", "-r", title, "-b", state])[0]
                return {"ok": rc == 0, "result": action}
            return {"ok": False, "error": f"{action} nicht verfügbar (wmctrl fehlt?)."}

        if action == "system.info":
            return {"ok": True, "result": f"{platform.system()} {platform.release()}, Host {platform.node()}, "
                    f"Python {platform.python_version()}, CPUs {os.cpu_count()}"}
        if action == "process.list":
            if IS_WIN:
                _, out, _ = _run(["tasklist", "/fo", "csv", "/nh"], 15)
                names = {ln.split('","')[0].strip('"') for ln in out.splitlines() if ln.startswith('"')}
            else:
                _, out, _ = _run(["ps", ("-eo" if IS_LINUX else "-axo"), "comm="], 15)
                names = {os.path.basename(ln.strip()) for ln in out.splitlines() if ln.strip()}
            return {"ok": True, "result": ", ".join(sorted(names)[:80])}
        if action in ("system.shutdown", "system.restart"):
            restart = action == "system.restart"
            if IS_WIN:
                _run(["shutdown", "/r" if restart else "/s", "/t", "0"])
            elif IS_MAC:
                _run(["osascript", "-e", f'tell app "System Events" to {"restart" if restart else "shut down"}'])
            else:
                _run(["systemctl", "reboot" if restart else "poweroff"])
            return {"ok": True, "result": "Neustart eingeleitet." if restart else "Herunterfahren eingeleitet."}
        if action == "system.lock":
            if IS_LINUX:
                for c in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
                    if _have(c[0]) and _run(c)[0] == 0:
                        return {"ok": True, "result": "Gesperrt."}
            elif IS_WIN:
                _run(["rundll32.exe", "user32.dll,LockWorkStation"]); return {"ok": True, "result": "Gesperrt."}
            elif IS_MAC:
                _run(["pmset", "displaysleepnow"]); return {"ok": True, "result": "Gesperrt."}
            return {"ok": False, "error": "Sperren nicht verfügbar."}
        if action == "system.suspend":
            if IS_LINUX and _have("systemctl"):
                _run(["systemctl", "suspend"]); return {"ok": True, "result": "Standby."}
            if IS_MAC:
                _run(["pmset", "sleepnow"]); return {"ok": True, "result": "Standby."}
            if IS_WIN:
                _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]); return {"ok": True, "result": "Standby."}
            return {"ok": False, "error": "Standby nicht verfügbar."}

        return {"ok": False, "error": f"Unbekannte Aktion: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _clipboard_get() -> str:
    if IS_WIN:
        return _ps("Get-Clipboard -Raw")[1]
    if IS_LINUX and _have("xclip"):
        return _run(["xclip", "-selection", "clipboard", "-o"])[1]
    if IS_MAC:
        return _run(["pbpaste"])[1]
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        return ""


def _clipboard_set(text: str) -> None:
    if IS_WIN:
        lit = "'" + (text or "").replace("'", "''") + "'"
        _ps("Set-Clipboard -Value " + lit)
        return
    if IS_LINUX and _have("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, creationflags=_NO_WINDOW)
        return
    if IS_MAC:
        subprocess.run(["pbcopy"], input=text, text=True)
        return
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception:
        pass


def _set_volume(level: int) -> dict:
    level = max(0, min(100, level))
    if IS_WIN:
        rc, _, err = _ps(_AUDIO_PS + f"[AudioCtl]::SetVol({level / 100.0})")
        return {"ok": rc == 0, "result": f"Lautstärke {level}%.", "error": err[:200] or None}
    if IS_LINUX and _have("pactl"):
        rc = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])[0]
        return {"ok": rc == 0, "result": f"Lautstärke {level}%."}
    if IS_LINUX and _have("amixer"):
        rc = _run(["amixer", "-M", "set", "Master", f"{level}%"])[0]
        return {"ok": rc == 0, "result": f"Lautstärke {level}%."}
    if IS_MAC:
        _run(["osascript", "-e", f"set volume output volume {level}"]); return {"ok": True, "result": f"Lautstärke {level}%."}
    return {"ok": False, "error": "Lautstärkeregelung nicht verfügbar."}


def _volume_step(up: bool) -> dict:
    if IS_WIN:
        _vk(0xAF if up else 0xAE); return {"ok": True, "result": "lauter" if up else "leiser"}
    if IS_LINUX and _have("pactl"):
        _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", ("+5%" if up else "-5%")])
        return {"ok": True, "result": "lauter" if up else "leiser"}
    if IS_MAC:
        _run(["osascript", "-e", f"set volume output volume (output volume of (get volume settings) {'+' if up else '-'} 6)"])
        return {"ok": True, "result": "lauter" if up else "leiser"}
    return {"ok": False, "error": "Lautstärke-Schritt nicht verfügbar."}


def _media(cmd: str) -> dict:
    if IS_WIN:
        vk = {"play-pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}.get(cmd)
        if vk:
            _vk(vk); return {"ok": True, "result": cmd}
    if IS_LINUX and _have("playerctl"):
        rc = _run(["playerctl", cmd])[0]
        return {"ok": rc == 0, "result": cmd}
    if IS_MAC and cmd in ("play-pause", "next", "previous"):
        key = {"play-pause": 16, "next": 17, "previous": 18}[cmd]
        _run(["osascript", "-e", f'tell application "System Events" to key code {key}'])
        return {"ok": True, "result": cmd}
    return {"ok": False, "error": "Mediensteuerung nicht verfügbar (playerctl fehlt?)."}


def _mute(on: bool) -> dict:
    if IS_WIN:
        rc, _, err = _ps(_AUDIO_PS + f"[AudioCtl]::SetMute(${'true' if on else 'false'})")
        return {"ok": rc == 0, "result": "Stumm" if on else "Ton an", "error": err[:200] or None}
    if IS_LINUX and _have("pactl"):
        _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if on else "0"])
        return {"ok": True, "result": "Stumm" if on else "Ton an"}
    if IS_LINUX and _have("amixer"):
        _run(["amixer", "-M", "set", "Master", "mute" if on else "unmute"])
        return {"ok": True, "result": "Stumm" if on else "Ton an"}
    if IS_MAC:
        _run(["osascript", "-e", f"set volume {'with' if on else 'without'} output muted"])
        return {"ok": True, "result": "Stumm" if on else "Ton an"}
    return {"ok": False, "error": "Stummschaltung nicht verfügbar."}


def _screenshot() -> dict:
    """Bildschirmfoto → base64-data-URI (für die Vision-Pipeline)."""
    import base64
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        ok = False
        if IS_LINUX:
            for tool in (["grim", path], ["scrot", "-o", path], ["import", "-window", "root", path],
                         ["gnome-screenshot", "-f", path]):
                if _have(tool[0]) and _run(tool, 15)[0] == 0:
                    ok = True
                    break
        elif IS_MAC:
            ok = _run(["screencapture", "-x", path], 15)[0] == 0
        elif IS_WIN:
            ps = f"Add-Type -AssemblyName System.Windows.Forms,System.Drawing; " \
                 f"$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; " \
                 f"$bmp=New-Object Drawing.Bitmap $b.Width,$b.Height; " \
                 f"$g=[Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen(0,0,0,0,$bmp.Size); " \
                 f"$bmp.Save('{path}')"
            ok = _run(["powershell", "-Command", ps], 20)[0] == 0
        if not ok or not os.path.getsize(path):
            return {"ok": False, "error": "Screenshot-Werkzeug nicht verfügbar."}
        data = open(path, "rb").read()
        return {"ok": True, "result": "data:image/png;base64," + base64.b64encode(data).decode()}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _sendkeys(seq: str) -> tuple[int, str, str]:
    lit = "'" + seq.replace("'", "''") + "'"
    return _ps("Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait(" + lit + ")")


def _input_type(text: str) -> dict:
    if not text:
        return {"ok": False, "error": "Kein Text."}
    if IS_WIN:
        esc = "".join("{" + c + "}" if c in "+^%~(){}[]" else c for c in text).replace("\n", "{ENTER}")
        rc, _, err = _sendkeys(esc)
        return {"ok": rc == 0, "result": "getippt", "error": err[:200] or None}
    if IS_LINUX and _have("xdotool"):
        _run(["xdotool", "type", "--clearmodifiers", text]); return {"ok": True, "result": "getippt"}
    if IS_MAC:
        _run(["osascript", "-e", f'tell application "System Events" to keystroke {json.dumps(text)}'])
        return {"ok": True, "result": "getippt"}
    return {"ok": False, "error": "Tastatureingabe nicht verfügbar (xdotool fehlt?)."}


def _input_hotkey(keys: str) -> dict:
    if not keys:
        return {"ok": False, "error": "Keine Tastenkombination."}
    if IS_WIN:
        parts = [k.strip().lower() for k in keys.split("+")]
        if "win" in parts or "super" in parts:
            return {"ok": False, "error": "Win-Tastenkombinationen werden auf Windows (SendKeys) nicht unterstützt."}
        mods = {"ctrl": "^", "control": "^", "strg": "^", "alt": "%", "shift": "+"}
        named = {"enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}", "escape": "{ESC}",
                 "space": " ", "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
                 "delete": "{DEL}", "entf": "{DEL}", "home": "{HOME}", "end": "{END}", "backspace": "{BACKSPACE}"}
        pre = "".join(mods[k] for k in parts if k in mods)
        key = next((k for k in parts if k not in mods), "")
        seq = pre + named.get(key, key)
        rc, _, err = _sendkeys(seq)
        return {"ok": rc == 0, "result": keys, "error": err[:200] or None}
    if IS_LINUX and _have("xdotool"):
        _run(["xdotool", "key", keys.replace("+", "+")]); return {"ok": True, "result": keys}
    return {"ok": False, "error": "Hotkeys nicht verfügbar (xdotool fehlt?)."}


def _notify(title: str, message: str) -> dict:
    if IS_WIN:
        t = "'" + (title or "").replace("'", "''") + "'"
        m = "'" + (message or "").replace("'", "''") + "'"
        ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
              "$n=New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
              f"$n.ShowBalloonTip(5000,{t},{m},[System.Windows.Forms.ToolTipIcon]::Info); "
              "Start-Sleep -Seconds 6; $n.Dispose()")
        rc, _, err = _ps(ps, timeout=12)
        return {"ok": rc == 0, "result": "Benachrichtigt.", "error": err[:200] or None}
    if IS_LINUX and _have("notify-send"):
        _run(["notify-send", title, message]); return {"ok": True, "result": "Benachrichtigt."}
    if IS_MAC:
        _run(["osascript", "-e", f'display notification {json.dumps(message)} with title {json.dumps(title)}'])
        return {"ok": True, "result": "Benachrichtigt."}
    print(f"[notify] {title}: {message}")
    return {"ok": True, "result": "Benachrichtigt (Konsole)."}


# ── Verbindung ──────────────────────────────────────────────────────────────────
async def run() -> None:
    sid = _session_id()
    url = SERVER + "/ws/client"
    ssl_ctx = None
    if url.startswith("wss"):
        ssl_ctx = ssl.create_default_context()
        if not VERIFY_TLS:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
    while True:
        try:
            async with websockets.connect(url, ssl=ssl_ctx, max_size=8_000_000) as ws:
                await ws.send(json.dumps({"type": "hello", "session_id": sid, "name": NAME,
                                          "capabilities": capabilities(), "fw": "client-1.0"}))
                print(f"[client] verbunden mit {url} als {NAME!r} (Session {sid})")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") == "action":
                        res = await asyncio.to_thread(gated_act, msg.get("action", ""), msg.get("params") or {})
                        await ws.send(json.dumps({"type": "action_result", "id": msg.get("id"), **res}))
                    elif msg.get("type") == "welcome":
                        print("[client] registriert.")
        except Exception as e:
            print(f"[client] Verbindung verloren ({e}) — neuer Versuch in 3 s")
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
