"""Angebote: Anlegen, Bearbeiten (Positionen + Zusatzkosten), Status, Kopieren, Angebot -> Rechnung.

Die Rechenlogik steckt in app/services/calc.py. Interne Kosten (Kostensätze, Marge)
sind nur mit dem Sonderrecht „kalkulation_intern“ sichtbar und änderbar und
erscheinen nie im PDF. Alle Funktionen schreiben ins Änderungsprotokoll, ohne commit.
"""
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.formatting import format_date, format_decimal, format_price, parse_date, parse_decimal, round_money
from app.models import CatalogItem, Customer, CustomerAddress, Document, Quote, QuoteCostItem
from app.models import QuotePosition
from app.models.angebote import (
    QUOTE_ACCEPTED,
    QUOTE_DRAFT,
    QUOTE_EXPIRED,
    QUOTE_REJECTED,
    QUOTE_SENT,
)
from app.models.stammdaten import UNITS
from app.services import audit, calc, einstellungen, nummernkreise, rechnungen

BILLING_LABELS = {"menge": "Menge × Preis", "stunden": "Stunden × Satz × Personen", "pauschal": "Pauschal"}
BLOCK_SUGGESTIONS = ("Anfahrt", "Material", "Entsorgung")
VAT_RATES = ("19", "7", "0")
MAX_ROWS = 100
EMPTY_POSITIONS = 3
EMPTY_COSTS = 2
STATUS_ACTIONS = {
    # Ziel-Status: erlaubt aus
    QUOTE_SENT: {QUOTE_DRAFT},
    QUOTE_ACCEPTED: {QUOTE_DRAFT, QUOTE_SENT},
    QUOTE_REJECTED: {QUOTE_DRAFT, QUOTE_SENT},
    QUOTE_DRAFT: {QUOTE_SENT, QUOTE_REJECTED},
}


# ------------------------------------------------------------------ Laden
def get(db: Session, quote_id: int) -> Quote | None:
    return db.get(Quote, quote_id)


def listing(db: Session, *, q: str = "", status: str = "", customer_id: int | None = None,
            limit: int = 500) -> list[Quote]:
    stmt = select(Quote)
    today = date.today()
    open_states = (QUOTE_DRAFT, QUOTE_SENT)
    if status == QUOTE_EXPIRED:
        stmt = stmt.where(Quote.status.in_(open_states), Quote.valid_until < today)
    elif status in open_states:
        stmt = stmt.where(Quote.status == status, (Quote.valid_until.is_(None)) | (Quote.valid_until >= today))
    elif status:
        stmt = stmt.where(Quote.status == status)
    if customer_id:
        stmt = stmt.where(Quote.customer_id == customer_id)
    if q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Quote.recipient_name).like(pattern) | func.lower(Quote.number).like(pattern))
    return list(db.scalars(stmt.order_by(Quote.quote_date.desc(), Quote.id.desc()).limit(limit)))


def invoices_of(db: Session, quote: Quote) -> list[Document]:
    return list(db.scalars(select(Document).where(Document.quote_id == quote.id).order_by(Document.id)))


def recurring_enabled(db: Session) -> bool:
    return bool(einstellungen.get(db, "angebot.wiederkehrend"))


def result(quote: Quote) -> calc.QuoteResult:
    return calc.compute_quote(quote.positions, quote.cost_items, quote.extra_internal_cost)


# --------------------------------------------------------------- Protokoll
def _snapshot(quote: Quote) -> dict:
    def s(v):
        return str(v) if isinstance(v, Decimal) else (format_date(v) if isinstance(v, date) else v)

    data = {k: s(getattr(quote, k)) for k in ("number", "status", "recipient_name", "recipient_address",
                                              "object_address", "quote_date", "valid_until", "intro_text",
                                              "closing_text", "extra_internal_cost", "net_total", "gross_total")}
    data["positionen"] = [
        f"{p.title} [{p.billing_type}] {s(p.quantity) or ''} {s(p.hours) or ''}×{p.staff_count or ''} "
        f"à {s(p.unit_price)} ({s(p.vat_rate)} %){' wdh. ' + (p.interval or '') if p.recurring else ''}"
        f"{' Kosten ' + s(p.cost_rate) if p.cost_rate is not None else ''}" for p in quote.positions]
    data["zusatzkosten"] = [f"{c.block}: {c.description or ''} {s(c.quantity)} à {s(c.unit_price)}"
                            for c in quote.cost_items]
    return data


def _log(db: Session, quote: Quote, action: str, user_id: int | None, ip: str | None, changes=None) -> None:
    audit.log(db, action=action, entity="angebot", entity_id=quote.id, tenant_id=quote.tenant_id, user_id=user_id,
              changes=changes, ip=ip)


# ------------------------------------------------------------------ Anlegen
def create(db: Session, customer: Customer, *, user_id: int, ip: str | None) -> Quote:
    values = einstellungen.load(db)
    name, address = rechnungen.recipient_of(customer)
    today = date.today()
    quote = Quote(
        number=nummernkreise.allocate(db, "angebot", today), customer_id=customer.id, recipient_name=name,
        recipient_address=address, quote_date=today,
        valid_until=today + timedelta(days=int(values.get("angebot.gueltig_tage") or 30)), status=QUOTE_DRAFT,
        intro_text=values.get("angebot.einleitung"), closing_text=values.get("angebot.schluss"),
        created_by_id=user_id,
    )
    db.add(quote)
    db.flush()
    _log(db, quote, "angelegt", user_id, ip, {"nummer": quote.number, "kunde": customer.name})
    return quote


def duplicate(db: Session, quote: Quote, *, user_id: int, ip: str | None) -> Quote:
    values = einstellungen.load(db)
    today = date.today()
    copy = Quote(
        number=nummernkreise.allocate(db, "angebot", today), customer_id=quote.customer_id,
        recipient_name=quote.recipient_name, recipient_address=quote.recipient_address,
        object_address=quote.object_address, quote_date=today,
        valid_until=today + timedelta(days=int(values.get("angebot.gueltig_tage") or 30)), status=QUOTE_DRAFT,
        intro_text=quote.intro_text, closing_text=quote.closing_text, notes=quote.notes,
        extra_internal_cost=quote.extra_internal_cost, created_by_id=user_id,
    )
    db.add(copy)
    db.flush()
    for p in quote.positions:
        copy.positions.append(QuotePosition(**{c: getattr(p, c) for c in _POSITION_COLUMNS}, tenant_id=copy.tenant_id))
    for c in quote.cost_items:
        copy.cost_items.append(QuoteCostItem(**{k: getattr(c, k) for k in _COST_COLUMNS}, tenant_id=copy.tenant_id))
    _cache_totals(copy)
    _log(db, copy, "kopiert", user_id, ip, {"von": quote.number})
    return copy


_POSITION_COLUMNS = ("position", "catalog_item_id", "title", "description", "billing_type", "quantity", "unit",
                     "unit_price", "hours", "staff_count", "vat_rate", "recurring", "interval", "visits_per_month",
                     "contract_months", "cost_rate")
_COST_COLUMNS = ("position", "block", "description", "quantity", "unit", "unit_price", "vat_rate")


# ---------------------------------------------------------------- Formular
def _num(value, places: int = 4) -> str:
    if value is None:
        return ""
    text = format_decimal(Decimal(value), places)
    return text.rstrip("0").rstrip(",") if "," in text else text


def position_rows(quote: Quote, extra: int = EMPTY_POSITIONS) -> list[dict]:
    rows = [{
        "id": str(p.id), "titel": p.title, "beschreibung": p.description or "", "art": p.billing_type,
        "menge": _num(p.quantity), "einheit": p.unit or "", "preis": format_price(p.unit_price),
        "stunden": _num(p.hours, 2), "personen": str(p.staff_count or ""), "satz": f"{p.vat_rate.normalize():f}",
        "wdh": "1" if p.recurring else "", "intervall": p.interval or "", "einsaetze": _num(p.visits_per_month, 2),
        "laufzeit": str(p.contract_months or ""), "kosten": format_price(p.cost_rate) if p.cost_rate is not None else "",
        "katalog": "",
    } for p in quote.positions]
    return rows + empty_position_rows(extra)


def empty_position_rows(count: int) -> list[dict]:
    return [{"id": "", "titel": "", "beschreibung": "", "art": "menge", "menge": "", "einheit": "Stück", "preis": "",
             "stunden": "", "personen": "", "satz": "", "wdh": "", "intervall": "", "einsaetze": "", "laufzeit": "",
             "kosten": "", "katalog": ""} for _ in range(count)]


def cost_rows(quote: Quote, extra: int = EMPTY_COSTS) -> list[dict]:
    rows = [{"block": c.block, "beschreibung": c.description or "", "menge": _num(c.quantity), "einheit": c.unit or "",
             "preis": format_price(c.unit_price), "satz": f"{c.vat_rate.normalize():f}"} for c in quote.cost_items]
    return rows + empty_cost_rows(extra)


def empty_cost_rows(count: int) -> list[dict]:
    return [{"block": "", "beschreibung": "", "menge": "", "einheit": "", "preis": "", "satz": ""}
            for _ in range(count)]


def _dec(raw: str, what: str, *, required: bool = False, minimum: Decimal | None = Decimal(0)) -> Decimal | None:
    value = parse_decimal(raw) if raw else None
    if value is None:
        if required:
            raise ValueError(f"Bitte {what} angeben.")
        return None
    if minimum is not None and value < minimum:
        raise ValueError(f"{what[0].upper() + what[1:]} darf nicht negativ sein.")
    return value


def _int(raw: str, what: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{what}: bitte eine ganze Zahl eingeben.") from None


def _rate(raw: str, force_zero: bool, default: str) -> Decimal:
    rate = "0" if force_zero else (raw or default)
    if rate not in VAT_RATES:
        raise ValueError("Ungültiger Steuersatz.")
    return Decimal(rate)


def _parse_positions(db: Session, quote: Quote, form: Mapping[str, str], errors: dict, *, force_zero: bool,
                     default_rate: str, can_see_costs: bool, recurring_on: bool) -> list[dict]:
    existing = {str(p.id): p for p in quote.positions}
    rows = []
    for i in range(MAX_ROWS):
        prefix = f"pos-{i}-"
        if prefix + "titel" not in form:
            break
        raw = {k: (form.get(prefix + k) or "").strip() for k in (
            "id", "titel", "beschreibung", "art", "menge", "einheit", "preis", "stunden", "personen", "satz", "katalog",
            "wdh", "intervall", "einsaetze", "laufzeit", "kosten")}
        if form.get(prefix + "entfernen"):
            continue
        catalog_item = None
        cost_rate = existing[raw["id"]].cost_rate if raw["id"] in existing else None
        if raw["katalog"].isdigit() and not raw["titel"]:
            catalog_item = db.get(CatalogItem, int(raw["katalog"]))
            if catalog_item is not None:
                raw.update(titel=catalog_item.title, beschreibung=raw["beschreibung"] or (catalog_item.description or ""),
                           einheit=catalog_item.unit, art=catalog_item.billing_type,
                           preis=format_price(catalog_item.price_net), satz=f"{catalog_item.vat_rate.normalize():f}")
                cost_rate = catalog_item.cost_rate
        if not (raw["titel"] or raw["preis"] or raw["menge"] or raw["stunden"]):
            continue
        try:
            if not raw["titel"]:
                raise ValueError("Bitte eine Bezeichnung angeben.")
            billing = raw["art"] if raw["art"] in calc.BILLING_TYPES else "menge"
            price = _dec(raw["preis"], "einen Preis", required=True)
            quantity = hours = staff = None
            unit = raw["einheit"] if raw["einheit"] in UNITS else "Stück"
            if billing == "menge":
                quantity = _dec(raw["menge"], "eine Menge") or Decimal(1)
            elif billing == "stunden":
                hours = _dec(raw["stunden"], "die Stunden", required=True)
                staff = _int(raw["personen"] or "1", "Anzahl Personen")
                if not 1 <= staff <= 999:
                    raise ValueError("Anzahl Personen zwischen 1 und 999.")
                unit = "Std."
            else:
                unit = "pauschal"
            recurring = recurring_on and bool(raw["wdh"])
            interval = visits = months = None
            if recurring:
                interval = raw["intervall"] if raw["intervall"] in calc.INTERVALS else None
                if interval is None:
                    raise ValueError("Bitte ein Intervall wählen.")
                visits = _dec(raw["einsaetze"], "die Einsätze pro Monat", required=interval == "individuell")
                months = _int(raw["laufzeit"], "Laufzeit") if raw["laufzeit"] else None
                if months is not None and not 1 <= months <= 240:
                    raise ValueError("Laufzeit zwischen 1 und 240 Monaten.")
            # Mit Sonderrecht gilt das Eingabefeld – außer es ist leer und der Kostensatz kommt gerade aus dem Katalog
            if can_see_costs and (raw["kosten"] or catalog_item is None):
                cost_rate = _dec(raw["kosten"], "den Kostensatz")
            rows.append({
                "catalog_item_id": catalog_item.id if catalog_item else (
                    existing[raw["id"]].catalog_item_id if raw["id"] in existing else None),
                "title": raw["titel"][:300], "description": raw["beschreibung"] or None, "billing_type": billing,
                "quantity": quantity, "unit": unit, "unit_price": price, "hours": hours, "staff_count": staff,
                "vat_rate": _rate(raw["satz"], force_zero, default_rate), "recurring": recurring, "interval": interval,
                "visits_per_month": visits, "contract_months": months, "cost_rate": cost_rate,
            })
        except (ValueError, ArithmeticError) as exc:
            errors[f"pos-{i}"] = str(exc) if str(exc) else "Ungültige Eingabe."
    return rows


def _parse_costs(form: Mapping[str, str], errors: dict, *, force_zero: bool, default_rate: str) -> list[dict]:
    rows = []
    for j in range(MAX_ROWS):
        prefix = f"kost-{j}-"
        if prefix + "block" not in form:
            break
        raw = {k: (form.get(prefix + k) or "").strip() for k in ("block", "beschreibung", "menge", "einheit", "preis",
                                                                 "satz")}
        if form.get(prefix + "entfernen") or not (raw["block"] or raw["beschreibung"] or raw["preis"]):
            continue
        try:
            price = _dec(raw["preis"], "einen Preis", required=True)
            rows.append({"block": (raw["block"] or "Zusatzkosten")[:60], "description": raw["beschreibung"][:300] or None,
                         "quantity": _dec(raw["menge"], "eine Menge") or Decimal(1), "unit": raw["einheit"][:20] or None,
                         "unit_price": price, "vat_rate": _rate(raw["satz"], force_zero, default_rate)})
        except (ValueError, ArithmeticError) as exc:
            errors[f"kost-{j}"] = str(exc) if str(exc) else "Ungültige Eingabe."
    return rows


def save_form(db: Session, quote: Quote, form: Mapping[str, str], *, user_id: int, ip: str | None,
              can_see_costs: bool) -> dict:
    if not quote.is_editable:
        return {"_": "Angenommene oder abgelehnte Angebote können nicht mehr geändert werden – bitte kopieren."}
    errors: dict[str, str] = {}
    values: dict = {}
    for key in ("quote_date", "valid_until"):
        try:
            values[key] = parse_date(form.get(key))
        except ValueError as exc:
            errors[key] = str(exc)
    if not errors.get("quote_date") and values.get("quote_date") is None:
        errors["quote_date"] = "Bitte ein Angebotsdatum angeben."
    if values.get("valid_until") and values.get("quote_date") and values["valid_until"] < values["quote_date"]:
        errors["valid_until"] = "„Gültig bis“ liegt vor dem Angebotsdatum."
    for key, limit in (("recipient_name", 200), ("recipient_address", 1000), ("object_address", 1000),
                       ("intro_text", 4000), ("closing_text", 4000), ("notes", 4000)):
        value = (form.get(key) or "").strip()
        if len(value) > limit:
            errors[key] = f"Höchstens {limit} Zeichen."
        values[key] = value or None
    if not values["recipient_name"]:
        errors["recipient_name"] = "Bitte den Empfänger angeben."
    address_id = (form.get("object_address_id") or "").strip()
    if address_id.isdigit():
        address = db.get(CustomerAddress, int(address_id))
        if address is None or address.customer_id != quote.customer_id:
            errors["object_address"] = "Unbekannte Objektadresse."
        else:
            values["object_address"] = "\n".join(x for x in (
                address.label, address.street, f"{address.zip or ''} {address.city or ''}".strip()) if x)
    if can_see_costs:
        try:
            extra = parse_decimal(form.get("extra_internal_cost"))
            values["extra_internal_cost"] = round_money(extra) if extra is not None else None
        except ValueError as exc:
            errors["extra_internal_cost"] = str(exc)

    force_zero = rechnungen.is_small_business(db)
    default_rate = "0" if force_zero else (einstellungen.get(db, "design.ust_satz") or "19")
    positions = _parse_positions(db, quote, form, errors, force_zero=force_zero, default_rate=default_rate,
                                 can_see_costs=can_see_costs, recurring_on=recurring_enabled(db))
    costs = _parse_costs(form, errors, force_zero=force_zero, default_rate=default_rate)
    if errors:
        return errors

    old = _snapshot(quote)
    for key, value in values.items():
        setattr(quote, key, value)
    quote.positions.clear()
    quote.cost_items.clear()
    db.flush()
    for i, row in enumerate(positions, start=1):
        quote.positions.append(QuotePosition(tenant_id=quote.tenant_id, position=i, **row))
    for j, row in enumerate(costs, start=1):
        quote.cost_items.append(QuoteCostItem(tenant_id=quote.tenant_id, position=j, **row))
    _cache_totals(quote)
    changes = audit.diff(old, _snapshot(quote))
    if changes:
        _log(db, quote, "geaendert", user_id, ip, changes)
    return {}


def _cache_totals(quote: Quote) -> None:
    r = result(quote)
    quote.net_total, quote.gross_total = r.net_total, r.gross_total


# -------------------------------------------------------------------- Status
def set_status(db: Session, quote: Quote, status: str, *, user_id: int, ip: str | None) -> str | None:
    allowed_from = STATUS_ACTIONS.get(status)
    if allowed_from is None or quote.status not in allowed_from:
        return "Dieser Statuswechsel ist nicht möglich."
    if status == QUOTE_ACCEPTED and not quote.positions:
        return "Das Angebot enthält keine Positionen."
    if status == QUOTE_DRAFT and invoices_of(db, quote):
        return "Zu diesem Angebot gibt es bereits Rechnungen."
    old = quote.status
    quote.status = status
    _log(db, quote, "status", user_id, ip, {"status": {"alt": old, "neu": status}})
    return None


def delete(db: Session, quote: Quote, *, user_id: int, ip: str | None) -> str | None:
    if invoices_of(db, quote):
        return "Zu diesem Angebot gibt es Rechnungen – es kann nicht gelöscht werden."
    if quote.status == QUOTE_ACCEPTED:
        return "Angenommene Angebote können nicht gelöscht werden."
    _log(db, quote, "geloescht", user_id, ip, _snapshot(quote))
    db.delete(quote)
    return None


# -------------------------------------------------------- Angebot -> Rechnung
def billing_lines(quote: Quote) -> list[dict]:
    """Positionen und Zusatzkosten des Angebots als abrechenbare Zeilen (Menge × Preis netto).
    Wiederkehrende Positionen als ein Monat. Gemeinsam genutzt von Angebot -> Rechnung und Angebot -> Auftrag."""
    rows: list[dict] = []

    def add(title, description, quantity, unit, unit_price, vat_rate):
        rows.append(dict(title=title[:300], description=description, quantity=quantity, unit=unit,
                         unit_price=unit_price, vat_rate=vat_rate))

    for p, r in zip(quote.positions, result(quote).positions, strict=True):
        if p.recurring:
            label = calc.INTERVALS.get(p.interval or "", ("",))[0]
            detail = f"{label}, ca. {format_decimal(r.visits_per_month, 2)} Einsätze/Monat"
            add(f"{p.title} – monatlich", "\n".join(x for x in (p.description, detail) if x), Decimal(1), "Monat",
                r.monthly, p.vat_rate)
        elif p.billing_type == "stunden":
            detail = f"{p.staff_count or 1} Person(en) × {format_decimal(p.hours, 2)} Std."
            add(p.title, "\n".join(x for x in (p.description, detail) if x),
                (p.hours or Decimal(0)) * (p.staff_count or 1), "Std.", p.unit_price, p.vat_rate)
        elif p.billing_type == "pauschal":
            add(p.title, p.description, Decimal(1), "pauschal", p.unit_price, p.vat_rate)
        else:
            add(p.title, p.description, p.quantity or Decimal(1), p.unit or "Stück", p.unit_price, p.vat_rate)
    for c in quote.cost_items:
        unit = c.unit if c.unit in UNITS else "Stück"
        add(f"{c.block}{': ' + c.description if c.description else ''}", None, c.quantity, unit, c.unit_price, c.vat_rate)
    return rows


def to_invoice(db: Session, quote: Quote, *, user_id: int, ip: str | None) -> tuple[Document | None, str | None]:
    """Erstellt einen Rechnungsentwurf aus dem Angebot und markiert es als angenommen.
    Wiederkehrende Positionen werden als ein Monat abgerechnet (bitte prüfen)."""
    from app.models import Order

    if quote.status == QUOTE_REJECTED:
        return None, "Abgelehnte Angebote können nicht abgerechnet werden."
    if not quote.positions and not quote.cost_items:
        return None, "Das Angebot enthält keine Positionen."
    customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
    if customer is None:
        return None, "Das Angebot hat keinen Kunden."
    if quote.status != QUOTE_ACCEPTED:
        set_status(db, quote, QUOTE_ACCEPTED, user_id=user_id, ip=ip)

    doc = rechnungen.create_draft(db, customer, user_id=user_id, ip=ip)
    doc.quote_id = quote.id
    doc.source = "angebot"
    doc.recipient_name, doc.recipient_address = quote.recipient_name, quote.recipient_address
    doc.object_address = quote.object_address
    doc.intro_text = f"Gemäß unserem Angebot Nr. {quote.number} vom {format_date(quote.quote_date)} berechnen wir Ihnen:"
    # Gibt es zum Angebot schon einen Auftrag, erscheint die Rechnung dort bei den Belegen
    doc.order_id = db.scalar(select(Order.id).where(Order.quote_id == quote.id).order_by(Order.id).limit(1))
    rechnungen.add_lines(db, doc, billing_lines(quote))
    rechnungen.recalc(doc)
    db.flush()
    _log(db, quote, "rechnung_erstellt", user_id, ip, {"rechnung_id": doc.id})
    return doc, None
