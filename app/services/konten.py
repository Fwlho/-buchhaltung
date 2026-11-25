"""Häufige Aufwandskonten nach SKR03 und SKR04 (Vorschlagsliste für Eingangsrechnungen).

Die Liste ist bewusst kurz und nur eine Hilfe – jedes andere Konto kann frei
eingegeben werden. Die Zuordnung bitte im Zweifel mit der Steuerberatung abstimmen.
"""
import re

EXPENSE_ACCOUNTS: dict[str, list[tuple[str, str]]] = {
    "SKR03": [
        ("3100", "Fremdleistungen"),
        ("3300", "Wareneingang 7 % Vorsteuer"),
        ("3400", "Wareneingang 19 % Vorsteuer"),
        ("4200", "Raumkosten"),
        ("4210", "Miete"),
        ("4240", "Gas, Strom, Wasser"),
        ("4250", "Reinigung"),
        ("4260", "Instandhaltung betrieblicher Räume"),
        ("4360", "Versicherungen"),
        ("4380", "Beiträge"),
        ("4500", "Fahrzeugkosten"),
        ("4520", "Kfz-Versicherungen"),
        ("4530", "Laufende Kfz-Betriebskosten"),
        ("4540", "Kfz-Reparaturen"),
        ("4600", "Werbekosten"),
        ("4650", "Bewirtungskosten"),
        ("4660", "Reisekosten Arbeitnehmer"),
        ("4670", "Reisekosten Unternehmer"),
        ("4805", "Reparaturen und Instandhaltung von Betriebs- und Geschäftsausstattung"),
        ("4806", "Wartungskosten für Hard- und Software"),
        ("4855", "Sofortabschreibung geringwertiger Wirtschaftsgüter"),
        ("4900", "Sonstige betriebliche Aufwendungen"),
        ("4910", "Porto"),
        ("4920", "Telefon"),
        ("4925", "Telefax und Internetkosten"),
        ("4930", "Bürobedarf"),
        ("4940", "Zeitschriften, Bücher"),
        ("4945", "Fortbildungskosten"),
        ("4950", "Rechts- und Beratungskosten"),
        ("4955", "Buchführungskosten"),
        ("4960", "Mieten für Einrichtungen"),
        ("4970", "Nebenkosten des Geldverkehrs"),
        ("4980", "Sonstiger Betriebsbedarf"),
        ("4985", "Werkzeuge und Kleingeräte"),
    ],
    "SKR04": [
        ("5900", "Fremdleistungen"),
        ("5300", "Wareneingang 7 % Vorsteuer"),
        ("5400", "Wareneingang 19 % Vorsteuer"),
        ("6305", "Raumkosten"),
        ("6310", "Miete"),
        ("6325", "Gas, Strom, Wasser"),
        ("6330", "Reinigung"),
        ("6335", "Instandhaltung betrieblicher Räume"),
        ("6400", "Versicherungen"),
        ("6420", "Beiträge"),
        ("6500", "Fahrzeugkosten"),
        ("6520", "Kfz-Versicherungen"),
        ("6530", "Laufende Kfz-Betriebskosten"),
        ("6540", "Kfz-Reparaturen"),
        ("6600", "Werbekosten"),
        ("6640", "Bewirtungskosten"),
        ("6650", "Reisekosten Arbeitnehmer"),
        ("6670", "Reisekosten Unternehmer"),
        ("6470", "Reparaturen und Instandhaltung von Betriebs- und Geschäftsausstattung"),
        ("6495", "Wartungskosten für Hard- und Software"),
        ("6260", "Sofortabschreibung geringwertiger Wirtschaftsgüter"),
        ("6300", "Sonstige betriebliche Aufwendungen"),
        ("6800", "Porto"),
        ("6805", "Telefon"),
        ("6810", "Telefax und Internetkosten"),
        ("6815", "Bürobedarf"),
        ("6820", "Zeitschriften, Bücher"),
        ("6821", "Fortbildungskosten"),
        ("6825", "Rechts- und Beratungskosten"),
        ("6830", "Buchführungskosten"),
        ("6835", "Mieten für Einrichtungen"),
        ("6855", "Nebenkosten des Geldverkehrs"),
        ("6850", "Sonstiger Betriebsbedarf"),
        ("6845", "Werkzeuge und Kleingeräte"),
    ],
}


# Erlöskonten für Ausgangsrechnungen – ergeben sich aus Steuersatz bzw. Hinweis auf der Rechnung
INCOME_ACCOUNTS: dict[str, dict[str, tuple[str, str]]] = {
    "SKR03": {"19": ("8400", "Erlöse 19 % USt"), "7": ("8300", "Erlöse 7 % USt"),
              "ku": ("8195", "Erlöse als Kleinunternehmer (§ 19 UStG)"), "13b": ("8337", "Erlöse § 13b UStG"),
              "13b_eu": ("8336", "Erlöse sonstige Leistungen EU (Kunde schuldet die Steuer)"),
              "0": ("8100", "Steuerfreie Umsätze")},
    "SKR04": {"19": ("4400", "Erlöse 19 % USt"), "7": ("4300", "Erlöse 7 % USt"),
              "ku": ("4185", "Erlöse als Kleinunternehmer (§ 19 UStG)"), "13b": ("4337", "Erlöse § 13b UStG"),
              "13b_eu": ("4336", "Erlöse sonstige Leistungen EU (Kunde schuldet die Steuer)"),
              "0": ("4100", "Steuerfreie Umsätze")},
}

# Positionen der Anlage EÜR (nur Bezeichnungen – die Zeilennummern ändern sich jedes Jahr)
EUER_INCOME_GROUPS = {
    "ku": "Betriebseinnahmen als umsatzsteuerlicher Kleinunternehmer",
    "pflichtig": "Umsatzsteuerpflichtige Betriebseinnahmen",
    "frei": "Umsatzsteuerfreie / nicht steuerbare Betriebseinnahmen",
}
EUER_VAT_RECEIVED = "Vereinnahmte Umsatzsteuer"
EUER_VAT_PAID = "Gezahlte Vorsteuerbeträge"
EUER_VAT_REFUND = "Vom Finanzamt erstattete Umsatzsteuer"
EUER_VAT_TO_OFFICE = "An das Finanzamt gezahlte Umsatzsteuer"
EUER_OTHER_EXPENSES = "Übrige unbeschränkt abziehbare Betriebsausgaben"
_EXPENSE_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Waren, Roh- und Hilfsstoffe", ("3300", "3400", "5300", "5400")),
    ("Bezogene Fremdleistungen", ("3100", "5900")),
    ("Raumkosten und sonstige Grundstücksaufwendungen",
     ("4200", "4210", "4240", "4250", "4260", "6305", "6310", "6325", "6330", "6335")),
    ("Aufwendungen für Telekommunikation", ("4920", "4925", "6805", "6810")),
    ("Fortbildungskosten", ("4945", "6821")),
    ("Rechts- und Steuerberatung, Buchführung", ("4950", "4955", "6825", "6830")),
    ("Miete/Leasing für bewegliche Wirtschaftsgüter", ("4960", "6835")),
    ("Werbeaufwendungen", ("4600", "6600")),
    ("Reisekosten", ("4660", "4670", "6650", "6670")),
    ("Kraftfahrzeugkosten", ("4500", "4520", "4530", "4540", "6500", "6520", "6530", "6540")),
    ("Bewirtungsaufwendungen", ("4650", "6640")),
    ("Geringwertige Wirtschaftsgüter", ("4855", "6260")),
]
EXPENSE_GROUP_ORDER = [label for label, _ in _EXPENSE_GROUPS] + [EUER_OTHER_EXPENSES]
_EXPENSE_GROUP_BY_ACCOUNT = {acc: label for label, accounts in _EXPENSE_GROUPS for acc in accounts}


def income_account(chart: str, rate, hints, small_business: bool, reverse_charge: str | None = None
                   ) -> tuple[str, str, str]:
    """(Konto, Bezeichnung, EÜR-Gruppe) für eine Erlöszeile. ``reverse_charge``: § 13b-Fall der Rechnung
    („eu“ = Leistung an einen Unternehmer in einem anderen EU-Land, im Inland nicht steuerbar)."""
    accounts = INCOME_ACCOUNTS.get(chart, INCOME_ACCOUNTS["SKR03"])
    rate_key = f"{rate.normalize():f}" if rate is not None else "0"
    if small_business:
        number, label = accounts["ku"]
        return number, label, EUER_INCOME_GROUPS["ku"]
    if rate_key == "0" and "13b" in (hints or []) and reverse_charge == "eu":
        number, label = accounts["13b_eu"]
        return number, label, EUER_INCOME_GROUPS["frei"]
    if rate_key == "0" and "13b" in (hints or []):
        number, label = accounts["13b"]
        return number, label, EUER_INCOME_GROUPS["pflichtig"]
    if rate_key in accounts:
        number, label = accounts[rate_key]
        return number, label, EUER_INCOME_GROUPS["pflichtig" if rate_key != "0" else "frei"]
    return "", f"Erlöse {rate_key} % USt", EUER_INCOME_GROUPS["pflichtig"]


def expense_group(account: str | None) -> str:
    return _EXPENSE_GROUP_BY_ACCOUNT.get(account or "", EUER_OTHER_EXPENSES)


def accounts_for(chart: str) -> list[tuple[str, str]]:
    return sorted(EXPENSE_ACCOUNTS.get(chart, EXPENSE_ACCOUNTS["SKR03"]))


def account_label(chart: str, number: str | None) -> str:
    if not number:
        return ""
    names = dict(EXPENSE_ACCOUNTS.get(chart, []))
    return f"{number} {names[number]}" if number in names else number


def account_valid(text: str) -> bool:
    return bool(re.fullmatch(r"\d{4,8}", text or ""))
