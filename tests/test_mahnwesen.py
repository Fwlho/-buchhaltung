"""Mahnwesen: überfällige Rechnungen, drei Stufen mit Gebühren/Zinsen, Mahnungs-PDF, E-Mail-Entwurf."""
import email
from datetime import date, timedelta
from decimal import Decimal as D

from sqlalchemy import select

from app.database import tenant_scope
from app.models import Dunning
from app.services import einstellungen, mahnwesen
from tests.conftest import post
from tests.test_auswertung import _ready
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _final, _pdf_text
from tests.test_rechte import _invite

TODAY = date.today()
OLD = (TODAY - timedelta(days=40)).strftime("%d.%m.%Y")   # fällig vor 26 Tagen (Zahlungsziel 14 Tage)


def _overdue(client, db):
    doc = _final(client, db, doc_date=OLD, service_date=OLD)
    return doc.id, doc.number, doc.tenant_id


def _dunnings(db) -> list[Dunning]:
    stmt = select(Dunning).execution_options(all_tenants=True, populate_existing=True).order_by(Dunning.id)
    return list(db.scalars(stmt))


def test_drei_stufen_mit_gebuehren(client, db):
    _ready(client)
    doc_id, number, _ = _overdue(client, db)
    page = client.get("/mahnwesen").text
    assert number in page and "Zahlungserinnerung erstellen" in page and "26 Tage" in page

    r = post(client, f"/mahnwesen/rechnung/{doc_id}")
    assert "Zahlungserinnerung M-" in r.text and "erstellt" in r.text
    first = _dunnings(db)[0]
    assert first.level == 1 and first.number == f"M-{TODAY.year}-001" and first.fees == 0
    assert first.total == D("182.29") and first.pay_until == TODAY + timedelta(days=7)
    text = _pdf_text(client.get(f"/mahnwesen/{first.id}/pdf").content)
    for part in (f"Zahlungserinnerung zur Rechnung Nr. {number}", "Sehr geehrte Damen und Herren",
                 "sicher haben Sie unsere Rechnung", "182,29 €", "Muster Service", "Bäckerei Krume GmbH"):
        assert part in text, part

    post(client, f"/mahnwesen/rechnung/{doc_id}")
    post(client, f"/mahnwesen/rechnung/{doc_id}")
    levels = _dunnings(db)
    assert [(d.level, d.level_name, d.fees, d.total) for d in levels] == [
        (1, "Zahlungserinnerung", D("0.00"), D("182.29")), (2, "1. Mahnung", D("5.00"), D("187.29")),
        (3, "2. Mahnung", D("10.00"), D("192.29"))]
    assert "Mahngebühren" in _pdf_text(client.get(f"/mahnwesen/{levels[2].id}/pdf").content)
    r = post(client, f"/mahnwesen/rechnung/{doc_id}")
    assert "letzte Mahnstufe ist bereits erreicht" in r.text
    assert len(_dunnings(db)) == 3
    assert "2. Mahnung" in client.get(f"/rechnungen/{doc_id}").text  # an der Rechnung sichtbar


def test_verzugszinsen(client, db):
    _ready(client)
    doc_id, _, tenant_id = _overdue(client, db)
    with tenant_scope(db, tenant_id):
        einstellungen.set_values(db, {"mahnung.zinssatz": "9.00"})
        db.commit()
    post(client, f"/mahnwesen/rechnung/{doc_id}")
    d = _dunnings(db)[0]
    days = (TODAY - (TODAY - timedelta(days=26))).days
    assert d.interest == (D("182.29") * D(9) / 100 * days / 365).quantize(D("0.01"))
    assert d.total == D("182.29") + d.interest
    assert "Verzugszinsen 9,00 % p. a. für 26 Tage" in _pdf_text(client.get(f"/mahnwesen/{d.id}/pdf").content)


def test_nicht_faellig_und_bezahlt(client, db):
    _ready(client)
    doc = _final(client, db)  # heute, fällig in 14 Tagen
    r = post(client, f"/mahnwesen/rechnung/{doc.id}")
    assert "noch nicht fällig" in r.text
    assert doc.number not in client.get("/mahnwesen").text
    doc_id, number, _ = _overdue(client, db)
    post(client, f"/rechnungen/{doc_id}/zahlung", {"paid_on": TODAY.strftime("%d.%m.%Y")})
    r = post(client, f"/mahnwesen/rechnung/{doc_id}")
    assert "nicht mehr offen" in r.text and _dunnings(db) == []


def test_naechste_stufe_erst_nach_frist(client, db):
    _ready(client)
    doc_id, _, tenant_id = _overdue(client, db)
    post(client, f"/mahnwesen/rechnung/{doc_id}")
    db.expire_all()
    with tenant_scope(db, tenant_id):
        [candidate] = mahnwesen.overdue(db)
        assert candidate.next_level.name == "1. Mahnung" and not candidate.ready
        assert candidate.due_since == TODAY + timedelta(days=7 + 3)
        [later] = mahnwesen.overdue(db, today=TODAY + timedelta(days=11))
        assert later.days_overdue == 37
    db.rollback()


def test_mahnung_als_eml(client, db):
    _ready(client)
    doc_id, number, _ = _overdue(client, db)
    post(client, f"/mahnwesen/rechnung/{doc_id}")
    d = _dunnings(db)[0]
    page = client.get(f"/mahnwesen/{d.id}").text
    assert f"Zahlungserinnerung zur Rechnung {number}" in page and "info@krume.example" in page
    r = post(client, f"/mahnwesen/{d.id}/email", {"to": "info@krume.example", "aktion": "eml",
                                                   "subject": f"Zahlungserinnerung zur Rechnung {number}",
                                                   "body": "Bitte zahlen."})
    assert r.headers["content-type"].startswith("message/rfc822")
    msg = email.message_from_bytes(r.content)
    assert msg["To"] == "info@krume.example"
    attachments = [p for p in msg.walk() if p.get_content_type() == "application/pdf"]
    assert len(attachments) == 1 and attachments[0].get_payload(decode=True).startswith(b"%PDF")
    r = post(client, f"/mahnwesen/{d.id}/email", {"to": "", "subject": "x", "body": "y", "aktion": "entwurf"})
    assert r.status_code == 400 and "Empfänger-Adresse" in r.text  # nie ohne Empfänger ins Postfach


def test_rechte_und_mandantentrennung(client, outbox, db):
    client_b, _, tenant_b = _two_tenants(client, outbox, db)
    doc_id, number, _ = _overdue(client, db)
    post(client, f"/mahnwesen/rechnung/{doc_id}")
    d = _dunnings(db)[0]
    assert client_b.get(f"/mahnwesen/{d.id}").status_code == 404
    assert client_b.get(f"/mahnwesen/{d.id}/pdf").status_code == 404
    assert post(client_b, f"/mahnwesen/rechnung/{doc_id}").status_code == 404
    assert number not in client_b.get("/mahnwesen").text
    with tenant_scope(db, tenant_b.id):
        assert db.scalars(select(Dunning)).all() == []
    db.rollback()

    ma = _invite(client, outbox, "max@example.org", "mitarbeiter")
    assert ma.get("/mahnwesen").status_code == 403
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert stb.get(f"/mahnwesen/{d.id}").status_code == 200
    assert post(stb, f"/mahnwesen/rechnung/{doc_id}").status_code == 403
    assert len(_dunnings(db)) == 1
