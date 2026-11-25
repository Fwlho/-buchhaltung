"""Anmeldung per Authenticator-App (TOTP): Einrichten, Anmelden, kein Wiederverwenden, Notfall-Codes, Sperre."""
import base64

from sqlalchemy import select

from app.models import AuditLog, User
from app.services import totp
from tests.conftest import PASSWORD, ersteinrichtung, new_client, post


def _user(db) -> User:
    db.expire_all()
    return db.scalar(select(User))


def _setup_app(client, db) -> tuple[str, list[str]]:
    r = post(client, "/konto/app/einrichten", {"password": PASSWORD})
    assert 'src="/konto/app/qr.svg"' in r.text
    secret = totp.secret_of(_user(db))
    r = post(client, "/konto/app/bestaetigen", {"code": totp.code_at(secret, totp.current_step())})
    assert "Notfall-Codes" in r.text
    codes = [c.split("<")[0] for c in r.text.split("<li><code>")[1:]]
    return secret, codes


def _login_password(email="chefin@example.org"):
    c = new_client()
    c.get("/login")
    r = post(c, "/login", {"email": email, "password": PASSWORD})
    return c, r


# ------------------------------------------------------------ reine Funktionen
def test_rfc6238_testwert_und_zeitfenster():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(secret, 59 // 30) == "287082"            # RFC 6238, Anhang B (6 Stellen)
    now = 1_000_000_000.0
    step = totp.current_step(now)
    assert totp.match(secret, totp.code_at(secret, step - 1), now=now) == step - 1     # 30 s Uhrabweichung ok
    assert totp.match(secret, totp.code_at(secret, step - 2), now=now) is None
    assert totp.match(secret, totp.code_at(secret, step), last_step=step, now=now) is None   # schon benutzt
    assert totp.match(secret, "12 34", now=now) is None
    assert totp.uri("ABC", "a@b.de", "Buchhaltung").startswith("otpauth://totp/Buchhaltung%3Aa%40b.de?secret=ABC")
    codes = totp.new_recovery_codes()
    assert len(codes) == 8 and len(set(codes)) == 8 and all(len(c) == 11 and c[5] == "-" for c in codes)


# ------------------------------------------------------------ Einrichten
def test_einrichten_braucht_passwort_und_ersten_code(client, db):
    ersteinrichtung(client)
    r = post(client, "/konto/app/einrichten", {"password": "falsch-falsch"})
    assert r.status_code == 400 and "Anmelde-Passwort ist falsch" in r.text
    r = post(client, "/konto/app/einrichten", {"password": PASSWORD})
    qr = client.get("/konto/app/qr.svg")
    assert qr.status_code == 200 and qr.headers["content-type"].startswith("image/svg+xml") and b"<svg" in qr.content
    user = _user(db)
    secret = totp.secret_of(user)
    assert user.totp_secret and secret not in user.totp_secret        # verschlüsselt gespeichert
    assert not totp.enabled(user)                                     # erst nach dem ersten Code aktiv
    r = post(client, "/konto/app/bestaetigen", {"code": "000000"})
    assert r.status_code == 400 and "Der Code passt nicht" in r.text
    r = post(client, "/konto/app/bestaetigen", {"code": totp.code_at(secret, totp.current_step())})
    assert "nur einmal angezeigt" in r.text and r.text.count("<li><code>") == 8
    user = _user(db)
    assert totp.enabled(user) and len(user.totp_recovery) == 8
    assert all(c not in str(user.totp_recovery) for c in [x.split("<")[0] for x in r.text.split("<li><code>")[1:]])
    assert client.get("/konto/app/qr.svg").status_code == 404        # QR nur während der Einrichtung
    assert "aktiv" in client.get("/konto").text
    assert db.scalar(select(AuditLog).where(AuditLog.action == "authenticator_app_eingerichtet")) is not None


# ------------------------------------------------------------ Anmelden
def test_anmeldung_mit_app_statt_email(client, db, outbox):
    ersteinrichtung(client)
    secret, codes = _setup_app(client, db)
    c, r = _login_password()
    assert "Authenticator-App" in r.text and outbox == []            # KEIN Code per E-Mail
    assert "Neuen Code anfordern" not in r.text
    step = totp.current_step()
    r = post(c, "/login/code", {"code": totp.code_at(secret, step + 1)})
    assert r.status_code == 200 and "Abmelden" in r.text              # angemeldet

    # derselbe Code ein zweites Mal → abgelehnt
    c2, _ = _login_password()
    r = post(c2, "/login/code", {"code": totp.code_at(secret, step + 1)})
    assert r.status_code == 401 and "schon verwendet" in r.text
    # Notfall-Code: einmal ja, zweimal nein
    r = post(c2, "/login/code", {"code": codes[0].upper()})
    assert "Notfall-Code verwendet – noch 7 übrig" in r.text
    c3, _ = _login_password()
    assert post(c3, "/login/code", {"code": codes[0]}).status_code == 401
    assert "keinen Code per E-Mail" in post(c3, "/login/code/neu").text
    assert outbox == []


def test_fehlversuch_sperre(client, db):
    ersteinrichtung(client)
    secret, _ = _setup_app(client, db)
    c, _ = _login_password()
    for _ in range(totp.MAX_FAILURES):
        post(c, "/login/code", {"code": "111111"})
    r = post(c, "/login/code", {"code": totp.code_at(secret, totp.current_step() + 1)})
    assert r.status_code == 401 and "Zu viele Fehlversuche" in r.text


def test_abschalten_braucht_passwort_und_code(client, db, outbox):
    ersteinrichtung(client)
    secret, _ = _setup_app(client, db)
    r = post(client, "/konto/app/abschalten", {"password": PASSWORD, "code": "000000"})
    assert r.status_code == 400 and totp.enabled(_user(db))
    r = post(client, "/konto/app/abschalten", {"password": PASSWORD,
                                               "code": totp.code_at(secret, totp.current_step() + 1)})
    assert "abgeschaltet" in r.text and not totp.enabled(_user(db))
    _, r = _login_password()
    assert outbox and "Bestätigungscode" in outbox[-1]["subject"]   # wieder per E-Mail
