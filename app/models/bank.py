"""Bankabgleich: importierte Kontoauszüge und ihre Umsätze.

Eine Zuordnung Umsatz -> Rechnung ist eine ganz normale Zahlung (Tabelle payments) mit Verweis
auf den Umsatz (``payments.bank_transaction_id``). Ein Umsatz kann so auf mehrere Rechnungen
verteilt werden (Sammelüberweisung); Löschen der Zahlung gibt den Betrag wieder frei.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TenantMixin

TX_OPEN = "offen"
TX_DONE = "erledigt"  # vollständig zugeordnet oder als Finanzamt/privat/ignoriert markiert

# Umsätze ohne Rechnung
CATEGORIES = {
    "ust": "Umsatzsteuer an das / vom Finanzamt",
    "privat": "Privatentnahme / Privateinlage",
    "ignorieren": "nicht relevant (z. B. Umbuchung zwischen eigenen Konten)",
}


class BankImport(TenantMixin, Base):
    __tablename__ = "bank_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    format: Mapped[str] = mapped_column(String(10), nullable=False)          # csv, camt
    # zuordnung = CSV wartet auf die Spalten-Zuordnung, fertig = Umsätze übernommen
    status: Mapped[str] = mapped_column(String(20), default="fertig", nullable=False)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"), nullable=False)
    count_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    count_duplicate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    file: Mapped["StoredFile"] = relationship()  # noqa: F821


class BankTransaction(TenantMixin, Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (UniqueConstraint("tenant_id", "fingerprint", name="uq_bank_transactions_fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("bank_imports.id"), nullable=False, index=True)
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)   # + Eingang, − Ausgang
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)      # Auftraggeber / Empfänger
    iban: Mapped[str | None] = mapped_column(String(40), nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)          # Verwendungszweck
    # Doppelt importierte Umsätze (überlappende Kontoauszüge) werden daran erkannt
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=TX_OPEN, nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)

    bank_import: Mapped[BankImport] = relationship()
    payments: Mapped[list["Payment"]] = relationship(back_populates="bank_transaction")  # noqa: F821

    @property
    def assigned(self) -> Decimal:
        return sum((p.amount for p in self.payments), Decimal("0.00"))

    @property
    def remaining(self) -> Decimal:
        """Noch nicht zugeordneter Betrag (immer positiv)."""
        if self.category:
            return Decimal("0.00")
        return max(abs(self.amount) - self.assigned, Decimal("0.00"))

    @property
    def category_label(self) -> str:
        return CATEGORIES.get(self.category or "", "")
