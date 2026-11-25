"""Hintergrund-Aufgaben je Firma: automatischer E-Mail-Abruf, Eingangsordner, Rechnungsentwürfe aus Verträgen.

- Lokal (Phase 1): läuft als Thread im Programm (Start in app/main.py, abschaltbar mit
  BACKGROUND_JOBS=false in der .env).
- Server (später): als eigener Prozess starten:  python -m app.worker
  und im Web-Prozess BACKGROUND_JOBS=false setzen.
"""
import logging
import threading
import time

from sqlalchemy import select

from app.database import SessionLocal, tenant_scope
from app.models import Tenant
from app.models.tenant import TENANT_ACTIVE
from app.services import auftraege, eingangsordner, email_import, ki_auslese, kontierung, vertraege

log = logging.getLogger("app.worker")
TICK_SECONDS = 60


def run_due_jobs() -> int:
    """Einmal alle Firmen durchgehen und fällige Abrufe starten. Gibt die Anzahl Abrufe zurück."""
    runs = 0
    with SessionLocal() as db:
        tenant_ids = list(db.scalars(select(Tenant.id).where(Tenant.status == TENANT_ACTIVE,
                                                              Tenant.setup_completed.is_(True))))
        for tenant_id in tenant_ids:
            with tenant_scope(db, tenant_id):
                try:
                    if email_import.due(db):
                        email_import.run(db, tenant_id)
                        runs += 1
                except Exception:  # noqa: BLE001 – eine Firma darf die anderen nicht aufhalten
                    db.rollback()
                    log.exception("E-Mail-Abruf für Firma %s fehlgeschlagen", tenant_id)
                try:  # Eingangsordner (nur wenn eingeschaltet, nur lokal)
                    if eingangsordner.enabled(db) and eingangsordner.scan(db, tenant_id).imported:
                        runs += 1
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("Eingangsordner für Firma %s fehlgeschlagen", tenant_id)
                try:  # KI-Auslese neuer Belege im Prüfbereich (nur wenn eingeschaltet)
                    if ki_auslese.nachtragen(db):
                        runs += 1
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("KI-Auslese für Firma %s fehlgeschlagen", tenant_id)
                try:  # KI-Kontovorschläge für importierte Belege (nur wenn eingeschaltet)
                    if kontierung.ki_nachtragen(db):
                        runs += 1
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("KI-Kontovorschläge für Firma %s fehlgeschlagen", tenant_id)
                try:
                    created = vertraege.run_due(db)
                    if created:
                        db.commit()
                        runs += 1
                        log.info("Firma %s: %s Rechnungsentwürfe aus Verträgen angelegt", tenant_id, len(created))
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("Vertragsabrechnung für Firma %s fehlgeschlagen", tenant_id)
                try:  # Rechnungen, angenommene Angebote und Verträge ohne Auftrag -> Auftragsübersicht
                    synced = auftraege.sync(db)
                    db.commit()
                    if synced.changed:
                        runs += 1
                        log.info("Firma %s: %s Aufträge automatisch angelegt", tenant_id, synced.orders)
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("Aufträge einlesen für Firma %s fehlgeschlagen", tenant_id)
    return runs


class BackgroundWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="hintergrund-aufgaben", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(15):  # kurz warten, bis das Programm fertig gestartet ist
            return
        while not self._stop.is_set():
            try:
                run_due_jobs()
            except Exception:  # noqa: BLE001
                log.exception("Hintergrund-Aufgaben fehlgeschlagen")
            self._stop.wait(TICK_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("Hintergrund-Prozess gestartet (Abfrage alle %s Sekunden).", TICK_SECONDS)
    while True:
        run_due_jobs()
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
