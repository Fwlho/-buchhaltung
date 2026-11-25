"""Aufträge (Projektübersicht): je Objekt (Einsatzort beim Kunden) eine aufklappbare Karte mit Aufträgen.

Entschieden 17.11.2026 nach Skizze des Nutzers:
- Ein Auftrag gehört zu einem Kunden und einem Objekt (Objekt-Adresse des Kunden; ohne Objekt-Adresse gilt
  die Hauptadresse des Kunden als Objekt).
- Einzel- oder Dauerauftrag, Stand offen / in Arbeit / erledigt (setzt der Mensch; bei bezahlter Rechnung
  eines Einzelauftrags schlägt das Programm „erledigt“ vor).
- Entsteht aus einem angenommenen Angebot (Daten werden übernommen) oder von Hand. Aus dem Auftrag wird
  eine Rechnung (Entwurf) bzw. ein Vertrag (Dauerauftrag) erzeugt – Aufträge sind keine Buchungsbelege.
"""
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TenantMixin

ORDER_OPEN = "offen"
ORDER_WORKING = "arbeit"
ORDER_DONE = "erledigt"
ORDER_STATUS_LABELS = {ORDER_OPEN: "Offen", ORDER_WORKING: "In Arbeit", ORDER_DONE: "Erledigt"}

ORDER_SINGLE = "einzel"
ORDER_RECURRING = "dauer"
ORDER_KIND_LABELS = {ORDER_SINGLE: "Einzelauftrag", ORDER_RECURRING: "Dauerauftrag"}


class Order(TenantMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_orders_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    # Objekt: Objekt-Adresse des Kunden; leer = Hauptadresse (bzw. freier Text aus dem Angebot)
    address_id: Mapped[int | None] = mapped_column(ForeignKey("customer_addresses.id"), nullable=True, index=True)
    object_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(10), default=ORDER_SINGLE, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default=ORDER_OPEN, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"), nullable=True, index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_contracts.id"), nullable=True, index=True)
    # Herkunft: None = von Hand / Knopf im Angebot; "rechnung"/"vertrag"/"angebot" = automatisch eingelesen
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    customer: Mapped["Customer"] = relationship()  # noqa: F821
    address: Mapped["CustomerAddress | None"] = relationship()  # noqa: F821
    quote: Mapped["Quote | None"] = relationship()  # noqa: F821
    contract: Mapped["RecurringContract | None"] = relationship()  # noqa: F821
    lines: Mapped[list["OrderLine"]] = relationship(
        back_populates="order", order_by="OrderLine.position", cascade="all, delete-orphan")
    tasks: Mapped[list["OrderTask"]] = relationship(
        back_populates="order", order_by="OrderTask.position", cascade="all, delete-orphan")

    @property
    def status_label(self) -> str:
        return ORDER_STATUS_LABELS.get(self.status, self.status)

    @property
    def kind_label(self) -> str:
        return ORDER_KIND_LABELS.get(self.kind, self.kind)

    @property
    def net_total(self) -> Decimal:
        """Summe netto (Dauerauftrag: je Monat)."""
        return sum(((line.quantity * line.unit_price).quantize(Decimal("0.01")) for line in self.lines),
                   Decimal("0.00"))


class OrderLine(TenantMixin, Base):
    """Leistung eines Auftrags (Menge × Preis netto; beim Dauerauftrag je Monat)."""

    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="lines")

    @property
    def net(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))


class OrderTask(TenantMixin, Base):
    """Kleine Abhakliste je Auftrag."""

    __tablename__ = "order_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    order: Mapped[Order] = relationship(back_populates="tasks")
