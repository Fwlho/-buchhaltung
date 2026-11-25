"""Deutsches Zahlen- und Datumsformat für Eingabe und Anzeige.

Geld wird intern immer als Decimal geführt und kaufmännisch gerundet.
"""
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Berlin")
CENT = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def format_decimal(value: Decimal | int | None, places: int = 2) -> str:
    """Decimal('1234.5') -> '1.234,50'"""
    if value is None:
        return ""
    value = Decimal(value)
    quant = Decimal(1).scaleb(-places)
    value = value.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    text = f"{abs(value):,.{places}f}"  # englisches Format: 1,234.50
    text = text.replace(",", "X").replace(".", ",").replace("X", ".")
    return sign + text


def format_money(value: Decimal | int | None) -> str:
    if value is None:
        return ""
    return f"{format_decimal(value, 2)} €"


def format_price(value: Decimal | int | None) -> str:
    """Einzelpreise: mindestens 2, höchstens 4 Nachkommastellen (0,125 -> '0,125')."""
    if value is None:
        return ""
    value = Decimal(value)
    places = 2
    for candidate in (3, 4):
        if value.quantize(Decimal(1).scaleb(-(candidate - 1)), rounding=ROUND_HALF_UP) != value.quantize(
            Decimal(1).scaleb(-candidate), rounding=ROUND_HALF_UP
        ):
            places = candidate
    return format_decimal(value, places)


def format_percent(value: Decimal | int | None) -> str:
    """Steuersätze: 19 -> '19 %', 7.5 -> '7,5 %'"""
    if value is None:
        return ""
    value = Decimal(value).normalize()
    places = max(0, -value.as_tuple().exponent)
    return f"{format_decimal(value, places)} %"


_NUMBER_RE = re.compile(r"^[+-]?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?$")


def parse_decimal(text: str | None) -> Decimal | None:
    """'1.234,56' -> Decimal('1234.56'); leer -> None; ungültig -> ValueError.

    Erlaubt: '1234,56', '1.234,56', '1234', '-5,5'. Ein einzelner Punkt mit
    1–2 Nachkommastellen ('12.5') wird als Tippfehler für ein Komma gedeutet.
    """
    if text is None:
        return None
    raw = text.strip().replace("€", "").replace("%", "").replace(" ", "").replace(" ", "")
    if raw == "":
        return None
    if re.fullmatch(r"[+-]?\d+\.\d{1,2}", raw):
        raw = raw.replace(".", ",")
    if not _NUMBER_RE.match(raw):
        raise ValueError(f"„{text}“ ist keine gültige Zahl (Beispiel: 1.234,56).")
    try:
        return Decimal(raw.replace(".", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"„{text}“ ist keine gültige Zahl.") from exc


def parse_money(text: str | None) -> Decimal | None:
    value = parse_decimal(text)
    return None if value is None else round_money(value)


def format_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d.%m.%Y")


def format_datetime(value: datetime | None) -> str:
    """Zeitpunkte sind in der DB UTC – Anzeige in deutscher Ortszeit."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def parse_date(text: str | None) -> date | None:
    """'05.03.2026' bzw. '5.3.26' -> date; leer -> None; ungültig -> ValueError."""
    if text is None or text.strip() == "":
        return None
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})", text.strip())
    if not m:
        raise ValueError(f"„{text}“ ist kein gültiges Datum (Format TT.MM.JJJJ).")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"„{text}“ ist kein gültiges Datum.") from exc
