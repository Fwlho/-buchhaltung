"""Kontovorschläge: Lieferant, aus eigenen Buchungen gelernt, KI (Claude – hier immer nachgebildet)."""
import pytest
from sqlalchemy import select

from app.database import tenant_scope
from app.models import AuditLog, Tenant
from app.services import ki, kontierung
from tests.conftest import post
from tests.test_eingangsrechnungen import PDF_EINFACH, _all_docs, _form_of, _ready, accepted_doc, make_pdf, upload

TANKEN_A = """Aral Tankstelle Nordring
Rechnungsnummer: T-1001
Rechnungsdatum: 03.03.2026
Superbenzin E10 42,5 Liter
Nettobetrag 60,00 €
zzgl. 19 % MwSt. 11,40 €
Gesamtbetrag 71,40 €
"""
TANKEN_B = """Shell Station Ostallee
Rechnungsnummer: S-77
Rechnungsdatum: 05.03.2026
Superbenzin E10 30 Liter Tankstelle
Nettobetrag 40,00 €
zzgl. 19 % MwSt. 7,60 €
Gesamtbetrag 47,60 €
"""


@pytest.fixture
def no_real_ki(monkeypatch):
    """Sicherheitsnetz: ein Test darf nie die echte Schnittstelle erreichen."""
    def forbidden(*args, **kwargs):
        raise AssertionError("echte KI-Anfrage im Test")

    monkeypatch.setattr(ki, "ask_json", forbidden)


@pytest.fixture
def fake_ki(monkeypatch):
    calls: list[dict] = []
    answer = {"konto": "4930", "sicherheit": "hoch", "begruendung": "Büromaterial (Kopierpapier)."}

    def ask(db, *, system, user, schema, max_tokens=4000):
        calls.append({"system": system, "user": user, "schema": schema})
        if isinstance(answer.get("fehler"), Exception):
            raise answer["fehler"]
        return dict(answer)

    monkeypatch.setattr(ki, "ask_json", ask)
    return calls, answer


def _ki_on(client, **extra):
    r = post(client, "/einstellungen/ki", {"ki.lernen": "1", "ki.aktiv": "1", "ki.api_key": "sk-test-123", **extra})
    assert r.status_code == 200 and "gespeichert" in r.text, r.text


# ------------------------------------------------------------- reine Funktionen
def test_lernen_aehnlichster_beleg():
    examples = [({"superbenzin", "liter", "aral"}, "4530"), ({"kopierpapier", "toner", "schulz"}, "4930"),
                ({"miete", "gewerberaum", "februar"}, "4210")]
    assert kontierung.learn(examples, {"superbenzin", "liter", "shell"}) == ("4530", ["liter", "superbenzin"])
    assert kontierung.learn(examples, {"superbenzin", "shell"}) is None          # nur ein gemeinsames Wort
    # gleich ähnlich zu zwei Konten -> lieber kein Vorschlag
    tie = [({"wartung", "service"}, "4806"), ({"wartung", "service"}, "4540")]
    assert kontierung.learn(tie, {"wartung", "service"}) is None
    assert kontierung.learn([], {"x"}) is None


def test_woerter_ohne_fuellwoerter():
    assert kontierung.words("Rechnung Nr. 5 – Superbenzin, Liter; Gesamtbetrag zzgl. MwSt") == {"superbenzin", "liter"}


def test_datensparsamkeit():
    text = ("IBAN DE02 1203 0000 0000 2020 51\nmail: info@schulz.example\nTel. 040 / 123 456-78\n"
            "Kopierpapier A4" + " x" * 5000)
    clean = ki.minimize(text)
    assert "DE02" not in clean and "@" not in clean and "123 456" not in clean
    assert "Kopierpapier" in clean and len(clean) <= ki.MAX_TEXT


# -------------------------------------------------------- Stufe 2: gelernt
def test_gelernter_vorschlag_beim_upload(client, db, no_real_ki):
    _ready(client)
    accepted_doc(client, db, make_pdf(TANKEN_A), "a.pdf", account="4530")
    upload(client, ("b.pdf", make_pdf(TANKEN_B)))
    doc = _all_docs(db)[-1]
    assert doc.account == "4530" and doc.account_source == kontierung.SOURCE_LEARNED
    assert "account" in doc.review_fields and doc.needs_review
    page = client.get(f"/pruefbereich/{doc.id}").text
    assert "Aus früheren Buchungen gelernt" in page and "superbenzin" in page

    # Nach dem Speichern durch einen Menschen ist der Hinweis erledigt
    post(client, f"/pruefbereich/{doc.id}", _form_of(doc))
    db.expire_all()
    doc = _all_docs(db)[-1]
    assert doc.account_hint is None and "account" not in doc.review_fields


def test_lernen_abschaltbar(client, db, no_real_ki):
    _ready(client)
    post(client, "/einstellungen/ki", {})   # beide Haken aus
    accepted_doc(client, db, make_pdf(TANKEN_A), "a.pdf", account="4530")
    upload(client, ("b.pdf", make_pdf(TANKEN_B)))
    doc = _all_docs(db)[-1]
    assert doc.account is None and doc.account_source is None


def test_lieferant_geht_vor_und_ist_nicht_gelb(client, db, no_real_ki):
    _ready(client)
    accepted_doc(client, db, make_pdf(TANKEN_A), "a.pdf", account="4530")
    upload(client, ("b.pdf", make_pdf(TANKEN_A.replace("T-1001", "T-1002"))))
    doc = _all_docs(db)[-1]
    assert doc.account == "4530" and doc.account_source == kontierung.SOURCE_SUPPLIER
    assert "account" not in doc.review_fields


# --------------------------------------------------------------- Stufe 3: KI
def test_ki_nur_wenn_eingeschaltet(client, db, no_real_ki, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _ready(client)
    upload(client, ("a.pdf", PDF_EINFACH))          # KI aus -> keine Anfrage (sonst AssertionError)
    assert _all_docs(db)[-1].account is None
    r = post(client, "/einstellungen/ki", {"ki.lernen": "1", "ki.aktiv": "1"})
    assert r.status_code == 400 and "Für KI-Vorschläge nötig" in r.text


def test_ki_vorschlag_nach_upload(client, db, fake_ki):
    calls, _ = fake_ki
    _ready(client)
    _ki_on(client)
    r = upload(client, ("a.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    assert doc.account == "4930" and doc.account_source == kontierung.SOURCE_KI
    assert "account" in doc.review_fields
    assert "KI-Vorschlag (Claude): Büromaterial" in r.text
    # Übermittelt werden Kontenliste, Absender und Belegtext – ohne Bankverbindung
    assert len(calls) == 1
    sent = calls[0]["user"]
    assert "4930" in sent and "Kopierpapier" in sent and "DE02" not in sent
    assert "keins" in calls[0]["schema"]["properties"]["konto"]["enum"]
    with tenant_scope(db, doc.tenant_id):
        assert db.scalar(select(AuditLog).where(AuditLog.action == "konto_vorgeschlagen")) is not None


def test_ki_ohne_sicheren_vorschlag_und_knopf(client, db, fake_ki):
    calls, answer = fake_ki
    answer.update(konto="4930", sicherheit="niedrig")
    _ready(client)
    _ki_on(client)
    upload(client, ("a.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    assert doc.account is None and doc.account_source == kontierung.SOURCE_KI_NONE
    page = client.get(f"/pruefbereich/{doc.id}").text
    assert "kein sicheres Konto gefunden" in page and "Konto per KI vorschlagen" in page

    # Hintergrund-Abruf fragt nicht erneut …
    tenant_id = db.scalar(select(Tenant.id))
    with tenant_scope(db, tenant_id):
        assert kontierung.ki_nachtragen(db) == 0
    assert len(calls) == 1
    # … der Knopf schon
    answer.update(sicherheit="hoch")
    r = post(client, f"/belege/{doc.id}/konto-vorschlagen")
    assert "Die KI hat ein Konto vorgeschlagen" in r.text and len(calls) == 2
    db.expire_all()
    assert _all_docs(db)[-1].account == "4930"


def test_ki_fehler_blockiert_nichts(client, db, fake_ki):
    _, answer = fake_ki
    answer["fehler"] = ki.KiError("Die KI ist nicht erreichbar (Internetverbindung?).")
    _ready(client)
    _ki_on(client)
    r = upload(client, ("a.pdf", PDF_EINFACH))
    assert r.status_code == 200
    doc = _all_docs(db)[-1]
    assert doc.account is None and doc.account_source == kontierung.SOURCE_KI_ERROR
    assert "nicht erreichbar" in client.get(f"/pruefbereich/{doc.id}").text


def test_ki_unbekanntes_konto_wird_verworfen(client, db, fake_ki):
    _, answer = fake_ki
    answer["konto"] = "9999"
    _ready(client)
    _ki_on(client)
    upload(client, ("a.pdf", PDF_EINFACH))
    assert _all_docs(db)[-1].account is None


def test_ki_bei_festgeschriebenem_beleg_nicht_moeglich(client, db, fake_ki):
    _ready(client)
    doc = accepted_doc(client, db)
    post(client, f"/eingangsrechnungen/{doc.id}/festschreiben")
    assert post(client, f"/belege/{doc.id}/konto-vorschlagen").status_code == 403


# ------------------------------------------------- Aufbau der echten Anfrage
def _claude_stub(monkeypatch, reply: dict, status: int = 200) -> list:
    """Ersetzt die Internetverbindung des Anthropic-Pakets – prüft, was gesendet würde."""
    import json

    import anthropic
    import httpx2

    sent = []

    def handler(request):
        sent.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        return httpx2.Response(status, json=reply)

    real = anthropic.Anthropic

    def client(**kwargs):
        return real(**kwargs, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))

    monkeypatch.setattr(anthropic, "Anthropic", client)
    return sent


def _message(text: str, stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": ki.MODEL,
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def test_anfrage_an_claude(client, db, monkeypatch):
    _ready(client)
    _ki_on(client)
    sent = _claude_stub(monkeypatch, _message('{"konto": "4930", "sicherheit": "hoch", "begruendung": "Papier."}'))
    upload(client, ("a.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    assert doc.account == "4930" and doc.account_source == kontierung.SOURCE_KI
    body, headers = sent[0]["body"], sent[0]["headers"]
    assert sent[0]["url"].endswith("/v1/messages")
    # Schlüssel aus den Einstellungen entschlüsselt (Paket-Version 2 sendet ihn als Bearer-Token)
    assert "sk-test-123" in (headers.get("x-api-key") or headers.get("authorization", ""))
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "DE02" not in body["messages"][0]["content"]


@pytest.mark.parametrize("reply, status, expected", [
    (_message("", "refusal"), 200, "abgelehnt"),
    (_message("kein json"), 200, "nicht lesbar"),
    ({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, 401, "ungültig"),
])
def test_claude_fehler_werden_verstaendlich(client, db, monkeypatch, reply, status, expected):
    _ready(client)
    _ki_on(client)
    _claude_stub(monkeypatch, reply, status)
    upload(client, ("a.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    assert doc.account is None and doc.account_source == kontierung.SOURCE_KI_ERROR
    assert expected in doc.account_hint
