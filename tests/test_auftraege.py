"""Aufträge / Projektübersicht: Auftrag aus Angebot, Karten je Objekt, Reiter/Suche/Sortierung, Aufgaben,
Auftrag -> Rechnung (Vorschlag „erledigt“ bei bezahlter Rechnung), Dauerauftrag -> Vertrag, Rechte, Trennung."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import CustomerAddress, Document, Membership, Order, RecurringContract, User
from app.permissions import NONE, READ
from tests.conftest import post
from tests.test_angebote import _enable_recurring, _form, _new, _quotes, _ready
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _customer
from tests.test_rechte import _invite

YEAR = date.today().year


def _orders(db) -> list[Order]:
    stmt = (select(Order).options(selectinload(Order.lines), selectinload(Order.tasks))
            .execution_options(all_tenants=True, populate_existing=True).order_by(Order.id))
    return list(db.scalars(stmt))


def _address(client, db, customer, label="Bürohaus Nord"):
    r = post(client, f"/kunden/{customer.id}/adressen",
             {"label": label, "street": "Lindenstraße 12", "zip": "12345", "city": "Musterstadt",
              "country": "Deutschland"})
    assert "hinzugefügt" in r.text, r.text
    return db.scalars(select(CustomerAddress).where(CustomerAddress.label == label)
                      .execution_options(all_tenants=True, populate_existing=True)).one()


def _manual(client, db, customer=None, **form) -> Order:
    customer = customer or _customer(client, db)
    r = post(client, "/auftraege/neu", {"customer_id": str(customer.id)})
    assert f"AU-{YEAR}-" in r.text and "angelegt" in r.text, r.text
    order = _orders(db)[-1]
    data = {"title": "Glasreinigung", "kind": "einzel", "status": "offen", "address_id": "", "object_address": "",
            "description": "", "notes": "Schlüssel beim Hausmeister",
            "pos-0-titel": "Glasreinigung innen und außen", "pos-0-menge": "2", "pos-0-einheit": "Std.",
            "pos-0-preis": "40", "pos-0-satz": "19", "pos-1-titel": "", "pos-1-preis": "",
            "aufgabe-0-text": "Termin abstimmen", "aufgabe-1-text": ""}
    data.update(form)
    r = post(client, f"/auftraege/{order.id}", data)
    assert "Auftrag gespeichert" in r.text, r.text
    return next(o for o in _orders(db) if o.id == order.id)


# ------------------------------------------------------------- aus Angebot
def test_auftrag_aus_angebot_uebernimmt_daten(client, db):
    _ready(client)
    customer = _customer(client, db)
    address_id = _address(client, db, customer).id
    quote = _new(client, db, customer)
    quote_id = quote.id
    post(client, f"/angebote/{quote_id}", _form(quote, object_address_id=str(address_id)))
    r = post(client, f"/auftraege/aus-angebot/{quote_id}")
    assert "aus Angebot" in r.text, r.text
    order = _orders(db)[-1]
    assert order.number == f"AU-{YEAR}-001" and order.quote_id == quote_id and order.address_id == address_id
    assert order.kind == "einzel" and order.status == "offen"
    # Positionen wie Angebot -> Rechnung: 180 + 52,50 + 80 + Anfahrt 6 + Entsorgung 25
    assert order.net_total == D("343.50") and len(order.lines) == 5
    assert _quotes(db)[-1].status == "angenommen"
    order = _orders(db)[-1]
    # Karte je Objekt, aufgeklappt
    page = client.get("/auftraege").text
    assert "Bürohaus Nord" in page and order.number in page and f'id="objekt-a{address_id}"' in page
    assert "343,50" in page
    # zweiter Klick legt keinen zweiten Auftrag an; Angebot verlinkt den Auftrag
    r = post(client, f"/auftraege/aus-angebot/{quote_id}")
    assert "schon den Auftrag" in r.text and len(_orders(db)) == 1
    assert f"Zum Auftrag {order.number}" in client.get(f"/angebote/{quote_id}").text


def test_angebot_mit_wiederkehrender_leistung_wird_dauerauftrag_und_vertrag(client, db):
    _ready(client)
    _enable_recurring(client)
    quote = _new(client, db)
    quote_id = quote.id
    post(client, f"/angebote/{quote_id}", _form(quote, **{"pos-2-wdh": "1", "pos-2-intervall": "woechentlich"}))
    post(client, f"/auftraege/aus-angebot/{quote_id}")
    order = _orders(db)[-1]
    assert order.kind == "dauer"
    r = post(client, f"/auftraege/{order.id}/vertrag")
    assert "Vertrag aus Auftrag" in r.text, r.text
    order = _orders(db)[-1]
    contract = db.scalars(select(RecurringContract).where(RecurringContract.id == order.contract_id)
                          .options(selectinload(RecurringContract.lines)).execution_options(all_tenants=True)).one()
    assert contract.quote_id == quote_id and len(contract.lines) == len(order.lines)
    r = post(client, f"/auftraege/{order.id}/vertrag")
    assert "schon einen Vertrag" in r.text
    assert "Vertrag" in client.get("/auftraege").text


# --------------------------------------------------------- Übersicht/Karten
def test_reiter_suche_sortierung_und_objekte(client, db):
    _ready(client)
    anna = _customer(client, db, name="Anna Bäckerei")
    nord_id = _address(client, db, anna, "Filiale Nord").id
    first = _manual(client, db, anna, title="Fenster Filiale", address_id=str(nord_id))
    second = _manual(client, db, anna, title="Grundreinigung", status="arbeit", **{"pos-0-preis": "500"})
    zoe = _customer(client, db, name="Zoe Praxis")
    third = _manual(client, db, zoe, title="Praxisreinigung", kind="dauer", status="erledigt",
                    object_address="Praxis am Markt\nMarkt 1\n12345 Musterstadt")

    page = client.get("/auftraege").text
    assert page.count('class="card obj"') == 3                              # drei Objekte
    assert "Filiale Nord" in page and "Praxis am Markt" in page
    # Reiter „In Arbeit“: nur die Karte mit dem Auftrag in Arbeit
    page = client.get("/auftraege?status=arbeit").text
    assert second.number in page and first.number not in page and third.number not in page
    assert 'aria-current="page">In Arbeit' in page
    # Art und Suche
    page = client.get("/auftraege?art=dauer").text
    assert third.number in page and first.number not in page
    page = client.get("/auftraege?q=filiale").text
    assert first.number in page and second.number not in page
    assert "Keine Aufträge" in client.get("/auftraege?q=gibtsnicht").text
    # Sortierung nach Preis: 1.000 € (Anna Hauptadresse) vor den anderen
    page = client.get("/auftraege?sort=preis").text
    assert page.index(second.number) < page.index(first.number)
    page = client.get("/auftraege?sort=az").text
    assert page.index("Filiale Nord") < page.index("Praxis am Markt")


def test_stand_und_aufgaben_aus_der_karte(client, db):
    _ready(client)
    order = _manual(client, db)
    task = order.tasks[0]
    r = post(client, f"/auftraege/aufgabe/{task.id}", {"zurueck": "/auftraege?status=offen"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auftraege?status=offen&offen=")
    assert _orders(db)[-1].tasks[0].done
    r = post(client, f"/auftraege/{order.id}/stand", {"status": "arbeit", "zurueck": "https://boese.example"},
             follow_redirects=False)
    assert r.headers["location"].startswith("/auftraege?offen=")             # nur Rücksprung innerhalb /auftraege
    assert _orders(db)[-1].status == "arbeit"
    assert "Unbekannter Stand" in post(client, f"/auftraege/{order.id}/stand", {"status": "x"}).text


def test_pflichtfelder_und_fehler(client, db):
    _ready(client)
    order = _manual(client, db)
    r = post(client, f"/auftraege/{order.id}", {"title": "", "kind": "einzel", "status": "offen",
                                                "pos-0-titel": "X", "pos-0-preis": "abc"})
    assert r.status_code == 400 and "Bitte eine Bezeichnung angeben" in r.text
    assert _orders(db)[-1].title == "Glasreinigung"


# ------------------------------------------------- Rechnung / erledigt-Vorschlag
def test_rechnung_aus_auftrag_und_vorschlag_erledigt(client, db):
    _ready(client)
    order = _manual(client, db, object_address="Lager Süd\nIndustriestraße 5\n54321 Beispielort")
    order_id, number = order.id, order.number
    r = post(client, f"/auftraege/{order_id}/rechnung")
    assert "Rechnungsentwurf aus Auftrag" in r.text, r.text
    doc =db.scalars(select(Document).where(Document.order_id == order_id).options(selectinload(Document.lines))
                     .execution_options(all_tenants=True, populate_existing=True)).one()
    assert doc.net_total == D("80.00") and "Lager Süd" in doc.object_address and number in doc.intro_text
    page = client.get("/auftraege").text
    assert "Entwurf" in page and "Alle Rechnungen sind bezahlt" not in page
    # festschreiben und als bezahlt abhaken -> Vorschlag „erledigt“
    from tests.test_rechnungen import _form as invoice_form
    post(client, f"/rechnungen/{doc.id}", invoice_form(doc, service_date=date.today().strftime("%d.%m.%Y")))
    assert "festgeschrieben" in post(client, f"/rechnungen/{doc.id}/festschreiben").text
    post(client, f"/rechnungen/{doc.id}/bezahlt", {"paid_on": date.today().strftime("%d.%m.%Y")})
    page = client.get("/auftraege").text
    assert "Alle Rechnungen sind bezahlt" in page and "bezahlt" in page
    assert _orders(db)[-1].status == "offen"                                 # nur Vorschlag, nicht automatisch
    # mit Rechnung nicht mehr löschbar
    assert "schon eine Rechnung" in post(client, f"/auftraege/{order_id}/loeschen").text


def test_loeschen_ohne_belege(client, db):
    _ready(client)
    order = _manual(client, db)
    r = post(client, f"/auftraege/{order.id}/loeschen")
    assert "gelöscht" in r.text and not _orders(db)


# --------------------------------------------------------------- Rechte
def _set_perms(client, db, email: str, **levels):
    user = db.scalar(select(User).where(User.email == email))
    m = db.scalar(select(Membership).where(Membership.user_id == user.id))
    data = {"role": "mitarbeiter", "is_active": "1", "aktion": "speichern"}
    data.update({f"perm_{k}": v for k, v in m.permissions.items()})
    data.update({f"perm_{k}": v for k, v in levels.items()})
    assert "Änderungen für" in post(client, f"/benutzer/{m.id}", data).text


def test_rechte_und_preise(client, outbox, db):
    _ready(client)
    order = _manual(client, db)
    c = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert c.get("/auftraege").status_code == 200                            # Mitarbeiter: Aufträge bearbeiten
    _set_perms(client, db, "max@example.org", auftraege=READ, angebote=NONE, rechnungen_ausgang=NONE)
    page = c.get("/auftraege").text
    assert order.number in page and "80,00" not in page                     # ohne Angebote/Rechnungen: keine Preise
    assert "Stand ändern" not in page
    assert post(c, f"/auftraege/{order.id}/stand", {"status": "erledigt"}).status_code == 403
    assert post(c, f"/auftraege/{order.id}/rechnung").status_code == 403
    _set_perms(client, db, "max@example.org", auftraege=NONE)
    assert c.get("/auftraege").status_code == 403
    assert "/auftraege" not in c.get("/").text


def test_firmen_sehen_nur_eigene_auftraege(client, outbox, db):
    client_b, _tenant_a, _tenant_b = _two_tenants(client, outbox, db)
    order = _manual(client, db)
    assert client_b.get(f"/auftraege/{order.id}").status_code == 404
    assert order.number not in client_b.get("/auftraege").text
    assert post(client_b, f"/auftraege/aufgabe/{order.tasks[0].id}").status_code == 404


# ------------------------------------------------- automatisch einlesen
def _customers(db):
    from app.models import Customer

    return list(db.scalars(select(Customer).execution_options(all_tenants=True, populate_existing=True)
                           .order_by(Customer.id)))


def _out_docs(db):
    return list(db.scalars(select(Document).where(Document.direction == "ausgang")
                           .execution_options(all_tenants=True, populate_existing=True).order_by(Document.id)))


def test_alte_rechnungen_werden_automatisch_auftraege(client, db):
    from tests.test_eingangsrechnungen import make_pdf
    from tests.test_rechnungs_import import ALTE_RECHNUNG, _upload
    from tests.test_rechnungs_import import _form as import_form
    from tests.test_rechnungs_import import _pending

    _ready(client)
    for number, day in (("2026-016", "15.02.2026"), ("2026-017", "15.03.2026")):
        text = ALTE_RECHNUNG.replace("2026-016", number).replace("15.02.2026", day)
        _upload(client, (f"{number}.pdf", make_pdf(text)))
        doc = _pending(db)[0]
        r = post(client, f"/rechnungen/import/{doc.id}",
                 import_form(doc, aktion="uebernehmen", paid_on=day if number.endswith("6") else ""))
        assert "übernommen" in r.text, r.text[:300]
    assert not _customers(db) and all(d.order_id is None for d in _out_docs(db))

    page = client.get("/auftraege").text
    assert "Automatisch übernommen: 2 Aufträge" in page and "1 Kunde aus Name und Adresse" in page
    customer = _customers(db)[0]
    assert (customer.name, customer.street, customer.zip, customer.city) == ("Bäckerei Krume GmbH", "Hauptstr. 5",
                                                                           "10115", "Berlin")
    assert "Automatisch angelegt" in customer.notes
    orders = _orders(db)
    assert [o.title for o in orders] == ["Rechnung 2026-016", "Rechnung 2026-017"]
    assert [o.status for o in orders] == ["erledigt", "arbeit"]              # bezahlt -> erledigt
    assert [o.created_at.date() for o in orders] == [date(2026, 2, 15), date(2026, 3, 15)]
    assert all(o.customer_id == customer.id and o.source == "rechnung" and o.net_total == D("1000.00")
               for o in orders)
    docs = _out_docs(db)
    assert [d.order_id for d in docs] == [o.id for o in orders]
    assert all(d.customer_id is None for d in docs)                          # Rechnung selbst bleibt unverändert
    assert page.count('class="card obj"') == 1 and "Hauptstr. 5" in page    # ein Objekt (Hauptadresse)
    # zweites Öffnen: nichts Neues
    page = client.get("/auftraege").text
    assert "Automatisch übernommen:" not in page and len(_orders(db)) == 2 and len(_customers(db)) == 1


def test_neue_rechnung_wird_auftrag_und_folgt_dem_entwurf(client, db):
    from tests.test_rechnungen import _draft
    from tests.test_rechnungen import _form as invoice_form

    _ready(client)
    draft = _draft(client, db)
    draft_id = draft.id
    client.get("/auftraege")
    assert not _orders(db)                                                   # leerer Entwurf: noch kein Auftrag
    post(client, f"/rechnungen/{draft_id}", invoice_form(draft))
    client.get("/auftraege")
    order = _orders(db)[0]
    assert order.title == "Unterhaltsreinigung" and order.status == "arbeit" and len(order.lines) == 3
    # Entwurf ändern -> Auftrag folgt
    doc = next(d for d in _out_docs(db) if d.id == draft_id)
    post(client, f"/rechnungen/{draft_id}", invoice_form(doc, **{"pos-2-titel": "", "pos-2-preis": "",
                                                                 "pos-2-menge": ""}))
    client.get("/auftraege")
    assert len(_orders(db)[0].lines) == 2
    # Entwurf löschen -> automatischer Auftrag verschwindet
    post(client, f"/rechnungen/{draft_id}/loeschen")
    client.get("/auftraege")
    assert not _orders(db)


def test_angebot_und_vertrag_werden_automatisch_auftraege(client, db):
    from app.worker import run_due_jobs
    from tests.test_vertraege import _manual as manual_contract

    _ready(client)
    _enable_recurring(client)
    quote = _new(client, db)
    quote_id = quote.id
    post(client, f"/angebote/{quote_id}", _form(quote))
    post(client, f"/angebote/{quote_id}/status", {"status": "angenommen"})
    manual_contract(client, db)
    run_due_jobs()                                                            # auch im Hintergrund
    orders = _orders(db)
    by_source = {o.source: o for o in orders}
    assert set(by_source) == {"angebot", "vertrag"}
    assert by_source["angebot"].quote_id == quote_id and by_source["angebot"].status == "offen"
    assert by_source["vertrag"].kind == "dauer" and by_source["vertrag"].status == "arbeit"
    # Rechnung aus dem Angebot landet im vorhandenen Auftrag; Vertragsrechnungen hängen am Dauerauftrag
    post(client, f"/angebote/{quote_id}/rechnung")
    page = client.get("/auftraege").text
    assert len(_orders(db)) == 2
    assert any(d.order_id == by_source["angebot"].id for d in _out_docs(db))
    assert "Hausmeisterdienst" in page
