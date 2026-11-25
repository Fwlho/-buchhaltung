"""Datensicherung (KONZEPT.md 6.15).

Lokal (SQLite): Beim Programmstart wird – nur wenn sich seit der letzten Sicherung etwas geändert hat – eine
ZIP-Datei mit Datenbank und allen gespeicherten Dateien unter ``data/sicherungen/`` angelegt. Die letzten
10 Sicherungen bleiben erhalten. Wiederherstellen: siehe BETRIEB.md.
Server (PostgreSQL): Sicherung über ``deploy/sicherung.sh`` (pg_dump + Dateien), nicht über das Programm.
"""
import hashlib
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.config import get_settings

KEEP = int(os.environ.get("BACKUP_KEEP") or 10)
_MARKER = "letzte_sicherung.txt"


@dataclass
class Backup:
    path: Path
    created: datetime
    size: int

    @property
    def size_mb(self) -> Decimal:
        return Decimal(self.size) / Decimal(1024 * 1024)


def backup_dir() -> Path:
    return get_settings().data_dir / "sicherungen"


def sqlite_path() -> Path | None:
    url = get_settings().database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url.endswith(":memory:"):
        return None
    return Path(url[len(prefix):])


def _files() -> list[Path]:
    root = get_settings().files_dir
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _fingerprint(db_copy: Path, files: list[Path]) -> str:
    h = hashlib.sha256(db_copy.read_bytes())
    root = get_settings().files_dir
    for f in files:
        stat = f.stat()
        h.update(f"{f.relative_to(root).as_posix()}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return h.hexdigest()


def create_local_backup(force: bool = False) -> Path | None:
    """Sicherung anlegen. Gibt den Pfad zurück, None wenn nichts zu sichern war (kein SQLite, nichts geändert)."""
    source = sqlite_path()
    if source is None or not source.exists():
        return None
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    files = _files()
    with tempfile.TemporaryDirectory(prefix="sicherung-") as tmp:
        copy = Path(tmp) / "app.db"
        # Über die SQLite-Sicherungsfunktion kopieren – sicher auch, während das Programm läuft
        src = sqlite3.connect(source)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        fingerprint = _fingerprint(copy, files)
        marker = target_dir / _MARKER
        if not force and marker.exists() and marker.read_text(encoding="utf-8").strip() == fingerprint:
            return None
        stamp = f"sicherung_{datetime.now():%Y-%m-%d_%H-%M-%S}"
        name, n = f"{stamp}.zip", 2
        while (target_dir / name).exists():  # zwei Sicherungen in derselben Sekunde
            name, n = f"{stamp}_{n}.zip", n + 1
        partial = target_dir / (name + ".teil")
        root = get_settings().files_dir
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(copy, "app.db")
            for f in files:
                zf.write(f, "files/" + f.relative_to(root).as_posix())
            zf.writestr("LIESMICH.txt", "Datensicherung des Buchhaltungsprogramms.\r\n"
                                        "Wiederherstellen: siehe BETRIEB.md im Programmordner.\r\n")
        final = target_dir / name
        partial.replace(final)  # erst nach vollständigem Schreiben sichtbar
        marker.write_text(fingerprint, encoding="utf-8")
    prune()
    return final


def backups() -> list[Backup]:
    folder = backup_dir()
    if not folder.exists():
        return []
    found = [Backup(p, datetime.fromtimestamp(p.stat().st_mtime), p.stat().st_size)
             for p in folder.glob("sicherung_*.zip")]
    return sorted(found, key=lambda b: b.path.name, reverse=True)


def prune(keep: int = KEEP) -> int:
    removed = 0
    for old in backups()[keep:]:
        old.path.unlink(missing_ok=True)
        removed += 1
    return removed
