"""Startseite, Firmenwechsel, eigenes Konto, Health-Check, Tab-Symbol."""
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import TenantContext, membership_for, public, require_module, require_user
from app.models import User, UserSession
from app.security import hash_password, password_problem, verify_password
from app.services import admin_freigabe, anmeldung, audit, belege, einstellungen, totp, vertraege
from app.templating import redirect, render

router = APIRouter()


@router.get("/health", dependencies=[Depends(public)])
def health():
    return {"status": "ok"}


@router.get("/favicon.ico", dependencies=[Depends(public)])
def favicon():
    """Browser fragen das Tab-Symbol auch bei PDFs (ohne HTML-Kopf) unter /favicon.ico ab."""
    return RedirectResponse("/static/favicon.svg", status_code=301)


@router.get("/")
def start(request: Request, ctx: TenantContext = Depends(require_module("dashboard")), db: Session = Depends(get_db)):
    from app.routers.auswertung import _period_context, period_from_query
    from app.services import angebote, auswertung

    period, error = period_from_query(request)
    data: dict = {"missing": einstellungen.missing_required(db), "values": einstellungen.load(db),
                  "pending": belege.pending_count(db) if ctx.can("pruefbereich") else 0, "period_error": error,
                  **_period_context(request, period)}
    # Finanzkennzahlen nur mit Recht „EÜR“ (Entscheidung 28.10.2026)
    if ctx.can("euer"):
        data["euer"] = auswertung.euer(db, period)
        data["chart"] = _chart(auswertung.monthly(db, period.year))
    if ctx.can("steuern"):
        if auswertung.small_business(db):
            data["ku"] = auswertung.ku_monitor(db, date.today().year)
        else:
            vat_period = auswertung.period_containing(auswertung.vat_period_kind(db), date.today())
            data["vat"] = auswertung.ust(db, vat_period)
    if ctx.can("rechnungen_ausgang") or ctx.can("rechnungen_eingang"):
        data["open"] = auswertung.open_items(db)
    if ctx.can("rechnungen_ausgang") and vertraege.enabled(db):
        data["contract_drafts"] = vertraege.draft_count(db)
    if ctx.can("angebote"):
        data["open_quotes"] = len(angebote.listing(db, status="versendet")) + len(angebote.listing(db, status="entwurf"))
    return render(request, "start.html", **data)


def _chart(months: list[dict]) -> dict:
    """Balken je Monat (Einnahmen/Ausgaben brutto) als Höhen für ein einfaches SVG."""
    highest = max([m["income"] for m in months] + [m["expense"] for m in months] + [Decimal(1)])
    height = 120
    bars = [{"label": m["label"], "income": m["income"], "expense": m["expense"],
             "h_income": int(m["income"] / highest * height), "h_expense": int(m["expense"] / highest * height)}
            for m in months]
    return {"bars": bars, "height": height, "empty": highest == Decimal(1) and not any(m["income"] for m in months)}


@router.post("/firma-wechseln")
def switch_tenant(
    request: Request,
    tenant_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    membership = membership_for(db, user.id, tenant_id)
    if membership is None or not membership.is_active:
        raise HTTPException(403, "Kein Zugriff auf diese Firma.")
    sess: UserSession = request.state.session
    sess.active_tenant_id = tenant_id
    user.last_tenant_id = tenant_id
    db.commit()
    return redirect("/", f"Gewechselt zu {membership.tenant.name}.")


@router.get("/konto")
def account(request: Request, user: User = Depends(require_user)):
    return _account(request, user)


@router.post("/konto/passwort")
def change_password(
    request: Request,
    old_password: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    errors = {}
    if not verify_password(user.password_hash, old_password):
        errors["old_password"] = "Das bisherige Passwort ist falsch."
    problem = password_problem(password, password2)
    if not problem and admin_freigabe.check(user, password):
        problem = "Das Anmelde-Passwort muss sich vom Admin-Passwort unterscheiden."
    if problem:
        errors["password"] = problem
    if errors:
        return _account(request, user, 400, errors=errors)
    user.password_hash = hash_password(password)
    # Alle anderen Sitzungen dieses Benutzers beenden
    current: UserSession = request.state.session
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.id != current.id))
    audit.log(db, action="passwort_geaendert", entity="benutzer", entity_id=user.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    db.commit()
    return redirect("/konto", "Ihr Passwort wurde geändert. Andere Anmeldungen wurden beendet.")


def _account(request: Request, user: User, status_code: int = 200, **extra):
    return render(request, "konto.html", status_code=status_code, errors=extra.pop("errors", {}),
                  totp_on=totp.enabled(user), totp_rest=len(user.totp_recovery or []), **extra)


@router.post("/konto/app/einrichten")
def app_setup(request: Request, password: str = Form(""), user: User = Depends(require_user),
              db: Session = Depends(get_db)):
    """Schritt 1: mit dem Anmelde-Passwort bestätigen, dann QR-Code anzeigen (noch nicht aktiv)."""
    if totp.enabled(user):
        return redirect("/konto", "Die Authenticator-App ist bereits eingerichtet.", "hinweis")
    if not verify_password(user.password_hash, password):
        return _account(request, user, 400, errors={"app_password": "Das Anmelde-Passwort ist falsch."})
    secret = totp.start_setup(user)
    db.commit()
    return _account(request, user, app_secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))


@router.get("/konto/app/qr.svg")
def app_qr(user: User = Depends(require_user)):
    """QR-Code des noch nicht bestätigten Geheimnisses – nur für den angemeldeten Benutzer selbst."""
    secret = totp.secret_of(user)
    if secret is None or totp.enabled(user):
        raise HTTPException(404, "Kein Einrichtungsvorgang offen.")
    svg = totp.qr_svg(totp.uri(secret, user.email, "Buchhaltung"))
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store",
                                                              "Content-Security-Policy": "default-src 'none'"})


@router.post("/konto/app/bestaetigen")
def app_confirm(request: Request, code: str = Form(""), user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    """Schritt 2: ersten Code aus der App eingeben – erst dann ist die App-Anmeldung aktiv."""
    if totp.enabled(user) or not user.totp_secret:
        return redirect("/konto")
    codes = totp.confirm_setup(user, code)
    if codes is None:
        secret = totp.secret_of(user)
        return _account(request, user, 400, errors={"app_code": "Der Code passt nicht – Uhrzeit des Handys prüfen "
                                                                 "und den aktuellen Code eingeben."},
                        app_secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    audit.log(db, action="authenticator_app_eingerichtet", entity="benutzer", entity_id=user.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    db.commit()
    return _account(request, user, recovery_codes=codes)


@router.post("/konto/app/abschalten")
def app_disable(request: Request, password: str = Form(""), code: str = Form(""), user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    if not totp.enabled(user):
        return redirect("/konto")
    errors = {}
    if not verify_password(user.password_hash, password):
        errors["app_password"] = "Das Anmelde-Passwort ist falsch."
    else:
        ok, message = totp.check(db, user, code)
        if not ok:
            errors["app_code"] = message
    if errors:
        db.commit()  # Fehlversuche zählen
        return _account(request, user, 400, errors=errors)
    totp.disable(user)
    audit.log(db, action="authenticator_app_abgeschaltet", entity="benutzer", entity_id=user.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    db.commit()
    return redirect("/konto", "Die Authenticator-App ist abgeschaltet – die Anmeldung läuft wieder mit Code per E-Mail.")


@router.post("/konto/admin-passwort")
def change_admin_password(
    request: Request,
    old_admin_password: str = Form(""),
    admin_password: str = Form(""),
    admin_password2: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not admin_freigabe.has_password(user):
        return redirect(f"{admin_freigabe.PAGE}?weiter=/konto")
    errors = {}
    if admin_freigabe.lock_remaining(db, user):
        errors["old_admin_password"] = "Zu viele Fehlversuche. Bitte später erneut versuchen."
    elif not admin_freigabe.check(user, old_admin_password):
        if admin_freigabe.register_failure(db, user):
            audit.log(db, action="admin_passwort_gesperrt", entity="benutzer", entity_id=user.id, user_id=user.id,
                      ip=anmeldung.client_ip(request))
        db.commit()
        errors["old_admin_password"] = "Das bisherige Admin-Passwort ist falsch."
    problem = admin_freigabe.new_password_problem(user, admin_password, admin_password2)
    if problem:
        errors["admin_password"] = problem
    if errors:
        return _account(request, user, 400, errors=errors)
    admin_freigabe.clear_failures(db, user)
    admin_freigabe.set_password(db, user, admin_password, keep_session=request.state.session)
    audit.log(db, action="admin_passwort_geaendert", entity="benutzer", entity_id=user.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    db.commit()
    return redirect("/konto", "Ihr Admin-Passwort wurde geändert. Freigaben in anderen Anmeldungen wurden beendet.")
