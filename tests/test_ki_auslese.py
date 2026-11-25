"""KI-gestützte Belegprüfung (Claude liest EINEN Beleg) – die KI ist hier immer nachgebildet."""
import io
import json
from datetime import date
from decimal import Decimal as D

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import tenant_scope
from app.models import Document, KiReading, Tenant
from app.services import ki, ki_auslese, konten, ocr
from tests.conftest import post
from tests.test_eingangsrechnungen import PDF_EINFACH, _all_docs, _form_of, _ready, make_pdf, upload

ACCOUNTS = konten.accounts_for("SKR03")


def answer(**overrides) -> dict:
    """Typische KI-Antwort zur Beispielrechnung „Bürobedarf Schulz“ (499,80 € brutto)."""
    data = {
        "ist_beleg": True, "belegart": "rechnung", "absender": "Bürobedarf Schulz GmbH",
        "absender_anschrift": "Papierweg 3, 20095 Hamburg", "steuernummer_oder_ustid": "DE987654321",
        "empfaenger": "Muster Service", "rechnungsnummer": "RE-2026-0815", "rechnungsdatum": "2026-03-14",
        "leistungsdatum": "2026-03-14", "faellig_am": "2026-03-28",
        "betraege": [{"satz": "19", "netto": "420.00", "ust": "79.80", "brutto": "499.80"}],
        "brutto_gesamt": "499.80", "skonto_prozent": "2", "skonto_bis": "2026-03-21", "zahlungsart": "ueberweisung",
        "positionen": [{"text": "Kopierpapier A4", "menge": "10", "einzelpreis": "42.00", "gesamt": "420.00"}],
        "konto": "4930", "konto_begruendung": "Büromaterial (Kopierpapier).",
        "sicherheit": {"absender": "hoch", "rechnungsnummer": "hoch", "rechnungsdatum": "hoch", "betraege": "hoch",
                       "konto": "mittel"},
        "hinweise": [],
    }
    data.update(overrides)
    return data


def checked(**overrides):
    return ki_auslese.check(answer(**overrides), ACCOUNTS, own_name="Muster Service", today=date(2026, 4, 1))


# ------------------------------------------------------------------ Prüfregeln (reine Funktion)
def test_gute_antwort_gruen_und_gelb():
    c = checked()
    assert c.states == {"partner": "ok", "doc_date": "ok", "lines": "ok", "account": "pruefen", "number": "ok"}
    assert [ln.gross for ln in c.lines] == [D("499.80")] and c.values["account"] == "4930"
    assert not [ch for ch in c.checks if ch["stufe"] != "ok"]
    assert c.proposal["positionen"][0]["gesamt"] == "420.00"


def test_rechenfehler_und_summen_werden_rot():
    c = checked(betraege=[{"satz": "19", "netto": "420.00", "ust": "70.00", "brutto": "499.80"}])
    assert c.states["lines"] == "fehlt" and c.lines == []
    assert any("passen nicht zusammen" in ch["text"] for ch in c.checks)
    c = checked(brutto_gesamt="510.00")
    assert c.states["lines"] == "fehlt" and any("ergibt nicht den Gesamtbetrag" in ch["text"] for ch in c.checks)
    c = checked(positionen=[{"text": "Papier", "menge": "1", "einzelpreis": "", "gesamt": "300.00"}])
    assert any("Positionen ergeben" in ch["text"] for ch in c.checks)


def test_datum_konto_und_kein_beleg():
    c = checked(rechnungsdatum="2027-01-01")
    assert c.states["doc_date"] == "fehlt" and any("Zukunft" in ch["text"] for ch in c.checks)
    assert checked(rechnungsdatum="2010-01-01").states["doc_date"] == "fehlt"
    assert checked(rechnungsdatum="").states["doc_date"] == "fehlt"
    assert checked(konto="keins").states["account"] == "fehlt"
    c = checked(ist_beleg=False)
    assert not c.is_document and any("keinen Beleg erkannt" in ch["text"] for ch in c.checks)


def test_pflichtangaben_ueber_und_unter_250_euro():
    c = checked(rechnungsnummer="", steuernummer_oder_ustid="", leistungsdatum="", empfaenger="Irgendwer AG")
    texts = " ".join(ch["text"] for ch in c.checks)
    assert c.states["number"] == "fehlt" and "Rechnungsnummer fehlt" in texts
    assert "USt-IdNr. des Ausstellers fehlt" in texts and "Leistungsdatum" in texts and "Irgendwer AG" in texts
    bon = checked(rechnungsnummer="", steuernummer_oder_ustid="", leistungsdatum="", empfaenger="",
                  betraege=[{"satz": "19", "netto": "", "ust": "", "brutto": "23.80"}], brutto_gesamt="23.80",
                  zahlungsart="karte", positionen=[])
    assert "number" not in bon.states                                  # Kleinbetrag: keine Nummer nötig
    assert [ch["stufe"] for ch in bon.checks] == ["ok"] and "mit Karte bezahlt" in bon.checks[0]["text"]
    assert checked(steuernummer_oder_ustid="DE12").checks[0]["text"].startswith("Steuernummer/USt-IdNr.")


def test_betraege_lesen():
    assert ki_auslese.money("1.234,56") == D("1234.56") and ki_auslese.money("1234.56") == D("1234.56")
    assert ki_auslese.money("1,234.56") == D("1234.56") and ki_auslese.money("12,5 €") == D("12.50")
    assert ki_auslese.money("") is None and ki_auslese.money("abc") is None
    assert ki_auslese.a_date("14.03.2026") == date(2026, 3, 14) and ki_auslese.a_date("2026-02-30") is None


def test_e_rechnungen_werden_nicht_gelesen():
    doc = Document(direction="eingang", pending=True, state="entwurf", file_id=1, e_invoice_format="CII")
    assert not ki_auslese.can_read(doc)
    doc.e_invoice_format = None
    assert ki_auslese.can_read(doc)


# ------------------------------------------------------------------ Ablauf mit nachgebildeter KI
@pytest.fixture
def fake_ki(monkeypatch):
    calls: list[dict] = []
    state = {"answer": answer(), "error": None}

    def ask(db, *, system, user, schema, max_tokens=4000, effort="low"):
        calls.append({"system": system, "user": user, "schema": schema, "effort": effort})
        if state["error"]:
            raise ki.KiError(state["error"])
        if "ist_beleg" in schema["properties"]:
            return json.loads(json.dumps(state["answer"]))
        return {"konto": "4980", "sicherheit": "hoch", "begruendung": "Kontierung"}

    monkeypatch.setattr(ki, "ask_json", ask)
    monkeypatch.setattr(ocr, "available", lambda: False)   # Scans/Fotos: keine echte Texterkennung im Test
    return calls, state


def _ki_on(client, auslesen=True):
    data = {"ki.lernen": "1", "ki.aktiv": "1", "ki.api_key": "sk-test-123"}
    if auslesen:
        data["ki.auslesen"] = "1"
    r = post(client, "/einstellungen/ki", data)
    assert "gespeichert" in r.text, r.text


def _readings(db) -> list[KiReading]:
    db.expire_all()
    return list(db.scalars(select(KiReading).execution_options(all_tenants=True).order_by(KiReading.id)))


def test_upload_wird_ausgelesen_ampel_und_protokoll(client, db, fake_ki):
    calls, _ = fake_ki
    _ready(client)
    _ki_on(client)
    r = upload(client, ("r.pdf", PDF_EINFACH))
    assert len(calls) == 1                                             # EIN Aufruf, Konto inklusive
    sent = calls[0]["user"]
    text = " ".join(b.get("text", "") for b in sent)
    assert "Kopierpapier" in text and "[IBAN]" in text and "DE02 1203" not in text   # Bankdaten entfernt
    assert "Muster Service" in text and "4930" in text                 # eigene Firma + Kontenliste
    assert all(b["type"] == "text" for b in sent) and calls[0]["effort"] == "low"

    doc = _all_docs(db)[-1]
    doc_id = doc.id
    assert doc.pending and doc.partner_name == "Bürobedarf Schulz GmbH" and doc.account == "4930"
    assert doc.account_source == "ki" and doc.field_states["partner"] == "ok"
    assert doc.field_states["account"] == "pruefen" and "account" in doc.review_fields
    [reading] = _readings(db)
    assert reading.status == "vorschlag" and reading.mode == "text" and json.loads(reading.raw)["konto"] == "4930"

    page = client.get(f"/pruefbereich/{doc_id}").text
    assert "KI-Auslese" in page and "ampel-ok" in page and "Erneut mit KI auslesen" in page
    assert "KI gelesen" in client.get("/pruefbereich").text

    # Mensch ändert das Konto und übernimmt → Protokoll hält Vorschlag und Freigabe fest
    doc = _all_docs(db)[-1]
    r = post(client, f"/pruefbereich/{doc.id}", _form_of(doc, account="4980", aktion="uebernehmen"))
    [reading] = _readings(db)
    assert reading.status == "uebernommen" and reading.final["konto"] == "4980" and reading.decided_by_id
    assert reading.proposal["konto"] == "4930"
    page = client.get(f"/eingangsrechnungen/{doc_id}").text
    assert "(geändert)" in page and "Übernommen" in page and "Erneut mit KI auslesen" not in page


def test_foto_und_scan_gehen_als_bild(client, db, fake_ki):
    calls, _ = fake_ki
    _ready(client)
    _ki_on(client)
    img = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(img, "PNG")
    upload(client, ("bon.png", img.getvalue()))
    block = calls[-1]["user"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg"
    with Image.open(io.BytesIO(__import__("base64").b64decode(block["source"]["data"]))) as small:
        assert max(small.size) <= ki_auslese.MAX_IMAGE_SIDE                # verkleinert
    assert calls[-1]["effort"] == "medium"

    upload(client, ("scan.pdf", make_pdf("")))                           # PDF ohne Textebene
    block = calls[-1]["user"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"
    assert [r.mode for r in _readings(db)] == ["bild", "pdf"]


def test_fehler_und_kein_beleg(client, db, fake_ki):
    _, state = fake_ki
    _ready(client)
    _ki_on(client)
    state["error"] = "Die KI ist nicht erreichbar (Internetverbindung?)."
    upload(client, ("r.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    doc_id = doc.id
    assert doc.pending and _readings(db)[-1].status == "fehler"
    assert "KI: nicht gelesen" in client.get("/pruefbereich").text
    assert "KI konnte nicht lesen" in client.get(f"/pruefbereich/{doc_id}").text

    state["error"], state["answer"] = None, answer(ist_beleg=False)
    r = post(client, f"/belege/{doc_id}/ki-auslesen")
    assert "keinen Beleg erkannt" in r.text
    assert "kein Beleg?" in client.get("/pruefbereich").text


def test_ohne_automatik_nur_per_knopf_und_ueberschreibt(client, db, fake_ki):
    calls, state = fake_ki
    _ready(client)
    _ki_on(client, auslesen=False)
    upload(client, ("r.pdf", PDF_EINFACH))
    assert not any("ist_beleg" in c["schema"]["properties"] for c in calls)     # nicht automatisch
    doc = _all_docs(db)[-1]
    post(client, f"/pruefbereich/{doc.id}", _form_of(doc, partner_name="Von Hand", aktion="speichern"))
    r = post(client, f"/belege/{doc.id}/ki-auslesen")
    assert "Von der KI ausgelesen" in r.text
    db.expire_all()
    assert _all_docs(db)[-1].partner_name == "Bürobedarf Schulz GmbH"      # Knopf überschreibt bewusst


def test_hintergrund_ueberschreibt_keine_menschlichen_eingaben(client, db, fake_ki):
    _, state = fake_ki
    _ready(client)
    _ki_on(client, auslesen=False)
    upload(client, ("r.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    post(client, f"/pruefbereich/{doc.id}", _form_of(doc, partner_name="Von Hand", number="", aktion="speichern"))
    _ki_on(client)                                                        # jetzt automatisch
    tenant = db.scalar(select(Tenant))
    with tenant_scope(db, tenant.id):
        assert ki_auslese.nachtragen(db) == 1
        assert ki_auslese.nachtragen(db) == 0                             # nie doppelt
    db.expire_all()
    doc = _all_docs(db)[-1]
    assert doc.partner_name == "Von Hand" and doc.number == "RE-2026-0815"   # nur Leeres ergänzt
    assert "partner" not in (doc.field_states or {}) and doc.field_states.get("number") == "ok"


def test_rechte_und_mandantentrennung(client, outbox, db, fake_ki):
    from tests.test_rechte import _invite

    _ready(client)
    _ki_on(client)
    upload(client, ("r.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    tenant = db.scalar(select(Tenant))
    with tenant_scope(db, tenant.id):
        assert len(db.scalars(select(KiReading)).all()) == 1
    with tenant_scope(db, tenant.id + 999):
        assert db.scalars(select(KiReading)).all() == []
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert post(stb, f"/belege/{doc.id}/ki-auslesen").status_code == 403
