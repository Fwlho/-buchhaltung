from decimal import Decimal

from sqlalchemy import select

from app.models import AuditLog, CatalogItem, Customer, CustomerAddress, Membership, Supplier, User
from app.permissions import READ
from tests.conftest import complete_wizard, ersteinrichtung, post
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechte import _invite

KUNDE = {"kind": "firma", "name": "Bäckerei Krume GmbH", "contact_person": "Frau Krume", "street": "Hauptstr. 5",
         "zip": "10115", "city": "Berlin", "country": "Deutschland", "email": "info@krume.example"}
LIEFERANT = {"name": "Reinigungsbedarf Nord", "street": "Hafenweg 2", "zip": "20457", "city": "Hamburg",
             "country": "Deutschland", "iban": "DE89 3704 0044 0532 0130 00"}
ARTIKEL = {"kind": "leistung", "title": "Unterhaltsreinigung Büro", "unit": "m²", "billing_type": "menge",
           "price_net": "0,125", "vat_rate": "19", "cost_rate": "0,09"}


def _ready(client):
    ersteinrichtung(client)
    complete_wizard(client)


def _create_customer(client, db, **overrides) -> Customer:
    r = post(client, "/kunden/neu", {**KUNDE, **overrides})
    assert r.status_code == 200, r.text
    return db.scalars(select(Customer).execution_options(all_tenants=True).order_by(Customer.id.desc())).first()


# --------------------------------------------------------------------- Kunden
def test_kunde_anlegen_mit_automatischer_nummer(client, db):
    _ready(client)
    c1 = _create_customer(client, db)
    c2 = _create_customer(client, db, name="Zweiter Kunde")
    assert (c1.number, c2.number) == ("10000", "10001")
    r = client.get(f"/kunden/{c1.id}")
    assert "Bäckerei Krume GmbH" in r.text and "Kunde 10000" in r.text
    assert "14 Tage" in r.text  # Standard-Zahlungsziel aus den Einstellungen


def test_kundennummer_eindeutig_und_frei_waehlbar(client, db):
    _ready(client)
    c = _create_customer(client, db, number="12345")
    assert c.number == "12345"
    assert _create_customer(client, db, name="Danach").number == "12346"
    r = post(client, "/kunden/neu", {**KUNDE, "number": "12345"})
    assert r.status_code == 400
    assert "bereits vergeben" in r.text
    r = post(client, "/kunden/neu", {**KUNDE, "number": "K-1"})
    assert "nur Ziffern" in r.text


def test_kunde_pruefungen(client):
    _ready(client)
    r = post(client, "/kunden/neu", {**KUNDE, "name": "", "zip": "1234", "email": "kaputt",
                                     "ust_id": "DE12", "leitweg_id": "abc"})
    assert r.status_code == 400
    for text in ("Bitte ausfüllen", "5 Ziffern", "Keine gültige E-Mail", "USt-IdNr", "Leitweg-ID"):
        assert text in r.text
    # Halbe Rechnungsadresse
    r = post(client, "/kunden/neu", {**KUNDE, "billing_name": "Buchhaltung"})
    assert r.status_code == 400
    assert "abweichende Rechnungsadresse" in r.text
    # Eingaben bleiben nach Fehler erhalten
    assert "Hauptstr. 5" in r.text


def test_kunde_bearbeiten_protokoll_und_archiv(client, db):
    _ready(client)
    c = _create_customer(client, db)
    data = {**KUNDE, "number": "", "city": "Potsdam", "zip": "14467", "payment_terms_days": "30",
            "billing_street": "Postfach 1", "billing_zip": "10001", "billing_city": "Berlin"}
    r = post(client, f"/kunden/{c.id}/bearbeiten", data)
    assert "Änderungen gespeichert" in r.text
    assert "Rechnungen an:" in r.text and "30 Tage" in r.text
    db.refresh(c)
    assert c.number == "10000"  # leeres Nummernfeld ändert die Nummer nicht
    entry = db.scalars(select(AuditLog).where(AuditLog.entity == "kunde", AuditLog.action == "geaendert")).first()
    assert entry.changes["city"] == {"alt": "Berlin", "neu": "Potsdam"}

    # Suche
    assert "Bäckerei Krume" in client.get("/kunden?q=potsd").text
    assert "Bäckerei Krume" not in client.get("/kunden?q=xyz").text

    # Archivieren: verschwindet aus der Liste, bleibt im Archiv
    post(client, f"/kunden/{c.id}/archivieren", {"aktiv": "0"})
    assert "Bäckerei Krume" not in client.get("/kunden").text
    assert "Bäckerei Krume" in client.get("/kunden?archiv=1").text
    post(client, f"/kunden/{c.id}/archivieren", {"aktiv": "1"})
    assert "Bäckerei Krume" in client.get("/kunden").text


def test_objektadressen(client, db):
    _ready(client)
    c = _create_customer(client, db)
    r = post(client, f"/kunden/{c.id}/adressen", {"label": "Filiale Mitte", "street": "Torstr. 1",
                                                   "zip": "10119", "city": "Berlin"})
    assert "Filiale Mitte" in r.text
    a = db.scalar(select(CustomerAddress).execution_options(all_tenants=True))
    assert a.customer_id == c.id and a.tenant_id == c.tenant_id

    r = post(client, f"/kunden/{c.id}/adressen", {"label": "", "street": "", "zip": "", "city": ""})
    assert r.status_code == 400

    post(client, f"/kunden/{c.id}/adressen/{a.id}", {"label": "Filiale Nord", "street": "Torstr. 1",
                                                      "zip": "10119", "city": "Berlin"})
    assert "Filiale Nord" in client.get(f"/kunden/{c.id}").text
    post(client, f"/kunden/{c.id}/adressen/{a.id}/archivieren", {"aktiv": "0"})
    assert "1 archivierte Adresse" in client.get(f"/kunden/{c.id}").text

    # Adresse über einen anderen Kunden ansprechen geht nicht
    c2 = _create_customer(client, db, name="Anderer")
    assert client.get(f"/kunden/{c2.id}/adressen/{a.id}").status_code == 404


# ----------------------------------------------------------------- Lieferanten
def test_lieferanten(client, db):
    _ready(client)
    r = post(client, "/lieferanten/neu", LIEFERANT)
    assert "Lieferant 70000" in r.text
    s = db.scalar(select(Supplier).execution_options(all_tenants=True))
    assert s.iban == "DE89370400440532013000"
    r = post(client, "/lieferanten/neu", {**LIEFERANT, "iban": "DE00 0000"})
    assert "IBAN ist ungültig" in r.text
    post(client, f"/lieferanten/{s.id}", {**LIEFERANT, "our_customer_number": "K-778"})
    assert "K-778" in client.get("/lieferanten").text


# --------------------------------------------------------------------- Katalog
def test_katalog_preise_mit_vier_nachkommastellen(client, db):
    _ready(client)
    r = post(client, "/katalog/neu", ARTIKEL)
    assert "angelegt" in r.text
    item = db.scalar(select(CatalogItem).execution_options(all_tenants=True))
    assert item.price_net == Decimal("0.1250")
    assert item.cost_rate == Decimal("0.0900")
    assert item.vat_rate == Decimal("19")
    page = client.get("/katalog").text
    assert "0,125 €" in page
    assert "28,0 %" in page  # Marge (0,125 - 0,09) / 0,125

    r = post(client, "/katalog/neu", {**ARTIKEL, "price_net": "abc"})
    assert r.status_code == 400
    r = post(client, "/katalog/neu", {**ARTIKEL, "price_net": "-1"})
    assert "Mindestens" in r.text


def test_katalog_kosten_nur_mit_sonderrecht(client, outbox, db):
    _ready(client)
    post(client, "/katalog/neu", ARTIKEL)
    item = db.scalar(select(CatalogItem).execution_options(all_tenants=True))

    # Mitarbeiter: Katalog nur lesen, keine Kosten sichtbar
    c = _invite(client, outbox, "max@example.org", "mitarbeiter")
    page = c.get("/katalog").text
    assert "Unterhaltsreinigung" in page and "Marge" not in page and "0,09" not in page
    detail = c.get(f"/katalog/{item.id}").text
    assert "Kostensatz" not in detail
    assert post(c, f"/katalog/{item.id}", {**ARTIKEL, "title": "Gehackt"}).status_code == 403

    # Mitarbeiter darf Katalog bearbeiten, hat aber kein Kalkulations-Recht:
    # Speichern lässt den Kostensatz unverändert
    user = db.scalar(select(User).where(User.email == "max@example.org"))
    m = db.scalar(select(Membership).where(Membership.user_id == user.id))
    perms = dict(m.permissions)
    perms["katalog"] = "bearbeiten"
    post(client, f"/benutzer/{m.id}", {"role": "mitarbeiter", "is_active": "1", "aktion": "speichern",
                                       **{f"perm_{k}": v for k, v in perms.items()}})
    r = post(c, f"/katalog/{item.id}", {**ARTIKEL, "title": "Neu", "cost_rate": "5"})
    assert "gespeichert" in r.text
    db.refresh(item)
    assert item.title == "Neu" and item.cost_rate == Decimal("0.0900")


def test_steuerberater_liest_stammdaten_nur(client, outbox, db):
    _ready(client)
    c1 = _create_customer(client, db)
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get("/kunden").status_code == 200
    assert "Neuer Kunde" not in stb.get("/kunden").text
    assert stb.get(f"/kunden/{c1.id}").status_code == 200
    assert stb.get(f"/kunden/{c1.id}/bearbeiten").status_code == 403
    assert post(stb, "/kunden/neu", KUNDE).status_code == 403
    assert post(stb, f"/kunden/{c1.id}/archivieren", {"aktiv": "0"}).status_code == 403
    m = db.scalar(select(Membership).join(User).where(User.email == "stb@example.org"))
    assert m.permissions["kunden"] == READ


# --------------------------------------------------------- Mandantentrennung
def test_stammdaten_anderer_firma_unerreichbar(client, outbox, db):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    c = _create_customer(client, db)
    post(client, f"/kunden/{c.id}/adressen", {"label": "Filiale", "street": "Weg 1", "zip": "10115", "city": "Berlin"})
    post(client, "/lieferanten/neu", LIEFERANT)
    post(client, "/katalog/neu", ARTIKEL)
    a = db.scalar(select(CustomerAddress).execution_options(all_tenants=True))
    s = db.scalar(select(Supplier).execution_options(all_tenants=True))
    i = db.scalar(select(CatalogItem).execution_options(all_tenants=True))
    assert c.tenant_id == a.tenant_id == s.tenant_id == i.tenant_id == tenant_a.id

    # Firma B sieht nichts davon …
    assert "Bäckerei" not in client_b.get("/kunden").text
    assert "Reinigungsbedarf" not in client_b.get("/lieferanten").text
    assert "Unterhaltsreinigung" not in client_b.get("/katalog").text
    for url in (f"/kunden/{c.id}", f"/kunden/{c.id}/bearbeiten", f"/kunden/{c.id}/adressen/{a.id}",
                f"/lieferanten/{s.id}", f"/katalog/{i.id}"):
        assert client_b.get(url).status_code == 404, url
    # … und kann nichts ändern
    for url, data in ((f"/kunden/{c.id}/bearbeiten", KUNDE), (f"/kunden/{c.id}/archivieren", {"aktiv": "0"}),
                      (f"/kunden/{c.id}/adressen", {"label": "X", "street": "X", "zip": "10115", "city": "X"}),
                      (f"/lieferanten/{s.id}", LIEFERANT), (f"/katalog/{i.id}", ARTIKEL)):
        assert post(client_b, url, data).status_code == 404, url
    db.expire_all()
    with_tenant = db.scalars(select(Customer).execution_options(all_tenants=True)).all()
    assert [x.name for x in with_tenant] == ["Bäckerei Krume GmbH"]
    assert db.scalars(select(CustomerAddress).execution_options(all_tenants=True)).one().label == "Filiale"

    # Gleiche Kundennummer in beiden Firmen erlaubt (Nummernkreis je Firma)
    r = post(client_b, "/kunden/neu", {**KUNDE, "name": "Kunde von B"})
    assert "Kunde 10000" in r.text
