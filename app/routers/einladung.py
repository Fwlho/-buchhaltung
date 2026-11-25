"""Einladung annehmen: Passwort festlegen (neues Konto) bzw. mit bestehendem Konto bestätigen."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db, utcnow
from app.deps import public
from app.models import Invitation, User
from app.security import hash_password, password_problem, verify_password
from app.services import anmeldung, audit, firmen
from app.templating import redirect, render

router = APIRouter()


def _context(db: Session, inv: Invitation) -> dict:
    existing = db.scalar(select(User).where(User.email == inv.email))
    return {"inv": inv, "existing": existing, "tenant_name": inv.tenant.name}


@router.get("/einladung/{token}", name="invitation_form", dependencies=[Depends(public)])
def invitation_form(request: Request, token: str, db: Session = Depends(get_db)):
    inv = anmeldung.find_invitation(db, token)
    if inv is None:
        return render(request, "einladung_ungueltig.html", status_code=404)
    return render(request, "einladung.html", token=token, errors={}, name=inv.name, **_context(db, inv))


@router.post("/einladung/{token}", dependencies=[Depends(public)])
def invitation_accept(
    request: Request,
    token: str,
    name: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    db: Session = Depends(get_db),
):
    inv = anmeldung.find_invitation(db, token)
    if inv is None:
        return render(request, "einladung_ungueltig.html", status_code=404)
    ctx = _context(db, inv)
    user: User | None = ctx["existing"]
    errors: dict[str, str] = {}
    ip = anmeldung.client_ip(request)

    if user is not None:
        locked = anmeldung.lock_remaining(db, ip, user.email)
        if locked or not user.is_active or not verify_password(user.password_hash, password):
            if not locked:
                anmeldung.register_failure(db, ip, user.email)
                db.commit()
            errors["password"] = "Das Passwort ist falsch." if not locked else "Zu viele Fehlversuche, bitte später erneut."
    else:
        if not name.strip():
            errors["name"] = "Bitte Ihren Namen angeben."
        problem = password_problem(password, password2)
        if problem:
            errors["password"] = problem
    if errors:
        return render(request, "einladung.html", status_code=400, token=token, errors=errors, name=name, **ctx)

    if user is None:
        user = User(email=inv.email, name=name.strip(), password_hash=hash_password(password))
        db.add(user)
        db.flush()
    firmen.add_membership(db, user, inv.tenant_id, inv.role, inv.permissions, inv.extra_rights)
    inv.accepted_at = utcnow()
    user.last_tenant_id = inv.tenant_id
    audit.log(db, action="einladung_angenommen", entity="benutzer", entity_id=user.id, tenant_id=inv.tenant_id,
              user_id=user.id, changes={"rolle": inv.role}, ip=ip)

    # Wer den Link aus der E-Mail hat und das Passwort kennt, ist doppelt bestätigt.
    anmeldung.end_session(db, request.cookies.get(anmeldung.SESSION_COOKIE))
    session_token = anmeldung.complete_login(db, user, request, tenant_id=inv.tenant_id)
    db.commit()
    response = redirect("/", f"Willkommen bei {inv.tenant.name}!")
    anmeldung.set_session_cookie(response, session_token)
    return response
