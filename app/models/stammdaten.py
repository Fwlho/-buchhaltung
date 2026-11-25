"""Stammdaten: Kunden (mit Objekt-/Leistungsadressen), Lieferanten, Artikel-/Leistungskatalog.

Stammdaten werden nicht gelöscht, sondern archiviert (is_active = false) – später
verweisen Belege darauf, die nach GoBD erhalten bleiben müssen.
"""
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TenantMixin

CUSTOMER_KINDS = (("firma", "Firma"), ("privat", "Privatperson"), ("behoerde", "Behörde / öffentlicher Auftraggeber"))


class _AddressColumns:
    street: Mapped[str | None] = mapped_column(String(200), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)


class Customer(TenantMixin, _AddressColumns, Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_customers_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Kundennummer = Debitorenkonto (für den DATEV-Export)
    number: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="firma", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ust_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    leitweg_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # § 13b: Hinweis „Kunde schuldet die Umsatzsteuer“ bei neuen Rechnungen vorauswählen
    reverse_charge: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Abweichende Rechnungsadresse (leer = Hauptadresse)
    billing_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    billing_street: Mapped[str | None] = mapped_column(String(200), nullable=True)
    billing_zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    billing_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    billing_country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    addresses: Mapped[list["CustomerAddress"]] = relationship(
        back_populates="customer", order_by="CustomerAddress.label"
    )

    @property
    def has_billing_address(self) -> bool:
        return bool(self.billing_street or self.billing_city)

    @property
    def kind_label(self) -> str:
        return dict(CUSTOMER_KINDS).get(self.kind, self.kind)


class CustomerAddress(TenantMixin, _AddressColumns, Base):
    """Objekt-/Leistungsadresse eines Kunden (z. B. „Bürogebäude Nord“)."""

    __tablename__ = "customer_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False,
                                             index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="addresses")


class Supplier(TenantMixin, _AddressColumns, Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_suppliers_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Lieferantennummer = Kreditorenkonto (für den DATEV-Export)
    number: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ust_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    bic: Mapped[str | None] = mapped_column(String(11), nullable=True)
    our_customer_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # § 13b: Fall (inland/eu/drittland) – bei neuen Belegen dieses Lieferanten vorausgewählt
    reverse_charge: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


UNITS = ("Stück", "Std.", "m²", "m", "m³", "kg", "l", "km", "Tag", "Monat", "pauschal")
ITEM_KINDS = (("leistung", "Leistung"), ("artikel", "Artikel / Material"))
BILLING_TYPES = (
    ("menge", "Menge × Preis"),
    ("stunden", "Stunden × Satz × Personen"),
    ("pauschal", "Pauschal"),
)


class CatalogItem(TenantMixin, Base):
    __tablename__ = "catalog_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="leistung", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    billing_type: Mapped[str] = mapped_column(String(20), default="menge", nullable=False)
    # Einzelpreise mit bis zu 4 Nachkommastellen (z. B. 0,125 €/m²); Summen später mit 2
    price_net: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # Interner Kostensatz – nur mit Sonderrecht „kalkulation_intern“ sichtbar, nie im PDF
    cost_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def kind_label(self) -> str:
        return dict(ITEM_KINDS).get(self.kind, self.kind)

    @property
    def billing_label(self) -> str:
        return dict(BILLING_TYPES).get(self.billing_type, self.billing_type)
