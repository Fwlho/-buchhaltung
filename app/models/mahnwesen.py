"""Mahnungen zu Ausgangsrechnungen.

Die Mahnstufen (Name, Frist, Gebühr, Text) stehen in den Firmen-Einstellungen (Gruppe „mahnwesen“).
Jede erstellte Mahnung bekommt eine Nummer aus dem Nummernkreis „mahnung“; ihr PDF wird einmal
erzeugt und unverändert gespeichert.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TenantMixin

MONEY = Numeric(12, 2)


class Dunning(TenantMixin, Base):
    __tablename__ = "dunnings"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False,
                                             index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)        # 1 = Zahlungserinnerung, 2, 3
    level_name: Mapped[str] = mapped_column(String(100), nullable=False)
    number: Mapped[str] = mapped_column(String(60), nullable=False)
    dunning_date: Mapped[date] = mapped_column(Date, nullable=False)
    pay_until: Mapped[date] = mapped_column(Date, nullable=False)
    open_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)   # offener Rechnungsbetrag
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)    # bisherige + diese
    interest: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id"), nullable=True)
    email_draft_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    document: Mapped["Document"] = relationship()  # noqa: F821
    file: Mapped["StoredFile | None"] = relationship()  # noqa: F821
