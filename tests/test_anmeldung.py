from sqlalchemy import select

from app.models import LoginCode, User
from tests.conftest import PASSWORD, code_from, csrf, ersteinrichtung, login, new_client, post


def test_ohne_benutzer_geht_es_zur_ersteinrichtung(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ersteinrichtung"
    assert client.get("/health").json() == {"status": "ok"}


def test_tab_symbol_ohne_anmeldung(client):
    r = client.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/static/favicon.svg"
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert 'rel="icon"' in ersteinrichtung(client).text


def test_ersteinrichtung_legt_admin_und_firma_an(client, db):
    r = ersteinrichtung(client)
    assert "Einrichtung Ihrer Firma" in r.text
    user = db.scalar(select(User))
    assert user.is_platform_admin
    assert user.password_hash != PASSWORD  # nur als Hash gespeichert
    # Ein zweites Mal ist die Ersteinrichtung gesperrt
    r = client.get("/ersteinrichtung", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_ersteinrichtung_prueft_eingaben(client):
    client.get("/ersteinrichtung")
    r = post(client, "/ersteinrichtung", {"name": "", "email": "kaputt", "password": "kurz",
                                           "password2": "kurz", "firma": ""})
    assert r.status_code == 400
    assert "mindestens 10 Zeichen" in r.text


def test_login_mit_bestaetigungscode(client, outbox):
    ersteinrichtung(client)
    post(client, "/logout")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"

    client.get("/login")
    r = post(client, "/login", {"email": "Chefin@Example.org", "password": PASSWORD})
    assert "Bestätigungscode" in r.text
    # Ohne Code noch kein Zugriff
    assert client.get("/einrichtung", follow_redirects=False).headers["location"] == "/login"

    real = code_from(outbox, "chefin@example.org")
    r = post(client, "/login/code", {"code": "000000" if real != "000000" else "111111"})
    assert r.status_code == 401
    r = post(client, "/login/code", {"code": real})
    assert r.status_code == 200
    assert "Einrichtung Ihrer Firma" in r.text


def test_code_nur_als_hash_und_einmalig(client, outbox, db):
    ersteinrichtung(client)
    post(client, "/logout")
    login(client, outbox, "chefin@example.org")
    code = code_from(outbox, "chefin@example.org")
    row = db.scalar(select(LoginCode))
    assert row.code_hash != code
    assert row.used_at is not None

    # Gleicher Code ein zweites Mal geht nicht
    c2 = new_client()
    c2.get("/login")
    post(c2, "/login", {"email": "chefin@example.org", "password": PASSWORD})
    # neuer Code wurde verschickt, alter ist ungültig
    if code_from(outbox, "chefin@example.org") != code:
        r = post(c2, "/login/code", {"code": code})
        assert r.status_code == 401


def test_falsches_passwort_und_sperre(client, outbox):
    ersteinrichtung(client)
    post(client, "/logout")
    client.get("/login")
    for _ in range(5):
        r = post(client, "/login", {"email": "chefin@example.org", "password": "falsch-falsch"})
        assert r.status_code == 401
    # Jetzt gesperrt – auch mit richtigem Passwort
    r = post(client, "/login", {"email": "chefin@example.org", "password": PASSWORD})
    assert r.status_code == 429
    assert not outbox


def test_zu_viele_falsche_codes(client, outbox):
    ersteinrichtung(client)
    post(client, "/logout")
    client.get("/login")
    post(client, "/login", {"email": "chefin@example.org", "password": PASSWORD})
    real = code_from(outbox, "chefin@example.org")
    wrong = "123456" if real != "123456" else "654321"
    for _ in range(5):
        post(client, "/login/code", {"code": wrong})
    r = post(client, "/login/code", {"code": real})
    assert r.status_code == 401
    assert "neuen Code" in r.text


def test_unbekannte_email_verraet_nichts(client):
    ersteinrichtung(client)
    post(client, "/logout")
    client.get("/login")
    r = post(client, "/login", {"email": "gibtsnicht@example.org", "password": PASSWORD})
    assert r.status_code == 401
    assert "E-Mail-Adresse oder Passwort ist falsch" in r.text


def test_csrf_schutz(client):
    ersteinrichtung(client)
    csrf(client)
    r = client.post("/logout", data={"csrf_token": "falsch"})
    assert r.status_code == 403
    r = client.post("/logout", data={})
    assert r.status_code == 403


def test_passwort_aendern(client, outbox):
    ersteinrichtung(client)
    r = post(client, "/konto/passwort", {"old_password": "falsch", "password": "neues-passwort-2",
                                         "password2": "neues-passwort-2"})
    assert r.status_code == 400
    r = post(client, "/konto/passwort", {"old_password": PASSWORD, "password": "neues-passwort-2",
                                         "password2": "neues-passwort-2"})
    assert "Passwort wurde geändert" in r.text
    post(client, "/logout")
    login(client, outbox, "chefin@example.org", "neues-passwort-2")


def test_sicherheits_header(client):
    r = client.get("/health")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
