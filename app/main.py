"""FastAPI-App: Routen, Fehlerseiten, Sicherheits-Header, Ersteinrichtungs-Sperre."""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import BASE_DIR, get_settings
from app.database import SessionLocal
from app.deps import AuthRedirect, csrf_protect
from app.routers import (
    admin,
    admin_freigabe,
    angebote,
    anmeldung,
    auftraege,
    auswertung,
    bank,
    belege,
    benutzer,
    datensicherung,
    einladung,
    einstellungen,
    ersteinrichtung,
    export,
    mahnwesen,
    rechnungen,
    rechnungs_import,
    stammdaten,
    start,
    vertraege,
)
from app.security import new_token
from app.templating import CSRF_COOKIE, render

log = logging.getLogger("app")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker = None
    if get_settings().background_jobs:
        from app.worker import BackgroundWorker

        worker = BackgroundWorker()
        worker.start()
    yield
    if worker is not None:
        worker.stop()


app = FastAPI(title="Buchhaltung & Angebote", docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(csrf_protect)], lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# rechnungs_import vor rechnungen: /rechnungen/import darf nicht als /rechnungen/{doc_id} gelesen werden
for module in (start, anmeldung, admin_freigabe, ersteinrichtung, einladung, admin, einstellungen, benutzer, stammdaten, belege,
               rechnungs_import, rechnungen, angebote, auftraege, auswertung, bank, mahnwesen, export, vertraege,
               datensicherung):
    app.include_router(module.router)

_SETUP_FREE_PREFIXES = ("/static", "/ersteinrichtung", "/health", "/favicon.ico")
_setup_done = False


def _needs_setup() -> bool:
    global _setup_done
    if _setup_done:
        return False
    with SessionLocal() as db:
        if ersteinrichtung.setup_needed(db):
            return True
    _setup_done = True
    return False


@app.middleware("http")
async def base_middleware(request: Request, call_next):
    if not request.url.path.startswith(_SETUP_FREE_PREFIXES) and _needs_setup():
        return RedirectResponse("/ersteinrichtung", status_code=303)

    token = request.cookies.get(CSRF_COOKIE) or new_token()
    request.state.csrf_token = token
    response = await call_next(request)
    if request.cookies.get(CSRF_COOKIE) != token:
        response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="lax",
                            secure=get_settings().cookie_secure, path="/")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Einladungs-Links enthalten Tokens – nicht an fremde Seiten weitergeben
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'",
    )
    if request.url.path != "/health" and not request.url.path.startswith("/static"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ----------------------------------------------------------------- Fehler
@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, exc: AuthRedirect):
    return RedirectResponse(exc.url, status_code=303)


_TITLES = {400: "Ungültige Anfrage", 403: "Kein Zugriff", 404: "Nicht gefunden", 405: "Nicht erlaubt",
           429: "Zu viele Anfragen"}
# Standardtexte von Starlette auf Deutsch
_ENGLISH = {"Not Found": "Diese Seite gibt es nicht.", "Method Not Allowed": "Diese Aktion ist hier nicht erlaubt."}


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path == "/health":
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    detail = exc.detail if isinstance(exc.detail, str) else "Fehler"
    detail = _ENGLISH.get(detail, detail)
    return render(request, "fehler.html", status_code=exc.status_code,
                  title=_TITLES.get(exc.status_code, "Fehler"), message=detail)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return render(request, "fehler.html", status_code=400, title="Ungültige Eingabe",
                  message="Die Eingabe war unvollständig oder ungültig. Bitte zurückgehen und erneut versuchen.")


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    log.exception("Unerwarteter Fehler bei %s", request.url.path)
    # Die Datenbank-Sitzung ist hier schon geschlossen – Firma/Benutzer daher nicht mehr anzeigen,
    # sonst scheitert auch die Fehlerseite (DetachedInstanceError)
    return render(request, "fehler.html", status_code=500, title="Interner Fehler", ctx=None, nav_user=None,
                  message="Es ist ein unerwarteter Fehler aufgetreten. Bitte erneut versuchen.")
