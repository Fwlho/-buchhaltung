from datetime import date, datetime
from decimal import Decimal

import pytest

from app.formatting import (
    format_date,
    format_datetime,
    format_decimal,
    format_money,
    format_percent,
    parse_date,
    parse_decimal,
    parse_money,
    round_money,
)


def test_geld_anzeige_deutsch():
    assert format_money(Decimal("1234.5")) == "1.234,50 €"
    assert format_money(Decimal("-0.5")) == "-0,50 €"
    assert format_money(Decimal("1234567.891")) == "1.234.567,89 €"
    assert format_decimal(0) == "0,00"
    assert format_money(None) == ""


def test_kaufmaennisch_runden():
    assert round_money(Decimal("2.345")) == Decimal("2.35")
    assert round_money(Decimal("2.344")) == Decimal("2.34")
    assert round_money(Decimal("-2.345")) == Decimal("-2.35")


@pytest.mark.parametrize("text,expected", [
    ("1.234,56", Decimal("1234.56")),
    ("1234,56", Decimal("1234.56")),
    ("1234", Decimal("1234")),
    ("-5,5", Decimal("-5.5")),
    ("12.5", Decimal("12.5")),       # Punkt statt Komma vertippt
    ("1.234", Decimal("1234")),      # Tausenderpunkt
    (" 99,90 € ", Decimal("99.90")),
])
def test_zahl_eingabe(text, expected):
    assert parse_decimal(text) == expected


@pytest.mark.parametrize("text", ["abc", "1,2,3", "12.34.5", "1.23,4.5"])
def test_zahl_eingabe_ungueltig(text):
    with pytest.raises(ValueError):
        parse_decimal(text)


def test_leere_eingabe():
    assert parse_decimal("") is None
    assert parse_money("  ") is None
    assert parse_money("10,005") == Decimal("10.01")


def test_prozent():
    assert format_percent(Decimal("19")) == "19 %"
    assert format_percent(Decimal("7.50")) == "7,5 %"


def test_datum():
    assert format_date(date(2026, 3, 5)) == "05.03.2026"
    assert parse_date("05.03.2026") == date(2026, 3, 5)
    assert parse_date("5.3.26") == date(2026, 3, 5)
    assert parse_date("") is None
    with pytest.raises(ValueError):
        parse_date("2026-03-05")
    with pytest.raises(ValueError):
        parse_date("31.02.2026")


def test_zeitpunkt_in_ortszeit():
    # 28.10.2026 11:30 UTC = 12:30 Uhr deutscher Winterzeit
    assert format_datetime(datetime(2026, 10, 28, 11, 30)) == "28.10.2026 12:30"
