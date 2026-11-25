"""Texterkennung (OCR) für Scans und Fotos.

Tesseract ist auf Test-Rechnern meist nicht installiert – die Einbindung wird daher mit einer
nachgebildeten Erkennung geprüft. Ein Test mit echtem Tesseract läuft nur, wenn es installiert ist.
"""
import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.services import ocr
from tests.test_auslese import RECHNUNG_EINFACH
from tests.test_eingangsrechnungen import PNG, _all_docs, _ready, upload


def _scan_pdf() -> bytes:
    """PDF ohne Textebene (wie ein Scan): nur eine Fläche, kein Text."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(50, 500, 300, 200, fill=1)
    c.save()
    return buf.getvalue()


def test_ohne_tesseract_bleibt_handeingabe(client, db, monkeypatch):
    monkeypatch.setattr(ocr, "tesseract_cmd", lambda: None)
    monkeypatch.setattr(ocr, "language", lambda: None)
    _ready(client)
    upload(client, ("foto.png", PNG), ("scan.pdf", _scan_pdf()))
    foto, scan = _all_docs(db)
    assert "nicht installiert" in foto.notes and foto.notes.startswith("Foto/Bild")
    assert "nicht installiert" in scan.notes and scan.notes.startswith("Gescanntes PDF")
    assert foto.pending and foto.number is None
    assert "nicht installiert" in client.get("/eingangsrechnungen/hochladen").text


def test_foto_wird_erkannt(client, db, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "image_text", lambda data: RECHNUNG_EINFACH)
    _ready(client)
    upload(client, ("foto.png", PNG))
    doc = _all_docs(db)[0]
    assert doc.number == "RE-2026-0815" and str(doc.gross_total) == "499.80"
    assert "lines" in doc.review_fields and doc.needs_review  # Beträge aus OCR immer prüfen
    assert "per Texterkennung (OCR) gelesen" in doc.notes
    assert doc.extracted_text.startswith("Bürobedarf Schulz")


def test_scan_pdf_wird_erkannt(client, db, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "pdf_text", lambda data: RECHNUNG_EINFACH)
    _ready(client)
    upload(client, ("scan.pdf", _scan_pdf()))
    doc = _all_docs(db)[0]
    assert doc.number == "RE-2026-0815" and doc.notes.startswith("Gescanntes PDF: per Texterkennung")


def test_fehler_der_erkennung(client, db, monkeypatch):
    def broken(data):
        raise ocr.OcrError("Die Texterkennung hat zu lange gedauert.")

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "image_text", broken)
    _ready(client)
    upload(client, ("foto.png", PNG))
    doc = _all_docs(db)[0]
    assert "zu lange gedauert" in doc.notes and "von Hand" in doc.notes and doc.pending


@pytest.mark.skipif(not ocr.available(), reason="Tesseract-OCR ist nicht installiert")
def test_echte_texterkennung():
    from pathlib import Path

    import reportlab
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"), 32)
    image = Image.new("RGB", (1700, 1000), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(RECHNUNG_EINFACH.splitlines()[:14]):
        draw.text((60, 40 + i * 50), line, fill="black", font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    text = ocr.image_text(buf.getvalue())
    assert "RE-2026-0815" in text and "499,80" in text
