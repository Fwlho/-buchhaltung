"""Jede Route muss eine Schutz-Abhängigkeit haben (siehe app/deps.py) – damit
eine Rechteprüfung nicht vergessen werden kann."""
from fastapi.routing import APIRoute

from app.deps import csrf_protect
from app.main import app


def _all_routes() -> list[APIRoute]:
    routes = []
    for entry in app.routes:
        if isinstance(entry, APIRoute):
            routes.append(entry)
        elif hasattr(entry, "original_router"):  # eingebundener Router (FastAPI ≥ 0.150)
            routes.extend(r for r in entry.original_router.routes if isinstance(r, APIRoute))
        elif hasattr(entry, "routes"):
            routes.extend(r for r in entry.routes if isinstance(r, APIRoute))
    return routes


def _calls(dependant) -> list:
    found = []
    for dep in dependant.dependencies:
        found.append(dep.call)
        found.extend(_calls(dep))
    return found


def test_routen_gefunden():
    paths = {r.path for r in _all_routes()}
    assert {"/login", "/", "/einstellungen/{key}", "/benutzer", "/admin"} <= paths


def test_jede_route_hat_schutz():
    missing = []
    for route in _all_routes():
        guards = [getattr(c, "_guard", None) for c in _calls(route.dependant)]
        if not any(guards):
            missing.append(f"{sorted(route.methods)} {route.path}")
    assert not missing, "Routen ohne Schutz-Abhängigkeit: " + ", ".join(missing)


def test_csrf_global_aktiv():
    assert any(d.dependency is csrf_protect for d in app.router.dependencies)
