"""Protokoll der KI-Auslese von Eingangsbelegen (app/services/ki_auslese.py).

Je Aufruf eine Zeile: was die KI vorgeschlagen hat (``proposal``, normalisiert, und ``raw`` = Roh-Antwort),
welche Prüfungen das Programm selbst gerechnet hat (``checks``), welche Felder eingetragen wurden
(``applied``) und – beim Übernehmen – was ein Mensch tatsächlich freigegeben hat (``final``).
Damit ist nachvollziehbar, was die KI vorschlug und was geändert wurde (GoBD).
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantMixin

STATUS_PROPOSED = "vorschlag"
STATUS_TAKEN = "uebernommen"
STATUS_ERROR = "fehler"
STATUS_NO_DOCUMENT = "kein_beleg"


class KiReading(TenantMixin, Base):
    __tablename__ = "ki_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False,
                                             index=True)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)          # text / pdf / bild
    status: Mapped[str] = mapped_column(String(20), default=STATUS_PROPOSED, nullable=False)
    proposal: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    checks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)   # [{"stufe", "text"}]
    applied: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # eingetragene Felder
    final: Mapped[dict | None] = mapped_column(JSON, nullable=True)            # Werte beim Übernehmen
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
