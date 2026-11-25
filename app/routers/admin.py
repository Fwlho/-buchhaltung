"""Plattform-Admin: Firmen anlegen, Inhaber einladen, Firmen sperren.

Der Admin sieht bewusst KEINE Buchhaltungsdaten der Firmen – nur Name, Status
und Anzahl der Benutzer.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_platform_admin
from app.models import Invitation, Membership, Tenant, User
from app.models.tenant import TENANT_ACTIVE, TENANT_LOCKED
from app.permissions import ROLE_OWNER, role_template
from app.services import anmeldung, audit, firmen
from app.services.pruefungen import email_valid
from app.templating import redirect, render

router = APIRouter(prefix="/admin")


def _overview(db: Session) -> list[dict]:
    member_counts = dict(
        db.execute(
            select(Membership.tenant_id, func.count(Membership.id))
            .where(Membership.is_active.is_(True))
            .group_by(Membership.tenant_id)
        ).all()
    )
    rows = []
    for tenant in db.scalars(select(Tenant).order_by(Tenant.name)):
        open_inv = [
            inv for inv in db.scalars(
                select(Invitation).where(Invitation.tenant_id == tenant.id, Invitation.role == ROLE_OWNER)
            ) if inv.is_open
        ]
        rows.append({"tenant": tenant, "members": member_counts.get(tenant.id, 0), "invitations": open_inv})
    return rows


def _page(request: Request, db: Session, user: User, status_code: int = 200, **extra):
    return render(request, "admin.html", status_code=status_code, rows=_overview(db), nav_user=user,
                  form=extra.pop("form", {}), errors=extra.pop("errors", {}), **extra)


@router.get("")
def overview(request: Request, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    return _page(request, db, user)


def _invite_owner(request: Request, db: Session, user: User, tenant: Tenant, name: str, email: str):
    perms, extras = role_template(ROLE_OWNER)
    inv, token = anmeldung.create_invitation(
        db, tenant_id=tenant.id, email=email, name=name, role=ROLE_OWNER,
        permissions=perms, extra_rights=extras, created_by_id=user.id,
    )
    db.flush()
    link = str(request.url_for("invitation_form", token=token))
    ok, message = anmeldung.send_invitation_mail(db, inv, link)
    return link, ok, message


@router.post("/firmen")
def create_tenant(
    request: Request,
    firma: str = Form(""),
    inhaber_name: str = Form(""),
    inhaber_email: str = Form(""),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    errors = {}
    if not firma.strip():
        errors["firma"] = "Bitte einen Firmennamen angeben."
    if not inhaber_name.strip():
        errors["inhaber_name"] = "Bitte den Namen des Inhabers angeben."
    if not email_valid(inhaber_email):
        errors["inhaber_email"] = "Bitte eine gültige E-Mail-Adresse angeben."
    if errors:
        form = {"firma": firma, "inhaber_name": inhaber_name, "inhaber_email": inhaber_email}
        return _page(request, db, user, status_code=400, form=form, errors=errors)

    tenant = firmen.create_tenant(db, firma)
    audit.log(db, action="angelegt", entity="firma", entity_id=tenant.id, tenant_id=tenant.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    link, ok, message = _invite_owner(request, db, user, tenant, inhaber_name, inhaber_email)
    db.commit()
    return _page(request, db, user, new_link=link, new_link_for=inhaber_email,
                 info=message, info_kind="ok" if ok else "fehler")


@router.post("/firmen/{tenant_id}/einladung")
def reinvite_owner(
    request: Request,
    tenant_id: int,
    inhaber_name: str = Form(""),
    inhaber_email: str = Form(""),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Firma nicht gefunden.")
    if not email_valid(inhaber_email):
        return redirect("/admin", "Bitte eine gültige E-Mail-Adresse angeben.", "fehler")
    link, ok, message = _invite_owner(request, db, user, tenant, inhaber_name, inhaber_email)
    db.commit()
    return _page(request, db, user, new_link=link, new_link_for=inhaber_email,
                 info=message, info_kind="ok" if ok else "fehler")


@router.post("/firmen/{tenant_id}/status")
def set_status(
    request: Request,
    tenant_id: int,
    status: str = Form(...),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None or status not in (TENANT_ACTIVE, TENANT_LOCKED):
        raise HTTPException(404, "Firma nicht gefunden.")
    old = tenant.status
    tenant.status = status
    audit.log(db, action="status", entity="firma", entity_id=tenant.id, tenant_id=tenant.id, user_id=user.id,
              changes={"status": {"alt": old, "neu": status}}, ip=anmeldung.client_ip(request))
    db.commit()
    text = "gesperrt" if status == TENANT_LOCKED else "entsperrt"
    return redirect("/admin", f"Firma „{tenant.name}“ wurde {text}.")
