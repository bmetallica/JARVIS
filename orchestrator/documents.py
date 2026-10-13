"""
Dokumente lesen (PDF) — allgemeine Fähigkeit, nicht auf einen Anwendungsfall zugeschnitten
(phase1.md, Baustein 1).

Aufbau genau wie der Bild-Pfad: der Text wird seitenweise geholt; hat eine Seite kaum Text
(typisch für Scans und Fotos von Papier), wird sie als BILD gerendert und vom Vision-Modell
gelesen — derselbe `services.vision_call`, der auch Telegram-Fotos auswertet.

Warum pypdfium2 und nicht PyMuPDF: PyMuPDF steht unter AGPL, pypdfium2 unter Apache-2.0/BSD
(PDFium selbst BSD-3). Für ein Projekt, das man weitergeben kann, ist das der Unterschied.

Grenzen stehen alle in der Config (`pdf_*`), weil Seitenzahl und Zahl der per Vision gelesenen
Scan-Seiten direkt auf Antwortzeit und GPU-Last durchschlagen: eine Scan-Seite kostet einen
vollen Vision-Aufruf (~5-15 s), eine Textseite praktisch nichts.
"""
from __future__ import annotations

import base64
import io
import re
import time

import debug
import services

# Weniger echte Textzeichen auf einer Seite → als Scan behandeln und per Vision lesen.
MIN_CHARS_PER_PAGE = 40

# Frage ans Vision-Modell für eine gerenderte Seite: WÖRTLICH wiedergeben, nicht zusammenfassen.
_OCR_PROMPT = (
    "Das ist eine Seite aus einem Dokument. Transkribiere ALLE darauf lesbaren Texte WÖRTLICH "
    "und vollständig auf Deutsch — Überschriften, Listen, Tabellen (zeilenweise), Zahlen, "
    "Mengenangaben, Handschrift, Beschriftungen. Erfinde nichts, lasse nichts weg, fasse nicht "
    "zusammen und kommentiere nicht. Ist die Seite leer, antworte genau: (leer)"
)

_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")


def is_pdf(filename: str = "", mime: str = "", data: bytes | None = None) -> bool:
    """PDF erkennen — Dateiname, MIME-Typ oder die Signatur selbst (Telegram liefert mal das eine,
    mal das andere; manche Clients schicken application/octet-stream)."""
    if (filename or "").lower().endswith(".pdf"):
        return True
    if "pdf" in (mime or "").lower():
        return True
    return bool(data) and data[:5] == b"%PDF-"


def available() -> str:
    """Leerer String = einsatzbereit, sonst der Grund (für klare Meldungen statt Tracebacks)."""
    try:
        import pypdfium2  # noqa: F401
        return ""
    except Exception as e:                                   # pragma: no cover - Installationsfrage
        return f"pypdfium2 ist nicht installiert ({e}) — `pip install pypdfium2`."


def _clean(text: str) -> str:
    """Harmlose Normalisierung: Mehrfach-Leerzeichen und Leerzeilenwüsten kosten nur Tokens."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    return _NL_RE.sub("\n\n", text).strip()


def _page_png(page, scale: float) -> bytes:
    """Seite als PNG rendern (scale 2.08 ≈ 150 dpi — genug für Vision-OCR, ohne das Bild zu sprengen)."""
    bitmap = page.render(scale=scale)
    buf = io.BytesIO()
    bitmap.to_pil().save(buf, format="PNG")
    return buf.getvalue()


def _open_error(e: Exception) -> str:
    """Öffnen fehlgeschlagen → verständlicher Grund statt Traceback."""
    msg = str(e).lower()
    if "password" in msg or "encrypt" in msg:
        return ("Das PDF ist passwortgeschützt — ich kann es nicht lesen. "
                "Bitte ohne Passwort erneut schicken.")
    return f"Das PDF ist beschädigt oder kein gültiges PDF ({str(e)[:120]})."


def extract_pdf(data: bytes, cfg: dict | None = None, page_from: int = 1,
                page_to: int | None = None, ocr: bool = True,
                ocr_budget: int | None = None) -> dict:
    """PDF lesen (phase1.md Baustein 1).

    Rückgabe:
      pages       Gesamtzahl der Seiten im Dokument
      text        gelesener Text mit [Seite n]-Marken
      page_texts  [{"page", "text", "scanned"}] je gelesener Seite
      ocr_pages   Seitennummern, die per Vision gelesen wurden
      title       Titel aus den PDF-Metadaten (oft leer)
      read_from/read_to/truncated   welcher Bereich tatsächlich gelesen wurde
      error       leer = alles gut, sonst die Meldung für den Nutzer

    `page_from`/`page_to` sind 1-basiert und inklusiv (so, wie Menschen Seiten zählen).
    `ocr=False` liest nur die Textebene — für Fälle, in denen Tempo zählt.
    Blockierend (PDFium + Vision-Aufrufe) → immer in einem Thread aufrufen.
    """
    cfg = cfg or {}
    empty = {"pages": 0, "text": "", "page_texts": [], "ocr_pages": [], "title": "",
             "read_from": 0, "read_to": 0, "truncated": False}
    err = available()
    if err:
        return {**empty, "error": err}
    import pypdfium2 as pdfium

    max_pages = int(cfg.get("pdf_max_pages", 40) or 40)
    min_chars = int(cfg.get("pdf_scan_min_chars", MIN_CHARS_PER_PAGE) or MIN_CHARS_PER_PAGE)
    scale = float(cfg.get("pdf_render_scale", 2.08) or 2.08)
    budget = int(cfg.get("pdf_ocr_max_pages", 8) or 0) if ocr_budget is None else int(ocr_budget)

    t0 = time.time()
    try:
        pdf = pdfium.PdfDocument(data)
        n_pages = len(pdf)
    except Exception as e:
        return {**empty, "error": _open_error(e)}

    try:
        try:
            title = (pdf.get_metadata_value("Title") or "").strip()
        except Exception:
            title = ""
        if not n_pages:
            return {**empty, "title": title, "error": "Das PDF enthält keine Seiten."}

        first = max(1, int(page_from or 1))
        if first > n_pages:
            return {**empty, "pages": n_pages, "title": title,
                    "error": f"Das Dokument hat nur {n_pages} Seiten."}
        last = n_pages if page_to is None else min(int(page_to), n_pages)
        last = max(last, first)
        truncated = (last - first + 1) > max_pages
        if truncated:
            last = first + max_pages - 1

        page_texts, ocr_pages = [], []
        for num in range(first, min(last, n_pages) + 1):
            page = pdf[num - 1]
            try:
                text = _clean(page.get_textpage().get_text_range())
            except Exception:
                text = ""
            scanned = len(text) < min_chars
            if scanned and ocr and len(ocr_pages) < budget:
                try:
                    png = _page_png(page, scale)
                    uri = "data:image/png;base64," + base64.b64encode(png).decode()
                    vis = _clean(services.vision_call(_OCR_PROMPT, uri, cfg) or "")
                    ocr_pages.append(num)
                    # Nur übernehmen, wenn es MEHR hergibt als die Textebene: eine knappe
                    # Vision-Antwort („Ein Dokument mit Text.") darf echten Text nicht verdrängen.
                    if vis and vis != "(leer)" and len(vis) > len(text):
                        text = vis
                except Exception as e:
                    debug.log("document_ocr_error", page=num, error=str(e)[:200])
            elif scanned and ocr and len(ocr_pages) >= budget:
                text = text or "(Bildseite — nicht gelesen, Scan-Budget erschöpft)"
            page_texts.append({"page": num, "text": text, "scanned": scanned})

        out = {"pages": n_pages, "page_texts": page_texts, "ocr_pages": ocr_pages, "title": title,
               "read_from": first, "read_to": page_texts[-1]["page"] if page_texts else first - 1,
               "truncated": truncated, "error": ""}
        out["text"] = pages_text(out)
        debug.log("document_read", pages=n_pages, read_from=out["read_from"], read_to=out["read_to"],
                  ocr_pages=len(ocr_pages), chars=len(out["text"]), title=title[:80],
                  truncated=truncated, ms=int((time.time() - t0) * 1000))
        return out
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def pages_text(result: dict, with_markers: bool = True) -> str:
    """Seiten eines extract_pdf-Ergebnisses zu einem Text — mit Seitenmarken, damit das Modell
    später gezielt nachladen („Seite 7") und zitieren kann."""
    out = []
    for p in result.get("page_texts") or []:
        if not (p.get("text") or "").strip():
            continue
        out.append(f"[Seite {p['page']}]\n{p['text']}" if with_markers else p["text"])
    return "\n\n".join(out).strip()


def extract_text(data: bytes, cfg: dict | None = None, ocr: bool = True) -> str:
    """Kurzform für Aufrufer, die nur den Text wollen (Wissensbasis-Upload). Wirft bei Fehlern,
    damit der Upload-Endpunkt eine echte Meldung an den Nutzer geben kann."""
    res = extract_pdf(data, cfg, ocr=ocr)
    if res.get("error"):
        raise ValueError(res["error"])
    return res.get("text") or ""


def excerpt(result: dict, max_chars: int) -> tuple[str, bool]:
    """Auszug fürs Gespräch: ganze Seiten, bis das Zeichenbudget voll ist.
    Gibt (Text, ob gekürzt) zurück — gekürzt heißt: der Rest geht über read_document."""
    out, used = [], 0
    for p in result.get("page_texts") or []:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        block = f"[Seite {p['page']}]\n{text}"
        if used and used + len(block) > max_chars:
            return "\n\n".join(out), True
        out.append(block)
        used += len(block) + 2
    joined = "\n\n".join(out)
    if len(joined) > max_chars:                              # erste Seite allein schon zu lang
        return joined[:max_chars], True
    return joined, bool(result.get("truncated"))
