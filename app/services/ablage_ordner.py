"""Ablage-Ordner: festgeschriebene Rechnungen als Datei in <Ablage>\\<Jahr>\\<MM Monat> einsortieren.

Entschieden 25.11.2026 (Nutzer):
- Eingangsrechnungen nach ``ordner.ablage_eingang``, Ausgangsrechnungen (eigene, Stornos, übernommene alte)
  nach ``ordner.ablage_ausgang``. Leer = derselbe Ordner wie der Eingangsordner (``ordner.eingang`` bzw.
  ``ordner.ausgang``). Nur lokale Version.
- Erst beim Festschreiben, Ordner nach dem Rechnungsdatum, z. B. ``2026\\11 November`` – fehlende Jahres- und
  Monatsordner (auch beim Jahreswechsel, ``2027\\01 Januar``) werden angelegt.
- Es wird KOPIERT (die Datei im Unterordner „verarbeitet“ bleibt liegen). Nie etwas überschreiben oder löschen:
  gleiche Datei schon da = nichts tun, gleicher Name mit anderem Inhalt = „Name (2).pdf“.
- ``nachtragen`` sortiert alle festgeschriebenen, noch nicht abgelegten Belege ein (``documents.ablage_at``) – direkt
  nach dem Festschreiben und jede Minute im Hintergrund. Schlägt das Kopieren fehl (Laufwerk nicht da), wird es beim
  nächsten Lauf erneut versucht; das Festschreiben selbst wird nie blockiert.
"""
import logging
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import utcnow
from app.models import Document
from app.models.belege import DIRECTION_IN, DIRECTION_OUT, STATE_FINAL
from app.services import ablage, audit, einstellungen

log = logging.getLogger("app.ablage_ordner")

MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")
SETTINGS = {DIRECTION_IN: ("ordner.ablage_eingang", "ordner.eingang"),
            DIRECTION_OUT: ("ordner.ablage_ausgang", "ordner.ausgang")}
MAX_PER_RUN = 200


def month_folder(base: Path, day: date) -> Path:
    return base / f"{day.year:04d}" / f"{day.month:02d} {MONTHS[day.month - 1]}"


def base_folder(db: Session, direction: str) -> Path | None:
    """Ablage-Ordner für Ein- bzw. Ausgang; leer = derselbe Ordner wie der Eingangsordner. Nur lokal."""
    if not get_settings().is_local:
        return None
    values = einstellungen.load(db)
    own, fallback = SETTINGS[direction]
    folder = (values.get(own) or values.get(fallback) or "").strip()
    return Path(folder) if folder else None


def _filename(doc: Document) -> str:
    if doc.direction == DIRECTION_OUT:
        from app.services import rechnungen

        return rechnungen.pdf_filename(doc)
    name = ablage.clean_filename(doc.file.original_name or "") if doc.file else ""
    return name or f"Beleg-{doc.id}.pdf"


def _place(folder: Path, name: str, data: bytes) -> Path:
    """Datei ablegen, ohne etwas zu überschreiben. Gibt den Pfad zurück (auch wenn sie schon da war)."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(1, 1000):
        target = folder / (name if n == 1 else f"{stem} ({n}){suffix}")
        if target.exists():
            if target.read_bytes() == data:
                return target                       # schon abgelegt
            continue
        partial = target.with_name(target.name + ".teil")
        partial.write_bytes(data)
        partial.replace(target)
        return target
    raise OSError(f"Zu viele gleichnamige Dateien in {folder}")


def file_away(db: Session, doc: Document, base: Path) -> Path:
    """Einen festgeschriebenen Beleg einsortieren (wirft OSError bei Problemen mit dem Laufwerk)."""
    folder = month_folder(base, doc.doc_date)
    target = _place(folder, _filename(doc), ablage.read_bytes(doc.file, db))
    if doc.direction == DIRECTION_OUT and doc.xml_file is not None:   # XRechnung gehört dazu
        _place(folder, ablage.clean_filename(doc.xml_file.original_name) or f"{target.stem}.xml",
               ablage.read_bytes(doc.xml_file, db))
    return target


def nachtragen(db: Session) -> list[str]:
    """Alle festgeschriebenen, noch nicht abgelegten Belege der aktiven Firma einsortieren.
    Gibt Problem-Meldungen zurück; ruft KEIN commit auf."""
    bases = {direction: base_folder(db, direction) for direction in SETTINGS}
    wanted = [d for d, base in bases.items() if base is not None]
    if not wanted:
        return []
    problems: list[str] = []
    stmt = (select(Document).options(selectinload(Document.file), selectinload(Document.xml_file))
            .where(Document.state == STATE_FINAL, Document.ablage_at.is_(None), Document.file_id.is_not(None),
                   Document.doc_date.is_not(None), Document.direction.in_(wanted))
            .order_by(Document.doc_date, Document.id).limit(MAX_PER_RUN))
    for doc in db.scalars(stmt):
        try:
            target = file_away(db, doc, bases[doc.direction])
        except OSError as exc:
            log.warning("Ablage-Ordner: Beleg %s konnte nicht abgelegt werden: %s", doc.id, exc)
            problems.append(f"{doc.number or doc.partner_name or doc.id}: {exc.strerror or exc}")
            continue
        doc.ablage_at, doc.ablage_pfad = utcnow(), str(target)[:500]
        audit.log(db, action="abgelegt", entity="beleg", entity_id=doc.id, tenant_id=doc.tenant_id,
                  changes={"pfad": str(target)})
    db.flush()
    return problems


def nach_festschreiben(db: Session) -> str | None:
    """Nach einem Commit aufrufen (Festschreiben, Zahlung, Übernehmen): einsortieren und speichern.
    Gibt einen Hinweis zurück, falls etwas nicht abgelegt werden konnte."""
    try:
        problems = nachtragen(db)
        db.commit()
    except Exception:  # noqa: BLE001 – die Ablage darf nie das Festschreiben stören
        db.rollback()
        log.exception("Ablage-Ordner: Einsortieren fehlgeschlagen")
        return "Die Datei konnte nicht in den Ablage-Ordner kopiert werden – das Programm versucht es später erneut."
    if problems:
        return ("Nicht in den Ablage-Ordner kopiert (Laufwerk erreichbar?): " + "; ".join(problems[:3])
                + ". Das Programm versucht es später erneut.")
    return None
