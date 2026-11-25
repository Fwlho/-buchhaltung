"""Stammdaten: Felder, Prüfung, Nummernvergabe, Suche, Speichern mit Protokoll.

Kunden, Lieferanten und Katalog-Einträge funktionieren gleich; die Unterschiede
stecken in der jeweiligen ``Kind``-Beschreibung unten.
"""
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.forms import Field, parse_fields
from app.models import CatalogItem, Customer, CustomerAddress, Supplier
from app.models.stammdaten import BILLING_TYPES, CUSTOMER_KINDS, ITEM_KINDS, UNITS
from app.services import audit, einstellungen, steuerschuld

LIST_LIMIT = 200
VAT_CHOICES = (("19", "19 %"), ("7", "7 %"), ("0", "0 %"))

_ADDRESS = (
    Field("street", "Straße und Hausnummer"),
    Field("zip", "PLZ"),
    Field("city", "Ort"),
    Field("country", "Land", default="Deutschland"),
)


@dataclass(frozen=True)
class Section:
    title: str
    fields: tuple[Field, ...]
    intro: str = ""


@dataclass(frozen=True)
class Kind:
    model: type
    entity: str             # Name im Änderungsprotokoll
    label: str              # „Kunde“
    sections: tuple[Section, ...]
    search_columns: tuple[str, ...]
    number_setting: str | None = None  # Einstellung mit der ersten Nummer
    checks: Callable[[dict, dict], None] | None = None

    @property
    def fields(self) -> tuple[Field, ...]:
        return tuple(f for s in self.sections for f in s.fields)


# ---------------------------------------------------------------- Prüfungen
def _check_zip(values: dict, errors: dict, zip_key: str, country_key: str) -> None:
    country = (values.get(country_key) or "Deutschland").strip().lower()
    plz = values.get(zip_key)
    if plz and country in ("deutschland", "de", "germany") and not re.fullmatch(r"\d{5}", plz):
        errors[zip_key] = "Deutsche Postleitzahlen haben 5 Ziffern."


def _customer_checks(values: dict, errors: dict) -> None:
    _check_zip(values, errors, "zip", "country")
    billing = ("billing_name", "billing_street", "billing_zip", "billing_city")
    if any(values.get(k) for k in billing):
        for key in ("billing_street", "billing_zip", "billing_city"):
            if not values.get(key):
                errors[key] = "Für eine abweichende Rechnungsadresse nötig."
        _check_zip(values, errors, "billing_zip", "billing_country")
    if values.get("kind") == "privat":
        values["leitweg_id"] = None  # nur für Behörden sinnvoll


def _supplier_checks(values: dict, errors: dict) -> None:
    _check_zip(values, errors, "zip", "country")


def _address_checks(values: dict, errors: dict) -> None:
    _check_zip(values, errors, "zip", "country")


def _catalog_checks(values: dict, errors: dict) -> None:
    if values.get("vat_rate") is not None:
        values["vat_rate"] = Decimal(values["vat_rate"])


# ------------------------------------------------------------------ Arten
CUSTOMER = Kind(
    model=Customer, entity="kunde", label="Kunde", number_setting="nummern.kunde.start",
    search_columns=("name", "number", "city", "email", "contact_person"),
    checks=_customer_checks,
    sections=(
        Section("Stammdaten", (
            Field("kind", "Art", "choice", "firma", required=True, choices=CUSTOMER_KINDS),
            Field("number", "Kundennummer", "digits", help="Leer lassen = nächste freie Nummer."),
            Field("name", "Name / Firma", required=True),
            Field("contact_person", "Ansprechpartner/in"),
            Field("email", "E-Mail", "email"),
            Field("phone", "Telefon"),
        )),
        Section("Adresse", _ADDRESS),
        Section("Rechnungsangaben", (
            Field("ust_id", "USt-IdNr. des Kunden", "ustid"),
            Field("leitweg_id", "Leitweg-ID", "leitweg", help="Nur für E-Rechnungen an Behörden (XRechnung)."),
            Field("payment_terms_days", "Zahlungsziel in Tagen", "int", min=0, max=365,
                  help="Leer lassen = Standard aus den Einstellungen."),
            Field("reverse_charge", "§ 13b: Kunde schuldet die Umsatzsteuer (Reverse-Charge)", "bool", False,
                  help="Z. B. Bauleistungen an andere Bauunternehmen oder Dienstleistungen an Firmen in anderen "
                       "EU-Ländern. Der Hinweis ist dann bei neuen Rechnungen vorausgewählt."),
        )),
        Section("Abweichende Rechnungsadresse", (
            Field("billing_name", "Name / Firma (falls abweichend)"),
            Field("billing_street", "Straße und Hausnummer"),
            Field("billing_zip", "PLZ"),
            Field("billing_city", "Ort"),
            Field("billing_country", "Land"),
        ), intro="Nur ausfüllen, wenn Rechnungen an eine andere Adresse gehen sollen."),
        Section("Notizen", (Field("notes", "Interne Notizen", "textarea"),)),
    ),
)

CUSTOMER_ADDRESS = Kind(
    model=CustomerAddress, entity="objektadresse", label="Objektadresse",
    search_columns=("label", "city"), checks=_address_checks,
    sections=(Section("Objekt-/Leistungsadresse", (
        Field("label", "Bezeichnung", required=True, placeholder="z. B. Bürogebäude Nord"),
        Field("street", "Straße und Hausnummer", required=True),
        Field("zip", "PLZ", required=True),
        Field("city", "Ort", required=True),
        Field("country", "Land", default="Deutschland"),
        Field("note", "Hinweis (z. B. Zugang, Ansprechpartner vor Ort)", "textarea"),
    )),),
)

SUPPLIER = Kind(
    model=Supplier, entity="lieferant", label="Lieferant", number_setting="nummern.lieferant.start",
    search_columns=("name", "number", "city", "email", "contact_person"),
    checks=_supplier_checks,
    sections=(
        Section("Stammdaten", (
            Field("number", "Lieferantennummer", "digits", help="Leer lassen = nächste freie Nummer."),
            Field("name", "Name / Firma", required=True),
            Field("contact_person", "Ansprechpartner/in"),
            Field("email", "E-Mail", "email"),
            Field("phone", "Telefon"),
            Field("our_customer_number", "Unsere Kundennummer dort"),
        )),
        Section("Adresse", _ADDRESS),
        Section("Steuer & Bank", (
            Field("ust_id", "USt-IdNr.", "ustid"),
            Field("reverse_charge", "§ 13b: Ich schulde die Umsatzsteuer (Reverse-Charge)", "choice", "",
                  choices=(("", "nein"),) + tuple(steuerschuld.KINDS_IN.items()),
                  help="Bei neuen Belegen dieses Lieferanten vorausgewählt."),
            Field("iban", "IBAN", "iban"),
            Field("bic", "BIC", "bic"),
        )),
        Section("Notizen", (Field("notes", "Interne Notizen", "textarea"),)),
    ),
)

COST_FIELD = Field("cost_rate", "Interner Kostensatz (netto, je Einheit)", "price", min=0, places=4,
                   help="Nur intern für die Kalkulation – erscheint nie auf Angeboten oder Rechnungen.")

CATALOG = Kind(
    model=CatalogItem, entity="artikel", label="Katalog-Eintrag",
    search_columns=("title", "number", "description"),
    checks=_catalog_checks,
    sections=(
        Section("Eintrag", (
            Field("kind", "Art", "choice", "leistung", required=True, choices=ITEM_KINDS),
            Field("number", "Artikelnummer (optional)"),
            Field("title", "Bezeichnung (erscheint fett auf Belegen)", required=True),
            Field("description", "Beschreibung (optional, erscheint unter der Bezeichnung)", "textarea"),
        )),
        Section("Preis", (
            Field("unit", "Einheit", "choice", "Stück", required=True, choices=tuple((u, u) for u in UNITS)),
            Field("billing_type", "Abrechnungsart", "choice", "menge", required=True, choices=BILLING_TYPES),
            Field("price_net", "Einzelpreis netto (€)", "price", required=True, min=0, places=4),
            Field("vat_rate", "Umsatzsteuersatz", "choice", "19", required=True, choices=VAT_CHOICES),
        )),
        Section("Interne Kalkulation", (COST_FIELD,)),
    ),
)


def visible_sections(kind: Kind, can_see_costs: bool) -> tuple[Section, ...]:
    """Ohne Sonderrecht „kalkulation_intern“ wird der Kostensatz gar nicht angezeigt."""
    if can_see_costs:
        return kind.sections
    return tuple(s for s in kind.sections if COST_FIELD not in s.fields)


# ---------------------------------------------------------------- Nummern
def next_number(db: Session, kind: Kind) -> str:
    """Nächste freie Nummer: höchste vorhandene rein numerische Nummer + 1,
    mindestens die Startnummer aus den Einstellungen."""
    start = int(einstellungen.get(db, kind.number_setting) or 1)
    numbers = db.scalars(select(kind.model.number))
    highest = max((int(n) for n in numbers if n and n.isdigit()), default=start - 1)
    return str(max(highest + 1, start))


def number_taken(db: Session, kind: Kind, number: str, exclude_id: int | None) -> bool:
    stmt = select(func.count()).select_from(kind.model).where(kind.model.number == number)
    if exclude_id is not None:
        stmt = stmt.where(kind.model.id != exclude_id)
    return db.scalar(stmt) > 0


# ------------------------------------------------------------------ Suche
def search(db: Session, kind: Kind, query: str = "", archived: bool = False, order_by: str = "name"):
    model = kind.model
    stmt = select(model).where(model.is_active.is_(not archived))
    q = (query or "").strip().lower()
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(*(func.lower(getattr(model, col)).like(pattern) for col in kind.search_columns)))
    stmt = stmt.order_by(func.lower(getattr(model, order_by))).limit(LIST_LIMIT + 1)
    rows = list(db.scalars(stmt))
    return rows[:LIST_LIMIT], len(rows) > LIST_LIMIT


# ---------------------------------------------------------------- Speichern
def _jsonable(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


def snapshot(obj: Any, fields: tuple[Field, ...]) -> dict[str, Any]:
    return {f.key: _jsonable(getattr(obj, f.key)) for f in fields}


def form_source(obj: Any | None, kind: Kind) -> dict[str, Any]:
    """Werte für das Formular (bei neuem Datensatz die Standardwerte)."""
    if obj is None:
        return {f.key: f.default for f in kind.fields}
    return {f.key: getattr(obj, f.key) for f in kind.fields}


def save(
    db: Session,
    kind: Kind,
    obj: Any | None,
    form: Mapping[str, str],
    *,
    user_id: int,
    ip: str | None,
    fields: tuple[Field, ...] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> tuple[Any | None, dict[str, str]]:
    """Prüft und speichert (neu oder geändert). ``fields`` begrenzt die
    änderbaren Felder (z. B. ohne Kostensatz). Ruft KEIN commit auf."""
    fields = fields if fields is not None else kind.fields
    values, errors = parse_fields(fields, form)
    if kind.checks:
        kind.checks(values, errors)

    if kind.number_setting:
        if values.get("number"):
            if number_taken(db, kind, values["number"], getattr(obj, "id", None)):
                errors["number"] = "Diese Nummer ist bereits vergeben."
        elif obj is not None:
            values["number"] = obj.number  # Nummer bleibt beim Leeren erhalten
        else:
            values["number"] = next_number(db, kind)
    if errors:
        return None, errors

    tenant_id = db.info.get("tenant_id")
    if obj is None:
        obj = kind.model(**values, **(extra or {}))
        db.add(obj)
        db.flush()
        audit.log(db, action="angelegt", entity=kind.entity, entity_id=obj.id, tenant_id=tenant_id,
                  user_id=user_id, changes={k: _jsonable(v) for k, v in values.items() if v not in (None, "")},
                  ip=ip)
        return obj, {}

    old = snapshot(obj, fields)
    for key, value in values.items():
        setattr(obj, key, value)
    changes = audit.diff(old, snapshot(obj, fields))
    if changes:
        audit.log(db, action="geaendert", entity=kind.entity, entity_id=obj.id, tenant_id=tenant_id,
                  user_id=user_id, changes=changes, ip=ip)
    return obj, {}


def set_active(db: Session, kind: Kind, obj: Any, active: bool, *, user_id: int, ip: str | None) -> None:
    if obj.is_active == active:
        return
    obj.is_active = active
    audit.log(db, action="reaktiviert" if active else "archiviert", entity=kind.entity, entity_id=obj.id,
              tenant_id=db.info.get("tenant_id"), user_id=user_id, ip=ip)
