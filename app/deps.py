"""Zentrale Abhängigkeiten für alle Routen: Anmeldung, aktive Firma, Rechte, CSRF.

Jede Route MUSS genau eine Schutz-Abhängigkeit haben:
  - ``public``                 öffentlich (Login, Einladung, Ersteinrichtung)
  - ``require_user``           angemeldet, ohne Firmenbezug (z. B. eigenes Passwort)
  - ``require_platform_admin`` Plattform-Admin (Firmenverwaltung)
  - ``require_module(...)``    angemeldet + aktive Firma + Modulrecht
Ein Test (tests/test_routen_schutz.py) prüft das für alle Routen automatisch.

Admin-Bereiche (Firmenverwaltung und die Module in ``ADMIN_MODULES``) verlangen zusätzlich das
persönliche Admin-Passwort (app/services/admin_freigabe.py) – außer während der ersten Einrichtung.
"""
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Membership, Tenant, User, UserSession
from app.permissions import has_extra, has_module
from app.security import constant_equal
from app.config import get_settings
from app.services import admin_freigabe, totp
from app.services.anmeldung import SESSION_COOKIE, active_memberships, find_session
from app.templating import CSRF_COOKIE, CSRF_FIELD


# Module, die nur nach Eingabe des Admin-Passworts erreichbar sind (Datensicherung gehört zu „einstellungen“)
ADMIN_MODULES = {"einstellungen", "benutzer"}
# Ohne eingerichtete Authenticator-App (Pflicht, entschieden 25.11.2026) sind nur diese Seiten erreichbar
APP_SETUP_PATHS = {"/konto", "/konto/app/einrichten", "/konto/app/qr.svg", "/konto/app/bestaetigen", "/logout"}
APP_SETUP_PAGE = "/konto?app=pflicht"


class AuthRedirect(Exception):
    """Wird in eine Weiterleitung umgewandelt (z. B. zur Login-Seite)."""

    def __init__(self, url: str):
        self.url = url


def _guard(kind: str):
    def mark(fn):
        fn._guard = kind
        return fn

    return mark


@dataclass
class TenantContext:
    user: User
    tenant: Tenant
    membership: Membership
    session: UserSession
    memberships: list[Membership] = field(default_factory=list)

    @property
    def tenant_id(self) -> int:
        return self.tenant.id

    @property
    def role(self) -> str:
        return self.membership.role

    def can(self, module: str, edit: bool = False) -> bool:
        return has_module(self.membership.role, self.membership.permissions, module, edit)

    def has_extra(self, right: str) -> bool:
        return has_extra(self.membership.role, self.membership.extra_rights, right)


# ------------------------------------------------------------------- CSRF
async def csrf_protect(request: Request) -> None:
    """Für alle ändernden Anfragen: Token aus Formular/Header muss zum Cookie passen."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get(CSRF_FIELD) or ""
    if not cookie or not sent or not constant_equal(cookie, str(sent)):
        raise HTTPException(403, "Sicherheitsprüfung fehlgeschlagen. Bitte die Seite neu laden und erneut versuchen.")


# ------------------------------------------------------------- Anmeldung
def current_session(request: Request, db: Session) -> UserSession | None:
    if not hasattr(request.state, "session"):
        request.state.session = find_session(db, request.cookies.get(SESSION_COOKIE))
    return request.state.session


@_guard("public")
def public() -> None:
    """Kennzeichnet eine Route ausdrücklich als öffentlich."""
    return None


@_guard("user")
def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    sess = current_session(request, db)
    if sess is None or sess.stage != "ok":
        raise AuthRedirect("/login")
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        raise AuthRedirect("/login")
    request.state.user = user
    if get_settings().totp_required and not totp.enabled(user) and request.url.path not in APP_SETUP_PATHS:
        raise AuthRedirect(APP_SETUP_PAGE)
    return user


@_guard("admin")
def require_platform_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_user(request, db)
    if not user.is_platform_admin:
        raise HTTPException(403, "Nur für den Plattform-Admin.")
    require_admin_unlock(request, db)
    return user


def require_admin_unlock(request: Request, db: Session) -> None:
    """Admin-Passwort muss in dieser Sitzung vor kurzem eingegeben worden sein."""
    sess: UserSession = request.state.session
    if admin_freigabe.is_unlocked(sess):
        admin_freigabe.touch(db, sess)
        request.state.admin_area = True          # Seite lädt nach Ablauf der Admin-Zeit neu (base.html)
        return
    raise AuthRedirect(admin_freigabe.unlock_url(request))


def _tenant_context(request: Request, db: Session, allow_unconfigured: bool) -> TenantContext:
    user = require_user(request, db)
    sess: UserSession = request.state.session
    memberships = active_memberships(db, user)
    membership = next((m for m in memberships if m.tenant_id == sess.active_tenant_id), None)
    if membership is None:
        if memberships:
            sess.active_tenant_id = memberships[0].tenant_id
            db.commit()
            membership = memberships[0]
        elif user.is_platform_admin:
            raise AuthRedirect("/admin")
        else:
            raise HTTPException(403, "Ihr Konto ist keiner Firma zugeordnet.")

    tenant = db.get(Tenant, membership.tenant_id)
    if tenant is None or tenant.is_locked:
        raise HTTPException(403, "Diese Firma ist gesperrt. Bitte wenden Sie sich an den Betreiber.")

    # Ab hier filtern alle Abfragen automatisch auf diese Firma (app/database.py)
    db.info["tenant_id"] = tenant.id
    ctx = TenantContext(user=user, tenant=tenant, membership=membership, session=sess, memberships=memberships)
    request.state.ctx = ctx

    if not tenant.setup_completed and not allow_unconfigured:
        if ctx.can("einstellungen", edit=True):
            raise AuthRedirect("/einrichtung")
        raise HTTPException(403, "Die Firma wird gerade eingerichtet. Bitte später erneut versuchen.")
    return ctx


def require_module(module: str, edit: bool = False, allow_unconfigured: bool = False):
    """Abhängigkeit: angemeldet, aktive Firma, Recht auf ``module`` (lesen/bearbeiten)."""

    @_guard("module")
    def dependency(request: Request, db: Session = Depends(get_db)) -> TenantContext:
        ctx = _tenant_context(request, db, allow_unconfigured)
        if not ctx.can(module, edit):
            raise HTTPException(403, "Dafür fehlt Ihnen die Berechtigung.")
        # Während der ersten Einrichtung (Assistent) noch ohne Admin-Passwort
        if module in ADMIN_MODULES and ctx.tenant.setup_completed:
            require_admin_unlock(request, db)
        return ctx

    dependency.__name__ = f"require_{module}_{'bearbeiten' if edit else 'lesen'}"
    dependency.module = module
    return dependency


def membership_for(db: Session, user_id: int, tenant_id: int) -> Membership | None:
    return db.scalar(
        select(Membership).where(Membership.user_id == user_id, Membership.tenant_id == tenant_id)
    )
