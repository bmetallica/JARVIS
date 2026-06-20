"""
JARVIS Code-Sandbox — interner Ausführungsdienst (läuft NUR im Container, nie auf dem Host).

Der Orchestrator schickt Code hierher; die Sandbox führt ihn isoliert aus und gibt
stdout/stderr/Exit-Code + neu erzeugte Dateien zurück. Pro Nutzer-Namespace gibt es ein
eigenes Workspace-Verzeichnis (persistente Dateien zwischen Läufen).

Isolation/Schutz:
  • eigener Container, Nicht-root-Nutzer, CPU-/Dateigrößen-/Prozess-Limits (setrlimit)
  • Timeout + Kill der gesamten Prozessgruppe
  • Netzwerk pro Job abschaltbar: bei allow_network=false läuft der Job in einer eigenen
    Netz-Namespace (unshare -rn) → nur loopback, kein Internet.
"""
from __future__ import annotations

import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="JARVIS Sandbox")

WORKSPACE_ROOT = Path("/workspace")
MAX_OUTPUT = 20000          # stdout/stderr je auf so viele Zeichen kappen
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
CPU_SECONDS = 60            # harte CPU-Zeit pro Job
FSIZE_BYTES = 50 * 1024 * 1024   # max. Dateigröße, die ein Job schreiben darf
NPROC = 128


def _ns_dir(namespace: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", namespace or "default")[:40] or "default"
    d = WORKSPACE_ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot(d: Path) -> dict[str, float]:
    out = {}
    for p in d.rglob("*"):
        if p.is_file():
            try:
                out[str(p.relative_to(d))] = p.stat().st_mtime
            except OSError:
                pass
    return out


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_BYTES, FSIZE_BYTES))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
    except (ValueError, OSError):
        pass


class ExecReq(BaseModel):
    language: str = "python"          # python | shell
    code: str
    namespace: str = "default"
    timeout: int = DEFAULT_TIMEOUT
    allow_network: bool = True


@app.get("/health")
def health():
    return {"ok": True, "service": "jarvis-sandbox"}


# ── Abhängigkeiten (apt/pip) installieren — persistiert im Volume, Re-Install beim Start ──
DEPS_MANIFEST = WORKSPACE_ROOT / ".deps.json"


def _read_manifest() -> dict:
    import json
    try:
        return json.loads(DEPS_MANIFEST.read_text())
    except Exception:
        return {"apt": [], "pip": []}


def _install_pkgs(apt: list, pip: list) -> dict:
    log, ok = [], True
    is_root = (os.geteuid() == 0)
    if pip:
        cmd = ["pip", "install", "--no-cache-dir"] + ([] if is_root else ["--user"]) + list(pip)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = ok and r.returncode == 0
        log.append(f"pip {' '.join(pip)} → rc={r.returncode}\n" + (r.stderr or r.stdout)[-600:])
    if apt:
        if is_root:
            subprocess.run(["apt-get", "update"], capture_output=True, text=True, timeout=300)
            r = subprocess.run(["apt-get", "install", "-y", "--no-install-recommends", *apt],
                               capture_output=True, text=True, timeout=900)
            ok = ok and r.returncode == 0
            log.append(f"apt {' '.join(apt)} → rc={r.returncode}\n" + (r.stderr or r.stdout)[-600:])
        else:
            ok = False
            log.append(f"apt {' '.join(apt)} NICHT möglich (nicht root — nur in der privilegierten Spur).")
    return {"ok": ok, "log": "\n".join(log) or "nichts zu tun"}


class InstallReq(BaseModel):
    apt: list[str] = []
    pip: list[str] = []


@app.post("/install")
def install(req: InstallReq):
    import json
    m = _read_manifest()
    m["apt"] = sorted(set(m.get("apt", []) + (req.apt or [])))
    m["pip"] = sorted(set(m.get("pip", []) + (req.pip or [])))
    try:
        DEPS_MANIFEST.write_text(json.dumps(m))
    except Exception:
        pass
    return _install_pkgs(req.apt or [], req.pip or [])


@app.on_event("startup")
def _reinstall_on_boot():
    m = _read_manifest()
    if m.get("apt") or m.get("pip"):
        try:
            _install_pkgs(m.get("apt", []), m.get("pip", []))
        except Exception as e:
            print(f"[sandbox] Re-Install beim Start fehlgeschlagen: {e}")


@app.post("/exec")
def execute(req: ExecReq):
    work = _ns_dir(req.namespace)
    timeout = max(1, min(int(req.timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))

    # Code in temporäre Datei im Workspace schreiben
    suffix = ".py" if req.language == "python" else ".sh"
    fd, script = tempfile.mkstemp(suffix=suffix, dir=str(work))
    os.write(fd, req.code.encode("utf-8")); os.close(fd)

    if req.language == "python":
        cmd = ["python", "-I", script]
    else:
        cmd = ["bash", script]
    # Netz-Isolation: ohne Internet in eigener Netz-Namespace ausführen (nur loopback)
    if not req.allow_network:
        cmd = ["unshare", "-rn", "--"] + cmd

    before = _snapshot(work)
    t0 = time.time()
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=_limits, start_new_session=True,
            env={**os.environ, "HOME": os.environ.get("HOME", "/home/sandbox"),
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
            rc = -1
    except FileNotFoundError as e:
        return {"ok": False, "stdout": "", "stderr": f"Ausführung fehlgeschlagen: {e}",
                "exit_code": 127, "duration_ms": 0, "files": [], "timed_out": False}
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass

    after = _snapshot(work)
    changed = sorted(p for p, m in after.items()
                     if p != Path(script).name and (p not in before or before[p] != m))
    files = []
    for rel in changed[:50]:
        try:
            files.append({"path": rel, "bytes": (work / rel).stat().st_size})
        except OSError:
            pass

    def _clip(b: bytes) -> str:
        s = b.decode("utf-8", "replace")
        return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + f"\n…[gekürzt, {len(s)} Zeichen]"

    return {
        "ok": (rc == 0 and not timed_out),
        "stdout": _clip(out), "stderr": _clip(err),
        "exit_code": rc, "timed_out": timed_out,
        "duration_ms": int((time.time() - t0) * 1000),
        "files": files,
    }


def _safe_path(namespace: str, rel: str) -> Path | None:
    work = _ns_dir(namespace)
    target = (work / (rel or "")).resolve()
    return target if str(target).startswith(str(work.resolve())) else None


@app.get("/files")
def list_files(namespace: str = "default"):
    work = _ns_dir(namespace)
    return {"files": [{"path": str(p.relative_to(work)), "bytes": p.stat().st_size}
                      for p in sorted(work.rglob("*")) if p.is_file()][:200]}


@app.get("/file")
def read_file(namespace: str = "default", path: str = ""):
    target = _safe_path(namespace, path)
    if not target or not target.is_file():
        return {"ok": False, "error": "Datei nicht gefunden."}
    data = target.read_bytes()[: MAX_OUTPUT * 2]
    return {"ok": True, "path": path, "content": data.decode("utf-8", "replace"),
            "bytes": target.stat().st_size}


@app.get("/file_b64")
def read_file_b64(namespace: str = "default", path: str = ""):
    """Datei BINÄR (Base64) zurückgeben — für Bilder/PDFs etc. (max. 12 MB)."""
    import base64
    target = _safe_path(namespace, path)
    if not target or not target.is_file():
        return {"ok": False, "error": "Datei nicht gefunden."}
    data = target.read_bytes()
    if len(data) > 12 * 1024 * 1024:
        return {"ok": False, "error": "Datei zu groß (>12 MB)."}
    return {"ok": True, "name": target.name, "bytes": len(data),
            "b64": base64.b64encode(data).decode()}


class WriteReq(BaseModel):
    namespace: str = "default"
    path: str
    content: str


@app.post("/file")
def write_file(req: WriteReq):
    target = _safe_path(req.namespace, req.path)
    if not target:
        return {"ok": False, "error": "Ungültiger Pfad."}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(req.content, encoding="utf-8")
    return {"ok": True, "path": req.path, "bytes": target.stat().st_size}


class ResetReq(BaseModel):
    namespace: str = "default"


@app.post("/reset")
def reset(req: ResetReq):
    work = _ns_dir(req.namespace)
    shutil.rmtree(work, ignore_errors=True)
    _ns_dir(req.namespace)
    return {"ok": True}


# ── browser_control (headless Chromium via Playwright) ────────────────────────────
_pw = None
_ctx: dict = {}                 # namespace -> persistent context (Cookies/Logins bleiben erhalten)
_page: dict = {}                # namespace -> aktuelle Seite


async def _get_page(ns: str):
    global _pw
    from playwright.async_api import async_playwright
    if _pw is None:
        _pw = await async_playwright().start()
    if ns not in _ctx:
        udir = _ns_dir(ns) / ".browser"
        _ctx[ns] = await _pw.chromium.launch_persistent_context(
            str(udir), headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        _page[ns] = _ctx[ns].pages[0] if _ctx[ns].pages else await _ctx[ns].new_page()
    return _page[ns]


async def _page_snapshot(page, max_chars: int = 4000) -> dict:
    try:
        text = " ".join((await page.inner_text("body")).split())
    except Exception:
        text = ""
    links = []
    try:
        for a in await page.query_selector_all("a[href]"):
            t = (await a.inner_text() or "").strip()
            href = await a.get_attribute("href")
            if t and href and href.startswith("http"):
                links.append({"text": t[:60], "href": href})
            if len(links) >= 25:
                break
    except Exception:
        pass
    return {"title": await page.title(), "url": page.url, "text": text[:max_chars], "links": links}


class BrowseReq(BaseModel):
    namespace: str = "default"
    url: str


@app.post("/browser/goto")
async def browser_goto(req: BrowseReq):
    try:
        page = await _get_page(req.namespace)
        await page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(800)
        return {"ok": True, **await _page_snapshot(page)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ActReq(BaseModel):
    namespace: str = "default"
    action: str                       # click | type | press
    text: str = ""                    # Klicktext / Feld-Label / Taste
    value: str = ""                   # Eingabewert (bei type)
    submit: bool = False              # nach type Enter drücken


@app.post("/browser/act")
async def browser_act(req: ActReq):
    try:
        page = await _get_page(req.namespace)
        if req.action == "click":
            try:
                await page.get_by_text(req.text, exact=False).first.click(timeout=8000)
            except Exception:
                await page.click(req.text, timeout=8000)        # Fallback: CSS-Selektor
        elif req.action == "type":
            loc = None
            for getter in (lambda: page.get_by_label(req.text),
                           lambda: page.get_by_placeholder(req.text),
                           lambda: page.locator(req.text)):
                try:
                    loc = getter(); await loc.first.fill(req.value, timeout=4000); break
                except Exception:
                    loc = None
            if loc is None:
                return {"ok": False, "error": f"Feld „{req.text}“ nicht gefunden."}
            if req.submit:
                await loc.first.press("Enter")
        elif req.action == "press":
            await page.keyboard.press(req.text or "Enter")
        else:
            return {"ok": False, "error": f"Unbekannte Aktion: {req.action}"}
        await page.wait_for_timeout(800)
        return {"ok": True, **await _page_snapshot(page)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class NsReq(BaseModel):
    namespace: str = "default"


@app.post("/browser/content")
async def browser_content(req: NsReq):
    if req.namespace not in _page:
        return {"ok": False, "error": "Keine offene Seite."}
    return {"ok": True, **await _page_snapshot(_page[req.namespace])}


@app.post("/browser/screenshot")
async def browser_screenshot(req: NsReq):
    if req.namespace not in _page:
        return {"ok": False, "error": "Keine offene Seite."}
    import base64
    png = await _page[req.namespace].screenshot(full_page=False)
    return {"ok": True, "image": "data:image/png;base64," + base64.b64encode(png).decode()}


@app.post("/browser/close")
async def browser_close(req: NsReq):
    c = _ctx.pop(req.namespace, None)
    _page.pop(req.namespace, None)
    if c:
        try:
            await c.close()
        except Exception:
            pass
    return {"ok": True}
