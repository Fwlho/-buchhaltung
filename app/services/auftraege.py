"""Aufträge (Projektübersicht): Logik für Übersicht, Anlegen, Bearbeiten, Auftrag -> Rechnung/Vertrag.

- Übersicht ``cards``: je Objekt (Einsatzort) eine Karte mit den Aufträgen, gefiltert nach Stand (Reiter),
  Art und Suche, sortiert nach Datum, A–Z oder Preis. Karten ohne passende Aufträge fallen weg.
- Objekt: Objekt-Adresse des Kunden (``address_id``); sonst freier Text aus dem Angebot (``object_address``);
  sonst die Hauptadresse des Kunden.
- Stand setzt der Mensch. Ist bei einem Einzelauftrag jede Rechnung bezahlt, schlägt ``suggest_done`` vor,
  ihn als erledigt zu markieren (kein automatisches Umstellen).
Alle Funktionen schreiben ins Änderungsprotokoll und rufen KEIN commit auf.
"""
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.formatting import format_decimal, parse_decimal
from app.models import Customer, CustomerAddress, Document, Order, OrderLine, OrderTask, Quote, RecurringContract
from app.models import RecurringContractLine
from app.models.angebote import QUOTE_ACCEPTED, QUOTE_REJECTED
from app.models.auftraege import (
    ORDER_DONE,
    ORDER_KIND_LABELS,
    ORDER_OPEN,
    ORDER_RECURRING,
    ORDER_SINGLE,
    ORDER_STATUS_LABELS,
    ORDER_WORKING,
)
from app.models.belege import DIRECTION_OUT, KIND_INVOICE, STATE_FINAL, STATUS_CANCELLED, STATUS_PAID
from app.models.stammdaten import UNITS
from app.services import angebote, audit, einstellungen, nummernkreise, rechnungen, vertraege

MAX_LINES = 50
MAX_TASKS = 30
EMPTY_LINES = 2
EMPTY_TASKS = 2
SORTS = {"datum": "Datum", "az": "A–Z", "preis": "Preis"}
TABS = {"": "Alle", **ORDER_STATUS_LABELS}


def _log(db: Session, order: Order, action: str, user_id: int | None, ip: str | None, changes=None) -> None:
    audit.log(db, action=action, entity="auftrag", entity_id=order.id, tenant_id=order.tenant_id,
              user_id=user_id, changes=changes, ip=ip)


def address_text(address: CustomerAddress) -> str:
    """Objekt-Adresse als Text – genauso wie im Angebot (Bezeichnung, Straße, PLZ Ort)."""
    return "\n".join(x for x in (address.label, address.street,
                                 f"{address.zip or ''} {address.city or ''}".strip()) if x)


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


# ==================================================================== Laden
def get(db: Session, order_id: int) -> Order | None:
    return db.get(Order, order_id)


def of_quote(db: Session, quote: Quote) -> Order | None:
    return db.scalar(select(Order).where(Order.quote_id == quote.id).order_by(Order.id).limit(1))


def documents_of(db: Session, orders: list[Order]) -> dict[int, list[Document]]:
    """Rechnungen je Auftrag: direkt erzeugte (order_id) und die aus seinem Vertrag (contract_id)."""
    result: dict[int, list[Document]] = {o.id: [] for o in orders}
    if not orders:
        return result
    by_contract = {o.contract_id: o.id for o in orders if o.contract_id}
    stmt = select(Document).where(Document.direction == DIRECTION_OUT, or_(
        Document.order_id.in_(list(result)), Document.contract_id.in_(list(by_contract) or [0])))
    for doc in db.scalars(stmt.order_by(Document.doc_date, Document.id)):
        target = doc.order_id if doc.order_id in result else by_contract.get(doc.contract_id)
        if target is not None:
            result[target].append(doc)
    return result


def suggest_done(order: Order, docs: list[Document]) -> bool:
    """Einzelauftrag mit festgeschriebener Rechnung, alle bezahlt -> „erledigt“ vorschlagen."""
    if order.kind != ORDER_SINGLE or order.status == ORDER_DONE:
        return False
    invoices = [d for d in docs if d.kind == KIND_INVOICE and d.status != STATUS_CANCELLED]
    return bool(invoices) and all(d.state == STATE_FINAL and d.status == STATUS_PAID for d in invoices)


# ================================================================ Übersicht
@dataclass
class Card:
    key: str
    title: str
    address: list[str]
    customer: Customer
    orders: list[Order] = field(default_factory=list)       # passend zum Filter
    all_count: int = 0

    @property
    def total(self) -> Decimal:
        return sum((o.net_total for o in self.orders), Decimal("0.00"))

    @property
    def latest(self) -> date:
        return max(o.created_at for o in self.orders).date()


def object_of(order: Order) -> tuple[str, str, list[str]]:
    """(Schlüssel, Titel, Adresszeilen) des Objekts eines Auftrags."""
    customer = order.customer
    if order.address is not None:
        a = order.address
        return f"a{a.id}", a.label, [x for x in (a.street, f"{a.zip or ''} {a.city or ''}".strip()) if x]
    if order.object_address and order.object_address.strip():
        lines = [x.strip() for x in order.object_address.splitlines() if x.strip()]
        return f"t{customer.id}-{zlib.crc32(_norm(order.object_address).encode()):x}", lines[0], lines[1:]
    lines = [x for x in (customer.street, f"{customer.zip or ''} {customer.city or ''}".strip()) if x]
    return f"k{customer.id}", lines[0] if lines else customer.name, lines[1:] if lines else []


def cards(db: Session, *, status: str = "", kind: str = "", q: str = "", sort: str = "datum") -> list[Card]:
    stmt = select(Order).options(selectinload(Order.lines), selectinload(Order.tasks),
                                 selectinload(Order.customer), selectinload(Order.address))
    result: dict[str, Card] = {}
    needle = _norm(q)
    for order in db.scalars(stmt.order_by(Order.created_at.desc(), Order.id.desc())):
        key, title, address = object_of(order)
        card = result.get(key)
        if card is None:
            card = result[key] = Card(key, title, address, order.customer)
        card.all_count += 1
        if status and order.status != status:
            continue
        if kind and order.kind != kind:
            continue
        if needle:
            haystack = _norm(" ".join([title, *address, order.customer.name, order.customer.number or "",
                                       order.number, order.title, order.description or ""]))
            if needle not in haystack:
                continue
        card.orders.append(order)
    found = [c for c in result.values() if c.orders]
    if sort == "az":
        found.sort(key=lambda c: (c.title.casefold(), c.customer.name.casefold()))
    elif sort == "preis":
        found.sort(key=lambda c: c.total, reverse=True)
    else:
        found.sort(key=lambda c: max(o.created_at for o in c.orders), reverse=True)
    return found


def counts(db: Session) -> dict[str, int]:
    """Anzahl Aufträge je Stand (für die Reiter)."""
    result = {key: 0 for key in TABS}
    for status in db.scalars(select(Order.status)):
        result[""] += 1
        result[status] = result.get(status, 0) + 1
    return result


# =================================================================== Anlegen
def create(db: Session, customer: Customer, *, user_id: int, ip: str | None, title: str | None = None) -> Order:
    order = Order(number=nummernkreise.allocate(db, "auftrag", date.today()), customer_id=customer.id,
                  title=(title or f"Auftrag {customer.name}")[:200], kind=ORDER_SINGLE, status=ORDER_OPEN,
                  created_by_id=user_id)
    order.customer = customer
    db.add(order)
    db.flush()
    _log(db, order, "angelegt", user_id, ip, {"kunde": customer.name})
    return order


def from_quote(db: Session, quote: Quote, *, user_id: int, ip: str | None) -> tuple[Order | None, str | None]:
    """Auftrag aus einem Angebot: Kunde, Objekt, Leistungen und Preise werden übernommen.
    Wiederkehrende Positionen -> Dauerauftrag (Monatsbetrag). Das Angebot wird als angenommen markiert."""
    if quote.status == QUOTE_REJECTED:
        return None, "Aus einem abgelehnten Angebot kann kein Auftrag entstehen."
    customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
    if customer is None:
        return None, "Das Angebot hat keinen Kunden."
    existing = of_quote(db, quote)
    if existing is not None:
        return existing, None
    rows = angebote.billing_lines(quote)
    if not rows:
        return None, "Das Angebot enthält keine Positionen."
    first = quote.positions[0].title if quote.positions else rows[0]["title"]
    order = create(db, customer, user_id=user_id, ip=ip, title=first)
    order.quote_id = quote.id
    order.kind = ORDER_RECURRING if any(p.recurring for p in quote.positions) else ORDER_SINGLE
    order.description = f"Aus Angebot {quote.number}"
    if quote.object_address:
        match = next((a for a in customer.addresses
                      if a.is_active and _norm(address_text(a)) == _norm(quote.object_address)), None)
        if match is not None:
            order.address_id = match.id
            order.address = match
        else:
            order.object_address = quote.object_address
    for i, row in enumerate(rows, start=1):
        order.lines.append(OrderLine(tenant_id=order.tenant_id, position=i, **row))
    db.flush()
    if quote.status != QUOTE_ACCEPTED:
        angebote.set_status(db, quote, QUOTE_ACCEPTED, user_id=user_id, ip=ip)
    _log(db, order, "aus_angebot", user_id, ip, {"angebot": quote.number})
    return order, None


# ================================================================ Bearbeiten
def _num(value: Decimal) -> str:
    text = f"{value:.4f}".replace(".", ",")
    return text.rstrip("0").rstrip(",")


def header_values(order: Order) -> dict:
    return {"title": order.title, "kind": order.kind, "status": order.status,
            "address_id": str(order.address_id or ""), "object_address": order.object_address or "",
            "description": order.description or "", "notes": order.notes or ""}


def line_rows(order: Order, extra: int = EMPTY_LINES) -> list[dict]:
    rows = [{"titel": line.title, "beschreibung": line.description or "", "menge": _num(line.quantity),
             "einheit": line.unit or "Stück", "preis": _num(line.unit_price), "satz": f"{line.vat_rate.normalize():f}"}
            for line in order.lines]
    return rows + [{"titel": "", "beschreibung": "", "menge": "1", "einheit": "Stück", "preis": "", "satz": ""}
                   for _ in range(extra)]


def task_rows(order: Order, extra: int = EMPTY_TASKS) -> list[dict]:
    return ([{"text": t.text, "erledigt": t.done} for t in order.tasks]
            + [{"text": "", "erledigt": False} for _ in range(extra)])


def _snapshot(order: Order) -> dict:
    return header_values(order) | {"summe": str(order.net_total), "positionen": len(order.lines),
                                   "aufgaben": [(t.text, t.done) for t in order.tasks]}


def save_form(db: Session, order: Order, form: Mapping[str, str], *, user_id: int, ip: str | None,
              with_prices: bool = True) -> dict[str, str]:
    """Kopf, Leistungen (nur ``with_prices``) und Aufgaben speichern. Gibt Fehler je Feld zurück."""
    errors: dict[str, str] = {}
    title = (form.get("title") or "").strip()
    if not title:
        errors["title"] = "Bitte eine Bezeichnung angeben."
    kind = form.get("kind") or ""
    if kind not in ORDER_KIND_LABELS:
        errors["kind"] = "Bitte Einzel- oder Dauerauftrag wählen."
    status = form.get("status") or ""
    if status not in ORDER_STATUS_LABELS:
        errors["status"] = "Bitte einen Stand wählen."
    address = None
    raw_address = (form.get("address_id") or "").strip()
    if raw_address.isdigit():
        address = db.get(CustomerAddress, int(raw_address))
        if address is None or address.customer_id != order.customer_id:
            errors["address_id"] = "Unbekannte Objekt-Adresse."

    lines = []
    if with_prices:
        default_rate = "0" if rechnungen.is_small_business(db) else (einstellungen.get(db, "design.ust_satz") or "19")
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
            unit = form.get(prefix + "einheit") or "Stück"
            lines.append(dict(title=line_title[:300], description=(form.get(prefix + "beschreibung") or "").strip()
                              or None, quantity=quantity, unit=unit if unit in UNITS else "Stück", unit_price=price,
                              vat_rate=Decimal(0) if rechnungen.is_small_business(db) else rate))
    tasks = []
    for i in range(MAX_TASKS):
        key = f"aufgabe-{i}-text"
        if key not in form:
            break
        text = (form.get(key) or "").strip()
        if text:
            tasks.append((text[:300], bool(form.get(f"aufgabe-{i}-erledigt"))))
    if errors:
        return errors

    old = _snapshot(order)
    order.title, order.kind, order.status = title[:200], kind, status
    order.address_id = address.id if address else None
    order.address = address
    order.object_address = None if address else ((form.get("object_address") or "").strip() or None)
    order.description = (form.get("description") or "").strip() or None
    order.notes = (form.get("notes") or "").strip() or None
    if with_prices:
        order.lines.clear()
        db.flush()
        for i, values in enumerate(lines, start=1):
            order.lines.append(OrderLine(tenant_id=order.tenant_id, position=i, **values))
    order.tasks.clear()
    db.flush()
    for i, (text, done) in enumerate(tasks, start=1):
        order.tasks.append(OrderTask(tenant_id=order.tenant_id, position=i, text=text, done=done))
    db.flush()
    changes = audit.diff(old, _snapshot(order))
    if changes:
        _log(db, order, "geaendert", user_id, ip, changes)
    return {}


def set_status(db: Session, order: Order, status: str, *, user_id: int, ip: str | None) -> str | None:
    if status not in ORDER_STATUS_LABELS:
        return "Unbekannter Stand."
    if order.status != status:
        _log(db, order, "stand", user_id, ip, {"stand": [order.status, status]})
        order.status = status
        db.flush()
    return None


def toggle_task(db: Session, task: OrderTask, *, user_id: int, ip: str | None) -> None:
    task.done = not task.done
    _log(db, task.order, "aufgabe", user_id, ip, {"aufgabe": task.text, "erledigt": task.done})
    db.flush()


def delete(db: Session, order: Order, *, user_id: int, ip: str | None) -> str | None:
    """Löschen nur, solange es weder Rechnung noch Vertrag dazu gibt."""
    if order.contract_id or documents_of(db, [order])[order.id]:
        return "Zu diesem Auftrag gibt es schon eine Rechnung oder einen Vertrag – bitte als erledigt markieren."
    _log(db, order, "geloescht", user_id, ip, {"nummer": order.number, "titel": order.title})
    db.delete(order)
    db.flush()
    return None


# ======================================================= Rechnung / Vertrag
def _rows(order: Order) -> list[dict]:
    return [dict(title=line.title, description=line.description, quantity=line.quantity, unit=line.unit,
                 unit_price=line.unit_price, vat_rate=line.vat_rate) for line in order.lines]


def _object_text(order: Order) -> str | None:
    return address_text(order.address) if order.address is not None else order.object_address


def to_invoice(db: Session, order: Order, *, user_id: int, ip: str | None) -> tuple[Document | None, str | None]:
    """Rechnungsentwurf aus den Leistungen (Dauerauftrag: ein Monat)."""
    if not order.lines:
        return None, "Der Auftrag enthält keine Leistungen mit Preis."
    doc = rechnungen.create_draft(db, order.customer, user_id=user_id, ip=ip)
    doc.source, doc.order_id, doc.quote_id = "auftrag", order.id, order.quote_id
    doc.object_address = _object_text(order)
    doc.intro_text = f"Gemäß Auftrag Nr. {order.number} („{order.title}“) berechnen wir Ihnen:"
    rechnungen.add_lines(db, doc, _rows(order))
    rechnungen.recalc(doc)
    db.flush()
    _log(db, order, "rechnung_erstellt", user_id, ip, {"rechnung_id": doc.id})
    return doc, None


def to_contract(db: Session, order: Order, *, user_id: int, ip: str | None) -> tuple[RecurringContract | None,
                                                                                    str | None]:
    """Vertrag (monatliche Rechnungsentwürfe) aus einem Dauerauftrag."""
    if not vertraege.enabled(db):
        return None, "Verträge sind ausgeschaltet (Einstellungen → Angebote → Wiederkehrende Leistungen)."
    if order.kind != ORDER_RECURRING:
        return None, "Verträge gibt es nur für Daueraufträge."
    if order.contract_id:
        return None, "Zu diesem Auftrag gibt es schon einen Vertrag."
    if not order.lines:
        return None, "Der Auftrag enthält keine Leistungen mit Preis."
    contract = vertraege.create(db, order.customer, user_id=user_id, ip=ip)
    contract.title = f"{order.title} (Auftrag {order.number})"[:200]
    contract.quote_id = order.quote_id
    contract.object_address = _object_text(order)
    for line in order.lines:
        contract.lines.append(RecurringContractLine(
            tenant_id=contract.tenant_id, position=line.position, title=line.title, description=line.description,
            quantity=line.quantity, unit=line.unit or "Monat", unit_price=line.unit_price, vat_rate=line.vat_rate))
    order.contract_id = contract.id
    db.flush()
    _log(db, order, "vertrag_erstellt", user_id, ip, {"vertrag_id": contract.id})
    return contract, None


def price_text(order: Order) -> str:
    return f"{format_decimal(order.net_total, 2)} €" + (" / Monat" if order.kind == ORDER_RECURRING else "")


# ===================================================== Automatisch einlesen
# Entschieden 17.11.2026: Bisherige und neue Ausgangsrechnungen, angenommene Angebote und Verträge werden
# automatisch zu Aufträgen. Je Rechnung ein Einzelauftrag; Rechnungen aus demselben Angebot bzw. Vertrag
# landen im Auftrag dieses Angebots/Vertrags. Fehlt der Rechnung ein Kunde (hochgeladene alte Rechnungen),
# wird er aus Name und Adresse angelegt bzw. ein gleichnamiger Kunde verwendet. Die Rechnung selbst bleibt
# unverändert (GoBD) – nur der Verweis ``documents.order_id`` wird gesetzt; der Kunde hängt am Auftrag.
# Stand beim Einlesen: bezahlt -> erledigt, sonst in Arbeit; Angebot ohne Rechnung -> offen;
# Vertrag aktiv/pausiert -> in Arbeit, beendet -> erledigt.
SOURCE_INVOICE = "rechnung"
SOURCE_CONTRACT = "vertrag"
SOURCE_QUOTE = "angebot"
_ZIP_RE = re.compile(r"^(?:D-)?(\d{4,5})\s+(.+)$")


@dataclass
class SyncResult:
    orders: int = 0          # neu angelegte Aufträge
    linked: int = 0          # Rechnungen, die einem vorhandenen Auftrag zugeordnet wurden
    customers: int = 0       # automatisch angelegte Kunden

    @property
    def changed(self) -> bool:
        return bool(self.orders or self.linked or self.customers)


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0)


def _status_from(docs: list[Document]) -> str:
    invoices = [d for d in docs if d.kind == KIND_INVOICE and d.status != STATUS_CANCELLED]
    if invoices and all(d.state == STATE_FINAL and d.status == STATUS_PAID for d in invoices):
        return ORDER_DONE
    return ORDER_WORKING if invoices else ORDER_OPEN


def _split_address(name: str, text: str | None) -> dict:
    """Empfänger-Adresse einer Rechnung -> Straße, PLZ, Ort (Namenszeilen werden übersprungen)."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip() and x.strip().casefold() != name.casefold()]
    result = {"street": None, "zip": None, "city": None}
    for i, line in enumerate(lines):
        m = _ZIP_RE.match(line)
        if m:
            result["zip"], result["city"] = m.group(1), m.group(2)[:120]
            if i > 0:
                result["street"] = lines[i - 1][:200]
            break
    else:
        if lines:
            result["street"] = lines[-1][:200]
    return result


class _Customers:
    """Kunden nach Name (ohne Groß-/Kleinschreibung); legt fehlende an."""

    def __init__(self, db: Session, result: SyncResult):
        self.db, self.result = db, result
        self.by_name: dict[str, Customer] = {}
        for c in db.scalars(select(Customer).order_by(Customer.is_active.desc(), Customer.id)):
            self.by_name.setdefault(_norm(c.name), c)

    def for_document(self, doc: Document) -> Customer | None:
        if doc.customer_id:
            return doc.customer
        name = (doc.partner_name or doc.recipient_name or "").strip()
        if not name:
            return None
        found = self.by_name.get(_norm(name))
        if found is not None:
            return found
        from app.services import stammdaten

        customer = Customer(number=stammdaten.next_number(self.db, stammdaten.CUSTOMER), name=name[:200],
                            country="Deutschland", **_split_address(name, doc.recipient_address),
                            notes=f"Automatisch angelegt aus Rechnung {doc.number or ''} – bitte Daten prüfen.")
        self.db.add(customer)
        self.db.flush()
        audit.log(self.db, action="angelegt", entity="kunde", entity_id=customer.id, tenant_id=customer.tenant_id,
                  user_id=None, changes={"name": customer.name, "grund": f"Auftrag aus Rechnung {doc.number}"})
        self.by_name[_norm(name)] = customer
        self.result.customers += 1
        return customer


def _set_object(order: Order, customer: Customer, text: str | None) -> None:
    order.address_id, order.address, order.object_address = None, None, None
    if not text or not text.strip():
        return
    match = next((a for a in customer.addresses if a.is_active and _norm(address_text(a)) == _norm(text)), None)
    if match is not None:
        order.address_id, order.address = match.id, match
    else:
        order.object_address = text.strip()


def _copy_lines(order: Order, doc: Document) -> None:
    order.lines.clear()
    for i, line in enumerate(doc.lines, start=1):
        quantity, price = line.quantity, line.unit_price
        if quantity is None or price is None or (quantity * price).quantize(Decimal("0.01")) != line.net:
            quantity, price = Decimal(1), line.net          # Rabatt o. ä.: als Pauschale übernehmen
        order.lines.append(OrderLine(tenant_id=order.tenant_id, position=i, title=line.title[:300],
                                     description=line.description, quantity=quantity, unit=line.unit,
                                     unit_price=price, vat_rate=line.vat_rate))


def _invoice_title(doc: Document) -> str:
    if doc.source == "import" or not doc.lines:
        return f"Rechnung {doc.number}" if doc.number else "Rechnung (Entwurf)"
    return doc.lines[0].title[:200]


def _new_order(db: Session, customer: Customer, day: date, *, title: str, kind: str, status: str,
               source: str) -> Order:
    order = Order(number=nummernkreise.allocate(db, "auftrag", day), customer_id=customer.id, title=title[:200],
                  kind=kind, status=status, source=source, created_at=_at(day))
    order.customer = customer
    db.add(order)
    db.flush()
    return order


def _order_for_contract(db: Session, contract: RecurringContract, result: SyncResult) -> Order:
    order = _new_order(db, contract.customer, contract.start_month, title=contract.title, kind=ORDER_RECURRING,
                       status=ORDER_DONE if contract.status == "beendet" else ORDER_WORKING, source=SOURCE_CONTRACT)
    order.contract_id, order.quote_id = contract.id, contract.quote_id
    order.description = "Automatisch übernommen aus dem Vertrag."
    _set_object(order, contract.customer, contract.object_address)
    for line in contract.lines:
        order.lines.append(OrderLine(tenant_id=order.tenant_id, position=line.position, title=line.title,
                                     description=line.description, quantity=line.quantity, unit=line.unit,
                                     unit_price=line.unit_price, vat_rate=line.vat_rate))
    db.flush()
    _log(db, order, "automatisch", None, None, {"vertrag_id": contract.id})
    result.orders += 1
    return order


def sync(db: Session) -> SyncResult:
    """Fehlende Aufträge anlegen (idempotent; läuft beim Öffnen der Übersicht und im Hintergrund).
    Schreibt ins Änderungsprotokoll, ruft KEIN commit auf."""
    result = SyncResult()
    customers = _Customers(db, result)

    # 1. Verträge -> Daueraufträge (die Rechnungen eines Vertrags gehören über contract_id dazu)
    has_order = select(Order.id).where(Order.contract_id == RecurringContract.id).exists()
    for contract in list(db.scalars(select(RecurringContract).where(~has_order))):
        _order_for_contract(db, contract, result)

    # 2. Ausgangsrechnungen ohne Auftrag (chronologisch, damit die Nummern zur Zeit passen)
    stmt = (select(Document).options(selectinload(Document.lines))
            .where(Document.direction == DIRECTION_OUT, Document.kind == KIND_INVOICE, Document.order_id.is_(None),
                   Document.contract_id.is_(None), Document.status != STATUS_CANCELLED)
            .order_by(Document.doc_date, Document.id))
    for doc in list(db.scalars(stmt)):
        if doc.state != STATE_FINAL and not doc.lines:
            continue                                         # leerer Entwurf: erst wenn Positionen da sind
        if doc.quote_id:
            quote = db.get(Quote, doc.quote_id)
            order = of_quote(db, quote) if quote else None
            if order is not None:
                doc.order_id = order.id
                result.linked += 1
                continue
        customer = customers.for_document(doc)
        if customer is None:
            continue
        order = _new_order(db, customer, doc.doc_date or date.today(), title=_invoice_title(doc), kind=ORDER_SINGLE,
                           status=_status_from([doc]), source=SOURCE_INVOICE)
        order.quote_id = doc.quote_id
        order.description = (f"Automatisch übernommen aus Rechnung {doc.number}" if doc.number
                             else "Automatisch übernommen aus einem Rechnungsentwurf")
        _set_object(order, customer, doc.object_address)
        _copy_lines(order, doc)
        doc.order_id = order.id
        db.flush()
        _log(db, order, "automatisch", None, None, {"rechnung_id": doc.id, "nummer": doc.number})
        result.orders += 1

    # 3. Angenommene Angebote ohne Auftrag
    has_order = select(Order.id).where(Order.quote_id == Quote.id).exists()
    for quote in list(db.scalars(select(Quote).where(Quote.status == QUOTE_ACCEPTED, Quote.customer_id.is_not(None),
                                                     ~has_order))):
        order, problem = from_quote(db, quote, user_id=None, ip=None)
        if order is None or problem:
            continue
        order.source, order.created_at = SOURCE_QUOTE, _at(quote.quote_date)
        result.orders += 1

    # 4. Automatische Aufträge einer Rechnung im Entwurf folgen der Rechnung, bis sie festgeschrieben ist;
    #    wurde der Entwurf gelöscht, verschwindet auch der automatische Auftrag (wenn nichts eingetragen wurde).
    for order in list(db.scalars(select(Order).options(selectinload(Order.lines), selectinload(Order.tasks))
                                 .where(Order.source == SOURCE_INVOICE))):
        docs = list(db.scalars(select(Document).options(selectinload(Document.lines))
                               .where(Document.order_id == order.id)))
        if not docs:
            if not order.tasks and not order.notes:
                _log(db, order, "geloescht", None, None, {"grund": "Rechnungsentwurf gelöscht"})
                db.delete(order)
            continue
        if len(docs) == 1 and docs[0].state != STATE_FINAL:
            doc = docs[0]
            title = _invoice_title(doc)
            if title != order.title or len(doc.lines) != len(order.lines) or order.net_total != (doc.net_total or 0):
                order.title = title[:200]
                _copy_lines(order, doc)
                _set_object(order, order.customer, doc.object_address)
    db.flush()
    return result
