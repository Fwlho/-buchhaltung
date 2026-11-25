"""§ 13b UStG – Steuerschuldnerschaft des Leistungsempfängers („Reverse-Charge“).

Zwei Richtungen (entschieden 12.11.2026):

- **Eingangsrechnungen** (die eigene Firma schuldet die Steuer): `documents.reverse_charge` = Fall
  „inland“ (z. B. Bau-/Reinigungsleistung eines deutschen Unternehmens, § 13b Abs. 2 Nr. 4/8),
  „eu“ (sonstige Leistung eines Unternehmens aus dem übrigen EU-Gebiet, § 13b Abs. 1) oder
  „drittland“ (Leistung eines Unternehmens außerhalb der EU, § 13b Abs. 2 Nr. 1). Die Rechnung selbst
  weist keine Umsatzsteuer aus (Beträge mit 0 %); die Steuer errechnet das Programm aus Netto ×
  `documents.reverse_charge_rate`. Voranmeldung: EU → Kz 46/47, sonst Kz 84/85; Vorsteuer daraus Kz 67
  (Kleinunternehmer: Steuer zahlen, aber kein Vorsteuerabzug).
- **Ausgangsrechnungen** (der Kunde schuldet die Steuer): Hinweis „13b“ wie bisher; der Fall „inland“
  (Kz 60) oder „eu“ (Kz 21 + Zusammenfassende Meldung) ergibt sich aus Land/USt-IdNr. des Kunden und wird
  beim Speichern in `documents.reverse_charge` festgehalten. Kunden außerhalb der EU: kein § 13b.

Stammdaten: `customers.reverse_charge` (Häkchen → Hinweis bei neuen Rechnungen vorausgewählt),
`suppliers.reverse_charge` (Fall → bei neuen Belegen vorausgewählt).
"""
import re
from decimal import Decimal

from app.formatting import round_money

INLAND, EU, DRITTLAND = "inland", "eu", "drittland"
KINDS_IN: dict[str, str] = {
    INLAND: "Bau-/Reinigungsleistung o. Ä. eines deutschen Unternehmens",
    EU: "Dienstleistung eines Unternehmens aus einem anderen EU-Land",
    DRITTLAND: "Leistung eines Unternehmens außerhalb der EU",
}
KINDS_OUT: dict[str, str] = {
    INLAND: "Kunde in Deutschland (Kz 60)",
    EU: "Kunde in einem anderen EU-Land (Kz 21, Zusammenfassende Meldung)",
}
RATES = ("19", "7")
DEFAULT_RATE = Decimal(19)

TEXT_INLAND = "Steuerschuldnerschaft des Leistungsempfängers (§ 13b UStG)."
TEXT_EU = "Steuerschuldnerschaft des Leistungsempfängers / Reverse charge (§ 13b UStG, Art. 196 MwStSystRL)."

# Länderkennungen der USt-IdNr. (Griechenland „EL“, Nordirland „XI“ nur für Waren – zählt hier als EU)
EU_VAT_PREFIXES = {"AT", "BE", "BG", "CY", "CZ", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU", "IE", "IT",
                   "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI"}
_GERMANY = {"", "de", "deu", "d", "deutschland", "germany", "bundesrepublik deutschland", "brd", "allemagne"}
_EU_NAMES = {
    "at", "österreich", "oesterreich", "austria", "be", "belgien", "belgium", "bg", "bulgarien", "bulgaria",
    "cy", "zypern", "cyprus", "cz", "tschechien", "tschechische republik", "czech republic", "czechia",
    "dk", "dänemark", "daenemark", "denmark", "ee", "estland", "estonia", "es", "spanien", "spain",
    "fi", "finnland", "finland", "fr", "frankreich", "france", "gr", "el", "griechenland", "greece",
    "hr", "kroatien", "croatia", "hu", "ungarn", "hungary", "ie", "irland", "ireland", "it", "italien", "italy",
    "lt", "litauen", "lithuania", "lu", "luxemburg", "luxembourg", "lv", "lettland", "latvia", "mt", "malta",
    "nl", "niederlande", "netherlands", "holland", "the netherlands", "pl", "polen", "poland",
    "pt", "portugal", "ro", "rumänien", "rumaenien", "romania", "se", "schweden", "sweden",
    "si", "slowenien", "slovenia", "sk", "slowakei", "slovakia",
}
_VAT_IN_TEXT = re.compile(r"\b(" + "|".join(sorted(EU_VAT_PREFIXES)) + r")\s?[0-9A-Z]{8,12}\b")


def zone(country: str | None, ust_id: str | None = None) -> str:
    """„inland“, „eu“ oder „drittland“ – die USt-IdNr. hat Vorrang vor dem (frei eingegebenen) Land."""
    prefix = re.sub(r"[^A-Za-z]", "", (ust_id or "").strip()[:2]).upper()
    if len(prefix) == 2:
        if prefix == "DE":
            return INLAND
        if prefix in EU_VAT_PREFIXES:
            return EU
    name = re.sub(r"\s+", " ", (country or "").strip().lower().rstrip("."))
    if name in _GERMANY:
        return INLAND
    if name in _EU_NAMES:
        return EU
    return DRITTLAND


def customer_zone(customer) -> str:
    """Zone eines Kunden: Land der Rechnungsadresse (falls abweichend), sonst Hauptadresse."""
    if customer is None:
        return INLAND
    country = customer.billing_country if customer.has_billing_address else customer.country
    return zone(country, customer.ust_id)


def guess_incoming(supplier, text: str | None, own_ust_id: str | None = None) -> str:
    """Fall einer Eingangsrechnung mit § 13b-Hinweis schätzen: Lieferant (Stammdaten), sonst eine fremde
    EU-USt-IdNr. im Belegtext, sonst „inland“. Wird immer gelb zur Prüfung markiert."""
    if supplier is not None:
        if supplier.reverse_charge in KINDS_IN:
            return supplier.reverse_charge
        if supplier.ust_id or supplier.country:
            return zone(supplier.country, supplier.ust_id)
    own = re.sub(r"\s", "", own_ust_id or "").upper()
    for match in _VAT_IN_TEXT.finditer((text or "").upper()):
        if re.sub(r"\s", "", match.group(0)) != own:
            return EU
    return INLAND


# ------------------------------------------------------------------ Eingang
def incoming_kind(doc) -> str | None:
    return doc.reverse_charge if doc.direction == "eingang" and doc.reverse_charge in KINDS_IN else None


def incoming_rate(doc) -> Decimal:
    return doc.reverse_charge_rate if doc.reverse_charge_rate else DEFAULT_RATE


def incoming_tax(doc) -> Decimal:
    """Umsatzsteuer, die die eigene Firma nach § 13b für diese Eingangsrechnung schuldet."""
    if not incoming_kind(doc) or doc.net_total is None:
        return Decimal("0.00")
    return round_money(doc.net_total * incoming_rate(doc) / 100)


def kennzahl_in(kind: str) -> tuple[str, str]:
    """(Kz Bemessungsgrundlage, Kz Steuer) der Voranmeldung."""
    return ("46", "47") if kind == EU else ("84", "85")


# ------------------------------------------------------------------ Ausgang
def outgoing_kind(doc) -> str | None:
    """Fall einer Ausgangsrechnung mit Hinweis „13b“ (ältere Rechnungen ohne Eintrag: Inland)."""
    if doc.direction != "ausgang" or "13b" not in (doc.hints or []):
        return None
    return doc.reverse_charge if doc.reverse_charge in KINDS_OUT else INLAND


def hint_text(doc) -> str:
    return TEXT_EU if outgoing_kind(doc) == EU else TEXT_INLAND
