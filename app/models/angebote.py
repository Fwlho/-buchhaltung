"""Angebote mit Positionen und Zusatzkosten. Rechenlogik: app/services/calc.py."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TenantMixin

QUOTE_DRAFT = "entwurf"
QUOTE_SENT = "versendet"
QUOTE_ACCEPTED = "angenommen"
QUOTE_REJECTED = "abgelehnt"
QUOTE_EXPIRED = "abgelaufen"  # wird nicht gespeichert, sondern aus „gültig bis“ abgeleitet
QUOTE_STATUS_LABELS = {QUOTE_DRAFT: "Entwurf", QUOTE_SENT: "versendet", QUOTE_ACCEPTED: "angenommen",
                       QUOTE_REJECTED: "abgelehnt", QUOTE_EXPIRED: "abgelaufen"}


class Quote(TenantMixin, Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_quotes_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    recipient_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recipient_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_date: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=QUOTE_DRAFT, nullable=False)
    intro_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    closing_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Zusätzliche interne Kosten (z. B. Gemeinkosten) – nur intern, nie im PDF
    extra_internal_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Zwischengespeicherte Summen (für Listen); maßgeblich ist calc.compute_quote
    net_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    gross_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    email_draft_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer | None"] = relationship()  # noqa: F821
    positions: Mapped[list["QuotePosition"]] = relationship(
        back_populates="quote", order_by="QuotePosition.position", cascade="all, delete-orphan")
    cost_items: Mapped[list["QuoteCostItem"]] = relationship(
        back_populates="quote", order_by="QuoteCostItem.position", cascade="all, delete-orphan")

    @property
    def effective_status(self) -> str:
        if self.status in (QUOTE_DRAFT, QUOTE_SENT) and self.valid_until and self.valid_until < date.today():
            return QUOTE_EXPIRED
        return self.status

    @property
    def status_label(self) -> str:
        return QUOTE_STATUS_LABELS.get(self.effective_status, self.status)

    @property
    def is_editable(self) -> bool:
        return self.status in (QUOTE_DRAFT, QUOTE_SENT)


class QuotePosition(TenantMixin, Base):
    __tablename__ = "quote_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    catalog_item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_items.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_type: Mapped[str] = mapped_column(String(20), default="menge", nullable=False)  # menge/stunden/pauschal
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Einzelpreis, Stundensatz bzw. Pauschalpreis (netto)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    staff_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # Wiederkehrende Leistung
    recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interval: Mapped[str | None] = mapped_column(String(20), nullable=True)
    visits_per_month: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    contract_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Interner Kostensatz (nur mit Sonderrecht „kalkulation_intern“ sichtbar)
    cost_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    quote: Mapped[Quote] = relationship(back_populates="positions")


class QuoteCostItem(TenantMixin, Base):
    """Zusatzkosten in frei benennbaren Blöcken (Anfahrt, Material, Entsorgung …)."""

    __tablename__ = "quote_cost_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    block: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)

    quote: Mapped[Quote] = relationship(back_populates="cost_items")
