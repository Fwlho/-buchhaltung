"""Export für die Steuerberatung: DATEV-Buchungsstapel (EXTF), Belege-ZIP, Übersicht.

Beispiel (von Hand nachgerechnet): Ausgangsrechnung 10.03.2026 = 19 % 160,89 € brutto + 7 % 21,40 € brutto,
Eingangsrechnung 14.03.2026 = 499,80 € brutto (19 %, Konto 4930), bezahlt mit 2 % Skonto (10,00 €).
"""
import io
import zipfile
from datetime import date

from tests.conftest import VALID_SETTINGS, post
from tests.test_auswertung import _ready
from tests.test_eingangsrechnungen import PDF_ZWEI, accepted_doc, upload
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _final
from tests.test_rechte import _invite

DATEV = {"datev.berater": "1001", "datev.mandant": "42", "datev.sammelkreditor": "99999",
         "datev.sammeldebitor": "69999"}
Q1 = "zeitraum=quartal&jahr=2026&nr=1"


def _setup(client, db, **datev):
    _ready(client)
    post(client, "/einstellungen/datev", {**DATEV, **datev})
    out = _final(client, db, doc_date="10.03.2026", service_date="10.03.2026")
    inc = _final_incoming(client, db)
    accepted_doc(client, db, pdf=PDF_ZWEI, name="entwurf.pdf")  # übernommen, aber nicht festgeschrieben
    upload(client, ("offen.pdf", PDF_ZWEI))  # Prüfbereich – nie im Export
    return out.id, out.number, inc.id


def _final_incoming(client, db, **overrides):
    inc = accepted_doc(client, db, **overrides)
    r = post(client, f"/eingangsrechnungen/{inc.id}/festschreiben")
    assert r.status_code == 200, r.text
    return inc


def _zip(client, query: str = Q1) -> zipfile.ZipFile:
    r = client.get(f"/export/datev.zip?{query}")
    assert r.headers["content-type"] == "application/zip", r.text[:300]
    return zipfile.ZipFile(io.BytesIO(r.content))


def _extf(zf: zipfile.ZipFile) -> list[str]:
    name = next(n for n in zf.namelist() if n.startswith("EXTF_"))
    return zf.read(name).decode("cp1252").split("\r\n")


def test_buchungsstapel(client, db):
    _, number, _ = _setup(client, db)
    page = client.get(f"/export?{Q1}").text
    assert "2 Dateien" in page and "3 Buchungssätze" in page and "ZIP herunterladen" in page

    zf = _zip(client)
    lines = _extf(zf)
    header = lines[0].split(";")
    assert header[:5] == ['"EXTF"', "700", "21", '"Buchungsstapel"', "13"]
    assert header[10:16] == ["1001", "42", "20260101", "4", "20260101", "20260331"]
    assert header[26] == '"03"' and len(header) == 31
    assert lines[1].startswith("Umsatz (ohne Soll/Haben-Kz);Soll/Haben-Kennzeichen;WKZ Umsatz")
    rows = [line.split(";") for line in lines[2:] if line]
    ref = f'"{number}"'
    assert [(r[0], r[1], r[6], r[7], r[8], r[9], r[10]) for r in rows] == [
        ("160,89", '"S"', "10000", "8400", '""', "1003", ref),        # Kunde an Erlöse 19 %
        ("21,40", '"S"', "10000", "8300", '""', "1003", ref),         # Kunde an Erlöse 7 %
        ("499,80", '"S"', "4930", "99999", '"9"', "1403", '"RE-2026-0815"'),  # Bürobedarf an Lieferant, BU 9
    ]
    assert rows[0][13] == '"Bäckerei Krume GmbH"'

    names = zf.namelist()
    assert f"Belege/AR_2026-03-10_{number}.pdf" in names and "Belege/ER_2026-03-14_RE-2026-0815.pdf" in names
    assert zf.read(f"Belege/AR_2026-03-10_{number}.pdf").startswith(b"%PDF")
    overview = zf.read("Uebersicht_2026-01-01_2026-03-31.csv").decode("utf-8")
    assert overview.startswith("﻿Belegdatum;Art;Nummer") and "Eingangsrechnung;RE-2026-0815" in overview
    assert "2026/117" not in overview  # Beleg aus dem Prüfbereich
    assert "LIESMICH.txt" in names


def test_zahlungen_und_skonto(client, db):
    out_id, number, inc_id = _setup(client, db, **{"datev.zahlungen": "1"})
    post(client, f"/rechnungen/{out_id}/zahlung", {"paid_on": "20.03.2026"})
    post(client, f"/eingangsrechnungen/{inc_id}/zahlung", {"paid_on": "21.03.2026", "mit_skonto": "1"})
    rows = [line.split(";") for line in _extf(_zip(client))[2:] if line]
    payments = [(r[0], r[1], r[6], r[7], r[9]) for r in rows[3:]]
    assert payments == [
        ("182,29", '"S"', "1200", "10000", "2003"),   # Bank an Kunde
        ("489,80", '"H"', "1200", "99999", "2103"),   # Lieferant an Bank
        ("10,00", '"S"', "99999", "3736", "2103"),    # Lieferant an Erhaltene Skonti 19 %
    ]


def test_storno_wird_gegengebucht(client, db):
    _ready(client)
    post(client, "/einstellungen/datev", DATEV)
    out = _final(client, db)
    post(client, f"/rechnungen/{out.id}/stornieren", {"grund": "Doppelt"})
    year = date.today().year
    rows = [line.split(";") for line in _extf(_zip(client, f"zeitraum=jahr&jahr={year}"))[2:] if line]
    assert [(r[0], r[1], r[7]) for r in rows] == [("160,89", '"S"', "8400"), ("21,40", '"S"', "8300"),
                                                  ("160,89", '"H"', "8400"), ("21,40", '"H"', "8300")]


def test_skr04_und_kleinunternehmer(client, db):
    _ready(client)
    post(client, "/einstellungen/datev", DATEV)
    post(client, "/einstellungen/konten", {"konten.rahmen": "SKR04"})
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.kleinunternehmer": "1"})
    _final(client, db, doc_date="10.03.2026", service_date="10.03.2026")
    _final_incoming(client, db, account="6815")
    lines = _extf(_zip(client))
    assert lines[0].split(";")[26] == '"04"'
    rows = [line.split(";") for line in lines[2:] if line]
    assert [(r[6], r[7], r[8]) for r in rows] == [("10000", "4185", '""'), ("6815", "99999", '""')]  # kein VSt-Abzug


def test_ohne_beraternummer_und_jahreswechsel(client, db):
    _ready(client)
    page = client.get("/export").text
    assert "Berater- und Mandantennummer" in page and "ZIP herunterladen" not in page
    r = client.get(f"/export/datev.zip?{Q1}")
    assert "Berater- und Mandantennummer eintragen" in r.text
    post(client, "/einstellungen/datev", DATEV)
    r = client.get("/export?zeitraum=frei&von=01.12.2025&bis=31.01.2026")
    assert "in einem Kalenderjahr liegen" in r.text


def test_rechte_und_mandantentrennung(client, outbox, db):
    client_b, _, _ = _two_tenants(client, outbox, db)
    post(client, "/einstellungen/datev", DATEV)
    _final(client, db, doc_date="10.03.2026", service_date="10.03.2026")
    post(client_b, "/einstellungen/datev", DATEV)
    zf = _zip(client_b)
    assert [n for n in zf.namelist() if n.startswith("Belege/")] == []
    assert len([line for line in _extf(zf)[2:] if line]) == 0
    assert _invite(client, outbox, "max@example.org", "mitarbeiter").get("/export").status_code == 403
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get(f"/export/datev.zip?{Q1}").headers["content-type"] == "application/zip"
