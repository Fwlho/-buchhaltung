"""Angebote: Anlegen, Kalkulation, interne Kosten, Status, PDF, Angebot -> Rechnung, Trennung."""
import io
from datetime import date, timedelta
from decimal import Decimal as D

import pdfplumber
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import CatalogItem, Document, Membership, Quote, User
from tests.conftest import VALID_SETTINGS, complete_wizard, ersteinrichtung, post
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _customer
from tests.test_rechte import _invite

YEAR = date.today().year


def _ready(client):
    ersteinrichtung(client)
    complete_wizard(client)


def _quotes(db) -> list[Quote]:
    db.expire_all()
    stmt = (select(Quote).options(selectinload(Quote.positions), selectinload(Quote.cost_items))
            .execution_options(all_tenants=True).order_by(Quote.id))
    return list(db.scalars(stmt))


def _new(client, db, customer=None) -> Quote:
    customer = customer or _customer(client, db)
    r = post(client, "/angebote/neu", {"customer_id": str(customer.id)})
    assert "angelegt" in r.text, r.text
    return _quotes(db)[-1]


def _form(quote: Quote, **extra) -> dict:
    data = {
        "recipient_name": quote.recipient_name, "recipient_address": quote.recipient_address,
        "quote_date": quote.quote_date.strftime("%d.%m.%Y"), "valid_until": quote.valid_until.strftime("%d.%m.%Y"),
        "intro_text": quote.intro_text or "", "closing_text": quote.closing_text or "",
        # 1) Stunden: 2 Personen × 3 Std. × 30 € = 180,00
        "pos-0-id": "", "pos-0-titel": "Grundreinigung", "pos-0-art": "stunden", "pos-0-preis": "30",
        "pos-0-stunden": "3", "pos-0-personen": "2", "pos-0-satz": "19", "pos-0-einheit": "Std.",
        # 2) Menge: 420 m² × 0,125 = 52,50
        "pos-1-id": "", "pos-1-titel": "Glasflächen", "pos-1-art": "menge", "pos-1-menge": "420", "pos-1-einheit": "m²",
        "pos-1-preis": "0,125", "pos-1-satz": "19",
        # 3) Pauschal 80,00
        "pos-2-id": "", "pos-2-titel": "Teppichreinigung", "pos-2-art": "pauschal", "pos-2-preis": "80", "pos-2-satz": "19",
        "pos-2-einheit": "pauschal",
        # Zusatzkosten: Anfahrt 20 km × 0,30 = 6,00; Entsorgung 25,00
        "kost-0-block": "Anfahrt", "kost-0-beschreibung": "20 km", "kost-0-menge": "20", "kost-0-einheit": "km",
        "kost-0-preis": "0,30", "kost-0-satz": "19",
        "kost-1-block": "Entsorgung", "kost-1-menge": "1", "kost-1-preis": "25", "kost-1-satz": "19",
    }
    data.update(extra)
    return data


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def _enable_recurring(client):
    post(client, "/einstellungen/angebote", {**VALID_SETTINGS["angebote"], "angebot.wiederkehrend": "1"})


# ---------------------------------------------------------------- Anlegen
def test_angebot_anlegen(client, db):
    _ready(client)
    q = _new(client, db)
    assert q.number == f"A-{YEAR}-001" and q.status == "entwurf"
    assert q.valid_until == date.today() + timedelta(days=30)
    assert q.intro_text == "Gerne bieten wir an:" and q.recipient_name == "Bäckerei Krume GmbH"
    assert _new(client, db, q.customer).number == f"A-{YEAR}-002"


def test_kalkulation_und_speichern(client, db):
    _ready(client)
    q = _new(client, db)
    r = post(client, f"/angebote/{q.id}", _form(q))
    assert "Gespeichert" in r.text
    q = _quotes(db)[0]
    assert [(p.billing_type, p.unit) for p in q.positions] == [("stunden", "Std."), ("menge", "m²"),
                                                               ("pauschal", "pauschal")]
    # 180 + 52,50 + 80 + 6 + 25 = 343,50 netto; 19 % = 65,27 -> 408,77
    assert (q.net_total, q.gross_total) == (D("343.50"), D("408.77"))
    page = client.get(f"/angebote/{q.id}").text
    for text in ("312,50 €", "Anfahrt", "6,00 €", "343,50 €", "408,77 €"):
        assert text in page, text


def test_eingabefehler(client, db):
    _ready(client)
    q = _new(client, db)
    r = post(client, f"/angebote/{q.id}", _form(q, **{"pos-0-stunden": "", "pos-1-preis": "", "pos-2-preis": "-5",
                                                       "kost-0-preis": "", "valid_until": "01.01.2000",
                                                       "pos-0-personen": "zwei"}))
    assert r.status_code == 400
    for text in ("Stunden", "einen Preis", "nicht negativ", "vor dem Angebotsdatum"):
        assert text in r.text, text
    assert 'value="Grundreinigung"' in r.text


def test_katalog_mit_kostensatz(client, db):
    _ready(client)
    post(client, "/katalog/neu", {"kind": "leistung", "title": "Unterhaltsreinigung", "unit": "Std.",
                                  "billing_type": "stunden", "price_net": "28", "vat_rate": "19", "cost_rate": "19"})
    item = db.scalars(select(CatalogItem).execution_options(all_tenants=True)).one()
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q, **{"pos-3-id": "", "pos-3-titel": "", "pos-3-katalog": str(item.id),
                                                 "pos-3-stunden": "10", "pos-3-personen": "1"}))
    q = _quotes(db)[0]
    p = q.positions[-1]
    assert (p.title, p.billing_type, p.unit_price, p.cost_rate) == ("Unterhaltsreinigung", "stunden", D("28"), D("19"))
    page = client.get(f"/angebote/{q.id}").text
    assert "Marge" in page and "190,00 €" in page  # interne Kosten 10 Std. × 19 €


def test_interne_kosten_nur_mit_sonderrecht(client, outbox, db):
    _ready(client)
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q, **{"pos-0-kosten": "18", "extra_internal_cost": "50"}))
    q = _quotes(db)[0]
    assert q.positions[0].cost_rate == D("18") and q.extra_internal_cost == D("50.00")

    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")
    page = ma.get(f"/angebote/{q.id}").text
    assert "Interner Kostensatz" not in page and "Marge" not in page
    # Mitarbeiter speichert ohne Kostenfelder -> Kostensätze bleiben erhalten
    form = _form(q, **{f"pos-{i}-id": str(p.id) for i, p in enumerate(q.positions)})
    r = post(ma, f"/angebote/{q.id}", {**form, "pos-0-preis": "32"})
    assert "Gespeichert" in r.text
    q = _quotes(db)[0]
    assert q.positions[0].unit_price == D("32") and q.positions[0].cost_rate == D("18")
    assert q.extra_internal_cost == D("50.00")

    # PDF enthält nie interne Werte
    text = _pdf_text(client.get(f"/angebote/{q.id}/pdf").content)
    assert "Marge" not in text and "18,00" not in text and "Angebot Nr. A-" in text


def test_wiederkehrende_leistungen(client, db):
    _ready(client)
    q = _new(client, db)
    data = _form(q, **{"pos-2-wdh": "1", "pos-2-intervall": "woechentlich", "pos-2-laufzeit": "12"})
    post(client, f"/angebote/{q.id}", data)
    q = _quotes(db)[0]
    assert not q.positions[2].recurring  # Modul ausgeschaltet -> ignoriert

    _enable_recurring(client)
    post(client, f"/angebote/{q.id}", data)
    q = _quotes(db)[0]
    p = q.positions[2]
    assert p.recurring and p.interval == "woechentlich" and p.contract_months == 12
    # 80 × 4,33 = 346,40/Monat; × 12 = 4.156,80 Vertragswert
    page = client.get(f"/angebote/{q.id}").text
    assert "346,40 €/Monat" in page and "4.156,80 €" in page
    text = _pdf_text(client.get(f"/angebote/{q.id}/pdf").content)
    assert "Wiederkehrende Leistungen" in text and "346,40 €" in text and "Laufzeit 12" in text
    assert "pauschal" in text  # Einheit passt in die Spalte (kein Umbruch „pausch/al“)

    r = post(client, f"/angebote/{q.id}", {**data, "pos-2-intervall": ""})
    assert "Intervall" in r.text and r.status_code == 400


def test_pdf_inhalt(client, db):
    _ready(client)
    q = _new(client, db)
    r = post(client, f"/angebote/{q.id}", _form(q, aktion="pdf"))
    assert r.headers["content-type"] == "application/pdf"
    text = _pdf_text(r.content)
    for expected in (f"Angebot Nr. A-{YEAR}-001", "Gültig bis", "Bäckerei Krume GmbH", "2 Person(en) × 3,00 Std.",
                     "Anfahrt", "Angebotssumme", "408,77 €", "Gerne bieten wir an:"):
        assert expected in text, expected


def test_status_und_sperre(client, db):
    _ready(client)
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q))
    assert "versendet" in post(client, f"/angebote/{q.id}/status", {"status": "versendet"}).text
    assert "nicht möglich" in post(client, f"/angebote/{q.id}/status", {"status": "versendet"}).text
    post(client, f"/angebote/{q.id}/status", {"status": "angenommen"})
    q = _quotes(db)[0]
    assert q.status == "angenommen" and not q.is_editable
    r = post(client, f"/angebote/{q.id}", _form(q))
    assert "nicht mehr geändert" in r.text
    assert "nicht gelöscht" in post(client, f"/angebote/{q.id}/loeschen").text
    r = post(client, f"/angebote/{q.id}/kopieren")
    copy = _quotes(db)[-1]
    assert copy.number == f"A-{YEAR}-002" and copy.status == "entwurf" and len(copy.positions) == 3


def test_abgelaufen(client, db):
    _ready(client)
    q = _new(client, db)
    yesterday = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
    two_days = (date.today() - timedelta(days=2)).strftime("%d.%m.%Y")
    post(client, f"/angebote/{q.id}", _form(q, quote_date=two_days, valid_until=yesterday))
    assert "abgelaufen" in client.get("/angebote").text
    assert q.number in client.get("/angebote?status=abgelaufen").text
    assert q.number not in client.get("/angebote?status=entwurf").text


def test_angebot_wird_rechnung(client, db):
    _ready(client)
    _enable_recurring(client)
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q, **{"pos-2-wdh": "1", "pos-2-intervall": "monatlich",
                                                 "object_address": "Filiale Mitte\nTorstr. 1\n10119 Berlin"}))
    r = post(client, f"/angebote/{q.id}/rechnung")
    assert "Rechnungsentwurf aus Angebot" in r.text
    q = _quotes(db)[0]
    assert q.status == "angenommen"
    doc = db.scalars(select(Document).options(selectinload(Document.lines))
                     .execution_options(all_tenants=True)).one()
    assert doc.quote_id == q.id and doc.is_draft and doc.object_address.startswith("Filiale Mitte")
    assert f"Angebot Nr. {q.number}" in doc.intro_text
    lines = [(l.title, l.quantity, l.unit, l.unit_price, l.net) for l in doc.lines]
    assert lines == [
        ("Grundreinigung", D("6"), "Std.", D("30"), D("180.00")),
        ("Glasflächen", D("420"), "m²", D("0.125"), D("52.50")),
        ("Teppichreinigung – monatlich", D("1"), "Monat", D("80"), D("80.00")),
        ("Anfahrt: 20 km", D("20"), "km", D("0.30"), D("6.00")),
        ("Entsorgung", D("1"), "Stück", D("25"), D("25.00")),
    ]
    assert doc.net_total == D("343.50")
    assert f"Angebot Nr. {q.number}" in client.get(f"/rechnungen/{doc.id}").text  # Einleitungstext der Rechnung
    page = client.get(f"/angebote/{q.id}").text
    assert "Rechnungen zu diesem Angebot" in page
    assert "Angebot löschen" not in page
    assert q.number in client.get(f"/kunden/{q.customer_id}").text


def test_kleinunternehmer(client, db):
    _ready(client)
    post(client, "/einstellungen/steuer", {**VALID_SETTINGS["steuer"], "steuer.kleinunternehmer": "1"})
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q))
    q = _quotes(db)[0]
    assert all(p.vat_rate == 0 for p in q.positions) and q.gross_total == q.net_total
    assert "§ 19 UStG" in _pdf_text(client.get(f"/angebote/{q.id}/pdf").content)


# -------------------------------------------------------------------- Rechte
def test_rechte(client, outbox, db):
    _ready(client)
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q))
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get(f"/angebote/{q.id}").status_code == 200
    assert post(stb, f"/angebote/{q.id}", _form(q)).status_code == 403
    assert post(stb, f"/angebote/{q.id}/rechnung").status_code == 403

    # Angebote bearbeiten, aber keine Rechnungen erstellen dürfen
    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")
    user = db.scalar(select(User).where(User.email == "max@example.org"))
    m = db.scalar(select(Membership).where(Membership.user_id == user.id))
    perms = {**m.permissions, "rechnungen_ausgang": "lesen"}
    post(client, f"/benutzer/{m.id}", {"role": "mitarbeiter", "is_active": "1", "aktion": "speichern",
                                       **{f"perm_{k}": v for k, v in perms.items()}})
    assert "In Rechnung umwandeln" not in ma.get(f"/angebote/{q.id}").text
    assert post(ma, f"/angebote/{q.id}/rechnung").status_code == 403
    assert _quotes(db)[0].status == "entwurf"


# ---------------------------------------------------------- Mandantentrennung
def test_angebote_anderer_firma_unerreichbar(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q))
    for url in (f"/angebote/{q.id}", f"/angebote/{q.id}/pdf"):
        assert client_b.get(url).status_code == 404, url
    for url, data in ((f"/angebote/{q.id}", _form(q)), (f"/angebote/{q.id}/status", {"status": "angenommen"}),
                      (f"/angebote/{q.id}/rechnung", {}), (f"/angebote/{q.id}/kopieren", {}),
                      (f"/angebote/{q.id}/loeschen", {})):
        assert post(client_b, url, data).status_code == 404, url
    assert "Bitte einen Kunden" in post(client_b, "/angebote/neu", {"customer_id": str(q.customer_id)}).text
    assert q.number not in client_b.get("/angebote").text
    b_quote = _new(client_b, db, _customer(client_b, db, name="Kunde B"))
    assert b_quote.number == f"A-{YEAR}-001" and b_quote.tenant_id == tenant_b.id
    assert _quotes(db)[0].status == "entwurf"
