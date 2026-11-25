"""Protokoll des E-Mail-Imports – zugleich Duplikatschutz über die Message-ID (je Firma)."""
from datetime import date

from sqlalchemy import JSON, Date, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantMixin


class EmailImportLog(TenantMixin, Base):
    __tablename__ = "email_import_log"
    __table_args__ = (UniqueConstraint("tenant_id", "message_id", name="uq_email_import_message"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(500), nullable=False)
    # importiert | ohne_anhang | abgelehnt (siehe app/services/email_import.py)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="importiert", server_default="importiert")
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sender: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mail_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    folder: Mapped[str | None] = mapped_column(String(200), nullable=True)
    document_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Wird erst eingetragen, wenn ein Verschieben in den Zielordner bestätigt ist
    moved_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
