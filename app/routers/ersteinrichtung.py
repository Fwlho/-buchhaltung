"""Ersteinrichtung beim allerersten Start: Plattform-Admin + erste Firma anlegen."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import public
from app.models import User
from app.permissions import ROLE_OWNER
from app.security import constant_equal, hash_password, password_problem
from app.services import admin_freigabe, anmeldung, audit, firmen
from app.services.pruefungen import email_valid
from app.templating import redirect, render

router = APIRouter()

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def setup_needed(db: Session) -> bool:
    return db.scalar(select(func.count(User.id))) == 0


def _check_allowed(request: Request, token: str) -> None:
    """Auf dem Server darf nicht jeder Besucher die Ersteinrichtung ausführen:
    erlaubt nur vom eigenen Rechner aus oder mit SETUP_TOKEN aus der .env."""
    client = request.client.host if request.client else ""
    if client in LOCAL_HOSTS or client == "testclient":
        return
    expected = get_settings().setup_token
    if expected and token and constant_equal(expected, token):
        return
    raise HTTPException(403, "Die Ersteinrichtung ist nur direkt am Server-Rechner oder mit SETUP_TOKEN erlaubt.")


@router.get("/ersteinrichtung", dependencies=[Depends(public)])
def setup_form(request: Request, token: str = "", db: Session = Depends(get_db)):
    if not setup_needed(db):
        return redirect("/login")
    _check_allowed(request, token)
    return render(request, "ersteinrichtung.html", form={}, errors={}, token=token)


@router.post("/ersteinrichtung", dependencies=[Depends(public)])
def setup_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    admin_password: str = Form(""),
    admin_password2: str = Form(""),
    firma: str = Form(""),
    token: str = Form(""),
    db: Session = Depends(get_db),
):
    if not setup_needed(db):
        return redirect("/login")
    _check_allowed(request, token)

    errors: dict[str, str] = {}
    if not name.strip():
        errors["name"] = "Bitte Ihren Namen angeben."
    if not email_valid(email):
        errors["email"] = "Bitte eine gültige E-Mail-Adresse angeben."
    problem = password_problem(password, password2)
    if problem:
        errors["password"] = problem
    problem = password_problem(admin_password, admin_password2)
    if not problem and admin_password == password:
        problem = "Das Admin-Passwort muss sich vom Anmelde-Passwort unterscheiden."
    if problem:
        errors["admin_password"] = problem
    if not firma.strip():
        errors["firma"] = "Bitte den Namen der ersten Firma angeben."
    if errors:
        form = {"name": name, "email": email, "firma": firma}
        return render(request, "ersteinrichtung.html", status_code=400, form=form, errors=errors, token=token)

    user = User(email=anmeldung.normalize_email(email), name=name.strip(),
                password_hash=hash_password(password), admin_password_hash=hash_password(admin_password),
                is_platform_admin=True)
    db.add(user)
    db.flush()
    tenant = firmen.create_tenant(db, firma)
    firmen.add_membership(db, user, tenant.id, ROLE_OWNER)
    user.last_tenant_id = tenant.id
    audit.log(db, action="ersteinrichtung", entity="plattform", user_id=user.id, ip=anmeldung.client_ip(request))
    audit.log(db, action="angelegt", entity="firma", entity_id=tenant.id, tenant_id=tenant.id, user_id=user.id)
    db.flush()

    # Direkt anmelden: Die Person sitzt am Rechner und hat gerade das Passwort vergeben.
    token_value = anmeldung.complete_login(db, user, request)
    db.flush()
    admin_freigabe.unlock(anmeldung.find_session(db, token_value))  # Admin-Passwort gerade vergeben
    db.commit()
    response = redirect("/einrichtung", "Willkommen! Bitte richten Sie jetzt Ihre Firma ein.")
    anmeldung.set_session_cookie(response, token_value)
    return response
