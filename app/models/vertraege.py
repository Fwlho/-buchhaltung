"""Verträge über wiederkehrende Leistungen (KONZEPT.md 6.8) – daraus entstehen monatlich Rechnungsentwürfe.

Abrechnung je Vertrag wählbar (Entscheidung 28.10.2026):
- „nachtraeglich“: am Monatsanfang für den abgelaufenen Monat (Standard),
- „voraus“: am Monatsanfang für den laufenden Monat.
Die Entwürfe werden nie automatisch festgeschrieben oder versendet.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TenantMixin

CONTRACT_ACTIVE = "aktiv"
CONTRACT_PAUSED = "pausiert"
CONTRACT_ENDED = "beendet"
CONTRACT_STATUS_LABELS = {CONTRACT_ACTIVE: "aktiv", CONTRACT_PAUSED: "pausiert", CONTRACT_ENDED: "beendet"}
BILLING_MODES = {"nachtraeglich": "am Monatsanfang für den Vormonat (Leistung erbracht)",
                 "voraus": "am Monatsanfang für den laufenden Monat (im Voraus)"}


class RecurringContract(TenantMixin, Base):
    __tablename__ = "recurring_contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    object_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_mode: Mapped[str] = mapped_column(String(20), default="nachtraeglich", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=CONTRACT_ACTIVE, nullable=False)
    start_month: Mapped[date] = mapped_column(Date, nullable=False)      # erster abzurechnender Monat (Tag 1)
    end_month: Mapped[date | None] = mapped_column(Date, nullable=True)  # letzter Monat (Tag 1), leer = unbefristet
    next_month: Mapped[date] = mapped_column(Date, nullable=False)       # nächster abzurechnender Monat (Tag 1)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    customer: Mapped["Customer"] = relationship()  # noqa: F821
    lines: Mapped[list["RecurringContractLine"]] = relationship(
        back_populates="contract", order_by="RecurringContractLine.position", cascade="all, delete-orphan")

    @property
    def status_label(self) -> str:
        return CONTRACT_STATUS_LABELS.get(self.status, self.status)

    @property
    def monthly_net(self) -> Decimal:
        return sum(((line.quantity * line.unit_price).quantize(Decimal("0.01")) for line in self.lines),
                   Decimal("0.00"))


class RecurringContractLine(TenantMixin, Base):
    """Monatliche Position eines Vertrags (Menge × Preis netto je Monat)."""

    __tablename__ = "recurring_contract_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("recurring_contracts.id", ondelete="CASCADE"),
                                             nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)

    contract: Mapped[RecurringContract] = relationship(back_populates="lines")
