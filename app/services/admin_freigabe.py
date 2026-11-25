"""Admin-Freigabe: zweites, persönliches Passwort für die Admin-Bereiche.

Geschützt sind Firmenverwaltung (Plattform-Admin), Einstellungen (mit Datensicherung) und
Benutzerverwaltung – die Prüfung sitzt zentral in app/deps.py (``ADMIN_MODULES``).
Nach richtiger Eingabe gilt die Freigabe nur für diese Sitzung und läuft nach ``IDLE``
ohne Nutzung eines Admin-Bereichs ab. Fehlversuche sperren die Eingabe vorübergehend.
"""
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database import utcnow
from app.models import LoginThrottle, User, UserSession
from app.security import hash_password, password_problem, verify_password

IDLE = timedelta(minutes=15)
MAX_FAILURES = 5
LOCK = timedelta(minutes=15)
PAGE = "/admin-passwort"


# ---------------------------------------------------------------- Freigabe
def is_unlocked(sess: UserSession) -> bool:
    return sess.admin_until is not None and sess.admin_until > utcnow()


def unlock(sess: UserSession) -> None:
    sess.admin_until = utcnow() + IDLE


def touch(db: Session, sess: UserSession) -> None:
    """Freigabe bei Nutzung verlängern – höchstens einmal pro Minute in die Datenbank schreiben."""
    if sess.admin_until - utcnow() < IDLE - timedelta(minutes=1):
        unlock(sess)
        db.commit()


def lock(sess: UserSession) -> None:
    sess.admin_until = None


def unlock_url(request: Request) -> str:
    """Adresse der Passwort-Abfrage; danach geht es zur aufgerufenen Seite zurück.
    Bei Formularen (POST) zurück zur Seite, von der das Formular kam – die Eingabe ist verworfen."""
    if request.method == "GET":
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return f"{PAGE}?{urlencode({'weiter': safe_next(target)})}"
    referer = urlsplit(request.headers.get("referer") or "")
    same_host = referer.netloc in ("", request.url.netloc)
    target = (referer.path + (f"?{referer.query}" if referer.query else "")) if same_host else "/"
    return f"{PAGE}?{urlencode({'weiter': safe_next(target), 'verworfen': '1'})}"


def safe_next(url: str | None) -> str:
    """Nur Ziele innerhalb des Programms (kein //fremde-seite, kein http://…)."""
    url = (url or "").strip()
    if not url.startswith("/") or url.startswith("//") or "\\" in url or url.startswith(PAGE):
        return "/"
    return url


# ---------------------------------------------------------------- Passwort
def has_password(user: User) -> bool:
    return bool(user.admin_password_hash)


def check(user: User, password: str) -> bool:
    return has_password(user) and verify_password(user.admin_password_hash, password or "")


def new_password_problem(user: User, password: str, repeat: str) -> str | None:
    problem = password_problem(password, repeat)
    if problem:
        return problem
    if verify_password(user.password_hash, password):
        return "Das Admin-Passwort muss sich vom Anmelde-Passwort unterscheiden."
    return None


def set_password(db: Session, user: User, password: str, keep_session: UserSession | None = None) -> None:
    """Neues Admin-Passwort; Freigaben anderer Sitzungen dieses Benutzers enden."""
    user.admin_password_hash = hash_password(password)
    stmt = update(UserSession).where(UserSession.user_id == user.id)
    if keep_session is not None:
        stmt = stmt.where(UserSession.id != keep_session.id)
    db.execute(stmt.values(admin_until=None))


# ------------------------------------------------------ Fehlversuch-Sperre
def _key(user: User) -> str:
    return f"admin:{user.id}"


def lock_remaining(db: Session, user: User) -> int:
    """Verbleibende Sperrzeit in Sekunden (0 = nicht gesperrt)."""
    row = db.get(LoginThrottle, _key(user))
    now = utcnow()
    if row and row.locked_until and row.locked_until > now:
        return int((row.locked_until - now).total_seconds()) + 1
    return 0


def register_failure(db: Session, user: User) -> bool:
    """Fehlversuch zählen. True, wenn die Eingabe jetzt gesperrt wurde."""
    row = db.get(LoginThrottle, _key(user)) or LoginThrottle(key=_key(user), failures=0)
    db.add(row)
    row.failures += 1
    if row.failures >= MAX_FAILURES:
        row.locked_until = utcnow() + LOCK
        row.failures = 0
        return True
    return False


def clear_failures(db: Session, user: User) -> None:
    row = db.get(LoginThrottle, _key(user))
    if row:
        db.delete(row)
