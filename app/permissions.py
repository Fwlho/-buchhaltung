"""Module, Rechtestufen und Rollen-Vorlagen.

Rollen sind nur Vorlagen: Beim Einladen bzw. Bearbeiten eines Benutzers werden die
Rechte aus der Rolle vorbelegt und können pro Modul angepasst werden.
Ausnahme: Die Rolle "inhaber" hat immer alle Rechte (damit sich eine Firma
nicht selbst aussperren kann).
"""
from collections.abc import Iterable

NONE = "keine"
READ = "lesen"
EDIT = "bearbeiten"
LEVELS = [NONE, READ, EDIT]
LEVEL_LABELS = {NONE: "kein Zugriff", READ: "lesen", EDIT: "bearbeiten"}

MODULES: list[tuple[str, str]] = [
    ("dashboard", "Übersicht"),
    ("rechnungen_eingang", "Eingangsrechnungen"),
    ("rechnungen_ausgang", "Ausgangsrechnungen"),
    ("pruefbereich", "Prüfbereich"),
    ("kunden", "Kunden & Lieferanten"),
    ("angebote", "Angebote"),
    ("auftraege", "Aufträge (Projektübersicht)"),
    ("katalog", "Artikel- und Leistungskatalog"),
    ("euer", "EÜR & Auswertungen"),
    ("steuern", "Steuern"),
    ("bank", "Bankabgleich"),
    ("mahnwesen", "Mahnwesen"),
    ("export", "Export für Steuerberater"),
    ("einstellungen", "Einstellungen"),
    ("benutzer", "Benutzerverwaltung"),
]
MODULE_KEYS = [key for key, _ in MODULES]
MODULE_LABELS = dict(MODULES)

EXTRA_RIGHTS: list[tuple[str, str]] = [
    ("festschreiben", "Belege festschreiben und stornieren"),
    ("kalkulation_intern", "Interne Kalkulation (Kosten, Marge) sehen"),
]
EXTRA_RIGHT_KEYS = [key for key, _ in EXTRA_RIGHTS]

ROLE_OWNER = "inhaber"
ROLES: list[tuple[str, str]] = [
    (ROLE_OWNER, "Inhaber"),
    ("buchhaltung", "Buchhaltung"),
    ("mitarbeiter", "Mitarbeiter"),
    ("steuerberater", "Steuerberater"),
]
ROLE_KEYS = [key for key, _ in ROLES]
ROLE_LABELS = dict(ROLES)


def _levels(edit: Iterable[str] = (), read: Iterable[str] = ()) -> dict[str, str]:
    perms = {key: NONE for key in MODULE_KEYS}
    for key in read:
        perms[key] = READ
    for key in edit:
        perms[key] = EDIT
    return perms


def role_template(role: str) -> tuple[dict[str, str], list[str]]:
    """Standardrechte (Module, Sonderrechte) einer Rolle."""
    if role == ROLE_OWNER:
        return _levels(edit=MODULE_KEYS), list(EXTRA_RIGHT_KEYS)
    if role == "buchhaltung":
        return (
            _levels(
                edit=["rechnungen_eingang", "rechnungen_ausgang", "pruefbereich", "kunden",
                      "steuern", "euer", "bank", "mahnwesen", "export"],
                read=["dashboard", "angebote", "auftraege", "katalog"],
            ),
            ["festschreiben"],
        )
    if role == "mitarbeiter":
        return (
            _levels(edit=["angebote", "auftraege", "kunden", "rechnungen_ausgang"], read=["dashboard", "katalog"]),
            [],
        )
    if role == "steuerberater":
        readable = [k for k in MODULE_KEYS if k not in ("einstellungen", "benutzer")]
        return _levels(read=readable), []
    raise ValueError(f"Unbekannte Rolle: {role}")


def clean_permissions(raw: dict) -> dict[str, str]:
    """Nur bekannte Module und Stufen übernehmen, Rest = kein Zugriff."""
    return {key: raw.get(key) if raw.get(key) in LEVELS else NONE for key in MODULE_KEYS}


def clean_extra_rights(raw: Iterable[str]) -> list[str]:
    return [key for key in EXTRA_RIGHT_KEYS if key in set(raw)]


def has_module(role: str, permissions: dict, module: str, edit: bool = False) -> bool:
    if module not in MODULE_KEYS:
        raise ValueError(f"Unbekanntes Modul: {module}")
    if role == ROLE_OWNER:
        return True
    level = (permissions or {}).get(module, NONE)
    if edit:
        return level == EDIT
    return level in (READ, EDIT)


def has_extra(role: str, extra_rights: Iterable[str], right: str) -> bool:
    if role == ROLE_OWNER:
        return True
    return right in set(extra_rights or [])
