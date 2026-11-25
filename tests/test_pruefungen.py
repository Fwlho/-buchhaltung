from app.services.nummernkreise import format_number, format_problem
from app.services.pruefungen import bic_valid, format_iban, iban_valid, ustid_valid


def test_iban():
    assert iban_valid("DE89370400440532013000")
    assert iban_valid("de89 3704 0044 0532 0130 00")
    assert not iban_valid("DE89370400440532013001")  # Prüfziffer falsch
    assert not iban_valid("DE8937040044053201300")   # zu kurz
    assert not iban_valid("")
    assert format_iban("DE89370400440532013000") == "DE89 3704 0044 0532 0130 00"


def test_bic():
    assert bic_valid("COBADEFFXXX")
    assert bic_valid("COBADEFF")
    assert not bic_valid("COBA")


def test_ust_id():
    assert ustid_valid("DE123456789")
    assert not ustid_valid("DE12345678")
    assert ustid_valid("ATU12345678")


def test_nummernformat():
    assert format_number("{JJJJ}-{NNN}", 2026, 3, 7) == "2026-007"
    assert format_number("RE{JJ}{MM}-{NNNN}", 2026, 3, 42) == "RE2603-0042"
    assert format_number("{JJJJ}-{N}", 2026, 1, 1234) == "2026-1234"


def test_nummernformat_pruefung():
    assert format_problem("{JJJJ}-{NNN}") is None
    assert format_problem("") is not None
    assert format_problem("{JJJJ}") is not None            # keine laufende Nummer
    assert format_problem("{NNN}-{NN}") is not None        # zwei laufende Nummern
    assert format_problem("{XX}-{NNN}") is not None        # unbekannter Platzhalter
    assert format_problem("R {NNN}") is not None           # Leerzeichen
