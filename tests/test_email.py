"""E-Mail-Import (IMAP) und E-Mail-Entwürfe – mit simuliertem IMAP-Server."""
import email
import email.policy
from email.message import EmailMessage

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import tenant_scope
from app.models import Document, EmailImportLog
from app.services import email_entwurf, email_import, imap
from tests.conftest import complete_wizard, ersteinrichtung, post, read_stored
from tests.fake_imap import FakeIMAP
from tests.test_eingangsrechnungen import PDF_EINFACH, PDF_ZWEI
from tests.test_mandantentrennung import _two_tenants
from tests.test_rechnungen import _final
from tests.test_rechte import _invite

MAIL_SETTINGS = {"imap.host": "imap.example.org", "imap.port": "993", "imap.ssl": "1", "imap.benutzer": "postfach",
                 "imap.passwort": "geheim", "imap.ordner_eingang": "Rechnungen", "imap.ordner_nach_import": "Verarbeitet",
                 "imap.intervall_minuten": "10", "smtp.port": "465"}


def mail(subject="Rechnung März", sender="Bürobedarf Schulz <rechnung@schulz.example>", attachments=(("r.pdf", PDF_EINFACH),),
         message_id=None) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "postfach@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 16 Mar 2026 10:00:00 +0100"
    msg["Message-ID"] = message_id or email.utils.make_msgid()
    msg.set_content("Anbei die Rechnung.")
    for name, data in attachments:
        maintype, subtype = ("application", "xml") if name.endswith(".xml") else ("application", "pdf")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return bytes(msg)


@pytest.fixture
def server(monkeypatch):
    fake = FakeIMAP(["INBOX", "INBOX.Rechnungen", "INBOX.Verarbeitet", "INBOX.Drafts"],
                    special={"INBOX.Drafts": "\\Drafts"})
    monkeypatch.setattr(imap, "connect", lambda cfg: fake)
    return fake


def _setup_mail(client, **overrides):
    ersteinrichtung(client)
    complete_wizard(client)
    r = post(client, "/einstellungen/email", {**MAIL_SETTINGS, **overrides})
    assert "gespeichert" in r.text, r.text


def _docs(db):
    db.expire_all()
    return list(db.scalars(select(Document).options(selectinload(Document.file))
                           .execution_options(all_tenants=True, include_pending=True).order_by(Document.id)))


# -------------------------------------------------------------- Grundlagen
def test_imap_utf7():
    assert imap.utf7_encode("Entwürfe") == "Entw&APw-rfe"
    assert imap.utf7_decode("Entw&APw-rfe") == "Entwürfe"
    assert imap.utf7_encode("A&B") == "A&-B" and imap.utf7_decode("A&-B") == "A&B"
    assert imap.utf7_decode(imap.utf7_encode("Größe/Übersicht €")) == "Größe/Übersicht €"


def test_ordner_aufloesung():
    folders = ["INBOX", "INBOX.Rechnungen", "INBOX.Archiv.2026", "Sent"]
    assert imap.resolve("rechnungen", folders) == "INBOX.Rechnungen"
    assert imap.resolve("INBOX/Rechnungen", folders) == "INBOX.Rechnungen"
    assert imap.resolve("imap://info%40firma.de@imap.example.de/INBOX/Rechnungen", folders) == "INBOX.Rechnungen"
    assert imap.resolve("Archiv/2026", folders) == "INBOX.Archiv.2026"
    assert imap.resolve("Gibtsnicht", folders) is None
    assert imap.normalize_setting("Rech\r\nnungen") == "Rechnungen"  # keine eingeschleusten Befehle
    assert imap.quote('Mein "Ordner"') == '"Mein \\"Ordner\\""'


def test_platzhalter_sicher():
    values = {"nummer": "2026-001"}
    assert email_entwurf.fill("Rechnung {nummer} {unbekannt}", values) == "Rechnung 2026-001 {unbekannt}"
    assert email_entwurf.fill("{nummer.__class__}", values) == "{nummer.__class__}"


# -------------------------------------------------------------------- Import
def test_import_mit_move(client, db, server):
    _setup_mail(client)
    server.add_mail("INBOX.Rechnungen", mail())
    server.add_mail("INBOX.Rechnungen", mail(subject="Newsletter", attachments=()))
    r = post(client, "/pruefbereich/email-abruf")
    assert "1 Beleg(e) aus 1 Mail(s) in den Prüfbereich übernommen" in r.text
    assert "1 Mail(s) ohne PDF/XML-Anhang belassen" in r.text and "verschoben" in r.text
    doc = _docs(db)[0]
    assert doc.pending and doc.source == "email" and doc.number == "RE-2026-0815"
    assert "Per E-Mail von Bürobedarf Schulz" in doc.notes
    moved = server.mails("INBOX.Verarbeitet")
    assert len(moved) == 1 and b"application/pdf" in moved[0]["data"]
    remaining = server.mails("INBOX.Rechnungen")
    assert len(remaining) == 1 and b"Newsletter" in remaining[0]["data"]  # Mail ohne Anhang bleibt liegen
    assert "Bürobedarf Schulz" in client.get("/pruefbereich").text

    # Zweiter Abruf: nichts Neues, Newsletter bleibt weiter liegen
    r = post(client, "/pruefbereich/email-abruf")
    assert "1 Mail(s) ohne PDF/XML-Anhang belassen" in r.text and "Beleg(e) aus" not in r.text
    assert len(_docs(db)) == 1


def test_duplikatschutz_ohne_zielordner(client, db, server):
    _setup_mail(client, **{"imap.ordner_nach_import": ""})
    server.add_mail("INBOX.Rechnungen", mail(message_id="<a@x>"))
    post(client, "/pruefbereich/email-abruf")
    r = post(client, "/pruefbereich/email-abruf")
    assert "1 bereits importierte Mail(s) übersprungen" in r.text
    assert "kein Zielordner eingestellt" in r.text
    assert len(_docs(db)) == 1 and len(server.mails("INBOX.Rechnungen")) == 1
    assert "MOVE" not in server.commands and "COPY" not in server.commands


def test_copy_fallback_loescht_nur_eigene_mails(client, db, server):
    server.capabilities = ("IMAP4REV1", "UIDPLUS")
    _setup_mail(client)
    fremd = server.add_mail("INBOX.Rechnungen", mail(subject="Vom Nutzer gelöscht markiert", attachments=()),
                            flags={"\\Deleted"})
    server.add_mail("INBOX.Rechnungen", mail())
    r = post(client, "/pruefbereich/email-abruf")
    assert "1 Mail(s) nach „INBOX.Verarbeitet“ verschoben" in r.text
    assert [m["uid"] for m in server.mails("INBOX.Rechnungen")] == [fremd]  # fremde Mail bleibt unangetastet
    assert len(server.mails("INBOX.Verarbeitet")) == 1


def test_copy_nicht_bestaetigt_nichts_entfernt(client, db, server):
    server.capabilities = ("IMAP4REV1", "UIDPLUS")
    server.drop_copies = True
    _setup_mail(client)
    server.add_mail("INBOX.Rechnungen", mail())
    r = post(client, "/pruefbereich/email-abruf")
    assert "Sicherheitsabbruch" in r.text
    assert len(server.mails("INBOX.Rechnungen")) == 1
    assert "\\Deleted" not in server.mails("INBOX.Rechnungen")[0]["flags"]
    assert len(_docs(db)) == 1  # Beleg ist trotzdem importiert (und vor Doppel-Import geschützt)


def test_ohne_move_und_uidplus_wird_nicht_verschoben(client, db, server):
    server.capabilities = ("IMAP4REV1",)
    _setup_mail(client)
    server.add_mail("INBOX.Rechnungen", mail())
    r = post(client, "/pruefbereich/email-abruf")
    assert "zur Sicherheit wird nicht verschoben" in r.text
    assert len(server.mails("INBOX.Rechnungen")) == 1 and "COPY" not in server.commands


def test_ordner_werden_angelegt_und_abonniert(client, db, monkeypatch):
    fake = FakeIMAP(["INBOX", "INBOX.Sent"])
    monkeypatch.setattr(imap, "connect", lambda cfg: fake)
    _setup_mail(client)
    post(client, "/pruefbereich/email-abruf")
    # Alle Ordner liegen unter „INBOX.“ -> neue Ordner ebenfalls dort, mit Server-Trennzeichen, und abonniert
    assert {"INBOX.Rechnungen", "INBOX.Verarbeitet"} <= set(fake.boxes)
    assert {"INBOX.Rechnungen", "INBOX.Verarbeitet"} <= fake.subscribed


def test_ordner_ohne_inbox_praefix(client, db, monkeypatch):
    fake = FakeIMAP(["INBOX", "Sent", "Archiv"], delimiter="/")
    monkeypatch.setattr(imap, "connect", lambda cfg: fake)
    _setup_mail(client)
    post(client, "/pruefbereich/email-abruf")
    assert {"Rechnungen", "Verarbeitet"} <= set(fake.boxes) and {"Rechnungen", "Verarbeitet"} <= fake.subscribed


def test_xml_und_getarnte_anhaenge(client, db, server):
    _setup_mail(client)
    xml = b'<?xml version="1.0" encoding="UTF-8"?><Invoice xmlns="urn:oasis:names:specification:ubl"></Invoice>'
    server.add_mail("INBOX.Rechnungen", mail(attachments=(("rechnung.xml", xml), ("boese.pdf", b"MZ\x90\x00 exe"),
                                                         ("zwei.pdf", PDF_ZWEI))))
    r = post(client, "/pruefbereich/email-abruf")
    assert "2 Beleg(e) aus 1 Mail(s)" in r.text and "Dateityp nicht erlaubt" in r.text
    docs = _docs(db)
    assert {d.file.content_type for d in docs} == {"application/xml", "application/pdf"}
    # Attrappe ohne gültigen UBL-/CII-Namensraum: gespeichert, aber nicht als E-Rechnung gelesen
    assert "Keine bekannte E-Rechnung" in next(d for d in docs if d.file.content_type == "application/xml").notes


def test_falsches_passwort(client, db, monkeypatch):
    def fail(cfg):
        raise imap.ImapError("Anmeldung am Mailserver fehlgeschlagen: AUTHENTICATIONFAILED")

    monkeypatch.setattr(imap, "connect", fail)
    _setup_mail(client)
    r = post(client, "/pruefbereich/email-abruf")
    assert "AUTHENTICATIONFAILED" in r.text
    assert "AUTHENTICATIONFAILED" in client.get("/einstellungen/email/diagnose").text


def test_diagnose(client, db, server):
    _setup_mail(client, **{"imap.ordner_entwuerfe": ""})
    server.add_mail("INBOX.Rechnungen", mail())
    page = client.get("/einstellungen/email/diagnose").text
    for text in ("Verbindung und Anmeldung erfolgreich", "INBOX.Rechnungen", "INBOX.Drafts", "MOVE, UIDPLUS"):
        assert text in page, text


def test_hintergrund_abruf(client, db, server):
    from app import worker

    _setup_mail(client)
    server.add_mail("INBOX.Rechnungen", mail())
    assert worker.run_due_jobs() == 0  # automatischer Abruf ist aus
    post(client, "/einstellungen/email", {**MAIL_SETTINGS, "imap.aktiv": "1"})
    assert worker.run_due_jobs() == 1
    assert worker.run_due_jobs() == 0  # Intervall noch nicht vorbei
    assert len(_docs(db)) == 1


def test_mandantentrennung(client, outbox, db, server):
    client_b, tenant_a, tenant_b = _two_tenants(client, outbox, db)
    post(client, "/einstellungen/email", MAIL_SETTINGS)
    post(client_b, "/einstellungen/email", {**MAIL_SETTINGS, "imap.ordner_nach_import": ""})
    raw = mail(message_id="<gleich@x>")
    server.add_mail("INBOX.Rechnungen", raw)
    post(client_b, "/pruefbereich/email-abruf")   # B importiert und lässt liegen
    post(client, "/pruefbereich/email-abruf")     # A importiert dieselbe Mail auch (eigener Duplikatschutz)
    docs = _docs(db)
    assert sorted(d.tenant_id for d in docs) == sorted([tenant_a.id, tenant_b.id])
    logs = db.scalars(select(EmailImportLog).execution_options(all_tenants=True)).all()
    assert len(logs) == 2
    with tenant_scope(db, tenant_b.id):
        assert [log.tenant_id for log in email_import.recent_log(db)] == [tenant_b.id]
    # A hat verschoben, B nicht – die Mail liegt bei B weiterhin im Eingang (gleiches Testpostfach)
    assert len(server.mails("INBOX.Verarbeitet")) == 1


# ------------------------------------------------------------------ Entwürfe
def test_rechnung_als_entwurf_ins_postfach(client, db, server, outbox):
    _setup_mail(client)
    doc = _final(client, db)
    page = client.get(f"/rechnungen/{doc.id}").text
    assert f"Rechnung {doc.number}" in page and "info@krume.example" in page and "182,29 €" in page
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "info@krume.example", "subject": f"Rechnung {doc.number}",
                                                     "body": "Hallo", "aktion": "entwurf"})
    assert "Entwurf im Ordner „INBOX.Drafts“ angelegt" in r.text and "Entwurf erstellt am" in r.text
    stored = server.mails("INBOX.Drafts")[0]
    assert stored["flags"] == {"\\Draft", "\\Seen"}
    msg = email.message_from_bytes(stored["data"], policy=email.policy.default)
    assert msg["To"] == "info@krume.example" and msg["Subject"] == f"Rechnung {doc.number}"
    pdf = next(msg.iter_attachments())
    assert pdf.get_content_type() == "application/pdf"
    assert pdf.get_content() == read_stored(doc.file)
    assert outbox == []  # es wurde NICHTS versendet


def test_entwuerfe_ordner_mit_umlaut(client, db, monkeypatch):
    fake = FakeIMAP(["INBOX", "INBOX.Rechnungen", "INBOX.Entwürfe"])
    monkeypatch.setattr(imap, "connect", lambda cfg: fake)
    _setup_mail(client, **{"imap.ordner_entwuerfe": "Entwürfe"})
    doc = _final(client, db)
    post(client, f"/rechnungen/{doc.id}/email", {"to": "a@b.de", "subject": "x", "body": "y", "aktion": "entwurf"})
    assert len(fake.boxes["INBOX.Entw&APw-rfe"]) == 1


def test_entwuerfe_ordner_fehlt_und_eml(client, db, monkeypatch):
    fake = FakeIMAP(["INBOX"])
    monkeypatch.setattr(imap, "connect", lambda cfg: fake)
    _setup_mail(client)
    doc = _final(client, db)
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "a@b.de", "subject": "x", "body": "y", "aktion": "entwurf"})
    assert r.status_code == 400 and "Entwürfe-Ordner wurde nicht gefunden" in r.text
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "", "subject": "Rechnung", "body": "y", "aktion": "eml"})
    assert r.headers["content-type"].startswith("message/rfc822")
    assert "attachment" in r.headers["content-disposition"]
    assert b"Subject: Rechnung" in r.content
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "kaputt", "subject": "", "body": "", "aktion": "eml"})
    assert "gültige E-Mail" in r.text and "Betreff" in r.text


def test_entwurf_nur_fuer_festgeschriebene(client, db, server):
    from tests.test_rechnungen import _draft

    _setup_mail(client)
    draft = _draft(client, db)
    r = post(client, f"/rechnungen/{draft.id}/email", {"to": "a@b.de", "subject": "x", "aktion": "entwurf"})
    assert "Nur festgeschriebene" in r.text and server.mails("INBOX.Drafts") == []


def test_angebot_als_entwurf(client, db, server):
    from tests.test_angebote import _form, _new

    _setup_mail(client)
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q))
    page = client.get(f"/angebote/{q.id}").text
    assert f"Angebot {q.number}" in page
    r = post(client, f"/angebote/{q.id}/email", {"to": "info@krume.example", "subject": f"Angebot {q.number}",
                                                  "body": "Hallo", "aktion": "entwurf"})
    assert "Entwurf im Ordner" in r.text
    msg = email.message_from_bytes(server.mails("INBOX.Drafts")[0]["data"], policy=email.policy.default)
    assert next(msg.iter_attachments()).get_filename() == f"Angebot-{q.number}.pdf"


def test_rechte(client, outbox, db, server):
    _setup_mail(client)
    doc = _final(client, db)
    stb = _invite(client, outbox, "stb@example.org", "steuerberater")
    assert post(stb, "/pruefbereich/email-abruf").status_code == 403
    assert post(stb, f"/rechnungen/{doc.id}/email", {"to": "a@b.de", "subject": "x", "aktion": "eml"}).status_code == 403
    assert stb.get("/einstellungen/email/diagnose").status_code == 403
    assert server.mails("INBOX.Drafts") == []
