"""Benutzer, Firmen-Zugehörigkeit, Sitzungen, Einladungen, Login-Codes.

Das sind Plattform-Tabellen (ohne TenantMixin): Ein Benutzer kann zu mehreren
Firmen gehören; die Zugehörigkeit samt Rechten steht in ``memberships``.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampMixin, utcnow


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Zweites Passwort für Admin-Bereiche (app/services/admin_freigabe.py); None = noch nicht festgelegt
    admin_password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Anmeldung per Authenticator-App (app/services/totp.py): Geheimnis verschlüsselt, aktiv ab totp_enabled_at
    totp_secret: Mapped[str | None] = mapped_column(String(300), nullable=True)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    totp_last_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    totp_recovery: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")


class Membership(TimestampMixin, Base):
    """Zugehörigkeit eines Benutzers zu einer Firma mit Rolle und Rechten.

    ``permissions``: {modul: "keine" | "lesen" | "bearbeiten"}
    ``extra_rights``: Liste von Sonderrechten (z. B. "festschreiben")
    """

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    extra_rights: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="memberships")
    tenant: Mapped["Tenant"] = relationship()  # noqa: F821


class UserSession(Base):
    """Serverseitige Sitzung. Im Cookie steht nur ein zufälliges Token,
    in der Datenbank nur dessen Prüfsumme."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # "code" = Passwort ok, Bestätigungscode fehlt noch; "ok" = vollständig angemeldet
    stage: Mapped[str] = mapped_column(String(10), nullable=False)
    active_tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Admin-Bereiche freigegeben bis (verlängert sich bei jeder Nutzung); None = gesperrt
    admin_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)

    user: Mapped[User] = relationship()


class Invitation(Base):
    """Einladung in eine Firma (zeitlich begrenzter Link)."""

    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    extra_rights: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    tenant: Mapped["Tenant"] = relationship()  # noqa: F821

    @property
    def is_open(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None and self.expires_at > utcnow()


class LoginCode(Base):
    """Bestätigungscode (2. Faktor) – gespeichert nur als HMAC."""

    __tablename__ = "login_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginThrottle(Base):
    """Zähler für Fehlversuche (Login-Rate-Limit) je IP bzw. E-Mail."""

    __tablename__ = "login_throttle"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
