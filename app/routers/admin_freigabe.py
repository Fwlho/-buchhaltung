"""Admin-Passwort: festlegen, eingeben (Freigabe), vergessen, Freigabe beenden.

Logik in app/services/admin_freigabe.py, die Abfrage selbst sitzt zentral in app/deps.py.
"""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models import User, UserSession
from app.security import verify_password
from app.services import admin_freigabe as svc
from app.services import anmeldung, audit
from app.templating import redirect, render

router = APIRouter(prefix=svc.PAGE)


def _page(request: Request, user: User, weiter: str, status_code: int = 200, **extra):
    mode = "eingeben" if svc.has_password(user) else "festlegen"
    return render(request, "admin_passwort.html", status_code=status_code, mode=mode,
                  weiter=svc.safe_next(weiter), errors=extra.pop("errors", {}), **extra)


def _locked_message(db: Session, user: User) -> str | None:
    remaining = svc.lock_remaining(db, user)
    if remaining:
        return f"Zu viele Fehlversuche. Bitte in etwa {max(1, remaining // 60)} Minute(n) erneut versuchen."
    return None


def _failure(request: Request, db: Session, user: User) -> None:
    if svc.register_failure(db, user):
        audit.log(db, action="admin_passwort_gesperrt", entity="benutzer", entity_id=user.id, user_id=user.id,
                  ip=anmeldung.client_ip(request))


@router.get("")
def form(request: Request, weiter: str = "/", verworfen: str = "", user: User = Depends(require_user)):
    return _page(request, user, weiter, verworfen=bool(verworfen))


@router.post("")
def unlock(
    request: Request,
    password: str = Form(""),
    weiter: str = Form("/"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not svc.has_password(user):
        return _page(request, user, weiter)
    locked = _locked_message(db, user)
    if locked:
        return _page(request, user, weiter, status_code=429, errors={"password": locked})
    if not svc.check(user, password):
        _failure(request, db, user)
        db.commit()
        return _page(request, user, weiter, status_code=401, errors={"password": "Das Admin-Passwort ist falsch."})
    svc.clear_failures(db, user)
    sess: UserSession = request.state.session
    svc.unlock(sess)
    audit.log(db, action="admin_freigegeben", entity="benutzer", entity_id=user.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    db.commit()
    return redirect(svc.safe_next(weiter))


@router.post("/festlegen")
def set_first(
    request: Request,
    login_password: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    weiter: str = Form("/"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Erstes Admin-Passwort – nur mit dem Anmelde-Passwort (eine offen gelassene Sitzung reicht nicht)."""
    if svc.has_password(user):
        return _page(request, user, weiter)
    locked = _locked_message(db, user)
    if locked:
        return _page(request, user, weiter, status_code=429, errors={"login_password": locked})
    errors = {}
    if not verify_password(user.password_hash, login_password):
        _failure(request, db, user)
        db.commit()
        errors["login_password"] = "Das Anmelde-Passwort ist falsch."
    problem = svc.new_password_problem(user, password, password2)
    if problem:
        errors["password"] = problem
    if errors:
        return _page(request, user, weiter, status_code=400, errors=errors)
    sess: UserSession = request.state.session
    svc.clear_failures(db, user)
    svc.set_password(db, user, password, keep_session=sess)
    svc.unlock(sess)
    audit.log(db, action="admin_passwort_festgelegt", entity="benutzer", entity_id=user.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    db.commit()
    return redirect(svc.safe_next(weiter), "Ihr Admin-Passwort ist festgelegt.")


@router.get("/vergessen")
def forgotten_form(request: Request, weiter: str = "/", user: User = Depends(require_user)):
    return render(request, "admin_passwort_vergessen.html", weiter=svc.safe_next(weiter), errors={})


@router.post("/vergessen/code")
def forgotten_code(request: Request, weiter: str = Form("/"), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    status, message = anmeldung.send_login_code(db, user)
    db.commit()
    return render(request, "admin_passwort_vergessen.html", status_code=200 if status != "fehler" else 502,
                  weiter=svc.safe_next(weiter), errors={}, info=message,
                  info_kind="fehler" if status == "fehler" else "ok", code_sent=status != "fehler")


@router.post("/vergessen")
def forgotten_reset(
    request: Request,
    code: str = Form(""),
    login_password: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    weiter: str = Form("/"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Neues Admin-Passwort mit Bestätigungscode per E-Mail + Anmelde-Passwort."""
    def fail(errors: dict, status_code: int = 400):
        return render(request, "admin_passwort_vergessen.html", status_code=status_code,
                      weiter=svc.safe_next(weiter), errors=errors, code_sent=True)

    locked = _locked_message(db, user)
    if locked:
        return fail({"code": locked}, 429)
    errors = {}
    if not verify_password(user.password_hash, login_password):
        _failure(request, db, user)
        errors["login_password"] = "Das Anmelde-Passwort ist falsch."
    problem = svc.new_password_problem(user, password, password2)
    if problem:
        errors["password"] = problem
    if not errors:  # Code erst zuletzt prüfen – ein richtiger Code wird dabei verbraucht
        ok, message = anmeldung.check_login_code(db, user, code)
        if not ok:
            errors["code"] = message
    if errors:
        db.commit()  # verbrauchte Code-Versuche und Fehlversuche zählen
        return fail(errors)
    sess: UserSession = request.state.session
    svc.clear_failures(db, user)
    svc.set_password(db, user, password, keep_session=sess)
    svc.unlock(sess)
    audit.log(db, action="admin_passwort_neu", entity="benutzer", entity_id=user.id, user_id=user.id,
              ip=anmeldung.client_ip(request))
    db.commit()
    return redirect(svc.safe_next(weiter), "Ihr neues Admin-Passwort ist festgelegt.")


@router.post("/sperren")
def end_unlock(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    svc.lock(request.state.session)
    db.commit()
    return redirect("/", "Die Admin-Bereiche sind wieder gesperrt.")
