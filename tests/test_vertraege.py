"""Verträge über wiederkehrende Leistungen: aus Angebot, monatliche Rechnungsentwürfe, nachträglich/im Voraus."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import tenant_scope
from app.models import Document, RecurringContract
from app.services import vertraege
from app.services.vertraege import add_months, format_month, month_start
from app.worker import run_due_jobs
from tests.conftest import post
from tests.test_angebote import _enable_recurring, _form, _new, _quotes, _ready
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _customer
from tests.test_rechte import _invite

THIS_MONTH = month_start(date.today())


def _contracts(db) -> list[RecurringContract]:
    stmt = (select(RecurringContract).options(selectinload(RecurringContract.lines))
            .execution_options(all_tenants=True, populate_existing=True).order_by(RecurringContract.id))
    return list(db.scalars(stmt))


def _drafts(db) -> list[Document]:
    stmt = (select(Document).options(selectinload(Document.lines)).where(Document.contract_id.is_not(None))
            .execution_options(all_tenants=True, populate_existing=True).order_by(Document.service_date))
    return list(db.scalars(stmt))


def _quote_with_recurring(client, db) -> int:
    _ready(client)
    _enable_recurring(client)
    q = _new(client, db)
    # Pauschal 80 € wöchentlich -> 80 × 4,33 = 346,40 €/Monat, Laufzeit 3 Monate
    post(client, f"/angebote/{q.id}", _form(q, **{"pos-2-wdh": "1", "pos-2-intervall": "woechentlich",
                                                 "pos-2-laufzeit": "3"}))
    return q.id


def _manual(client, db, **form) -> int:
    customer = _customer(client, db)
    r = post(client, "/vertraege/neu", {"customer_id": str(customer.id)})
    assert "Vertrag angelegt" in r.text
    contract_id = _contracts(db)[-1].id
    data = {"title": "Hausmeisterdienst", "billing_mode": "voraus", "status": "aktiv",
            "start_month": format_month(add_months(THIS_MONTH, -3)), "end_month": "",
            "pos-0-titel": "Hausmeisterdienst pauschal", "pos-0-menge": "1", "pos-0-einheit": "Monat",
            "pos-0-preis": "250", "pos-0-satz": "19", "pos-1-titel": "", "pos-1-preis": ""}
    data.update(form)
    r = post(client, f"/vertraege/{contract_id}", data)
    assert r.status_code == 200, r.text
    return contract_id


def test_vertrag_aus_angebot_nachtraeglich(client, db):
    quote_id = _quote_with_recurring(client, db)
    assert "Vertrag für monatliche Abrechnung anlegen" in client.get(f"/angebote/{quote_id}").text
    r = post(client, f"/vertraege/aus-angebot/{quote_id}")
    assert "Vertrag angelegt" in r.text and "Teppichreinigung" in r.text
    contract = _contracts(db)[0]
    start = add_months(THIS_MONTH, 1)
    assert contract.start_month == start and contract.end_month == add_months(start, 2)
    assert contract.billing_mode == "nachtraeglich" and contract.quote_id == quote_id
    assert [(line.title, line.unit, line.unit_price) for line in contract.lines] == [
        ("Teppichreinigung", "Monat", D("346.4000"))]
    tenant_id = contract.tenant_id
    assert _quotes(db)[0].status == "angenommen"

    with tenant_scope(db, tenant_id):
        assert vertraege.run_due(db, today=start) == []                       # Monat läuft noch
        first = vertraege.run_due(db, today=add_months(start, 1))             # 1. des Folgemonats
        assert [d.service_date for d in first] == [start]
        rest = vertraege.run_due(db, today=add_months(start, 6))              # Rest bis Vertragsende
        assert [d.service_date for d in rest] == [add_months(start, 1), add_months(start, 2)]
        db.commit()
    contract = _contracts(db)[0]
    assert contract.status == "beendet"
    drafts = _drafts(db)
    assert len(drafts) == 3 and all(d.is_draft and d.source == "vertrag" for d in drafts)
    assert drafts[0].gross_total == D("412.22") and drafts[0].service_until.month == start.month
    assert "gemäß Vertrag" in drafts[0].intro_text and drafts[0].recipient_name == "Bäckerei Krume GmbH"
    assert "3</strong> neue(r) Rechnungsentwurf" in client.get("/").text
    assert "aus Vertrag" in client.get("/rechnungen?status=entwurf").text


def test_im_voraus_nachholen_und_pausieren(client, db):
    _ready(client)
    _enable_recurring(client)
    contract_id = _manual(client, db)
    r = post(client, "/vertraege/abrechnen")
    assert "4 Rechnungsentwürfe angelegt" in r.text   # 3 verpasste Monate + laufender Monat (im Voraus)
    assert [d.service_date for d in _drafts(db)] == [add_months(THIS_MONTH, n) for n in (-3, -2, -1, 0)]
    assert "Zurzeit ist kein Monat abzurechnen" in post(client, "/vertraege/abrechnen").text

    # Beginn ist nach der ersten Rechnung fest; Pausieren stoppt die Entwürfe
    _manual_save = {"title": "Hausmeisterdienst", "billing_mode": "voraus", "status": "pausiert",
                    "start_month": "01.2020", "end_month": "", "pos-0-titel": "Hausmeisterdienst pauschal",
                    "pos-0-menge": "1", "pos-0-preis": "260", "pos-0-satz": "19"}
    post(client, f"/vertraege/{contract_id}", _manual_save)
    contract = _contracts(db)[0]
    assert contract.start_month == add_months(THIS_MONTH, -3) and contract.status == "pausiert"
    assert contract.lines[0].unit_price == D("260")
    with tenant_scope(db, contract.tenant_id):
        assert vertraege.run_due(db, today=add_months(THIS_MONTH, 3)) == []
    db.rollback()


def test_eingaben_werden_geprueft(client, db):
    _ready(client)
    _enable_recurring(client)
    contract_id = _manual(client, db)
    base = {"title": "X", "billing_mode": "voraus", "status": "aktiv", "start_month": "13.2026", "end_month": "",
            "pos-0-titel": "", "pos-0-preis": ""}
    r = post(client, f"/vertraege/{contract_id}", base)
    assert r.status_code == 400 and "MM.JJJJ" in r.text and "mindestens eine Position" in r.text
    r = post(client, f"/vertraege/{contract_id}", {**base, "start_month": "05.2027", "end_month": "04.2027",
                                                   "pos-0-titel": "A", "pos-0-preis": "10"})
    assert r.status_code == 400 and "Das Ende liegt vor dem Beginn" in r.text


def test_hintergrund_prozess_legt_entwuerfe_an(client, db):
    _ready(client)
    _enable_recurring(client)
    _manual(client, db)
    assert run_due_jobs() >= 1
    assert len(_drafts(db)) == 4
    run_due_jobs()
    assert len(_drafts(db)) == 4   # kein zweites Mal


def test_modul_ausgeschaltet(client, db):
    _ready(client)
    assert client.get("/vertraege").status_code == 404
    assert "Verträge" not in client.get("/rechnungen").text


def test_rechte_und_mandantentrennung(client, outbox, db):
    client_b, _, tenant_b = _two_tenants(client, outbox, db)
    _enable_recurring(client)
    _enable_recurring(client_b)
    contract_id = _manual(client, db)
    assert client_b.get(f"/vertraege/{contract_id}").status_code == 404
    assert "Hausmeisterdienst" not in client_b.get("/vertraege").text
    with tenant_scope(db, tenant_b.id):
        assert vertraege.run_due(db) == [] and db.scalars(select(RecurringContract)).all() == []
    db.rollback()
    assert post(client_b, "/vertraege/abrechnen").status_code == 200
    assert _drafts(db) == []   # Firma B darf die Verträge von A nicht abrechnen

    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get(f"/vertraege/{contract_id}").status_code == 200
    assert post(stb, "/vertraege/abrechnen").status_code == 403
    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")  # darf Rechnungen bearbeiten
    assert "4 Rechnungsentwürfe angelegt" in post(ma, "/vertraege/abrechnen").text
