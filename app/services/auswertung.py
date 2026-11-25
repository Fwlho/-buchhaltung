"""Auswertungen: EÜR, Umsatzsteuer (UStVA-Kennzahlen), Kleinunternehmer-Grenzen, Kennzahlen der Übersicht.

Grundsätze (KONZEPT.md 6.9/6.10):
- Belege aus dem Prüfbereich zählen NIE mit (automatischer Filter in app/database.py).
- EÜR nach Zufluss/Abfluss: Jede Zahlung wird anteilig auf die Steuersätze des Belegs verteilt.
- Umsatzsteuer: Ist-Versteuerung nach Zahlungseingang, Soll-Versteuerung nach Rechnungsdatum.
  Vorsteuer in beiden Fällen nach Rechnungsdatum (§ 15 UStG); gezogener Skonto mindert sie im
  Zeitraum der Zahlung.
- Ergebnisse sind Hilfen und ersetzen keine Steuerberatung.
"""
import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, Payment
from app.models.belege import (
    DIRECTION_IN,
    DIRECTION_OUT,
    KIND_INVOICE,
    STATE_FINAL,
    STATUS_CANCELLED,
    STATUS_OPEN,
)
from app.services import einstellungen, konten
from app.services.belege import tax_lines_of
from app.services.betraege import TaxLine, group_by_rate, split_by_rate

ZERO = Decimal("0.00")
MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")
KU_LIMIT_PREVIOUS = Decimal("25000")
KU_LIMIT_CURRENT = Decimal("100000")


# ------------------------------------------------------------------ Zeiträume
@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str
    kind: str = "frei"
    year: int = 0
    nr: int = 0


def period(kind: str, year: int, nr: int = 1, start: date | None = None, end: date | None = None) -> Period:
    if kind == "monat":
        nr = min(max(nr, 1), 12)
        return Period(date(year, nr, 1), date(year, nr, calendar.monthrange(year, nr)[1]), f"{MONTHS[nr - 1]} {year}",
                      kind, year, nr)
    if kind == "quartal":
        nr = min(max(nr, 1), 4)
        first = 3 * (nr - 1) + 1
        return Period(date(year, first, 1), date(year, first + 2, calendar.monthrange(year, first + 2)[1]),
                      f"{nr}. Quartal {year}", kind, year, nr)
    if kind == "frei" and start and end:
        from app.formatting import format_date

        return Period(start, end, f"{format_date(start)} – {format_date(end)}", kind, start.year)
    return Period(date(year, 1, 1), date(year, 12, 31), f"Jahr {year}", "jahr", year)


def vat_period_kind(db: Session) -> str:
    return {"monatlich": "monat", "quartalsweise": "quartal"}.get(einstellungen.get(db, "steuer.voranmeldung"), "jahr")


def vat_periods(kind: str, year: int) -> list[Period]:
    count = {"monat": 12, "quartal": 4}.get(kind, 1)
    return [period(kind, year, n) for n in range(1, count + 1)]


def period_containing(kind: str, day: date) -> Period:
    if kind == "monat":
        return period("monat", day.year, day.month)
    if kind == "quartal":
        return period("quartal", day.year, (day.month - 1) // 3 + 1)
    return period("jahr", day.year)


# ------------------------------------------------------------------ Hilfen
def _chart(db: Session) -> str:
    return einstellungen.get(db, "konten.rahmen") or "SKR03"


def small_business(db: Session) -> bool:
    return bool(einstellungen.get(db, "steuer.kleinunternehmer"))


def tax_groups(doc: Document) -> list[TaxLine]:
    """Steuersätze eines Belegs: Eingangsrechnung = Zeilen je Satz, Ausgangsrechnung = Summe je Satz."""
    if doc.direction == DIRECTION_IN:
        return tax_lines_of(doc)
    return group_by_rate((line.vat_rate, line.net) for line in doc.lines)


@dataclass
class Flow:
    """Ein Zahlungsanteil zu einem Steuersatz."""

    day: date
    direction: str
    doc: Document
    rate: Decimal
    net: Decimal
    vat: Decimal
    gross: Decimal
    account: str
    account_label: str
    group: str


def flows(db: Session, start: date, end: date) -> list[Flow]:
    """Alle Zahlungen im Zeitraum, je Steuersatz aufgeteilt (nur geprüfte Belege)."""
    chart = _chart(db)
    ku = small_business(db)
    stmt = (select(Payment, Document).join(Document, Document.id == Payment.document_id)
            .where(Payment.paid_on >= start, Payment.paid_on <= end).order_by(Payment.paid_on, Payment.id))
    result = []
    for payment, doc in db.execute(stmt).all():
        groups = tax_groups(doc)
        for part in split_by_rate(payment.amount, groups):
            if doc.direction == DIRECTION_OUT:
                zero_doc = all(g.rate == 0 for g in groups)
                account, label, group = konten.income_account(chart, part.rate, doc.hints, ku and zero_doc,
                                                              doc.reverse_charge)
            else:
                account = doc.account or ""
                label = konten.account_label(chart, doc.account) or "ohne Konto"
                group = konten.expense_group(doc.account)
            result.append(Flow(payment.paid_on, doc.direction, doc, part.rate, part.net, part.vat, part.gross,
                               account, label, group))
    return result


# -------------------------------------------------------------------- EÜR
@dataclass
class EuerGroup:
    label: str
    net: Decimal = ZERO
    accounts: dict[str, list] = field(default_factory=dict)  # Konto -> [Bezeichnung, Netto]

    def add(self, account: str, label: str, net: Decimal) -> None:
        self.net += net
        entry = self.accounts.setdefault(account or label, [label, ZERO])
        entry[1] += net


@dataclass
class Euer:
    period: Period
    income: list[EuerGroup]
    vat_received: Decimal
    expenses: list[EuerGroup]
    vat_paid: Decimal
    income_total: Decimal
    expense_total: Decimal
    surplus: Decimal
    hints: list[str]
    vat_refund: Decimal = ZERO     # vom Finanzamt erstattete Umsatzsteuer (Bankabgleich)
    vat_to_office: Decimal = ZERO  # an das Finanzamt gezahlte Umsatzsteuer (Bankabgleich)

    @property
    def income_net(self) -> Decimal:
        return sum((g.net for g in self.income), ZERO)

    @property
    def expense_net(self) -> Decimal:
        return sum((g.net for g in self.expenses), ZERO)


def euer(db: Session, p: Period) -> Euer:
    income: dict[str, EuerGroup] = {}
    expenses: dict[str, EuerGroup] = {}
    vat_received = vat_paid = ZERO
    hints = []
    for f in flows(db, p.start, p.end):
        if f.direction == DIRECTION_OUT:
            income.setdefault(f.group, EuerGroup(f.group)).add(f.account, f.account_label, f.net)
            vat_received += f.vat
        else:
            expenses.setdefault(f.group, EuerGroup(f.group)).add(f.account, f.account_label, f.net)
            vat_paid += f.vat
            if not f.account and "ohne Konto" not in " ".join(hints):
                hints.append("Einige Ausgaben haben kein Konto und stehen unter „Übrige Betriebsausgaben“ "
                             "(ohne Konto) – bitte bei den Eingangsrechnungen ergänzen.")
    if "Bewirtungsaufwendungen" in expenses:
        hints.append("Bewirtungsaufwendungen sind steuerlich nur zu 70 % abziehbar – hier ist der volle Betrag "
                     "ausgewiesen.")
    vat_refund, vat_to_office = vat_office_payments(db, p.start, p.end)
    hints.append("Umsatzsteuer-Zahlungen an das bzw. Erstattungen vom Finanzamt zählen mit, wenn sie im "
                 "Bankabgleich als „Umsatzsteuer an das / vom Finanzamt“ markiert sind.")
    if p.start.month == 1 or p.end.month == 12:
        hints.append("10-Tage-Regel: Regelmäßig wiederkehrende Zahlungen (z. B. Miete), die bis 10 Tage vor oder "
                     "nach dem Jahreswechsel fließen, gehören in das Jahr, zu dem sie wirtschaftlich gehören.")
    order = {label: i for i, label in enumerate(konten.EXPENSE_GROUP_ORDER)}
    income_list = sorted(income.values(), key=lambda g: list(konten.EUER_INCOME_GROUPS.values()).index(g.label)
                         if g.label in konten.EUER_INCOME_GROUPS.values() else 99)
    expense_list = sorted(expenses.values(), key=lambda g: order.get(g.label, 99))
    income_total = sum((g.net for g in income_list), ZERO) + vat_received + vat_refund
    expense_total = sum((g.net for g in expense_list), ZERO) + vat_paid + vat_to_office
    return Euer(p, income_list, vat_received, expense_list, vat_paid, income_total, expense_total,
                income_total - expense_total, hints, vat_refund, vat_to_office)


def vat_office_payments(db: Session, start: date, end: date) -> tuple[Decimal, Decimal]:
    """(Erstattungen vom, Zahlungen an das Finanzamt) – Bankumsätze mit Kategorie „ust“."""
    from app.models import BankTransaction

    stmt = select(BankTransaction.amount).where(BankTransaction.category == "ust",
                                                BankTransaction.booking_date >= start,
                                                BankTransaction.booking_date <= end)
    amounts = list(db.scalars(stmt))
    return (sum((a for a in amounts if a > 0), ZERO), sum((-a for a in amounts if a < 0), ZERO))


def euer_csv(result: Euer) -> bytes:
    """CSV für Excel (Semikolon, Dezimalkomma, UTF-8 mit BOM)."""
    from app.formatting import format_decimal

    rows = [["Einnahmen-Überschuss-Rechnung", result.period.label, "", ""],
            ["Bereich", "Position", "Konto", "Betrag (EUR)"]]
    for g in result.income:
        for account, (label, net) in g.accounts.items():
            rows.append(["Einnahmen", g.label, f"{account} {label}".strip(), format_decimal(net)])
    rows.append(["Einnahmen", konten.EUER_VAT_RECEIVED, "", format_decimal(result.vat_received)])
    if result.vat_refund:
        rows.append(["Einnahmen", konten.EUER_VAT_REFUND, "", format_decimal(result.vat_refund)])
    rows.append(["Einnahmen", "Summe Betriebseinnahmen", "", format_decimal(result.income_total)])
    for g in result.expenses:
        for account, (label, net) in g.accounts.items():
            rows.append(["Ausgaben", g.label, label if account == label else f"{account} {label}",
                         format_decimal(net)])
    rows.append(["Ausgaben", konten.EUER_VAT_PAID, "", format_decimal(result.vat_paid)])
    if result.vat_to_office:
        rows.append(["Ausgaben", konten.EUER_VAT_TO_OFFICE, "", format_decimal(result.vat_to_office)])
    rows.append(["Ausgaben", "Summe Betriebsausgaben", "", format_decimal(result.expense_total)])
    rows.append(["Ergebnis", "Überschuss" if result.surplus >= 0 else "Verlust", "", format_decimal(result.surplus)])

    def cell(text: str) -> str:
        text = str(text)
        if text[:1] in ("=", "+", "-", "@") and not text[1:2].isdigit():
            text = "'" + text  # keine Formeln in Excel einschleusen
        return '"' + text.replace('"', '""') + '"' if any(c in text for c in ';"\n') else text

    return ("﻿" + "\r\n".join(";".join(cell(c) for c in row) for row in rows) + "\r\n").encode("utf-8")


# ---------------------------------------------------------------- Umsatzsteuer
@dataclass
class Ust:
    period: Period
    mode: str
    small_business: bool
    outputs: dict[Decimal, list]   # Satz -> [Bemessungsgrundlage, Steuer]
    reverse_charge_base: Decimal   # Ausgang § 13b Inland (Kz 60)
    input_tax: Decimal
    skonto_correction: Decimal
    hints: list[str]
    # § 13b (services/steuerschuld.py): Ausgang an EU-Kunden (Kz 21), Eingang je Kennzahl-Paar
    eu_services_base: Decimal = ZERO
    rc_in: dict = field(default_factory=dict)          # "46"/"84" -> [Bemessungsgrundlage, Steuer]
    rc_input_tax: Decimal = ZERO                       # Kz 67 (Kleinunternehmer: 0)
    zm: list = field(default_factory=list)             # Zusammenfassende Meldung: [{ust_id, name, amount}]

    @property
    def output_tax(self) -> Decimal:
        return sum((v[1] for v in self.outputs.values()), ZERO)

    @property
    def rc_tax(self) -> Decimal:
        """Umsatzsteuer, die die eigene Firma nach § 13b schuldet."""
        return sum((v[1] for v in self.rc_in.values()), ZERO)

    @property
    def input_tax_net(self) -> Decimal:
        return self.input_tax - self.skonto_correction

    @property
    def tax_due(self) -> Decimal:
        return self.output_tax + self.rc_tax

    @property
    def tax_deductible(self) -> Decimal:
        return self.input_tax_net + self.rc_input_tax

    @property
    def payable(self) -> Decimal:
        """Zahllast (positiv) bzw. Erstattung (negativ)."""
        return self.tax_due - self.tax_deductible

    def kennzahlen(self) -> list[tuple[str, str, Decimal]]:
        """Vorschlag für die UStVA (Bemessungsgrundlagen in vollen Euro, abgerundet)."""
        def whole(v: Decimal) -> Decimal:
            return v.quantize(Decimal(1), rounding=ROUND_DOWN)

        rows = []
        if Decimal(19) in self.outputs:
            rows.append(("81", "Umsätze zu 19 % (Bemessungsgrundlage, volle Euro)", whole(self.outputs[Decimal(19)][0])))
        if Decimal(7) in self.outputs:
            rows.append(("86", "Umsätze zu 7 % (Bemessungsgrundlage, volle Euro)", whole(self.outputs[Decimal(7)][0])))
        if self.eu_services_base:
            rows.append(("21", "Nicht steuerbare sonstige Leistungen an Unternehmer in anderen EU-Ländern "
                               "(§ 18b, der Kunde schuldet die Steuer)", whole(self.eu_services_base)))
        if self.reverse_charge_base:
            rows.append(("60", "Umsätze, für die der Leistungsempfänger die Steuer schuldet (§ 13b)",
                         whole(self.reverse_charge_base)))
        if "46" in self.rc_in:
            base, tax = self.rc_in["46"]
            rows.append(("46", "§ 13b Abs. 1: Leistungen von Unternehmern aus anderen EU-Ländern", whole(base)))
            rows.append(("47", "Steuer dazu (von Ihnen geschuldet)", tax))
        if "84" in self.rc_in:
            base, tax = self.rc_in["84"]
            rows.append(("84", "§ 13b Abs. 2: andere Leistungen (z. B. Bauleistungen, Unternehmer außerhalb der EU)",
                         whole(base)))
            rows.append(("85", "Steuer dazu (von Ihnen geschuldet)", tax))
        if not self.small_business:
            rows.append(("66", "Vorsteuerbeträge aus Rechnungen anderer Unternehmer", self.input_tax_net))
            if self.rc_input_tax:
                rows.append(("67", "Vorsteuerbeträge aus Leistungen im Sinne des § 13b", self.rc_input_tax))
        rows.append(("83", "Verbleibende Vorauszahlung (+) bzw. Erstattung (−)", self.payable))
        return rows


# Bemessungsgrundlagen (volle Euro) in der Kennzahlen-Tabelle
WHOLE_EURO_KZ = ("81", "86", "60", "21", "46", "84")


def _reverse_charge_in(db: Session, p: Period) -> dict:
    """§ 13b-Eingangsrechnungen nach Rechnungsdatum (die Steuer entsteht unabhängig von der Zahlung)."""
    from app.services import steuerschuld

    result: dict[str, list] = {}
    stmt = select(Document).where(Document.direction == DIRECTION_IN, Document.status != STATUS_CANCELLED,
                                  Document.reverse_charge.is_not(None),
                                  Document.doc_date >= p.start, Document.doc_date <= p.end)
    for doc in db.scalars(stmt):
        kind = steuerschuld.incoming_kind(doc)
        if not kind or doc.net_total is None:
            continue
        sign = Decimal(-1) if doc.kind == "gutschrift" else Decimal(1)
        entry = result.setdefault(steuerschuld.kennzahl_in(kind)[0], [ZERO, ZERO])
        entry[0] += sign * doc.net_total
        entry[1] += sign * steuerschuld.incoming_tax(doc)
    return result


def _eu_services(db: Session, p: Period) -> tuple[Decimal, list[dict]]:
    """Ausgangsrechnungen § 13b an EU-Kunden nach Rechnungsdatum: Kz 21 und Zusammenfassende Meldung."""
    from app.services import steuerschuld

    total = ZERO
    per_customer: dict[str, dict] = {}
    stmt = select(Document).where(Document.direction == DIRECTION_OUT, Document.state == STATE_FINAL,
                                  Document.reverse_charge == steuerschuld.EU,
                                  Document.doc_date >= p.start, Document.doc_date <= p.end)
    for doc in db.scalars(stmt):
        if steuerschuld.outgoing_kind(doc) != steuerschuld.EU:
            continue
        net = sum((g.net for g in tax_groups(doc) if g.rate == 0), ZERO)
        total += net
        ust_id = (doc.customer.ust_id if doc.customer else None) or "?"
        entry = per_customer.setdefault(ust_id, {"ust_id": ust_id, "amount": ZERO,
                                                 "name": doc.recipient_name or doc.partner_name or ""})
        entry["amount"] += net
    rows = sorted(per_customer.values(), key=lambda e: e["ust_id"])
    for row in rows:
        row["amount_whole"] = row["amount"].quantize(Decimal(1), rounding=ROUND_DOWN)
    return total, rows


def _late_unbacked_payments(db: Session, p: Period) -> int:
    """Eingänge auf Ausgangsrechnungen ohne Bankumsatz, gebucht > 30 Tage nach dem Rechnungsdatum – typisch für
    nachträglich gesetzte Häkchen „Bezahlt“ (Vorschlag „heute“), die bei Ist-Versteuerung das Quartal verschieben."""
    stmt = (select(Payment, Document).join(Document, Document.id == Payment.document_id)
            .where(Document.direction == DIRECTION_OUT, Payment.bank_transaction_id.is_(None),
                   Payment.paid_on >= p.start, Payment.paid_on <= p.end))
    return sum(1 for payment, doc in db.execute(stmt).all()
               if doc.doc_date and (payment.paid_on - doc.doc_date).days > 30)


def ust(db: Session, p: Period) -> Ust:
    from app.services import steuerschuld

    mode = einstellungen.get(db, "steuer.versteuerung") or "ist"
    ku = small_business(db)
    outputs: dict[Decimal, list] = defaultdict(lambda: [ZERO, ZERO])
    rc_base = ZERO
    hints: list[str] = []
    rc_in = _reverse_charge_in(db, p)
    if ku:
        hints = ["Kleinunternehmer (§ 19 UStG): keine Umsatzsteuer, kein Vorsteuerabzug."]
        if rc_in:
            hints.append("§ 13b: Für diesen Zeitraum müssen Sie trotzdem eine Umsatzsteuer-Voranmeldung abgeben und "
                         "die Steuer aus den § 13b-Eingangsrechnungen zahlen – ohne Vorsteuerabzug.")
        return Ust(p, mode, True, {}, ZERO, ZERO, ZERO, hints, rc_in=rc_in)

    def is_eu(doc) -> bool:
        return steuerschuld.outgoing_kind(doc) == steuerschuld.EU

    if mode == "soll":
        stmt = select(Document).where(Document.direction == DIRECTION_OUT, Document.state == STATE_FINAL,
                                      Document.doc_date >= p.start, Document.doc_date <= p.end)
        for doc in db.scalars(stmt):
            for g in tax_groups(doc):
                if g.rate == 0 and "13b" in (doc.hints or []):
                    if not is_eu(doc):
                        rc_base += g.net
                else:
                    outputs[g.rate][0] += g.net
                    outputs[g.rate][1] += g.vat
    else:
        for f in flows(db, p.start, p.end):
            if f.direction != DIRECTION_OUT:
                continue
            if f.rate == 0 and "13b" in (f.doc.hints or []):
                if not is_eu(f.doc):
                    rc_base += f.net
            else:
                outputs[f.rate][0] += f.net
                outputs[f.rate][1] += f.vat
    eu_base, zm = _eu_services(db, p)

    input_tax = ZERO
    stmt = select(Document).where(Document.direction == DIRECTION_IN, Document.status != STATUS_CANCELLED,
                                  Document.doc_date >= p.start, Document.doc_date <= p.end)
    for doc in db.scalars(stmt):
        input_tax += sum((line.vat for line in doc.lines), ZERO)
    correction = ZERO
    stmt = (select(Payment, Document).join(Document, Document.id == Payment.document_id)
            .where(Document.direction == DIRECTION_IN, Payment.skonto > 0,
                   Payment.paid_on >= p.start, Payment.paid_on <= p.end))
    for payment, doc in db.execute(stmt).all():
        correction += sum((part.vat for part in split_by_rate(payment.skonto, tax_groups(doc))), ZERO)

    others = [r for r in outputs if r not in (Decimal(19), Decimal(7), Decimal(0))]
    if others:
        hints.append("Es gibt Umsätze mit anderen Steuersätzen – diese gehören in der Voranmeldung in eigene "
                     "Kennzahlen (bitte mit der Steuerberatung klären).")
    outputs.pop(Decimal(0), None)
    if mode == "ist":
        hints.append("Ist-Versteuerung: Umsatzsteuer nach Zahlungseingang. Vorsteuer nach Rechnungsdatum.")
    else:
        hints.append("Soll-Versteuerung: Umsatzsteuer nach Rechnungsdatum (inkl. Stornorechnungen). "
                     "Vorsteuer nach Rechnungsdatum.")
    if mode == "ist":
        late = _late_unbacked_payments(db, p)
        if late:
            hints.append(f"Achtung: {late} Zahlung(en) in diesem Zeitraum wurden ohne Bankbeleg gebucht (z. B. per "
                         "Häkchen „Bezahlt“), mehr als 30 Tage nach dem Rechnungsdatum. Bei Ist-Versteuerung zählt das "
                         "echte Zahlungsdatum – bitte im Bankabgleich zuordnen (ersetzt die Zahlung mit dem Datum vom "
                         "Kontoauszug) oder das Datum korrigieren.")
    if rc_in or eu_base:
        hints.append("§ 13b: Eingangsrechnungen, bei denen Sie die Steuer schulden, und Leistungen an EU-Kunden "
                     "zählen immer nach Rechnungsdatum.")
    if zm:
        hints.append("Leistungen an EU-Kunden zusätzlich in der Zusammenfassenden Meldung an das Bundeszentralamt "
                     "für Steuern melden (Liste unten).")
    rc_input = sum((v[1] for v in rc_in.values()), ZERO)
    return Ust(p, mode, False, dict(sorted(outputs.items(), reverse=True)), rc_base, input_tax, correction, hints,
               eu_services_base=eu_base, rc_in=rc_in, rc_input_tax=rc_input, zm=zm)


# ------------------------------------------------------- Kleinunternehmer-Grenzen
def revenue_of_year(db: Session, year: int) -> Decimal:
    """Gesamtumsatz (netto) nach Rechnungsdatum – festgeschriebene Rechnungen inkl. Stornos."""
    stmt = select(Document).where(Document.direction == DIRECTION_OUT, Document.state == STATE_FINAL,
                                  Document.doc_date >= date(year, 1, 1), Document.doc_date <= date(year, 12, 31))
    return sum((doc.net_total or ZERO for doc in db.scalars(stmt)), ZERO)


def ku_monitor(db: Session, year: int) -> dict:
    previous, current = revenue_of_year(db, year - 1), revenue_of_year(db, year)
    if current > KU_LIMIT_CURRENT:
        level, text = "fehler", (f"Der Umsatz {year} liegt über 100.000 €. Die Kleinunternehmerregelung entfällt "
                                 "ab dem Umsatz, mit dem die Grenze überschritten wurde – bitte sofort mit der "
                                 "Steuerberatung sprechen.")
    elif previous > KU_LIMIT_PREVIOUS:
        level, text = "fehler", (f"Der Umsatz {year - 1} lag über 25.000 € – die Kleinunternehmerregelung ist "
                                 f"für {year} voraussichtlich nicht mehr anwendbar.")
    elif current > KU_LIMIT_PREVIOUS:
        level, text = "hinweis", (f"Der Umsatz {year} liegt über 25.000 € – ab {year + 1} ist die "
                                  "Kleinunternehmerregelung voraussichtlich nicht mehr möglich.")
    elif current > KU_LIMIT_PREVIOUS * Decimal("0.8"):
        level, text = "hinweis", "Der Umsatz nähert sich der Grenze von 25.000 € für das Folgejahr."
    else:
        level, text = "ok", "Die Umsatzgrenzen werden eingehalten."
    return {"previous": previous, "current": current, "limit_previous": KU_LIMIT_PREVIOUS,
            "limit_current": KU_LIMIT_CURRENT, "level": level, "text": text, "year": year,
            "pct_current": min(Decimal(100), current / KU_LIMIT_PREVIOUS * 100).quantize(Decimal(1))}


# ---------------------------------------------------------------- Offene Posten
def open_items(db: Session) -> dict:
    today = date.today()
    receivables = list(db.scalars(select(Document).where(
        Document.direction == DIRECTION_OUT, Document.state == STATE_FINAL, Document.kind == KIND_INVOICE,
        Document.status == STATUS_OPEN)))
    payables = list(db.scalars(select(Document).where(Document.direction == DIRECTION_IN,
                                                      Document.status == STATUS_OPEN)))
    overdue = [d for d in receivables if d.due_date and d.due_date < today]
    overdue_payables = [d for d in payables if d.due_date and d.due_date < today]
    return {
        "receivables": sum((d.open_amount for d in receivables), ZERO), "receivables_count": len(receivables),
        "overdue": sum((d.open_amount for d in overdue), ZERO), "overdue_count": len(overdue),
        "overdue_docs": sorted(overdue, key=lambda d: d.due_date)[:5],
        "payables": sum((d.open_amount for d in payables), ZERO), "payables_count": len(payables),
        "overdue_payables_count": len(overdue_payables),
    }


def monthly(db: Session, year: int) -> list[dict]:
    """Einnahmen/Ausgaben brutto je Monat (nach Zahlungsdatum)."""
    months = [{"label": MONTHS[i][:3], "income": ZERO, "expense": ZERO} for i in range(12)]
    for f in flows(db, date(year, 1, 1), date(year, 12, 31)):
        key = "income" if f.direction == DIRECTION_OUT else "expense"
        months[f.day.month - 1][key] += f.gross
    return months
