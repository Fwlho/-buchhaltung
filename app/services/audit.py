"""Änderungsprotokoll schreiben und lesen."""
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog

MASK = "•••"


def log(
    db: Session,
    *,
    action: str,
    entity: str,
    entity_id: Any = None,
    tenant_id: int | None = None,
    user_id: int | None = None,
    changes: dict | None = None,
    ip: str | None = None,
) -> None:
    """Fügt einen Protokolleintrag hinzu (wird mit dem nächsten commit gespeichert).

    ``changes``: {feld: {"alt": …, "neu": …}} oder beliebige Zusatzangaben.
    """
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            entity=entity,
            entity_id=None if entity_id is None else str(entity_id),
            changes=_json_safe(changes),
            ip=ip,
        )
    )


def _plain(value: Any) -> Any:
    """Für die JSON-Spalte: Beträge und Datumswerte als Text (sonst schlägt das Speichern fehl)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(v) for v in value]
    return _plain(value)


def diff(old: dict, new: dict, secret_keys: set[str] | frozenset = frozenset()) -> dict:
    """Nur geänderte Felder als {feld: {"alt", "neu"}}; Geheimnisse maskiert."""
    result = {}
    for key in sorted(set(old) | set(new)):
        before, after = _plain(old.get(key)), _plain(new.get(key))
        if before != after:
            if key in secret_keys:
                result[key] = {"alt": MASK if before else "", "neu": MASK if after else ""}
            else:
                result[key] = {"alt": before, "neu": after}
    return result


def entries_for_tenant(db: Session, tenant_id: int, limit: int = 200) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))
