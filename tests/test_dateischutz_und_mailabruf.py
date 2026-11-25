"""Prüfbericht 04.11.2026, Befunde 1, 2 und 7:
- fremde Belegdateien (vor allem XML) dürfen im Browser nie als Webseite mit Skript laufen,
- neue Mails kommen immer an, auch wenn viele alte im Abruf-Ordner liegen bleiben,
- Mails mit nur unbrauchbaren Anhängen bleiben im Postfach und werden gemeldet."""
from sqlalchemy import select

from app.models import EmailImportLog
from app.services import email_import
from tests.conftest import post
from tests.test_eingangsrechnungen import PDF_EINFACH, PDF_ZWEI, _all_docs, _ready, upload
from tests.test_email import _docs, _setup_mail, mail, server  # noqa: F401

XSS_XML = (b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
           b'<script>document.title="XSS"</script></body></html>')


def _log(db):
    db.expire_all()
    return list(db.scalars(select(EmailImportLog).execution_options(all_tenants=True).order_by(EmailImportLog.id)))


# ------------------------------------------------------------ Dateischutz (Befund 1)
def test_xml_beleg_nur_als_text_und_ohne_skripte(client, db):
    _ready(client)
    upload(client, ("rechnung.xml", XSS_XML))
    doc = _all_docs(db)[-1]
    r = client.get(f"/pruefbereich/{doc.id}/datei")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    csp = r.headers["content-security-policy"]
    assert "sandbox" in csp and "default-src 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_pdf_vorschau_bleibt_einbettbar(client, db):
    _ready(client)
    upload(client, ("r.pdf", PDF_EINFACH))
    doc = _all_docs(db)[-1]
    r = client.get(f"/pruefbereich/{doc.id}/datei")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"  # sonst sperrt Chrome die PDF-Anzeige


# ------------------------------------------------------------ Mail-Abruf (Befund 2)
def test_neue_mail_kommt_an_auch_hinter_vielen_alten(client, db, server, monkeypatch):
    monkeypatch.setattr(email_import, "MAX_MAILS_PER_RUN", 5)
    _setup_mail(client, **{"imap.ordner_nach_import": ""})
    for i in range(12):
        server.add_mail("INBOX.Rechnungen", mail(subject=f"Newsletter {i}", attachments=()))
    server.add_mail("INBOX.Rechnungen", mail(subject="Echte Rechnung", message_id="<neu@x>"))
    r = post(client, "/pruefbereich/email-abruf")
    assert "mehr als 5 neue Mails" in r.text and not _docs(db)
    post(client, "/pruefbereich/email-abruf")
    post(client, "/pruefbereich/email-abruf")
    assert len(_docs(db)) == 1  # nach drei Abrufen ist die Rechnung da
    r = post(client, "/pruefbereich/email-abruf")
    assert "12 Mail(s) ohne PDF/XML-Anhang belassen" in r.text and "neue Mails" not in r.text
    assert len(server.mails("INBOX.Rechnungen")) == 13  # nichts verschoben, nichts gelöscht
    # bekannte Mails ohne Anhang werden nicht erneut vollständig geladen und nicht im Prüfbereich gelistet
    assert "Newsletter" not in client.get("/pruefbereich").text


def test_bereits_importierte_zaehlen_nicht_zur_hoechstzahl(client, db, server, monkeypatch):
    monkeypatch.setattr(email_import, "MAX_MAILS_PER_RUN", 2)
    _setup_mail(client, **{"imap.ordner_nach_import": ""})
    server.add_mail("INBOX.Rechnungen", mail(message_id="<a@x>"))
    server.add_mail("INBOX.Rechnungen", mail(message_id="<b@x>", attachments=(("r2.pdf", PDF_ZWEI),)))
    post(client, "/pruefbereich/email-abruf")
    assert len(_docs(db)) == 2
    server.add_mail("INBOX.Rechnungen", mail(message_id="<c@x>", subject="Ohne", attachments=()))
    r = post(client, "/pruefbereich/email-abruf")
    assert "2 bereits importierte Mail(s) übersprungen" in r.text and "neue Mails" not in r.text


# ------------------------------------------------------------ Abgelehnte Anhänge (Befund 7)
def test_mail_mit_unbrauchbarem_anhang_bleibt_liegen_und_wird_gemeldet(client, db, server):
    _setup_mail(client)
    server.add_mail("INBOX.Rechnungen", mail(attachments=(("rechnung.pdf", b"kein echtes PDF"),),
                                             message_id="<kaputt@x>"))
    r = post(client, "/pruefbereich/email-abruf")
    assert "1 Mail(s) mit unbrauchbarem Anhang belassen" in r.text
    assert not _docs(db)
    assert len(server.mails("INBOX.Rechnungen")) == 1 and not server.mails("INBOX.Verarbeitet")
    log = _log(db)
    assert len(log) == 1 and log[0].status == "abgelehnt" and "bleibt im Postfach" in log[0].note
    assert "nicht übernommen" in client.get("/pruefbereich").text
    # Zweiter Abruf: weder verschoben noch doppelt protokolliert
    post(client, "/pruefbereich/email-abruf")
    assert len(_log(db)) == 1 and len(server.mails("INBOX.Rechnungen")) == 1


def test_nur_doppelter_anhang_gilt_als_importiert(client, db, server):
    _setup_mail(client)
    server.add_mail("INBOX.Rechnungen", mail(message_id="<eins@x>"))
    server.add_mail("INBOX.Rechnungen", mail(message_id="<zwei@x>"))  # gleiche PDF noch einmal
    post(client, "/pruefbereich/email-abruf")
    assert len(_docs(db)) == 1
    assert [e.status for e in _log(db)] == ["importiert", "importiert"]
    assert len(server.mails("INBOX.Verarbeitet")) == 2
