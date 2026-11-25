"""Firmen (Mandanten) und ihre Einstellungen."""
from typing import Any

from sqlalchemy import JSON, Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantMixin, TimestampMixin

TENANT_ACTIVE = "aktiv"
TENANT_LOCKED = "gesperrt"


class Tenant(TimestampMixin, Base):
    """Plattform-Tabelle: eine Firma. Der Name hier dient nur der Verwaltung durch
    den Plattform-Admin; der Firmenname für Belege steht in den Einstellungen."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=TENANT_ACTIVE, nullable=False)
    setup_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Schlüssel für die Dateien dieser Firma, selbst verschlüsselt mit dem Hauptschlüssel (app/services/tresor.py)
    file_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def is_locked(self) -> bool:
        return self.status == TENANT_LOCKED


class TenantSetting(TenantMixin, Base):
    """Eine Einstellung einer Firma (Schlüssel/Wert). Welche Schlüssel es gibt und
    welchen Typ sie haben, steht in app/services/einstellungen.py."""

    __tablename__ = "tenant_settings"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_tenant_settings_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
