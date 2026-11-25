"""Firmen-Export (KONZEPT.md 6.15): alle Daten EINER Firma als ZIP – für Datenschutz-Auskunft oder Kündigung.

Inhalt: je Tabelle eine JSON-Datei (nur Zeilen dieser Firma), alle gespeicherten Dateien (Belege, PDFs, Logo,
Kontoauszüge) und eine Erklärung. Gespeicherte Passwörter (IMAP/SMTP) werden NICHT mit exportiert,
Passwort-Hashes der Benutzer ebenfalls nicht.
"""
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AuditLog, Membership, StoredFile, Tenant, User
from app.services import ablage, einstellungen

# Plattform-Tabellen werden unten gezielt (gefiltert) ausgegeben
_SKIP = {"tenants", "users", "memberships", "sessions", "invitations", "login_codes", "login_throttle", "audit_log"}


def _value(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, bytes):
        return None
    return v


def _rows(result) -> list[dict]:
    return [{k: _value(v) for k, v in row._mapping.items()} for row in result]


def build(db: Session, tenant_id: int) -> bytes:
    """ZIP mit allen Daten der Firma. Muss mit aktiver Firma ``tenant_id`` aufgerufen werden."""
    if db.info.get("tenant_id") != tenant_id:
        raise ValueError("Firmen-Export nur für die aktive Firma.")
    buf = io.BytesIO()
    counts = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def put(name: str, rows: list[dict]) -> None:
            counts[name] = len(rows)
            zf.writestr(f"daten/{name}.json", json.dumps(rows, ensure_ascii=False, indent=1))

        for table in Base.metadata.sorted_tables:
            if table.name in _SKIP or "tenant_id" not in table.c:
                continue
            stmt = (select(table).where(table.c.tenant_id == tenant_id)
                    .execution_options(include_pending=True).order_by(*table.primary_key.columns))
            rows = _rows(db.execute(stmt))
            if table.name == "tenant_settings":
                for row in rows:
                    if row["key"] in einstellungen.SECRET_KEYS:
                        row["value"] = "(Passwort – nicht exportiert)"
            put(table.name, rows)

        tenant = db.get(Tenant, tenant_id)
        put("firma", [{"id": tenant.id, "name": tenant.name, "status": tenant.status,
                       "angelegt": _value(tenant.created_at)}])
        members = list(db.scalars(select(Membership).where(Membership.tenant_id == tenant_id)))
        users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([m.user_id for m in members])))}
        put("benutzer", [{"name": users[m.user_id].name, "email": users[m.user_id].email, "rolle": m.role,
                          "rechte": m.permissions, "sonderrechte": m.extra_rights, "aktiv": m.is_active}
                         for m in members if m.user_id in users])
        put("aenderungsprotokoll", _rows(db.execute(
            select(AuditLog.__table__).where(AuditLog.__table__.c.tenant_id == tenant_id).order_by(AuditLog.id))))

        for stored in db.scalars(select(StoredFile).order_by(StoredFile.id)):
            path = ablage.absolute_path(stored)
            if path.exists():  # entschlüsselt – der Export ist für die Firma selbst
                zf.writestr(f"dateien/{stored.id}_{stored.original_name}", ablage.read_bytes(stored, db))

        lines = [f"Datenexport der Firma „{tenant.name}“ vom {datetime.now():%d.%m.%Y %H:%M}", "",
                 "daten/*.json  – alle Datensätze je Tabelle (Beträge als Text, Datum im Format JJJJ-MM-TT)",
                 "dateien/      – alle gespeicherten Dateien (Dateiname beginnt mit der ID aus daten/files.json)",
                 "", "Gespeicherte Passwörter und Passwort-Hashes sind nicht enthalten.", "", "Anzahl Datensätze:"]
        lines += [f"  {name}: {count}" for name, count in counts.items()]
        zf.writestr("LIESMICH.txt", "\r\n".join(lines))
    return buf.getvalue()
