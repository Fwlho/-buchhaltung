"""Login (Passwort + Bestätigungscode per E-Mail) und Logout."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import current_session, public
from app.models import User, UserSession
from app.services import anmeldung as svc
from app.services import audit, totp
from app.templating import redirect, render

router = APIRouter()


def _pending_user(request: Request, db: Session) -> User | None:
    sess = current_session(request, db)
    if sess is None or sess.stage != "code":
        return None
    return db.get(User, sess.user_id)


@router.get("/login", dependencies=[Depends(public)])
def login_form(request: Request, db: Session = Depends(get_db)):
    sess = current_session(request, db)
    if sess is not None and sess.stage == "ok":
        return redirect("/")
    return render(request, "login.html", email="")


@router.post("/login", dependencies=[Depends(public)])
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    ip = svc.client_ip(request)
    locked = svc.lock_remaining(db, ip, email)
    if locked:
        minutes = max(1, locked // 60)
        return render(request, "login.html", status_code=429, email=email,
                      error=f"Zu viele Fehlversuche. Bitte in etwa {minutes} Minute(n) erneut versuchen.")

    user = svc.check_password(db, email, password)
    if user is None:
        svc.register_failure(db, ip, email)
        audit.log(db, action="login_fehlgeschlagen", entity="benutzer", changes={"email": svc.normalize_email(email)},
                  ip=ip)
        db.commit()
        return render(request, "login.html", status_code=401, email=email,
                      error="E-Mail-Adresse oder Passwort ist falsch.")

    svc.clear_failures(db, ip, email)
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.stage == "code"))
    token = svc.create_session(db, user, "code", request)
    db.commit()

    if totp.enabled(user):  # Authenticator-App statt E-Mail-Code (ein gehacktes Postfach reicht dann nicht)
        response = redirect("/login/code", "Bitte den Code aus Ihrer Authenticator-App eingeben.")
        svc.set_session_cookie(response, token)
        return response
    status, message = svc.send_login_code(db, user)
    db.commit()
    if status == "fehler":
        svc.end_session(db, token)
        db.commit()
        return render(request, "login.html", status_code=503, email=email, error=message)
    response = redirect("/login/code", message, "ok" if status == "gesendet" else "hinweis")
    svc.set_session_cookie(response, token)
    return response


@router.get("/login/code", dependencies=[Depends(public)])
def code_form(request: Request, db: Session = Depends(get_db)):
    user = _pending_user(request, db)
    if user is None:
        return redirect("/login")
    return render(request, "login_code.html", email=user.email, app_mode=totp.enabled(user))


@router.post("/login/code", dependencies=[Depends(public)])
def code_submit(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    user = _pending_user(request, db)
    if user is None:
        return redirect("/login", "Die Anmeldung ist abgelaufen. Bitte erneut anmelden.", "fehler")
    app_mode = totp.enabled(user)
    ok, message = totp.check(db, user, code) if app_mode else svc.check_login_code(db, user, code)
    if not ok:
        db.commit()
        return render(request, "login_code.html", status_code=401, email=user.email, error=message,
                      app_mode=app_mode)

    svc.end_session(db, request.cookies.get(svc.SESSION_COOKIE))
    token = svc.complete_login(db, user, request)
    audit.log(db, action="login", entity="benutzer", entity_id=user.id, user_id=user.id, ip=svc.client_ip(request))
    db.commit()
    response = redirect("/", message or None, "hinweis") if message else redirect("/")
    svc.set_session_cookie(response, token)
    return response


@router.post("/login/code/neu", dependencies=[Depends(public)])
def code_resend(request: Request, db: Session = Depends(get_db)):
    user = _pending_user(request, db)
    if user is None:
        return redirect("/login")
    if totp.enabled(user):
        return redirect("/login/code", "Mit eingerichteter Authenticator-App gibt es keinen Code per E-Mail – "
                                       "bei verlorenem Handy einen Notfall-Code verwenden.", "hinweis")
    status, message = svc.send_login_code(db, user)
    db.commit()
    return redirect("/login/code", message, {"gesendet": "ok", "wartezeit": "hinweis"}.get(status, "fehler"))


@router.post("/logout", dependencies=[Depends(public)])
def logout(request: Request, db: Session = Depends(get_db)):
    svc.end_session(db, request.cookies.get(svc.SESSION_COOKIE))
    db.commit()
    response = redirect("/login", "Sie wurden abgemeldet.")
    response.delete_cookie(svc.SESSION_COOKIE, path="/")
    return response
