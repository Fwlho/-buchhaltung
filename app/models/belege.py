"""Belege (Eingangs- und später Ausgangsrechnungen), Steuer-Positionen, Zahlungen.

Wichtige Regeln:
- ``pending = true``  -> liegt im Prüfbereich, zählt nirgends mit (automatischer Filter).
- ``state``           -> "entwurf" (änderbar, löschbar) oder "festgeschrieben" (unveränderbar, GoBD).
- ``status``          -> "offen", "bezahlt", "storniert" (ergibt sich aus Zahlungen bzw. Storno).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, PendingMixin, TenantMixin

DIRECTION_IN = "eingang"
DIRECTION_OUT = "ausgang"

STATE_DRAFT = "entwurf"
STATE_FINAL = "festgeschrieben"

STATUS_OPEN = "offen"
STATUS_PAID = "bezahlt"
STATUS_CANCELLED = "storniert"
STATUS_DONE = "erledigt"  # z. B. Stornorechnung – nichts mehr offen
STATUS_LABELS = {STATUS_OPEN: "offen", STATUS_PAID: "bezahlt", STATUS_CANCELLED: "storniert",
                 STATUS_DONE: "erledigt"}

KIND_INVOICE = "rechnung"
KIND_STORNO = "storno"

MONEY = Numeric(12, 2)


class Document(TenantMixin, PendingMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_tenant_direction_date", "tenant_id", "direction", "doc_date"),
        # Eigene Belegnummern sind je Firma eindeutig (Sicherheitsnetz zur lückenlosen Vergabe)
        Index("uq_documents_own_number", "tenant_id", "number", unique=True,
              sqlite_where=text("direction = 'ausgang'"), postgresql_where=text("direction = 'ausgang'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="rechnung", nullable=False)
    state: Mapped[str] = mapped_column(String(20), default=STATE_DRAFT, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=STATUS_OPEN, nullable=False)
    # Herkunft: upload, email, duplikat, manuell
    source: Mapped[str] = mapped_column(String(20), default="upload", nullable=False)

    number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    partner_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)

    net_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    vat_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    gross_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    account: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Automatischer Kontovorschlag (app/services/kontierung.py): "lieferant" | "gelernt" | "ki" |
    # "ki_ohne" (KI gefragt, kein sicherer Vorschlag) | "ki_fehler"; dazu die Begründung für die Anzeige
    account_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    account_hint: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    skonto_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    skonto_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Felder, die die automatische Auslese nicht sicher erkannt hat
    review_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Ampel der KI-Auslese je Feld: {"partner": "ok" | "pruefen" | "fehlt", …} (ki_auslese.py)
    field_states: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- nur Ausgangsrechnungen: Angaben, wie sie auf der Rechnung stehen (Stand beim Erstellen)
    recipient_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recipient_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intro_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    closing_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Gewählte Hinweise, z. B. ["13b", "35a", "14b"]; § 19 kommt automatisch bei Kleinunternehmern
    hints: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    labor_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)  # § 35a Lohnanteil
    payment_qr: Mapped[str | None] = mapped_column(String(20), nullable=True)   # girocode / keiner
    # § 13b UStG (services/steuerschuld.py): Eingang = Fall inland/eu/drittland, die eigene Firma schuldet die
    # Steuer (Netto × reverse_charge_rate); Ausgang = Fall inland/eu beim Hinweis „13b“ (Kunde schuldet sie)
    reverse_charge: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reverse_charge_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    email_draft_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # „Entwurf erstellt am …“
    # E-Rechnung beim Festschreiben: "zugferd" (im PDF eingebettet), "xrechnung" (eigene XML-Datei) oder "keine"
    e_invoice_format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    xml_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id", name="fk_documents_xml_file_id"),
                                                    nullable=True)

    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id"), nullable=True)
    storno_of_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"), nullable=True, index=True)
    # Entwurf aus einem Vertrag über wiederkehrende Leistungen
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("recurring_contracts.id", name="fk_documents_contract_id"), nullable=True, index=True)
    # Rechnung aus einem Auftrag (Projektübersicht)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", name="fk_documents_order_id"), nullable=True, index=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Ablage-Ordner (app/services/ablage_ordner.py): wann/wohin die Datei nach dem Festschreiben kopiert wurde
    ablage_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ablage_pfad: Mapped[str | None] = mapped_column(String(500), nullable=True)
    finalized_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    lines: Mapped[list["DocumentLine"]] = relationship(
        back_populates="document", order_by="DocumentLine.position", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="document", order_by="Payment.paid_on", cascade="all, delete-orphan"
    )
    supplier: Mapped["Supplier | None"] = relationship()  # noqa: F821
    customer: Mapped["Customer | None"] = relationship()  # noqa: F821
    file: Mapped["StoredFile | None"] = relationship(foreign_keys=[file_id])  # noqa: F821
    xml_file: Mapped["StoredFile | None"] = relationship(foreign_keys=[xml_file_id])  # noqa: F821
    storno_of: Mapped["Document | None"] = relationship(remote_side="Document.id")

    @property
    def is_draft(self) -> bool:
        return self.state == STATE_DRAFT

    @property
    def is_final(self) -> bool:
        return self.state == STATE_FINAL

    @property
    def paid_total(self) -> Decimal:
        """Bezahlt inkl. abgezogenem Skonto (brutto)."""
        return sum((p.amount + p.skonto for p in self.payments), Decimal("0.00"))

    @property
    def open_amount(self) -> Decimal:
        if self.status in (STATUS_CANCELLED, STATUS_DONE) or self.gross_total is None:
            return Decimal("0.00")
        return max(self.gross_total - self.paid_total, Decimal("0.00"))

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


class DocumentLine(TenantMixin, Base):
    """Position eines Belegs. Bei Eingangsrechnungen: eine Zeile je Steuersatz."""

    __tablename__ = "document_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False,
                                             index=True)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    net: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    vat: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gross: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    document: Mapped[Document] = relationship(back_populates="lines")


class NumberRange(TenantMixin, Base):
    """Zuletzt vergebene laufende Nummer je Firma, Belegart und Jahr (lückenlose Vergabe)."""

    __tablename__ = "number_ranges"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "year", name="uq_number_ranges"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # rechnung, storno, angebot, mahnung
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False)


class Payment(TenantMixin, Base):
    """Zahlung zu einem Beleg. ``skonto`` = abgezogener Skonto (brutto)."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False,
                                             index=True)
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    skonto: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0.00"), nullable=False)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Herkunft aus dem Bankabgleich (sonst von Hand erfasst)
    bank_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transactions.id", name="fk_payments_bank_transaction_id"), nullable=True, index=True)

    document: Mapped[Document] = relationship(back_populates="payments")
    bank_transaction: Mapped["BankTransaction | None"] = relationship(back_populates="payments")  # noqa: F821
