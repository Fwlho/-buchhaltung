"""Ablage-Ordner (entschieden 25.11.2026): festgeschriebene Rechnungen als Kopie nach <Ablage>\\<Jahr>\\<MM Monat>.
Alle Dateien sind selbst erzeugte Test-Rechnungen in Test-Ordnern."""
from datetime import date

import pytest
from sqlalchemy import select

from app.database import tenant_scope
from app.models import Document, Tenant
from app.services import ablage_ordner
from tests.conftest import post, read_stored
from tests.test_auslese import RECHNUNG_EINFACH
from tests.test_eingangsrechnungen import PDF_EINFACH, _all_docs, _form_of, _ready, accepted_doc, make_pdf
from tests.test_rechnungen import _final

def _invoice(number: str) -> bytes:
    """Selbst erzeugte Test-Rechnung mit anderer Nummer (anderer Inhalt)."""
    return make_pdf(RECHNUNG_EINFACH.replace("RE-2026-0815", number))


def _errors(html: str) -> str:
    import re

    return " | ".join(re.sub(r"\s+", " ", m)[:200] for m in re.findall(r'class="(?:error|notice[^"]*)"[^>]*>(.*?)</', html, re.S))


MONAT = f"{date.today().month:02d} {ablage_ordner.MONTHS[date.today().month - 1]}"


def _folders(client, tmp_path, **extra) -> tuple:
    ein, aus = tmp_path / "Eingang", tmp_path / "Ausgang"
    r = post(client, "/einstellungen/ordner", {"ordner.eingang": str(ein), "ordner.ausgang": str(aus), **extra})
    assert "gespeichert" in r.text, r.text
    return ein, aus


def _doc(db, doc_id) -> Document:
    db.expire_all()
    return next(d for d in _all_docs(db) if d.id == doc_id)


def _finalize_incoming(client, db, day: str, name="rechnung.pdf", pdf=PDF_EINFACH) -> Document:
    from tests.test_eingangsrechnungen import upload

    upload(client, (name, pdf))
    db.expire_all()
    doc = _all_docs(db)[-1]
    r = post(client, f"/pruefbereich/{doc.id}", _form_of(doc, account="4930", aktion="uebernehmen"))
    doc = _doc(db, doc.id)
    assert not doc.pending, _errors(r.text)
    r = post(client, f"/eingangsrechnungen/{doc.id}/festschreiben",
             _form_of(doc, doc_date=day, due_date="", skonto_until="", skonto_percent=""))
    assert "festgeschrieben" in r.text, (r.status_code, _errors(r.text))
    return _doc(db, doc.id)


def test_monatsordner():
    from pathlib import Path

    assert ablage_ordner.month_folder(Path("Y:/R"), date(2027, 1, 3)) == Path("Y:/R/2027/01 Januar")
    assert ablage_ordner.month_folder(Path("Y:/R"), date(2026, 3, 9)) == Path("Y:/R/2026/03 März")


def test_eingangsrechnung_erst_beim_festschreiben_einsortiert(client, db, tmp_path):
    _ready(client)
    ein, _aus = _folders(client, tmp_path)                    # Ablage leer -> derselbe Ordner wie der Eingangsordner
    draft = accepted_doc(client, db, name="Lieferant März.pdf")
    assert draft.is_draft and not list(ein.rglob("*.pdf"))   # Entwurf: noch nichts einsortiert
    r = post(client, f"/eingangsrechnungen/{draft.id}/festschreiben", _form_of(draft, doc_date="15.03.2026"))
    assert "festgeschrieben" in r.text
    target = ein / "2026" / "03 März" / "Lieferant März.pdf"
    assert target.read_bytes() == PDF_EINFACH
    doc = _doc(db, draft.id)
    assert doc.ablage_at is not None and doc.ablage_pfad == str(target)
    # nochmal laufen lassen: nichts doppelt
    with tenant_scope(db, doc.tenant_id):
        assert ablage_ordner.nachtragen(db) == []
    assert len(list(ein.rglob("*.pdf"))) == 1


def test_jahreswechsel_und_eigene_ablage(client, db, tmp_path):
    _ready(client)
    ablage = tmp_path / "Ablage" / "Eingang"
    ein, _aus = _folders(client, tmp_path, **{"ordner.ablage_eingang": str(ablage)})
    _finalize_incoming(client, db, "31.12.2025", name="dezember.pdf")
    _finalize_incoming(client, db, "02.01.2026", name="januar.pdf", pdf=_invoice("RE-2026-0001"))
    assert (ablage / "2025" / "12 Dezember" / "dezember.pdf").exists()
    assert (ablage / "2026" / "01 Januar" / "januar.pdf").exists()      # neues Jahr: Ordner automatisch angelegt
    assert not list(ein.rglob("*.pdf"))                                # eigener Ablage-Ordner gewählt


def test_gleicher_name_wird_nicht_ueberschrieben(client, db, tmp_path):
    _ready(client)
    ein, _aus = _folders(client, tmp_path)
    _finalize_incoming(client, db, "10.03.2026", name="rechnung.pdf")
    other = _invoice("RE-2026-0999")
    _finalize_incoming(client, db, "11.03.2026", name="rechnung.pdf", pdf=other)
    folder = ein / "2026" / "03 März"
    assert (folder / "rechnung.pdf").read_bytes() == PDF_EINFACH
    assert (folder / "rechnung (2).pdf").read_bytes() == other


def test_ausgangsrechnung_und_storno(client, db, tmp_path):
    _ready(client)
    _ein, aus = _folders(client, tmp_path)
    doc = _final(client, db)
    files = sorted((aus / str(date.today().year) / MONAT).glob("*.pdf"))
    assert [f.name for f in files] == [f"{doc.number}-Rechnung-BäckereiKrumeGmbH.pdf"]
    assert files[0].read_bytes() == read_stored(doc.file)
    r = post(client, f"/rechnungen/{doc.id}/stornieren", {"grund": "Test"})
    assert "Stornorechnung" in r.text
    names = [f.name for f in (aus / str(date.today().year) / MONAT).glob("*.pdf")]
    assert any("Stornorechnung" in n for n in names) and len(names) == 2


def test_uebernommene_alte_ausgangsrechnung(client, db, tmp_path):
    from tests.test_rechnungs_import import ALTE_RECHNUNG, _upload
    from tests.test_rechnungs_import import _form as import_form
    from tests.test_rechnungs_import import _pending

    _ready(client)
    ablage = tmp_path / "Ablage-Ausgang"
    _folders(client, tmp_path, **{"ordner.ablage_ausgang": str(ablage)})
    _upload(client, ("alt.pdf", make_pdf(ALTE_RECHNUNG)))
    doc = _pending(db)[0]
    r = post(client, f"/rechnungen/import/{doc.id}", import_form(doc, aktion="uebernehmen"))
    assert "übernommen" in r.text, r.text[:300]
    assert len(list((ablage / "2026" / "02 Februar").glob("2026-016-Rechnung-*.pdf"))) == 1


def test_laufwerk_nicht_erreichbar_blockiert_nichts(client, db, tmp_path):
    _ready(client)
    blocker = tmp_path / "keinordner.txt"
    ein, _aus = _folders(client, tmp_path)
    blocker.write_text("x")
    post(client, "/einstellungen/ordner", {"ordner.eingang": str(ein), "ordner.ausgang": str(tmp_path / "Ausgang")})
    # Ablage zeigt auf etwas, das kein Ordner sein kann (wie ein getrenntes Netzlaufwerk)
    with tenant_scope(db, db.scalar(select(Tenant.id))):
        from app.services import einstellungen

        einstellungen.set_values(db, {"ordner.ablage_eingang": str(blocker / "unmöglich")})
        db.commit()
    doc = accepted_doc(client, db)
    r = post(client, f"/eingangsrechnungen/{doc.id}/festschreiben",
             _form_of(doc, doc_date="05.05.2026", due_date="", skonto_until="", skonto_percent=""))
    assert "festgeschrieben" in r.text and "versucht es später erneut" in r.text, _errors(r.text)
    assert _doc(db, doc.id).is_final and _doc(db, doc.id).ablage_at is None
    # Laufwerk wieder da -> der nächste Hintergrund-Lauf holt es nach
    with tenant_scope(db, doc.tenant_id):
        einstellungen.set_values(db, {"ordner.ablage_eingang": ""})
        db.commit()
    from app.worker import run_due_jobs

    run_due_jobs()
    assert (ein / "2026" / "05 Mai" / "r.pdf").exists() and _doc(db, doc.id).ablage_at is not None


@pytest.mark.parametrize("value", ["relativ\\ordner", "ordner"])
def test_einstellung_braucht_vollstaendigen_pfad(client, tmp_path, value):
    _ready(client)
    r = post(client, "/einstellungen/ordner", {"ordner.eingang": str(tmp_path / "E"), "ordner.ablage_eingang": value})
    assert "Bitte einen vollständigen Pfad angeben" in r.text
    assert "Ablage für festgeschriebene Eingangsrechnungen" in r.text and r"2026\11 November" in r.text
    page = client.get("/einstellungen/design").text
    assert "rechnung.ablage_ordner" not in page                        # alte Einstellung ist ersetzt
