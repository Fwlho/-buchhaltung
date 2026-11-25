"""Benutzerverwaltung einer Firma: einladen, Rolle und Rechte anpassen, sperren, Zugriffsmatrix.

Memberships/Invitations sind Plattform-Tabellen ohne automatischen Firmen-Filter –
deshalb wird hier bei JEDER Abfrage ausdrücklich nach ctx.tenant_id gefiltert.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db, utcnow
from app.deps import TenantContext, require_module
from app.models import Invitation, Membership, User
from app.permissions import (
    EDIT,
    EXTRA_RIGHTS,
    LEVEL_LABELS,
    LEVELS,
    MODULES,
    NONE,
    READ,
    ROLE_LABELS,
    ROLE_OWNER,
    ROLES,
    clean_extra_rights,
    clean_permissions,
    role_template,
)
from app.services import anmeldung, audit
from app.services.pruefungen import email_valid
from app.templating import redirect, render

router = APIRouter(prefix="/benutzer")

users_read = require_module("benutzer")
users_edit = require_module("benutzer", edit=True)

_LEVEL_RANK = {NONE: 0, READ: 1, EDIT: 2}


def _members(db: Session, tenant_id: int) -> list[Membership]:
    stmt = (
        select(Membership).join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant_id).order_by(User.name)
    )
    return list(db.scalars(stmt))


def _open_invitations(db: Session, tenant_id: int) -> list[Invitation]:
    """Noch nicht angenommene und nicht zurückgezogene Einladungen – auch abgelaufene (Link neu erzeugen)."""
    stmt = select(Invitation).where(Invitation.tenant_id == tenant_id).order_by(Invitation.created_at.desc())
    return [inv for inv in db.scalars(stmt) if inv.accepted_at is None and inv.revoked_at is None]


def _get_membership(db: Session, ctx: TenantContext, membership_id: int) -> Membership:
    m = db.scalar(select(Membership).where(Membership.id == membership_id, Membership.tenant_id == ctx.tenant_id))
    if m is None:
        raise HTTPException(404, "Benutzer nicht gefunden.")
    return m


def _active_owner_count(db: Session, tenant_id: int) -> int:
    return sum(1 for m in _members(db, tenant_id) if m.role == ROLE_OWNER and m.is_active)


def _within_own_rights(ctx: TenantContext, role: str, permissions: dict, extras: list[str]) -> str | None:
    """Niemand darf mehr Rechte vergeben, als er selbst hat (Inhaber dürfen alles)."""
    if ctx.role == ROLE_OWNER:
        return None
    if role == ROLE_OWNER:
        return "Nur Inhaber können die Rolle „Inhaber“ vergeben."
    for module, level in permissions.items():
        own = EDIT if ctx.can(module, edit=True) else (READ if ctx.can(module) else NONE)
        if _LEVEL_RANK[level] > _LEVEL_RANK[own]:
            return "Sie können keine Rechte vergeben, die Sie selbst nicht haben."
    if any(not ctx.has_extra(x) for x in extras):
        return "Sie können keine Sonderrechte vergeben, die Sie selbst nicht haben."
    return None


def _change_problem(db: Session, ctx: TenantContext, m: Membership, role: str, permissions: dict,
                    extras: list[str], is_active: bool) -> str | None:
    """Gemeinsame Regeln für „Rechte bearbeiten“ und die Zugriffsmatrix."""
    if m.role == ROLE_OWNER and ctx.role != ROLE_OWNER:
        return "Nur Inhaber können Inhaber bearbeiten."
    problem = _within_own_rights(ctx, role, permissions, extras)
    if problem:
        return problem
    if m.user_id == ctx.user.id and (not is_active or role != m.role):
        return "Die eigene Rolle und den eigenen Zugang können Sie nicht selbst ändern."
    if m.user_id == ctx.user.id and role != ROLE_OWNER and permissions.get("benutzer") != EDIT:
        return "Ihr eigenes Recht auf die Benutzerverwaltung können Sie nicht selbst entfernen."
    losing_owner = m.role == ROLE_OWNER and m.is_active and (role != ROLE_OWNER or not is_active)
    if losing_owner and _active_owner_count(db, ctx.tenant_id) <= 1:
        return "Die Firma braucht mindestens einen aktiven Inhaber."
    return None


def _apply_change(request: Request, db: Session, ctx: TenantContext, m: Membership, role: str, permissions: dict,
                  extras: list[str], is_active: bool) -> bool:
    """Rechte übernehmen und ins Änderungsprotokoll schreiben. True, wenn sich etwas geändert hat."""
    old = {"rolle": m.role, "rechte": m.permissions, "sonderrechte": m.extra_rights, "aktiv": m.is_active}
    m.role = role
    m.permissions = permissions
    m.extra_rights = extras
    m.is_active = is_active
    new = {"rolle": m.role, "rechte": m.permissions, "sonderrechte": m.extra_rights, "aktiv": m.is_active}
    changes = audit.diff(old, new)
    if changes:
        audit.log(db, action="rechte_geaendert", entity="benutzer", entity_id=m.user_id, tenant_id=ctx.tenant_id,
                  user_id=ctx.user.id, changes=changes, ip=anmeldung.client_ip(request))
    return bool(changes)


def _page(request: Request, db: Session, ctx: TenantContext, status_code: int = 200, **extra):
    return render(
        request, "benutzer.html", status_code=status_code,
        members=_members(db, ctx.tenant_id), invitations=_open_invitations(db, ctx.tenant_id),
        roles=ROLES, role_labels=ROLE_LABELS, can_edit=ctx.can("benutzer", edit=True),
        form=extra.pop("form", {}), errors=extra.pop("errors", {}), **extra,
    )


@router.get("")
def list_users(request: Request, ctx: TenantContext = Depends(users_read), db: Session = Depends(get_db)):
    return _page(request, db, ctx)


@router.post("/einladen")
def invite(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    role: str = Form("mitarbeiter"),
    ctx: TenantContext = Depends(users_edit),
    db: Session = Depends(get_db),
):
    errors = {}
    if not email_valid(email):
        errors["email"] = "Bitte eine gültige E-Mail-Adresse angeben."
    if role not in ROLE_LABELS:
        errors["role"] = "Bitte eine Rolle auswählen."
    email_norm = anmeldung.normalize_email(email)
    existing_user = db.scalar(select(User).where(User.email == email_norm))
    if existing_user is not None:
        m = db.scalar(select(Membership).where(Membership.user_id == existing_user.id,
                                               Membership.tenant_id == ctx.tenant_id))
        if m is not None and m.is_active:
            errors["email"] = "Diese Person gehört bereits zur Firma."
    if not errors:
        perms, extras = role_template(role)
        problem = _within_own_rights(ctx, role, perms, extras)
        if problem:
            errors["role"] = problem
    if errors:
        return _page(request, db, ctx, status_code=400, form={"name": name, "email": email, "role": role},
                     errors=errors)

    inv, token = anmeldung.create_invitation(
        db, tenant_id=ctx.tenant_id, email=email_norm, name=name, role=role,
        permissions=perms, extra_rights=extras, created_by_id=ctx.user.id,
    )
    db.flush()
    audit.log(db, action="eingeladen", entity="benutzer", entity_id=email_norm, tenant_id=ctx.tenant_id,
              user_id=ctx.user.id, changes={"rolle": role}, ip=anmeldung.client_ip(request))
    link = str(request.url_for("invitation_form", token=token))
    ok, message = anmeldung.send_invitation_mail(db, inv, link)
    db.commit()
    return _page(request, db, ctx, new_link=link, new_link_for=email_norm, info=message,
                 info_kind="ok" if ok else "fehler")


@router.post("/einladungen/{invitation_id}/neuer-link")
def renew_invitation(
    request: Request,
    invitation_id: int,
    ctx: TenantContext = Depends(users_edit),
    db: Session = Depends(get_db),
):
    """Neuen Einladungs-Link anzeigen (der alte wird ungültig) und die Einladungs-Mail erneut senden bzw. lokal
    ablegen. Gedacht für den lokalen Betrieb ohne E-Mail-Versand und für abgelaufene Einladungen."""
    inv = db.scalar(select(Invitation).where(Invitation.id == invitation_id, Invitation.tenant_id == ctx.tenant_id))
    if inv is None or inv.accepted_at is not None or inv.revoked_at is not None:
        raise HTTPException(404, "Einladung nicht gefunden.")
    if inv.role == ROLE_OWNER and ctx.role != ROLE_OWNER:
        raise HTTPException(403, "Nur Inhaber können diese Einladung erneuern.")
    token = anmeldung.renew_invitation(inv)
    audit.log(db, action="einladung_erneuert", entity="benutzer", entity_id=inv.email, tenant_id=ctx.tenant_id,
              user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    link = str(request.url_for("invitation_form", token=token))
    ok, message = anmeldung.send_invitation_mail(db, inv, link)
    db.commit()
    return _page(request, db, ctx, new_link=link, new_link_for=inv.email,
                 info="Neuer Einladungs-Link erzeugt – der bisherige Link gilt nicht mehr. " + message,
                 info_kind="ok" if ok else "fehler")


@router.post("/einladungen/{invitation_id}/zurueckziehen")
def revoke_invitation(
    request: Request,
    invitation_id: int,
    ctx: TenantContext = Depends(users_edit),
    db: Session = Depends(get_db),
):
    inv = db.scalar(select(Invitation).where(Invitation.id == invitation_id, Invitation.tenant_id == ctx.tenant_id))
    if inv is None:
        raise HTTPException(404, "Einladung nicht gefunden.")
    if inv.role == ROLE_OWNER and ctx.role != ROLE_OWNER:
        raise HTTPException(403, "Nur Inhaber können diese Einladung zurückziehen.")
    inv.revoked_at = utcnow()
    audit.log(db, action="einladung_zurueckgezogen", entity="benutzer", entity_id=inv.email,
              tenant_id=ctx.tenant_id, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    db.commit()
    return redirect("/benutzer", f"Die Einladung an {inv.email} wurde zurückgezogen.")


def _matrix_page(request: Request, db: Session, ctx: TenantContext, status_code: int = 200,
                 posted: dict | None = None, errors: list[str] | None = None):
    """Zeilen = Personen, Spalten = Bereiche und Sonderrechte. ``posted``: Eingaben nach einem Fehler."""
    rows = []
    for m in _members(db, ctx.tenant_id):
        perms, extras = (m.permissions, m.extra_rights) if posted is None or m.id not in posted else posted[m.id]
        rows.append({"m": m, "owner": m.role == ROLE_OWNER, "perms": perms, "extras": extras,
                     "editable": ctx.can("benutzer", edit=True) and m.role != ROLE_OWNER})
    return render(request, "benutzer_matrix.html", status_code=status_code, rows=rows, modules=MODULES,
                  levels=LEVELS, level_labels=LEVEL_LABELS, extra_rights=EXTRA_RIGHTS, role_labels=ROLE_LABELS,
                  can_edit=ctx.can("benutzer", edit=True), errors=errors or [])


@router.get("/matrix")
def matrix(request: Request, ctx: TenantContext = Depends(users_read), db: Session = Depends(get_db)):
    return _matrix_page(request, db, ctx)


@router.post("/matrix")
async def matrix_save(request: Request, ctx: TenantContext = Depends(users_edit), db: Session = Depends(get_db)):
    form = await request.form()
    posted, errors, changed = {}, [], 0
    for m in _members(db, ctx.tenant_id):
        if m.role == ROLE_OWNER or f"zeile_{m.id}" not in form:
            continue  # Inhaber haben immer alle Rechte; Zeile nicht im Formular = unverändert
        permissions = clean_permissions({key: form.get(f"p_{m.id}_{key}") for key, _ in MODULES})
        extras = clean_extra_rights(form.getlist(f"x_{m.id}"))
        posted[m.id] = (permissions, extras)
        if permissions == clean_permissions(m.permissions) and extras == clean_extra_rights(m.extra_rights):
            continue
        problem = _change_problem(db, ctx, m, m.role, permissions, extras, m.is_active)
        if problem:
            errors.append(f"{m.user.name}: {problem}")
            continue
        changed += _apply_change(request, db, ctx, m, m.role, permissions, extras, m.is_active)
    if errors:
        db.rollback()  # alles oder nichts – keine halb gespeicherte Matrix
        return _matrix_page(request, db, ctx, status_code=400, posted=posted, errors=errors)
    db.commit()
    if not changed:
        return redirect("/benutzer/matrix", "Keine Änderungen.")
    return redirect("/benutzer/matrix", f"Zugriffsmatrix gespeichert ({changed} Person(en) geändert).")


@router.post("/{membership_id}/app-zuruecksetzen")
def reset_app(request: Request, membership_id: int, ctx: TenantContext = Depends(users_edit),
              db: Session = Depends(get_db)):
    """Authenticator-App einer Person zurücksetzen (Handy und Notfall-Codes verloren). Die Person meldet sich
    danach mit Passwort + Code per E-Mail an und muss die App sofort neu einrichten (Pflicht)."""
    from sqlalchemy import delete as sql_delete

    from app.models import UserSession
    from app.services import totp

    m = _get_membership(db, ctx, membership_id)
    if not totp.enabled(m.user):
        return redirect(f"/benutzer/{m.id}", "Für diese Person ist keine Authenticator-App eingerichtet.", "hinweis")
    totp.disable(m.user)
    if m.user.id != ctx.user.id:   # alle Anmeldungen der Person beenden (die eigene bleibt bestehen)
        db.execute(sql_delete(UserSession).where(UserSession.user_id == m.user.id))
    audit.log(db, action="authenticator_app_zurueckgesetzt", entity="benutzer", entity_id=m.user.id,
              tenant_id=ctx.tenant_id, user_id=ctx.user.id, ip=anmeldung.client_ip(request))
    db.commit()
    return redirect(f"/benutzer/{m.id}", f"Authenticator-App von {m.user.name} zurückgesetzt – bei der nächsten "
                                         "Anmeldung richtet die Person sie neu ein.")


@router.get("/{membership_id}")
def edit_form(request: Request, membership_id: int, ctx: TenantContext = Depends(users_edit),
              db: Session = Depends(get_db)):
    m = _get_membership(db, ctx, membership_id)
    return render(request, "benutzer_bearbeiten.html", m=m, roles=ROLES, modules=MODULES, levels=LEVELS,
                  level_labels=LEVEL_LABELS, extra_rights=EXTRA_RIGHTS, error=None)


@router.post("/{membership_id}")
async def edit_save(request: Request, membership_id: int, ctx: TenantContext = Depends(users_edit),
                    db: Session = Depends(get_db)):
    m = _get_membership(db, ctx, membership_id)
    form = await request.form()
    role = str(form.get("role") or m.role)
    if role not in ROLE_LABELS:
        raise HTTPException(400, "Unbekannte Rolle.")

    if form.get("aktion") == "vorlage":
        permissions, extras = role_template(role)
    else:
        permissions = clean_permissions({key: form.get(f"perm_{key}") for key, _ in MODULES})
        extras = clean_extra_rights(form.getlist("extra"))
    is_active = bool(form.get("is_active"))

    def fail(message: str):
        return render(request, "benutzer_bearbeiten.html", status_code=400, m=m, roles=ROLES, modules=MODULES,
                      levels=LEVELS, level_labels=LEVEL_LABELS, extra_rights=EXTRA_RIGHTS, error=message)

    problem = _change_problem(db, ctx, m, role, permissions, extras, is_active)
    if problem:
        return fail(problem)
    _apply_change(request, db, ctx, m, role, permissions, extras, is_active)
    db.commit()
    if form.get("aktion") == "vorlage":
        return redirect(f"/benutzer/{m.id}", f"Rechte aus der Rolle „{ROLE_LABELS[role]}“ übernommen.")
    return redirect("/benutzer", f"Änderungen für {m.user.name} gespeichert.")
