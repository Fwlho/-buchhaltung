"""Datei-Ablage je Firma: data/files/<tenant_id>/<uuid>.<endung>

Nur echte PDFs/Bilder werden angenommen (Prüfung der ersten Bytes, nicht nur
der Dateiendung). Originale werden inhaltlich unverändert gespeichert, mit SHA-256 –
auf der Platte aber verschlüsselt (app/services/tresor.py, je Firma eigener Schlüssel).
Lesen daher NUR über ``read_bytes`` bzw. ``response`` – nie die Datei direkt öffnen.
"""
import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session, object_session

from app.config import get_settings
from app.models import StoredFile

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Dateityp anhand der ersten Bytes ("Magic Bytes")
_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"%PDF-", "pdf", "application/pdf"),
]


class UploadRejected(ValueError):
    pass


def detect_type(data: bytes) -> tuple[str, str] | None:
    for signature, ext, mime in _SIGNATURES:
        if data.startswith(signature):
            return ext, mime
    # XML (z. B. XRechnung): optionales BOM/Leerzeichen, dann „<?xml“ bzw. ein Element
    head = data[:200].lstrip(b"\xef\xbb\xbf").lstrip()
    if head.startswith(b"<?xml") or re.match(rb"<[A-Za-z_][\w:.\-]*[\s>]", head):
        return "xml", "application/xml"
    return None


# Schutzregel für fremde Dateien: Im Browser darf darin nie ein Skript laufen. ``sandbox`` sperrt Skripte,
# Formulare und den Zugriff auf die Anmeldung. PDFs bekommen sie nicht, weil Chrome sonst die eingebaute
# PDF-Anzeige blockiert; PDF-Anzeigen der Browser führen ohnehin keine Seiten-Skripte aus.
FILE_CSP_STRICT = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"
FILE_CSP_PDF = "frame-ancestors 'self'"


def serve_type(content_type: str) -> tuple[str, str]:
    """(Inhaltstyp für den Browser, Schutzregel) zum Ausliefern einer gespeicherten Datei.
    XML wird als reiner Text gezeigt – so kann der Browser es nie als Webseite mit Skript deuten."""
    if content_type == "application/pdf":
        return content_type, FILE_CSP_PDF
    if content_type == "application/xml":
        return "text/plain; charset=utf-8", FILE_CSP_STRICT
    if content_type in ("image/png", "image/jpeg"):
        return content_type, FILE_CSP_STRICT
    return "application/octet-stream", FILE_CSP_STRICT


def clean_filename(name: str) -> str:
    name = Path(name or "datei").name
    name = re.sub(r"[^\w.\- äöüÄÖÜß]", "_", name).strip(" .")
    return (name or "datei")[:200]


def absolute_path(stored: StoredFile) -> Path:
    """Ort der (verschlüsselten) Datei – nur für Vorhanden-Prüfung und Löschen, nicht zum Lesen."""
    return get_settings().files_dir / stored.path


def read_bytes(stored: StoredFile, db: Session | None = None) -> bytes:
    """Inhalt einer gespeicherten Datei, entschlüsselt."""
    from app.services import tresor

    session = db or object_session(stored)
    return tresor.decrypt(session, stored.tenant_id, stored.path, absolute_path(stored).read_bytes())


def disposition(kind: str, filename: str) -> str:
    """Content-Disposition mit Umlauten (RFC 6266/5987)."""
    if filename.isascii():
        return f'{kind}; filename="{filename}"'
    return f"{kind}; filename*=utf-8''{quote(filename)}"


def response(stored: StoredFile, *, media_type: str | None = None, csp: str | None = None, inline: bool = True,
             headers: dict | None = None) -> Response:
    """Gespeicherte Datei entschlüsselt an den Browser geben (Inhaltstyp/Schutzregel wie ``serve_type``)."""
    served, rule = serve_type(stored.content_type)
    head = {"Content-Disposition": disposition("inline" if inline else "attachment", stored.original_name),
            "Content-Security-Policy": csp or rule, "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store"}
    head.update(headers or {})
    return Response(read_bytes(stored), media_type=media_type or served, headers=head)


def save_bytes(
    db: Session, data: bytes, *, tenant_id: int, kind: str, original_name: str, content_type: str,
    user_id: int | None,
) -> StoredFile:
    """Vom Programm erzeugte Datei (z. B. Rechnungs-PDF) unverändert ablegen."""
    ext = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg",
           "application/xml": "xml", "text/csv": "csv", "application/zip": "zip"}.get(content_type, "bin")
    relative = f"{tenant_id}/{uuid.uuid4().hex}.{ext}"
    target = get_settings().files_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    from app.services import tresor

    target.write_bytes(tresor.encrypt(db, tenant_id, relative, data))
    stored = StoredFile(
        tenant_id=tenant_id, path=relative, original_name=clean_filename(original_name), content_type=content_type,
        kind=kind, size=len(data), sha256=hashlib.sha256(data).hexdigest(), created_by_id=user_id,
    )
    db.add(stored)
    db.flush()
    return stored


async def save_upload(
    db: Session, upload: UploadFile, *, tenant_id: int, kind: str, allowed: set[str],
    user_id: int | None, max_bytes: int = MAX_UPLOAD_BYTES,
) -> StoredFile:
    data = await upload.read(max_bytes + 1)
    return save_checked(db, data, upload.filename or "", tenant_id=tenant_id, kind=kind, allowed=allowed,
                        user_id=user_id, max_bytes=max_bytes)


def save_checked(
    db: Session, data: bytes, filename: str, *, tenant_id: int, kind: str, allowed: set[str],
    user_id: int | None, max_bytes: int = MAX_UPLOAD_BYTES,
) -> StoredFile:
    """Fremde Datei (Upload, Mail-Anhang) prüfen und ablegen: Größe, echter Dateityp (Magic Bytes)."""
    if len(data) > max_bytes:
        raise UploadRejected(f"Die Datei ist zu groß (höchstens {max_bytes // (1024 * 1024)} MB).")
    if not data:
        raise UploadRejected("Die Datei ist leer.")
    detected = detect_type(data)
    if detected is None or detected[0] not in allowed:
        names = ", ".join(sorted(x.upper() for x in allowed))
        raise UploadRejected(f"Dateityp nicht erlaubt. Erlaubt: {names}.")
    return save_bytes(db, data, tenant_id=tenant_id, kind=kind, original_name=filename or "datei",
                      content_type=detected[1], user_id=user_id)
