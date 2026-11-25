"""Formularfelder: Definition, Prüfung der Eingabe, Anzeige im Formular.

Wird von den Firmen-Einstellungen und den Stammdaten (Kunden, Lieferanten,
Katalog) gemeinsam benutzt. Zahlen werden im deutschen Format erwartet.
"""
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.formatting import format_decimal, format_price, parse_decimal
from app.security import encrypt
from app.services.nummernkreise import format_problem
from app.services.pruefungen import (
    bic_valid,
    color_valid,
    email_valid,
    format_iban,
    iban_valid,
    leitweg_valid,
    normalize_iban,
    ustid_valid,
)


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    # text, textarea, email, int, decimal, price, bool, choice, color, iban, bic, ustid,
    # leitweg, digits, secret, numformat, logo
    type: str = "text"
    default: Any = None
    required: bool = False
    choices: tuple[tuple[str, str], ...] = ()
    help: str = ""
    min: int | None = None
    max: int | None = None
    placeholder: str = ""
    places: int = 2  # Nachkommastellen bei decimal/price


def parse_field(f: Field, form: Mapping[str, str], old: Any = None) -> tuple[Any, str | None]:
    """Formularwert -> (zu speichernder Wert, Fehlermeldung oder None)."""
    raw = (form.get(f.key) or "").strip()

    if f.type == "bool":
        return bool(form.get(f.key)), None

    if f.type == "secret":
        if form.get(f"{f.key}__loeschen"):
            return None, None
        if raw == "":
            return old, None  # leer = unverändert lassen
        return encrypt(raw), None

    if raw == "":
        if f.required:
            return None, "Bitte ausfüllen."
        return None, None

    if f.type in ("text", "textarea"):
        limit = 4000 if f.type == "textarea" else 300
        if len(raw) > limit:
            return None, f"Höchstens {limit} Zeichen."
        return raw, None
    if f.type == "email":
        return (raw, None) if email_valid(raw) else (None, "Keine gültige E-Mail-Adresse.")
    if f.type == "int":
        try:
            number = int(raw.replace(".", ""))
        except ValueError:
            return None, "Bitte eine ganze Zahl eingeben."
        if f.min is not None and number < f.min:
            return None, f"Mindestens {f.min}."
        if f.max is not None and number > f.max:
            return None, f"Höchstens {f.max}."
        return number, None
    if f.type in ("decimal", "price"):
        try:
            value = parse_decimal(raw)
        except ValueError as exc:
            return None, str(exc)
        quant = Decimal(1).scaleb(-f.places)
        value = value.quantize(quant, rounding=ROUND_HALF_UP)
        if f.min is not None and value < f.min:
            return None, f"Mindestens {format_decimal(Decimal(f.min), 0)}."
        if abs(value) >= Decimal(10) ** 8:
            return None, "Der Betrag ist zu groß."
        return value, None
    if f.type == "choice":
        allowed = {value for value, _ in f.choices}
        return (raw, None) if raw in allowed else (None, "Bitte einen Eintrag auswählen.")
    if f.type == "color":
        return (raw.lower(), None) if color_valid(raw) else (None, "Farbe im Format #RRGGBB.")
    if f.type == "iban":
        iban = normalize_iban(raw)
        return (iban, None) if iban_valid(iban) else (None, "Die IBAN ist ungültig (Prüfziffer stimmt nicht).")
    if f.type == "bic":
        bic = raw.upper().replace(" ", "")
        return (bic, None) if bic_valid(bic) else (None, "Die BIC ist ungültig (8 oder 11 Zeichen).")
    if f.type == "ustid":
        value = raw.upper().replace(" ", "")
        return (value, None) if ustid_valid(value) else (None, "Ungültige USt-IdNr. (Beispiel: DE123456789).")
    if f.type == "leitweg":
        value = raw.replace(" ", "")
        return (value, None) if leitweg_valid(value) else (None, "Ungültige Leitweg-ID (Beispiel: 04011000-1234512345-06).")
    if f.type == "digits":
        if not re.fullmatch(r"\d{1,9}", raw):
            return None, "Bitte nur Ziffern (höchstens 9 Stellen)."
        return raw, None
    if f.type == "numformat":
        problem = format_problem(raw)
        return (raw, None) if problem is None else (None, problem)
    raise ValueError(f"Unbekannter Feldtyp {f.type}")


def parse_fields(
    fields: Iterable[Field], form: Mapping[str, str], old: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Alle Felder prüfen. Gibt (Werte, Fehler je Feld) zurück."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in fields:
        if f.type == "logo":
            continue
        value, error = parse_field(f, form, (old or {}).get(f.key))
        if error:
            errors[f.key] = error
        values[f.key] = value
    return values, errors


def display_value(f: Field, value: Any) -> str:
    """Gespeicherter Wert -> Text im Formularfeld."""
    if value is None:
        return ""
    if f.type == "decimal":
        return format_decimal(Decimal(value), f.places)
    if f.type == "price":
        return format_price(Decimal(value))
    if f.type == "iban":
        return format_iban(value)
    if f.type == "secret":
        return ""
    if f.type == "bool":
        return "1" if value else ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")  # Auswahlfelder: Decimal("19.00") -> "19"
    return str(value)


def display_values(fields: Iterable[Field], values: Mapping[str, Any]) -> dict[str, str]:
    return {f.key: display_value(f, values.get(f.key, f.default)) for f in fields}


def posted_values(fields: Iterable[Field], form: Mapping[str, str]) -> dict[str, str]:
    """Eingaben nach fehlgeschlagener Prüfung wieder anzeigen (ohne Passwörter)."""
    return {f.key: ("" if f.type == "secret" else (form.get(f.key) or "")) for f in fields}
