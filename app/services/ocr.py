"""Texterkennung (OCR) für eingescannte PDFs und Fotos – über das Programm Tesseract.

Tesseract ist ein eigenständiges, kostenloses Programm (kein Python-Paket). Es wird gesucht:
1. Pfad aus der .env (``TESSERACT_CMD``),
2. im Suchpfad (PATH),
3. an den üblichen Windows-Installationsorten.
Ist es nicht installiert, liefert ``available()`` False und die Belege werden wie bisher von Hand
erfasst – das Programm läuft also auch ohne Texterkennung.

Erkannt wird bevorzugt Deutsch („deu“), ersatzweise Englisch. Alles läuft lokal; es werden keine
Daten an fremde Server geschickt.
"""
import io
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

MAX_PAGES = 5            # Scans sind selten länger; schützt vor sehr langen Laufzeiten
TIMEOUT_SECONDS = 90
RESOLUTION = 300         # dpi beim Umwandeln von PDF-Seiten in Bilder
_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)


class OcrError(RuntimeError):
    pass


@lru_cache
def tesseract_cmd() -> str | None:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("tesseract")
    if found:
        return found
    return next((p for p in _WINDOWS_PATHS if Path(p).is_file()), None)


@lru_cache
def language() -> str | None:
    """„deu“ (falls installiert), sonst „eng“; None = Tesseract fehlt."""
    cmd = tesseract_cmd()
    if cmd is None:
        return None
    try:
        out = subprocess.run([cmd, "--list-langs"], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    langs = set(out.stdout.decode("utf-8", "replace").split())
    if "deu" in langs:
        return "deu"
    return "eng" if "eng" in langs else None


def available() -> bool:
    return language() is not None


def status_text() -> str:
    if tesseract_cmd() is None:
        return ("Die Texterkennung (Tesseract-OCR) ist auf diesem Rechner nicht installiert – Scans und Fotos "
                "bitte von Hand erfassen.")
    if language() is None:
        return "Tesseract ist installiert, aber ohne Sprachdaten – bitte „Deutsch“ nachinstallieren."
    if language() != "deu":
        return "Tesseract ist installiert, aber ohne deutsche Sprachdaten – die Erkennung ist dadurch ungenauer."
    return "Texterkennung (Tesseract-OCR, Deutsch) ist bereit."


def _run(png_path: Path) -> str:
    cmd, lang = tesseract_cmd(), language()
    if cmd is None or lang is None:
        raise OcrError(status_text())
    try:
        # --psm 3: automatische Seitenaufteilung (gut für Rechnungen mit Spalten)
        out = subprocess.run([cmd, str(png_path), "stdout", "-l", lang, "--psm", "3"], capture_output=True,
                             timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OcrError("Die Texterkennung hat zu lange gedauert.") from exc
    except OSError as exc:
        raise OcrError(f"Tesseract konnte nicht gestartet werden: {exc}") from exc
    if out.returncode != 0:
        raise OcrError("Die Texterkennung ist fehlgeschlagen: "
                       + out.stderr.decode("utf-8", "replace").strip()[:200])
    return out.stdout.decode("utf-8", "replace")


def _images_text(images) -> str:
    texts = []
    with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
        for i, image in enumerate(images):
            path = Path(tmp) / f"seite-{i}.png"
            image.save(path, format="PNG")
            texts.append(_run(path))
    return "\n".join(texts)


def image_text(data: bytes) -> str:
    """Text eines Fotos/Bilds (PNG, JPG). Wirft OcrError."""
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)  # Handyfotos richtig herum drehen
        image = image.convert("L")              # Graustufen erkennt Tesseract zuverlässiger
    except Exception as exc:  # noqa: BLE001 – kaputte Bilder
        raise OcrError("Das Bild kann nicht gelesen werden.") from exc
    return _images_text([image])


def pdf_text(data: bytes) -> str:
    """Text eines gescannten PDFs (Seiten werden als Bild erkannt). Wirft OcrError."""
    import pdfplumber

    images = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:MAX_PAGES]:
                images.append(page.to_image(resolution=RESOLUTION).original.convert("L"))
    except Exception as exc:  # noqa: BLE001 – kaputte PDFs
        raise OcrError("Die PDF-Seiten konnten nicht in Bilder umgewandelt werden.") from exc
    return _images_text(images)
