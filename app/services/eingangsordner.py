"""Eingangsordner: Rechnungen in einen Ordner legen, das Programm liest sie ein.

Entschieden 04.11.2026: je Firma zwei frei wählbare Ordner (Einstellungsgruppe „ordner“) – Eingangsrechnungen
landen im Prüfbereich (``belege.create_from_bytes``), alte Ausgangsrechnungen im Import-Prüfbereich
(``rechnungs_import.create_from_bytes``). Danach wird jede Datei verschoben, nie gelöscht:
``verarbeitet/`` bzw. ``fehler/`` (mit ``<Datei>.grund.txt``). Automatisch jede Minute im Hintergrund-Worker
(Einstellung ``ordner.aktiv``) und per Knopf „Jetzt einlesen“. Nur lokal; auf dem Server später feste Ordner je Firma.
"""
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services import ablage, belege, einstellungen, rechnungs_import

log = logging.getLogger("app.eingangsordner")

KINDS = {"eingang": "ordner.eingang", "ausgang": "ordner.ausgang"}
DONE_DIR = "verarbeitet"
ERROR_DIR = "fehler"
MIN_AGE_SECONDS = 10      # Dateien, die gerade noch kopiert werden, erst beim nächsten Durchlauf einlesen
MAX_FILES_PER_RUN = 50
STATUS_KEY = "ordner.letzter_lauf"
_IGNORE = {"desktop.ini", "thumbs.db", ".ds_store"}
_lock = threading.Lock()  # Worker und Knopf nie gleichzeitig (sonst könnte eine Datei doppelt eingelesen werden)


@dataclass
class Result:
    imported: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    busy: bool = False

    @property
    def message(self) -> str:
        if self.busy:
            return "Die Ordner werden gerade schon eingelesen – bitte gleich noch einmal versuchen."
        text = f"{len(self.imported)} Datei(en) eingelesen." if self.imported else "Keine neuen Dateien im Ordner."
        if self.problems:
            text += " In den Unterordner „fehler“ verschoben: " + " ".join(self.problems)
        return text


# ------------------------------------------------------------ Einstellungen
def setting_problems(eingang: str | None, ausgang: str | None, auto: bool) -> dict[str, str]:
    """Prüfung beim Speichern der Einstellungen; legt fehlende Ordner an."""
    errors: dict[str, str] = {}
    paths = {"ordner.eingang": eingang, "ordner.ausgang": ausgang}
    if any(paths.values()) and not get_settings().is_local:
        return {key: "Nur in der lokalen Version – auf dem Server Rechnungen bitte an das E-Mail-Postfach "
                     "weiterleiten oder im Prüfbereich hochladen." for key, value in paths.items() if value}
    resolved = {}
    for key, value in paths.items():
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            errors[key] = r"Bitte einen vollständigen Pfad angeben, z. B. E:\Rechnungen\Eingang."
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors[key] = f"Der Ordner lässt sich nicht anlegen oder öffnen ({exc.strerror or exc})."
            continue
        resolved[key] = path.resolve()
    if len(resolved) == 2:
        a, b = resolved["ordner.eingang"], resolved["ordner.ausgang"]
        if a == b or a in b.parents or b in a.parents:
            errors["ordner.ausgang"] = "Bitte zwei getrennte Ordner wählen (nicht derselbe und nicht ineinander)."
    if auto and not any(paths.values()):
        errors["ordner.eingang"] = "Für das automatische Einlesen bitte mindestens einen Ordner angeben."
    return errors


def folders(db: Session) -> dict[str, Path]:
    if not get_settings().is_local:
        return {}
    values = einstellungen.load(db)
    return {kind: Path(values[key]) for kind, key in KINDS.items() if values.get(key)}


def enabled(db: Session) -> bool:
    return bool(einstellungen.get(db, "ordner.aktiv")) and bool(folders(db))


def last_status(db: Session) -> dict | None:
    return einstellungen.get(db, STATUS_KEY)


# ---------------------------------------------------------------- Einlesen
def _candidates(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    now = time.time()
    files = []
    for path in folder.iterdir():
        name = path.name.lower()
        if not path.is_file() or name in _IGNORE or name.startswith((".", "~$")) or name.endswith((".tmp", ".part")):
            continue
        try:
            if now - path.stat().st_mtime < MIN_AGE_SECONDS:
                continue  # wird vermutlich gerade noch kopiert
        except OSError:
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.name.lower())[:MAX_FILES_PER_RUN]


def _move(path: Path, subdir: str, reason: str | None = None) -> None:
    """In den Unterordner verschieben; gibt es dort schon eine gleichnamige Datei, kommt die Uhrzeit dazu."""
    target_dir = path.parent / subdir
    target_dir.mkdir(exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}_{datetime.now():%Y%m%d-%H%M%S}{path.suffix}"
    shutil.move(str(path), str(target))
    if reason:
        target.with_name(target.name + ".grund.txt").write_text(reason + "\n", encoding="utf-8")


def _import_one(db: Session, kind: str, path: Path, data: bytes, tenant_id: int):
    if kind == "eingang":
        return belege.create_from_bytes(db, data, path.name, tenant_id=tenant_id, user_id=None, ip=None,
                                        source="ordner")
    return rechnungs_import.create_from_bytes(db, data, path.name, tenant_id=tenant_id, user_id=None, ip=None)


def scan(db: Session, tenant_id: int, kinds: tuple[str, ...] = ("eingang", "ausgang")) -> Result:
    """Ordner der aktiven Firma einlesen. Committet je Datei selbst."""
    result = Result()
    if not _lock.acquire(blocking=False):
        result.busy = True
        return result
    try:
        for kind, folder in folders(db).items():
            if kind not in kinds:
                continue
            for path in _candidates(folder):
                try:
                    data = path.read_bytes()
                except OSError:
                    continue  # z. B. noch von einem anderen Programm geöffnet – nächster Durchlauf
                reason = None
                try:
                    doc = _import_one(db, kind, path, data, tenant_id)
                    db.commit()
                    result.imported.append(doc.id)
                except belege.DuplicateUpload as exc:
                    db.rollback()
                    reason = (f"Diese Datei wurde bereits eingelesen (Beleg {exc.document.number or exc.document.id})."
                              if exc.document else "Diese Datei wurde bereits eingelesen.")
                except ablage.UploadRejected as exc:
                    db.rollback()
                    reason = str(exc)
                except Exception:  # noqa: BLE001 – eine kaputte Datei darf die anderen nicht aufhalten
                    db.rollback()
                    log.exception("Eingangsordner: %s konnte nicht eingelesen werden", path)
                    reason = "Die Datei konnte nicht eingelesen werden (unbekannter Fehler, siehe Programmfenster)."
                try:
                    _move(path, ERROR_DIR if reason else DONE_DIR, reason)
                except OSError:
                    log.warning("Eingangsordner: %s konnte nicht verschoben werden", path)
                if reason:
                    result.problems.append(f"„{path.name}“: {reason}")
        if result.imported or result.problems:
            einstellungen.set_values(db, {STATUS_KEY: {
                "zeit": datetime.now().strftime("%d.%m.%Y %H:%M"), "eingelesen": len(result.imported),
                "fehler": result.problems[:20]}})
            db.commit()
    finally:
        _lock.release()
    return result
