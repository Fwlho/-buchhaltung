"""Knopf „Link neu erzeugen“ bei offenen Einladungen (lokal ohne E-Mail-Versand, abgelaufene Einladungen)."""
import re
from datetime import timedelta

from sqlalchemy import select, update

from app.database import utcnow
from app.models import Invitation, User
from tests.conftest import PASSWORD, complete_wizard, ersteinrichtung, new_client, post
from tests.test_mandantentrennung import _two_tenants


def _link(html: str) -> str:
    return re.search(r'value="http://testserver(/einladung/[^"]+)"', html).group(1)


def test_neuer_link_ersetzt_den_alten(client, db, outbox):
    ersteinrichtung(client)
    complete_wizard(client)
    r = post(client, "/benutzer/einladen", {"name": "Max", "email": "max@example.org", "role": "mitarbeiter"})
    first = _link(r.text)
    inv = db.scalar(select(Invitation))
    page = client.get("/benutzer").text
    assert "Link neu erzeugen" in page and first not in page       # alter Link wird nicht erneut gezeigt

    r = post(client, f"/benutzer/einladungen/{inv.id}/neuer-link")
    second = _link(r.text)
    assert second != first and "der bisherige Link gilt nicht mehr" in r.text
    assert len(outbox) >= 2                                          # Einladungs-Mail erneut abgelegt

    c = new_client()
    assert c.get(first).status_code == 404                           # alter Link ungültig
    assert c.get(second).status_code == 200
    r = post(c, second, {"name": "Max", "password": PASSWORD, "password2": PASSWORD})
    assert r.status_code == 200
    db.expire_all()
    assert db.scalar(select(User).where(User.email == "max@example.org")) is not None
    assert "Link neu erzeugen" not in client.get("/benutzer").text   # angenommen: keine offene Einladung mehr


def test_abgelaufene_einladung_erneuern(client, db, outbox):
    ersteinrichtung(client)
    complete_wizard(client)
    post(client, "/benutzer/einladen", {"name": "Eva", "email": "eva@example.org", "role": "mitarbeiter"})
    db.execute(update(Invitation).values(expires_at=utcnow() - timedelta(days=1)))
    db.commit()
    page = client.get("/benutzer").text
    assert "abgelaufen" in page and "eva@example.org" in page
    inv = db.scalar(select(Invitation))
    r = post(client, f"/benutzer/einladungen/{inv.id}/neuer-link")
    assert new_client().get(_link(r.text)).status_code == 200
    db.expire_all()
    assert db.get(Invitation, inv.id).expires_at > utcnow() + timedelta(days=6)


def test_zurueckgezogene_oder_fremde_einladung(client, db, outbox):
    client_b, _a, _b = _two_tenants(client, outbox, db)
    post(client, "/benutzer/einladen", {"name": "Max", "email": "max@example.org", "role": "mitarbeiter"})
    inv = db.scalar(select(Invitation).where(Invitation.email == "max@example.org"))
    assert post(client_b, f"/benutzer/einladungen/{inv.id}/neuer-link").status_code == 404   # andere Firma
    post(client, f"/benutzer/einladungen/{inv.id}/zurueckziehen")
    assert post(client, f"/benutzer/einladungen/{inv.id}/neuer-link").status_code == 404     # zurückgezogen
