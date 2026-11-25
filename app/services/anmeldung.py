"""Anmeldung: Passwort, Fehlversuch-Sperre, Bestätigungscode, Sitzungen, Einladungen."""
import secrets
from datetime import timedelta

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import tenant_scope, utcnow
from app.models import Invitation, LoginCode, LoginThrottle, Membership, Tenant, User, UserSession
from app.security import code_hash, constant_equal, hash_password, new_token, token_hash, verify_password
from app.services import einstellungen, mail
from app.templating import templates

LOGIN_MAX_FAILURES = 5
LOGIN_LOCK = timedelta(minutes=15)
CODE_TTL = timedelta(minutes=10)
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_COOLDOWN = timedelta(seconds=60)
SESSION_PENDING_TTL = timedelta(minutes=15)  # Zeit für die Code-Eingabe
SESSION_IDLE = timedelta(minutes=30)        # ohne Aktivität abgemeldet (entschieden 25.11.2026)
SESSION_MAX = timedelta(hours=12)
INVITATION_TTL = timedelta(days=7)

SESSION_COOKIE = "sid"


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unbekannt"


# ------------------------------------------------------ Fehlversuch-Sperre
def _throttle_keys(ip: str, email: str) -> list[str]:
    return [f"ip:{ip}", f"mail:{normalize_email(email)}"]


def lock_remaining(db: Session, ip: str, email: str) -> int:
    """Verbleibende Sperrzeit in Sekunden (0 = nicht gesperrt)."""
    now = utcnow()
    remaining = 0
    for key in _throttle_keys(ip, email):
        row = db.get(LoginThrottle, key)
        if row and row.locked_until and row.locked_until > now:
            remaining = max(remaining, int((row.locked_until - now).total_seconds()) + 1)
    return remaining


def register_failure(db: Session, ip: str, email: str) -> None:
    for key in _throttle_keys(ip, email):
        row = db.get(LoginThrottle, key) or LoginThrottle(key=key, failures=0)
        db.add(row)
        row.failures += 1
        if row.failures >= LOGIN_MAX_FAILURES:
            row.locked_until = utcnow() + LOGIN_LOCK
            row.failures = 0


def clear_failures(db: Session, ip: str, email: str) -> None:
    for key in _throttle_keys(ip, email):
        row = db.get(LoginThrottle, key)
        if row:
            db.delete(row)


def check_password(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        # Gleiche Rechenzeit wie bei echtem Konto, damit man Konten nicht erraten kann
        verify_password(_DUMMY_HASH, password or "")
        return None
    if not verify_password(user.password_hash, password or ""):
        return None
    if not user.is_active:
        return None
    return user


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ------------------------------------------------------------- Sitzungen
def create_session(db: Session, user: User, stage: str, request: Request,
                   tenant_id: int | None = None) -> str:
    token = new_token()
    ttl = SESSION_PENDING_TTL if stage == "code" else SESSION_MAX
    db.add(
        UserSession(
            token_hash=token_hash(token),
            user_id=user.id,
            stage=stage,
            active_tenant_id=tenant_id,
            expires_at=utcnow() + ttl,
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    return token


def find_session(db: Session, token: str | None, touch: bool = True) -> UserSession | None:
    """Gültige Sitzung zum Cookie. ``touch=False``: nur nachsehen, ohne sie als Aktivität zu verlängern
    (für das automatische Neuladen nach Ablauf der Zeit)."""
    if not token:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
    if sess is None:
        return None
    now = utcnow()
    if sess.expires_at <= now or (sess.stage == "ok" and sess.last_seen_at + SESSION_IDLE <= now):
        db.delete(sess)
        db.commit()
        return None
    if touch and (now - sess.last_seen_at) > timedelta(minutes=1):
        sess.last_seen_at = now
        db.commit()
    return sess


def end_all_sessions(db: Session) -> int:
    """Alle Sitzungen beenden – beim Programmstart: nach einem Neustart muss sich jede/r neu anmelden und
    die Admin-Bereiche neu freischalten (entschieden 25.11.2026). Gibt die Anzahl zurück."""
    count = db.execute(delete(UserSession)).rowcount or 0
    db.commit()
    return count


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=get_settings().cookie_secure, max_age=int(SESSION_MAX.total_seconds()), path="/",
    )


def default_tenant_id(db: Session, user: User) -> int | None:
    """Zuletzt benutzte Firma, sonst die erste aktive Zugehörigkeit."""
    memberships = active_memberships(db, user)
    ids = [m.tenant_id for m in memberships]
    if user.last_tenant_id in ids:
        return user.last_tenant_id
    return ids[0] if ids else None


def active_memberships(db: Session, user: User) -> list[Membership]:
    stmt = (
        select(Membership)
        .join(Tenant, Tenant.id == Membership.tenant_id)
        .where(Membership.user_id == user.id, Membership.is_active.is_(True))
        .order_by(Tenant.name)
    )
    return list(db.scalars(stmt))


def complete_login(db: Session, user: User, request: Request, tenant_id: int | None = None) -> str:
    """Vollständige Anmeldung: neue Sitzung (neues Token gegen Session-Fixation)."""
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.stage == "code"))
    db.flush()
    token = create_session(db, user, "ok", request, tenant_id or default_tenant_id(db, user))
    user.last_login_at = utcnow()
    return token


# ------------------------------------------------------ Bestätigungscode
def _branding(db: Session, tenant_id: int | None) -> dict:
    if tenant_id is None:
        return {"firma": "Buchhaltung", "farbe": "#1f5fbf", "adresse": ""}
    with tenant_scope(db, tenant_id):
        values = einstellungen.load(db)
    tenant = db.get(Tenant, tenant_id)
    name = values.get("firma.name") or (tenant.name if tenant else "Buchhaltung")
    adresse = ", ".join(x for x in (values.get("firma.strasse"), " ".join(
        x for x in (values.get("firma.plz"), values.get("firma.ort")) if x)) if x)
    return {"firma": name, "farbe": values.get("design.akzentfarbe") or "#1f5fbf", "adresse": adresse}


def send_login_code(db: Session, user: User) -> tuple[str, str]:
    """Erzeugt einen 6-stelligen Code und schickt ihn an die E-Mail des Benutzers.
    Gibt (status, meldung) zurück; status ist "gesendet", "wartezeit" oder "fehler"."""
    now = utcnow()
    last = db.scalar(
        select(LoginCode).where(LoginCode.user_id == user.id, LoginCode.used_at.is_(None))
        .order_by(LoginCode.sent_at.desc())
    )
    if last and last.expires_at > now and now - last.sent_at < CODE_RESEND_COOLDOWN:
        wait = int((CODE_RESEND_COOLDOWN - (now - last.sent_at)).total_seconds()) + 1
        return "wartezeit", f"Es wurde gerade ein Code verschickt. Ein neuer ist in {wait} Sekunden möglich."

    db.execute(delete(LoginCode).where(LoginCode.user_id == user.id))
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(LoginCode(user_id=user.id, code_hash=code_hash(code), sent_at=now, expires_at=now + CODE_TTL))

    tenant_id = default_tenant_id(db, user)
    minutes = int(CODE_TTL.total_seconds() // 60)
    ctx = {"code": code, "minuten": minutes, **_branding(db, tenant_id)}
    text = (
        f"Ihr Bestätigungscode lautet: {code}\n\n"
        f"Der Code ist {minutes} Minuten gültig.\n\n"
        "Falls Sie sich nicht gerade anmelden, ignorieren Sie diese E-Mail."
    )
    html = templates.get_template("emails/login_code.html").render(ctx)
    try:
        how = mail.send_system_mail(db, to=user.email, subject="Ihr Bestätigungscode", text=text, html=html,
                                    tenant_id=tenant_id)
    except mail.MailError as exc:
        db.rollback()
        return "fehler", str(exc)
    if how == "datei":
        return "gesendet", "Testbetrieb ohne Mailserver: Der Code steht im Konsolenfenster des Programms."
    return "gesendet", f"Ein Bestätigungscode wurde an {user.email} gesendet (gültig {minutes} Minuten)."


def check_login_code(db: Session, user: User, code: str) -> tuple[bool, str]:
    now = utcnow()
    row = db.scalar(
        select(LoginCode).where(LoginCode.user_id == user.id, LoginCode.used_at.is_(None))
        .order_by(LoginCode.sent_at.desc())
    )
    if row is None:
        return False, "Kein gültiger Code vorhanden. Bitte einen neuen anfordern."
    if row.expires_at <= now:
        db.delete(row)
        return False, "Der Code ist abgelaufen. Bitte einen neuen anfordern."
    row.attempts += 1
    if row.attempts > CODE_MAX_ATTEMPTS:
        db.delete(row)
        return False, "Zu viele Fehlversuche. Bitte einen neuen Code anfordern."
    if not constant_equal(row.code_hash, code_hash((code or "").strip().replace(" ", ""))):
        return False, "Der Code ist falsch."
    row.used_at = now
    return True, ""


# ------------------------------------------------------------ Einladungen
def create_invitation(
    db: Session, *, tenant_id: int, email: str, name: str, role: str, permissions: dict,
    extra_rights: list[str], created_by_id: int | None,
) -> tuple[Invitation, str]:
    token = new_token()
    inv = Invitation(
        tenant_id=tenant_id, email=normalize_email(email), name=name.strip(), role=role,
        permissions=permissions, extra_rights=extra_rights, token_hash=token_hash(token),
        expires_at=utcnow() + INVITATION_TTL, created_by_id=created_by_id,
    )
    db.add(inv)
    return inv, token


def renew_invitation(inv: Invitation) -> str:
    """Neuen Einladungs-Link erzeugen (z. B. lokal ohne E-Mail-Versand oder abgelaufen): der alte Link wird
    ungültig, die neue Frist beginnt. Gibt das neue Token zurück."""
    token = new_token()
    inv.token_hash = token_hash(token)
    inv.expires_at = utcnow() + INVITATION_TTL
    return token


def find_invitation(db: Session, token: str) -> Invitation | None:
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash(token)))
    if inv is None or not inv.is_open:
        return None
    return inv


def send_invitation_mail(db: Session, inv: Invitation, link: str) -> tuple[bool, str]:
    brand = _branding(db, inv.tenant_id)
    days = INVITATION_TTL.days
    text = (
        f"Guten Tag{(' ' + inv.name) if inv.name else ''},\n\n"
        f"Sie wurden in die Buchhaltung von {brand['firma']} eingeladen.\n"
        f"Über diesen Link legen Sie Ihr Passwort fest (gültig {days} Tage):\n\n{link}\n\n"
        "Falls Sie diese Einladung nicht erwartet haben, ignorieren Sie diese E-Mail."
    )
    html = templates.get_template("emails/einladung.html").render(
        {"link": link, "tage": days, "name": inv.name, **brand}
    )
    try:
        how = mail.send_system_mail(db, to=inv.email, subject=f"Einladung: {brand['firma']}", text=text,
                                    html=html, tenant_id=inv.tenant_id)
    except mail.MailError as exc:
        return False, str(exc)
    if how == "datei":
        return True, "Testbetrieb ohne Mailserver: Die Einladung steht im Konsolenfenster. Link siehe unten."
    return True, f"Einladung an {inv.email} gesendet."
