"""
Client zum Code-Sandbox-Container (deploy/sandbox).

Der Orchestrator führt KEINEN vom Agenten geschriebenen Code selbst aus — er reicht ihn
an den isolierten Sandbox-Dienst weiter (eigener Container, nie auf dem Host). Netzwerk
ist global per Admin-Toggle (sandbox_allow_network) ab-/zuschaltbar.
"""
from __future__ import annotations

import requests

import config


def _base() -> str:
    return config.get().get("sandbox_url", "http://127.0.0.1:8090").rstrip("/")


def available() -> bool:
    try:
        r = requests.get(_base() + "/health", timeout=2)
        return r.ok and r.json().get("ok", False)
    except Exception:
        return False


def execute(code: str, language: str = "python", namespace: str = "default",
            allow_network: bool | None = None) -> dict:
    cfg = config.get()
    if not cfg.get("sandbox_enabled", True):
        return {"ok": False, "stderr": "Code-Ausführung ist deaktiviert (Admin).", "disabled": True}
    # allow_network: None = globaler Admin-Toggle; True/False = expliziter Override (z.B. Watcher-Skripte).
    net = bool(cfg.get("sandbox_allow_network", True)) if allow_network is None else bool(allow_network)
    payload = {
        "language": language,
        "code": code,
        "namespace": namespace,
        "timeout": int(cfg.get("sandbox_timeout_s", 30)),
        "allow_network": net,
    }
    try:
        r = requests.post(_base() + "/exec", json=payload,
                          timeout=int(cfg.get("sandbox_timeout_s", 30)) + 15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"ok": False, "stderr": "Sandbox-Container nicht erreichbar (läuft `deploy/sandbox`?).",
                "offline": True}
    except Exception as e:
        return {"ok": False, "stderr": f"Sandbox-Fehler: {e}"}


def list_files(namespace: str = "default") -> list[dict]:
    try:
        r = requests.get(_base() + "/files", params={"namespace": namespace}, timeout=5)
        return r.json().get("files", [])
    except Exception:
        return []


def read_file(namespace: str, path: str) -> dict:
    try:
        r = requests.get(_base() + "/file", params={"namespace": namespace, "path": path}, timeout=5)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── browser_control ───────────────────────────────────────────────────────────
def _post(path: str, payload: dict, timeout: int = 45) -> dict:
    if not config.get().get("sandbox_enabled", True):
        return {"ok": False, "error": "Sandbox/Browser ist deaktiviert (Admin)."}
    try:
        r = requests.post(_base() + path, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Sandbox-Container nicht erreichbar (läuft `deploy/sandbox`?)."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def browser_goto(namespace: str, url: str) -> dict:
    return _post("/browser/goto", {"namespace": namespace, "url": url})


def browser_act(namespace: str, action: str, text: str = "", value: str = "", submit: bool = False) -> dict:
    return _post("/browser/act", {"namespace": namespace, "action": action,
                                  "text": text, "value": value, "submit": submit})


def browser_content(namespace: str) -> dict:
    return _post("/browser/content", {"namespace": namespace})


def browser_screenshot(namespace: str) -> dict:
    return _post("/browser/screenshot", {"namespace": namespace})


def browser_close(namespace: str) -> dict:
    return _post("/browser/close", {"namespace": namespace})
