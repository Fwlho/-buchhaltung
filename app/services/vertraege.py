"""Verträge über wiederkehrende Leistungen -> monatliche Rechnungsentwürfe (KONZEPT.md 6.8).

- Ein Vertrag entsteht aus einem Angebot (wiederkehrende Positionen, Monatsbetrag) oder leer für einen Kunden.
- ``run_due`` legt für jeden fälligen Monat einen normalen Rechnungsentwurf an (Rechnungsliste, Hinweis auf
  der Übersicht). Festschreiben und Versenden bleiben Sache des Menschen.
- Fällig: „nachtraeglich“ ab dem 1. des Folgemonats, „voraus“ ab dem 1. des Monats selbst. Wurde länger nicht
  abgerechnet, entsteht je verpasstem Monat ein Entwurf (höchstens 12 auf einmal).
Alle Funktionen schreiben ins Änderungsprotokoll und rufen KEIN commit auf.
"""
import calendar
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.formatting import format_decimal, parse_decimal, round_money
from app.models import Customer, Document, DocumentLine, Quote, RecurringContract, RecurringContractLine
from app.models.belege import DIRECTION_OUT, STATE_DRAFT
from app.models.stammdaten import UNITS
from app.models.vertraege import BILLING_MODES, CONTRACT_ACTIVE, CONTRACT_ENDED, CONTRACT_STATUS_LABELS
from app.services import audit, calc, einstellungen, rechnungen

MAX_CATCH_UP = 12
MAX_LINES = 30
EMPTY_LINES = 2
MONTHS = rechnungen.MONTHS


# ==================================================================== Hilfen
def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def add_months(d: date, n: int) -> date:
    index = d.year * 12 + d.month - 1 + n
    return date(index // 12, index % 12 + 1, 1)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def month_label(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.year}"


def format_month(d: date | None) -> str:
    return f"{d.month:02d}.{d.year}" if d else ""


def parse_month(text: str | None) -> date | None:
    """„MM.JJJJ“ (auch „TT.MM.JJJJ“) -> erster Tag des Monats. Wirft ValueError."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(?:\d{1,2}\.)?(\d{1,2})\.(\d{4})", text)
    if not m or not 1 <= int(m.group(1)) <= 12:
        raise ValueError("Bitte den Monat als MM.JJJJ angeben, z. B. 01.2027.")
    return date(int(m.group(2)), int(m.group(1)), 1)


def _log(db: Session, contract: RecurringContract, action: str, user_id: int | None, ip: str | None,
         changes=None) -> None:
    audit.log(db, action=action, entity="vertrag", entity_id=contract.id, tenant_id=contract.tenant_id,
              user_id=user_id, changes=changes, ip=ip)


def enabled(db: Session) -> bool:
    return bool(einstellungen.get(db, "angebot.wiederkehrend"))


# ==================================================================== Laden
def get(db: Session, contract_id: int) -> RecurringContract | None:
    return db.get(RecurringContract, contract_id)


def listing(db: Session) -> list[RecurringContract]:
    return list(db.scalars(select(RecurringContract).order_by(RecurringContract.status, RecurringContract.title)))


def invoices_of(db: Session, contract: RecurringContract) -> list[Document]:
    return list(db.scalars(select(Document).where(Document.contract_id == contract.id)
                           .order_by(Document.service_date.desc(), Document.id.desc())))


def draft_count(db: Session) -> int:
    """Rechnungsentwürfe aus Verträgen, die noch geprüft und festgeschrieben werden müssen."""
    return db.scalar(select(func.count()).select_from(Document).where(
        Document.direction == DIRECTION_OUT, Document.state == STATE_DRAFT, Document.contract_id.is_not(None))) or 0


# =================================================================== Anlegen
def create(db: Session, customer: Customer, *, user_id: int, ip: str | None) -> RecurringContract:
    start = add_months(month_start(date.today()), 1)
    contract = RecurringContract(customer_id=customer.id, title=f"Wiederkehrende Leistungen {customer.name}"[:200],
                                 start_month=start, next_month=start, created_by_id=user_id)
    db.add(contract)
    db.flush()
    _log(db, contract, "angelegt", user_id, ip, {"kunde": customer.name})
    return contract


def from_quote(db: Session, quote: Quote, *, user_id: int, ip: str | None) -> tuple[RecurringContract | None,
                                                                                    str | None]:
    """Vertrag aus den wiederkehrenden Positionen eines Angebots (Monatsbetrag je Position)."""
    customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
    if customer is None:
        return None, "Das Angebot hat keinen Kunden."
    results = calc.compute_quote(quote.positions, quote.cost_items).positions
    recurring = [(p, r) for p, r in zip(quote.positions, results, strict=True) if p.recurring]
    if not recurring:
        return None, "Das Angebot enthält keine wiederkehrenden Positionen."
    contract = create(db, customer, user_id=user_id, ip=ip)
    contract.quote_id = quote.id
    contract.title = f"{recurring[0][0].title} (Angebot {quote.number})"[:200]
    contract.object_address = quote.object_address
    months = [p.contract_months for p, _ in recurring if p.contract_months]
    if months:
        contract.end_month = add_months(contract.start_month, max(months) - 1)
    for i, (p, r) in enumerate(recurring, start=1):
        label = calc.INTERVALS.get(p.interval or "", ("",))[0]
        detail = f"{label}, ca. {format_decimal(r.visits_per_month, 2)} Einsätze/Monat"
        contract.lines.append(RecurringContractLine(
            tenant_id=contract.tenant_id, position=i, title=p.title[:300],
            description="\n".join(x for x in (p.description, detail) if x), quantity=Decimal(1), unit="Monat",
            unit_price=r.monthly, vat_rate=p.vat_rate))
    db.flush()
    _log(db, contract, "aus_angebot", user_id, ip, {"angebot": quote.number})
    return contract, None


# ================================================================= Formular
def line_rows(contract: RecurringContract, extra: int = EMPTY_LINES) -> list[dict]:
    def num(value):
        text = f"{value:.4f}".replace(".", ",")
        return text.rstrip("0").rstrip(",")

    rows = [{"titel": line.title, "beschreibung": line.description or "", "menge": num(line.quantity),
             "einheit": line.unit or "Monat", "preis": num(line.unit_price), "satz": f"{line.vat_rate.normalize():f}"}
            for line in contract.lines]
    return rows + [{"titel": "", "beschreibung": "", "menge": "1", "einheit": "Monat", "preis": "", "satz": ""}
                   for _ in range(extra)]


def header_values(contract: RecurringContract) -> dict:
    return {"title": contract.title, "billing_mode": contract.billing_mode, "status": contract.status,
            "start_month": format_month(contract.start_month), "end_month": format_month(contract.end_month),
            "object_address": contract.object_address or "", "notes": contract.notes or ""}


def save_form(db: Session, contract: RecurringContract, form: Mapping[str, str], *, user_id: int,
              ip: str | None) -> dict[str, str]:
    errors: dict[str, str] = {}
    title = (form.get("title") or "").strip()
    if not title:
        errors["title"] = "Bitte eine Bezeichnung angeben."
    mode = form.get("billing_mode") or ""
    if mode not in BILLING_MODES:
        errors["billing_mode"] = "Bitte eine Abrechnungsart wählen."
    status = form.get("status") or ""
    if status not in CONTRACT_STATUS_LABELS:
        errors["status"] = "Bitte einen Status wählen."
    months = {}
    for key in ("start_month", "end_month"):
        try:
            months[key] = parse_month(form.get(key))
        except ValueError as exc:
            errors[key] = str(exc)
    if "start_month" not in errors and months.get("start_month") is None:
        errors["start_month"] = "Bitte den ersten Monat angeben."
    if months.get("start_month") and months.get("end_month") and months["end_month"] < months["start_month"]:
        errors["end_month"] = "Das Ende liegt vor dem Beginn."
    default_rate = "0" if rechnungen.is_small_business(db) else (einstellungen.get(db, "design.ust_satz") or "19")
    lines = []
    for i in range(MAX_LINES):
        prefix = f"pos-{i}-"
        if prefix + "titel" not in form:
            break
        line_title = (form.get(prefix + "titel") or "").strip()
        price_raw = (form.get(prefix + "preis") or "").strip()
        if not line_title and not price_raw:
            continue
        try:
            quantity = parse_decimal(form.get(prefix + "menge") or "1")
            price = parse_decimal(price_raw)
            rate = parse_decimal(form.get(prefix + "satz") or default_rate)
            if not line_title or price is None or quantity is None or quantity <= 0:
                raise ValueError("Bezeichnung, Menge und Preis angeben.")
        except ValueError as exc:
            errors[f"pos-{i}"] = str(exc)
            continue
        unit = form.get(prefix + "einheit") or "Monat"
        description = (form.get(prefix + "beschreibung") or "").strip() or None
        lines.append(dict(title=line_title[:300], description=description, quantity=quantity, unit=unit if unit in UNITS or unit == "Monat" else "Monat",
                          unit_price=price, vat_rate=Decimal(0) if rechnungen.is_small_business(db) else rate))
    if not lines and "_" not in errors:
        errors["_"] = "Bitte mindestens eine Position mit Preis eintragen."
    if errors:
        return errors

    old = header_values(contract) | {"monatlich": str(contract.monthly_net)}
    started = bool(invoices_of(db, contract))
    contract.title, contract.billing_mode, contract.status = title[:200], mode, status
    contract.object_address = (form.get("object_address") or "").strip() or None
    contract.notes = (form.get("notes") or "").strip() or None
    if not started:  # vor der ersten Rechnung darf der Beginn frei verschoben werden
        contract.start_month = contract.next_month = months["start_month"]
    contract.end_month = months["end_month"]
    contract.lines.clear()
    db.flush()
    for i, values in enumerate(lines, start=1):
        contract.lines.append(RecurringContractLine(tenant_id=contract.tenant_id, position=i, **values))
    db.flush()
    changes = audit.diff(old, header_values(contract) | {"monatlich": str(contract.monthly_net)})
    if changes:
        _log(db, contract, "geaendert", user_id, ip, changes)
    return {}


# ============================================================== Abrechnung
def billable_from(contract: RecurringContract, month: date) -> date:
    return month if contract.billing_mode == "voraus" else add_months(month, 1)


def due_months(contract: RecurringContract, today: date) -> list[date]:
    if contract.status != CONTRACT_ACTIVE or not contract.lines:
        return []
    result, month = [], contract.next_month
    while len(result) < MAX_CATCH_UP and billable_from(contract, month) <= today:
        if contract.end_month and month > contract.end_month:
            break
        result.append(month)
        month = add_months(month, 1)
    return result


def create_invoice(db: Session, contract: RecurringContract, month: date, *, user_id: int | None,
                   ip: str | None) -> Document:
    doc = rechnungen.create_draft(db, contract.customer, user_id=user_id, ip=ip)
    doc.source, doc.contract_id = "vertrag", contract.id
    doc.service_date, doc.service_until = month, month_end(month)
    doc.object_address = contract.object_address
    doc.intro_text = (f"Für unsere Leistungen im {month_label(month)} gemäß Vertrag „{contract.title}“ "
                      "berechnen wir Ihnen:")
    zero = rechnungen.is_small_business(db)
    for line in contract.lines:
        rate = Decimal(0) if zero else line.vat_rate
        net = round_money(line.quantity * line.unit_price)
        vat = round_money(net * rate / 100)
        doc.lines.append(DocumentLine(tenant_id=doc.tenant_id, position=line.position, title=line.title,
                                      description=line.description, quantity=line.quantity, unit=line.unit,
                                      unit_price=line.unit_price, vat_rate=rate, net=net, vat=vat, gross=net + vat))
    rechnungen.recalc(doc)
    db.flush()
    _log(db, contract, "rechnungsentwurf", user_id, ip, {"monat": month_label(month), "rechnung_id": doc.id})
    return doc


def run_due(db: Session, *, user_id: int | None = None, ip: str | None = None,
            today: date | None = None) -> list[Document]:
    """Für alle aktiven Verträge die fälligen Monate als Entwürfe anlegen. Gibt die neuen Entwürfe zurück."""
    if not enabled(db):
        return []
    today = today or date.today()
    created = []
    for contract in db.scalars(select(RecurringContract).where(RecurringContract.status == CONTRACT_ACTIVE)):
        for month in due_months(contract, today):
            created.append(create_invoice(db, contract, month, user_id=user_id, ip=ip))
            contract.next_month = add_months(month, 1)
        if contract.end_month and contract.next_month > contract.end_month:
            contract.status = CONTRACT_ENDED
            _log(db, contract, "beendet", user_id, ip, {"grund": "Vertragsende erreicht"})
    db.flush()
    return created
