#!/usr/bin/env python3
"""
Tests für die phase1.md-Bausteine 1–4: PDF-Verständnis, Session-Anhänge an MCP-Werkzeuge,
Weiterleitungsregeln und Zugangsschlüssel für MCP-Server.

Braucht KEIN LLM/GPU (OCR-Pfad wird mit ocr=False bzw. einem Stub geprüft) und kein Postgres
außer im Header-Test, der dann übersprungen wird.
Lauf:  python3 tests/test_phase1.py       (Mini-Runner, exit!=0 bei Fehler)
"""
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        _failures.append(name)


# ── Hilfsmittel: minimale PDFs ohne Fremdbibliothek ───────────────────────────

def make_pdf(pages_text: list[str]) -> bytes:
    """Baut ein gültiges, unkomprimiertes PDF mit einer Textzeile je Seite.
    Eine leere Zeichenkette ergibt eine Seite OHNE Textebene (wie ein Scan)."""
    objs, page_ids, page_objs = [], [], []
    nxt = 3
    for txt in pages_text:
        pid, cid = nxt, nxt + 1
        nxt += 2
        page_ids.append(pid)
        stream = ("BT /F1 14 Tf 60 700 Td (" + txt.replace("(", r"\(").replace(")", r"\)") + ") Tj ET") if txt else ""
        page_objs.append((pid, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                               "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
                               f"/BaseFont /Helvetica >> >> >> /Contents {cid} 0 R >>"))
        page_objs.append((cid, f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))
    objs.append((1, "<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append((2, f"<< /Type /Pages /Count {len(page_ids)} /Kids "
                    f"[{' '.join(f'{i} 0 R' for i in page_ids)}] >>"))
    objs.extend(page_objs)
    objs.sort()
    out, offsets = b"%PDF-1.4\n", {}
    for num, body in objs:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_at, n = len(out), max(offsets) + 1
    out += f"xref\n0 {n}\n0000000000 65535 f \n".encode()
    for i in range(1, n):
        out += f"{offsets.get(i, 0):010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return out


# ── Baustein 1: PDF lesen ────────────────────────────────────────────────────

def test_pdf_text_and_pages():
    """Textebene, Seitenbereiche, Seitenkappung und Auszug."""
    import config
    import documents
    cfg = config.get()
    check("pypdfium2 vorhanden", documents.available() == "")
    if documents.available():
        return

    pdf = make_pdf([f"Seite {i}: Abschnitt {i} mit ausreichend Text fuer die Textebene." for i in range(1, 51)])
    res = documents.extract_pdf(pdf, cfg, ocr=False)
    check("Seitenzahl erkannt (50)", res["pages"] == 50)
    check("auf pdf_max_pages gekappt", res["read_to"] == int(cfg.get("pdf_max_pages", 40)))
    check("Kappung gemeldet", res["truncated"] is True)
    check("Seitenmarken im Text", "[Seite 1]" in res["text"] and "[Seite 40]" in res["text"])

    teil = documents.extract_pdf(pdf, cfg, 30, 31, ocr=False)
    check("Teilbereich liest genau 30-31",
          [p["page"] for p in teil["page_texts"]] == [30, 31] and "Abschnitt 30" in teil["text"])
    check("Seite jenseits des Dokuments meldet Grenze",
          "nur 50 Seiten" in documents.extract_pdf(pdf, cfg, 99, 99, ocr=False)["error"])

    kurz, cut = documents.excerpt(res, 500)
    check("Auszug hält das Zeichenbudget", len(kurz) <= 520)
    check("Auszug meldet Kürzung", cut is True)
    check("Auszug schneidet an Seitengrenzen", kurz.count("[Seite") >= 1 and kurz.endswith("."))


def test_pdf_errors():
    """Kaputtes und passwortgeschütztes PDF → klare Meldung statt Traceback."""
    import config
    import documents
    if documents.available():
        return
    cfg = config.get()
    err = documents.extract_pdf(b"%PDF-1.4\nnur Muell\n", cfg, ocr=False)["error"]
    check("kaputtes PDF: klare Meldung", "beschädigt" in err or "kein gültiges PDF" in err)
    try:
        from pypdf import PdfWriter
        import io
        w = PdfWriter(clone_from=io.BytesIO(make_pdf(["Inhalt mit genug Text fuer die Ebene."])))
        w.encrypt("geheim")
        buf = io.BytesIO()
        w.write(buf)
        pw_err = documents.extract_pdf(buf.getvalue(), cfg, ocr=False)["error"]
        check("passwortgeschütztes PDF: klare Meldung", "passwortgeschützt" in pw_err)
    except ImportError:
        print("  --  passwortgeschütztes PDF übersprungen (pypdf fehlt)")
    check("is_pdf erkennt Name/MIME/Signatur",
          documents.is_pdf("x.PDF") and documents.is_pdf("", "application/pdf")
          and documents.is_pdf("", "", b"%PDF-1.7 ...") and not documents.is_pdf("x.txt", "text/plain"))


def test_pdf_scan_ocr():
    """Seite ohne Textebene wird gerendert und per Vision gelesen — Vision hier als Stub.
    Prüft außerdem, dass eine schwache OCR-Antwort echten Text NICHT verdrängt."""
    import config
    import documents
    import services
    if documents.available():
        return
    cfg = dict(config.get())
    cfg["pdf_ocr_max_pages"] = 1                 # Budget: nur die erste Scan-Seite
    orig = services.vision_call
    calls = []

    def fake_vision(prompt, image_url, cfg_, timeout=150):
        calls.append(image_url[:30])
        return "Zutaten aus dem Scan: 3 Eier, 200 g Mehl, eine Prise Salz."

    services.vision_call = fake_vision
    try:
        pdf = make_pdf(["", "", "Seite 3 hat eine richtige Textebene mit genug Zeichen."])
        res = documents.extract_pdf(pdf, cfg)
        check("Scan-Seite per Vision gelesen", res["ocr_pages"] == [1])
        check("Vision bekam ein PNG als data-URI", bool(calls) and calls[0].startswith("data:image/png;base64,"))
        check("OCR-Text übernommen", "3 Eier" in res["text"])
        check("Scan-Budget greift", "Budget erschöpft" in res["page_texts"][1]["text"])
        check("Textseite unangetastet", "richtige Textebene" in res["page_texts"][2]["text"])

        services.vision_call = lambda p, i, c, timeout=150: "Ein Dokument."
        res2 = documents.extract_pdf(make_pdf(["Diese Seite hat eine lange, echte Textebene mit vielen Zeichen."]),
                                     {**cfg, "pdf_scan_min_chars": 500})
        check("schwache OCR-Antwort verdrängt echten Text nicht",
              "lange, echte Textebene" in res2["text"])
    finally:
        services.vision_call = orig


def test_session_document_memory():
    """set_last_document/get_last_document inkl. Speicherbegrenzung (älteste zuerst)."""
    from session_hub import SessionHub
    hub = SessionHub()
    for i in range(7):
        hub.set_last_document(f"s{i}", {"name": f"d{i}.pdf", "mime": "application/pdf",
                                        "data_uri": "data:application/pdf;base64,AAA",
                                        "text": "x", "pages": i}, limit=5)
    check("nur die letzten 5 Dokumente bleiben", len(hub._doc) == 5)
    check("älteste wurden verworfen", hub.get_last_document("s0") == {} and hub.get_last_document("s6")["pages"] == 6)
    check("ohne data_uri wird nichts gemerkt",
          (hub.set_last_document("leer", {"name": "x.pdf"}), hub.get_last_document("leer"))[1] == {})


# ── Baustein 2: Session-Anhänge an MCP-Werkzeuge ─────────────────────────────

def test_mcp_session_attachments():
    """"session" wird durch den echten Anhang ersetzt; ohne Anhang wird NICHT aufgerufen."""
    import mcp_hub
    import tools
    sid = "attach-test"
    img = "data:image/jpeg;base64," + "A" * 400
    pdf = "data:application/pdf;base64," + "B" * 400
    tools.hub.set_last_image(sid, img)
    tools.hub.set_last_document(sid, {"name": "r.pdf", "mime": "application/pdf", "data_uri": pdf})

    # Schema des Dummy-Servers vortäuschen, damit der Schutz mitgeprüft wird
    mcp_hub._cache["t"] = {"url": "http://x/mcp", "enabled": True, "headers": {}, "error": None,
                           "tools": [{"name": "tool", "description": "",
                                      "inputSchema": {"type": "object",
                                                      "properties": {"bild": {}, "datei": {}, "notiz": {}}}}]}
    ctx = {"session_id": sid}
    args, err = tools._resolve_session_args("t", "tool", {"bild": "session", "datei": "session",
                                                          "notiz": "session"}, ctx, sid)
    check("Bild eingesetzt", args["bild"] == img)
    check("Dokument eingesetzt", args["datei"] == pdf)
    check("unbekannter Parametername bleibt unberührt", args["notiz"] == "session")
    check("kein Fehler bei vorhandenen Anhängen", err == "")

    args2, _ = tools._resolve_session_args("t", "tool", {"bild": "@bild", "datei": "@datei"}, ctx, sid)
    check("Aliasse @bild/@datei wirken", args2["bild"] == img and args2["datei"] == pdf)

    # Parameter, den das Schema NICHT deklariert → nicht ersetzen
    args3, _ = tools._resolve_session_args("t", "tool", {"foto": "session"}, ctx, sid)
    check("nicht deklarierter Parameter wird nicht ersetzt", args3["foto"] == "session")

    _, err_img = tools._resolve_session_args("t", "tool", {"bild": "session"}, {}, "leer-1")
    _, err_doc = tools._resolve_session_args("t", "tool", {"datei": "session"}, {}, "leer-2")
    check("ohne Bild: Meldung statt Aufruf", "kein Bild gesendet" in err_img)
    check("ohne Dokument: Meldung statt Aufruf", "kein Dokument gesendet" in err_doc)
    mcp_hub._cache.pop("t", None)


# ── Baustein 3: Weiterleitungsregeln ─────────────────────────────────────────

def test_forward_matching():
    """Präfix-Treffer, Typ-Filter, deaktivierte und kaputte Regeln, Maskierung."""
    import forwarding
    cfg = {"forward_rules": [
        {"prefix": "rezept", "url": "http://h/api", "token": "abcd1234",
         "types": ["text", "url", "image", "pdf"], "enabled": True},
        {"prefix": "nurtext", "url": "http://h/t", "types": ["text"], "enabled": True},
        {"prefix": "aus", "url": "http://h/a", "enabled": False},
        {"prefix": "kaputt", "url": "ftp://h/k", "enabled": True},
    ]}
    r, rest = forwarding.match(cfg, "rezept: Pfannkuchen", "text")
    check("Präfix mit Doppelpunkt trifft", r and r["prefix"] == "rezept" and rest == "Pfannkuchen")
    r, rest = forwarding.match(cfg, "Rezept Pfannkuchen", "text")
    check("Präfix ohne Doppelpunkt, Groß/Klein egal", r and rest == "Pfannkuchen")
    r, rest = forwarding.match(cfg, "rezept", "image")
    check("nacktes Präfix als Bildunterschrift trifft", r and rest == "")
    check("Wort mit gleichem Anfang trifft NICHT", forwarding.match(cfg, "rezepte sind toll", "text")[0] is None)
    check("deaktivierte Regel trifft nicht", forwarding.match(cfg, "aus: x", "text")[0] is None)
    check("Regel ohne http(s)-Adresse wird ignoriert", forwarding.match(cfg, "kaputt: x", "text")[0] is None)
    check("Typ-Filter: Link an Nur-Text-Regel trifft nicht",
          forwarding.match(cfg, "nurtext https://a.de", "text")[0] is None)
    check("Typ-Filter: Text an Nur-Text-Regel trifft", forwarding.match(cfg, "nurtext hallo", "text")[0] is not None)
    check("Link wird als url erkannt", forwarding.kind_for_text("https://a.de") == "url"
          and forwarding.kind_for_text("hallo") == "text")
    m = forwarding.masked(cfg)[0]
    check("Token nur maskiert", m["token_hint"] == "…1234" and "token" not in m and m["has_token"])


def test_forward_post_and_errors():
    """Weiterleitung sendet multipart (auch ohne Datei) und meldet Fehler klar."""
    import forwarding
    seen = {}

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"job_id": 12, "message": "📥 Rezept angenommen"}

    import requests as rq
    orig = rq.post

    def fake_post(url, files=None, headers=None, timeout=None, **kw):
        seen.update({"url": url, "files": files, "headers": headers, "timeout": timeout})
        return Resp()

    rq.post = fake_post
    try:
        rule = {"prefix": "rezept", "url": "http://h/api", "token": "tok", "types": ["text"]}
        ok, msg = forwarding.forward(rule, "text", "Pfannkuchen", requested_by="daniel", chat_id=42)
        names = [k for k, _ in (seen["files"] or [])]
        check("sendet multipart auch ohne Datei", isinstance(seen["files"], list) and "text" in names)
        check("Pflichtfelder dabei", {"source", "kind", "text", "requested_by", "chat_id"} <= set(names))
        check("Bearer-Header gesetzt", seen["headers"]["Authorization"] == "Bearer tok")
        check("Zeitlimit gesetzt", seen["timeout"] == forwarding.TIMEOUT_S)
        check("Antwort + Job-Nummer zurück", ok and "angenommen" in msg and "#12" in msg)

        ok2, msg2 = forwarding.forward(rule, "pdf", "x", file=("r.pdf", b"%PDF-1.4", "application/pdf"))
        check("Datei wird mitgeschickt", any(k == "file" for k, _ in (seen["files"] or [])))

        class Bad(Resp):
            status_code = 500

            def json(self):
                return {"message": "kaputt"}

        rq.post = lambda *a, **k: Bad()
        ok3, msg3 = forwarding.forward(rule, "text", "x")
        check("HTTP-Fehler → ok=False + klare Meldung",
              not ok3 and "abgelehnt" in msg3 and "nichts weiter" in msg3)

        def boom(*a, **k):
            raise rq.Timeout("zu lange")

        rq.post = boom
        ok4, msg4 = forwarding.forward(rule, "text", "x")
        check("Timeout → ok=False + klare Meldung", not ok4 and "zu lange gebraucht" in msg4)
    finally:
        rq.post = orig


# ── Baustein 4: Zugangsschlüssel für MCP-Server ──────────────────────────────

def test_mcp_headers_parsing():
    """Nackter Token → Bearer, JSON → eigene Header, Maskierung, kaputtes JSON."""
    import mcp_hub
    check("nackter Token wird Bearer",
          mcp_hub.parse_headers("geheim") == {"Authorization": "Bearer geheim"})
    check("JSON-Objekt wird übernommen",
          mcp_hub.parse_headers('{"X-Api-Key": "abc"}') == {"X-Api-Key": "abc"})
    check("leer = keine Header", mcp_hub.parse_headers("  ") == {})
    check("Maskierung zeigt nur die letzten 4 Zeichen",
          mcp_hub._headers_hint('{"Authorization": "Bearer supersecret9876"}') == "Authorization: …9876")
    try:
        mcp_hub.parse_headers("{kaputt")
        check("kaputtes JSON wirft ValueError", False)
    except ValueError:
        check("kaputtes JSON wirft ValueError", True)


def test_mcp_headers_reach_the_client():
    """Die Header müssen über die httpx-Factory gehen: streamablehttp_client(headers=…) ist
    seit mcp 1.28 „deprecated and will be ignored" — sonst kommt der Schlüssel nie an."""
    import mcp_hub
    factory = mcp_hub._client_factory({"Authorization": "Bearer probe"})
    client = factory({"Accept": "application/json"}, None, None)
    try:
        check("Factory mischt den Schlüssel in die Client-Header",
              client.headers.get("authorization") == "Bearer probe")
        check("vorhandene Header bleiben erhalten", client.headers.get("accept") == "application/json")
    finally:
        asyncio.run(client.aclose())


def _isolate_logs():
    """Testläufe dürfen NICHT ins Betriebs-Protokoll schreiben — sonst stehen erfundene
    Ereignisse (etwa eine Weiterleitung mit HTTP 500) in logs/events-*.jsonl und führen bei
    der nächsten echten Fehlersuche in die Irre."""
    import tempfile
    import debug
    debug._LOG_DIR = pathlib.Path(tempfile.mkdtemp(prefix="jarvis-test-logs-"))


def main():
    _isolate_logs()
    tests = [test_pdf_text_and_pages, test_pdf_errors, test_pdf_scan_ocr,
             test_session_document_memory, test_mcp_session_attachments,
             test_forward_matching, test_forward_post_and_errors,
             test_mcp_headers_parsing, test_mcp_headers_reach_the_client]
    for t in tests:
        print(f"\n## {t.__name__}")
        try:
            t()
        except Exception as e:
            print(" FAIL (Exception)", type(e).__name__, e)
            _failures.append(t.__name__)
    print(f"\n{'=' * 48}")
    if _failures:
        print(f"FEHLGESCHLAGEN: {len(_failures)} → {_failures}")
        sys.exit(1)
    print("ALLE PHASE-1-TESTS BESTANDEN ✓")


if __name__ == "__main__":
    main()
