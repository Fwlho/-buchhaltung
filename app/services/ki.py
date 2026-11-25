"""Anbindung an Claude (Anthropic) für KI-Vorschläge.

Nur aktiv, wenn die Firma es in den Einstellungen einschaltet (Gruppe „ki“) und ein API-Schlüssel
vorhanden ist (verschlüsselt in den Einstellungen oder ANTHROPIC_API_KEY in der .env).
Die KI macht ausschließlich Vorschläge – übernommen und festgeschrieben wird immer von einem Menschen.
In Tests wird ``ask_json`` per monkeypatch ersetzt – nie die echte Schnittstelle aufrufen.
"""
import json
import logging
import os
import re
from typing import Any

from sqlalchemy.orm import Session

from app.services import einstellungen

log = logging.getLogger("app.ki")

MODEL = "claude-opus-5-5"
TIMEOUT_SECONDS = 60.0
MAX_TEXT = 6000  # Zeichen Belegtext, die höchstens übermittelt werden


class KiError(Exception):
    """KI nicht erreichbar oder Antwort unbrauchbar – der Beleg bleibt dann einfach ohne Vorschlag."""


def api_key(db: Session) -> str:
    return einstellungen.get_secret(db, "ki.api_key") or os.environ.get("ANTHROPIC_API_KEY", "")


def enabled(db: Session) -> bool:
    return bool(einstellungen.get(db, "ki.aktiv")) and bool(api_key(db))


# Bankverbindungen, E-Mail-Adressen und Telefonnummern braucht die KI für Vorschläge nicht
_REDACT = [
    (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b"), "[IBAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[E-Mail]"),
    (re.compile(r"(?:\+49|\b0)[\d\s/()-]{7,}\d"), "[Telefon]"),
]


def minimize(text: str | None, limit: int = MAX_TEXT) -> str:
    """Belegtext für die Übermittlung: ohne Bankdaten/Kontaktdaten, gekürzt (Datensparsamkeit)."""
    text = text or ""
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def ask_json(db: Session, *, system: str, user: str | list[dict], schema: dict[str, Any], max_tokens: int = 4000,
             effort: str = "low") -> dict:
    """Eine Anfrage an Claude mit fest vorgegebenem JSON-Antwortformat. Wirft KiError.

    ``user``: Text oder Liste von Inhaltsblöcken (z. B. Bild/PDF des Belegs + Text, siehe ki_auslese.py).
    """
    key = api_key(db)
    if not key:
        raise KiError("Kein API-Schlüssel für die KI hinterlegt.")
    try:
        import anthropic
    except ImportError as exc:  # Paket fehlt – run.py installiert es beim nächsten Start nach
        raise KiError("Das Paket „anthropic“ ist nicht installiert. Bitte das Programm neu starten.") from exc

    client = anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=1)
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            # Lehnt das Modell ab, beantwortet automatisch ein anderes (serverseitig)
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise KiError("Der API-Schlüssel für die KI ist ungültig.") from exc
    except anthropic.PermissionDeniedError as exc:
        raise KiError("Der API-Schlüssel hat keine Berechtigung für diese Anfrage.") from exc
    except anthropic.RateLimitError as exc:
        raise KiError("Die KI ist gerade ausgelastet. Bitte später erneut versuchen.") from exc
    except anthropic.APIStatusError as exc:
        raise KiError(f"Die KI hat einen Fehler gemeldet ({exc.status_code}).") from exc
    except anthropic.APITimeoutError as exc:
        raise KiError("Die KI hat nicht rechtzeitig geantwortet.") from exc
    except anthropic.APIConnectionError as exc:
        raise KiError("Die KI ist nicht erreichbar (Internetverbindung?).") from exc

    if response.stop_reason == "refusal":
        raise KiError("Die KI hat diese Anfrage abgelehnt.")
    if response.stop_reason == "max_tokens":
        raise KiError("Die Antwort der KI war unvollständig.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("Unlesbare KI-Antwort (Anfrage %s)", getattr(response, "_request_id", "?"))
        raise KiError("Die Antwort der KI war nicht lesbar.") from exc
