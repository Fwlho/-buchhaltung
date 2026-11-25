"""E-Mail direkt in Thunderbird/Outlook öffnen (nur lokal) – mit nachgebildetem Programmstart."""
import email
import email.policy
import os
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.services import mailprogramm
from tests.conftest import post, read_stored
from tests.test_rechnungen import _final, _ready

EXE = {"thunderbird": r"C:\Fake\thunderbird.exe", "outlook": r"C:\Fake\OUTLOOK.EXE"}


@pytest.fixture
def programme(monkeypatch, tmp_path):
    """Beide Programme „installiert“; Starts werden nur aufgezeichnet."""
    started = []
    monkeypatch.setattr(mailprogramm, "find", lambda program: EXE[program])
    monkeypatch.setattr(mailprogramm, "_launch", started.append)
    monkeypatch.setattr(mailprogramm, "_base_dir", lambda: tmp_path / "mail")
    return started


def thunderbird_parse(data: str) -> dict[str, str]:
    """Nachbau von GetArgs() aus Thunderbirds MsgComposeCommands.js (Version 153)."""
    sep, out, quote, prev = "\x01", "", "", ""
    for i, ch in enumerate(data):
        nxt = data[i + 1] if i < len(data) - 1 else ""
        if ch == quote and nxt in (",", ""):
            quote, out = "", out + ch
        elif ch in "'\"" and prev == "=":
            quote = quote or ch
            out += ch
        elif ch == ",":
            out += ch if quote else sep
        else:
            out += ch
        prev = ch
    args = {}
    for pair in reversed(out.split(sep)):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if value.startswith("'") and value.endswith("'"):
            args[key] = value[1:-1]
        else:
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            args[key] = unquote(value)
    return args


def test_rechnung_in_thunderbird(client, db, programme, outbox):
    _ready(client)
    doc = _final(client, db)
    page = client.get(f"/rechnungen/{doc.id}").text
    assert "In Thunderbird öffnen" in page and "In Outlook öffnen" in page
    body = "Sehr geehrte Damen und Herren,\nanbei Rechnung – 182,29 €.\n\nGrüße, Ihr 'Team'"
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "info@krume.example", "subject": f"Rechnung {doc.number}",
                                                     "body": body, "aktion": "thunderbird"})
    assert "in Thunderbird geöffnet" in r.text and "Entwurf erstellt am" in r.text
    [args] = programme
    assert args[:2] == [EXE["thunderbird"], "-compose"] and len(args) == 3
    parsed = thunderbird_parse(args[2])
    assert parsed["to"] == "info@krume.example" and parsed["subject"] == f"Rechnung {doc.number}"
    assert parsed["body"] == body                                     # Zeilenumbrüche, Komma, € und ' bleiben erhalten
    files = parsed["attachment"].split(",file:")
    assert len(files) == 1
    pdf = Path(files[0].replace("file:///", "").replace("/", os.sep)) if os.name == "nt" else Path(files[0][7:])
    assert pdf.read_bytes() == read_stored(doc.file)                  # entschlüsselte Kopie des Original-PDFs
    assert outbox == []                                               # nichts versendet


def test_thunderbird_text_kann_keine_anhaenge_einschleusen(client, db, programme):
    _ready(client)
    doc = _final(client, db)
    evil = "x',attachment='file:///C:/Windows/win.ini',to='boese@example.org"
    post(client, f"/rechnungen/{doc.id}/email", {"to": "a@b.de", "subject": evil, "body": evil,
                                                 "aktion": "thunderbird"})
    parsed = thunderbird_parse(programme[0][2])
    assert parsed["subject"] == evil and parsed["body"] == evil and parsed["to"] == "a@b.de"
    assert "win.ini" not in parsed["attachment"] and programme[0][2].count("attachment=") == 1


def test_rechnung_in_outlook(client, db, programme):
    _ready(client)
    doc = _final(client, db)
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "info@krume.example", "subject": "Rechnung",
                                                     "body": "Hallo", "aktion": "outlook"})
    assert "in Outlook geöffnet" in r.text
    [args] = programme
    assert args[:2] == [EXE["outlook"], "/eml"]
    msg = email.message_from_bytes(Path(args[2]).read_bytes(), policy=email.policy.default)
    assert msg["X-Unsent"] == "1" and msg["From"] is None           # Outlook öffnet sie als ungesendete Mail
    assert msg["To"] == "info@krume.example" and msg.get_body(("plain",)).get_content().strip() == "Hallo"
    assert next(msg.iter_attachments()).get_content() == read_stored(doc.file)


def test_angebot_und_mahnung_in_thunderbird(client, db, programme):
    from tests.test_angebote import _form, _new
    from tests.test_mahnwesen import _dunnings, _overdue

    _ready(client)
    q = _new(client, db)
    post(client, f"/angebote/{q.id}", _form(q))
    r = post(client, f"/angebote/{q.id}/email", {"to": "", "subject": f"Angebot {q.number}", "body": "Hallo",
                                                  "aktion": "thunderbird"})
    assert "in Thunderbird geöffnet" in r.text and "Als versendet markieren" in r.text
    parsed = thunderbird_parse(programme[-1][2])
    assert "to" not in parsed and parsed["attachment"].endswith(f"Angebot-{q.number}.pdf")

    doc_id, number, _ = _overdue(client, db)
    post(client, f"/mahnwesen/rechnung/{doc_id}")
    d = _dunnings(db)[0]
    r = post(client, f"/mahnwesen/{d.id}/email", {"to": "info@krume.example", "subject": "Erinnerung", "body": "x",
                                                   "aktion": "outlook"})
    assert "in Outlook geöffnet" in r.text and programme[-1][1] == "/eml"


def test_ohne_programm_keine_knoepfe_und_fehlermeldung(client, db):
    _ready(client)
    doc = _final(client, db)                                          # conftest: kein Programm „installiert“
    assert "öffnen</button>" not in client.get(f"/rechnungen/{doc.id}").text
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "a@b.de", "subject": "x", "body": "y",
                                                     "aktion": "thunderbird"})
    assert r.status_code == 400 and "Thunderbird wurde auf diesem Rechner nicht gefunden" in r.text


def test_auf_dem_server_nie(client, db, programme, monkeypatch):
    _ready(client)
    doc = _final(client, db)
    monkeypatch.setattr(mailprogramm, "get_settings", lambda: SimpleNamespace(is_local=False))
    assert "In Thunderbird öffnen" not in client.get(f"/rechnungen/{doc.id}").text
    r = post(client, f"/rechnungen/{doc.id}/email", {"to": "a@b.de", "subject": "x", "body": "y",
                                                     "aktion": "thunderbird"})
    assert r.status_code == 400 and "eigenen Rechner" in r.text and programme == []


def test_alte_anhaenge_werden_aufgeraeumt(programme, tmp_path):
    base = tmp_path / "mail"
    old = base / "alt"
    old.mkdir(parents=True)
    (old / "Rechnung.pdf").write_bytes(b"%PDF")
    past = time.time() - mailprogramm.KEEP_SECONDS - 60
    os.utime(old, (past, past))
    fresh = mailprogramm._work_dir()
    assert not old.exists() and fresh.exists()
