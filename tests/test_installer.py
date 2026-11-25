"""Installer (installer/buchhaltung_installer.py): erst installieren, dann aktualisieren – Daten, .env und
fremde Dateien bleiben unberührt, entfernte Programmdateien verschwinden, der alte Stand wird aufgehoben."""
import importlib.util
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "buchhaltung_installer", Path(__file__).resolve().parents[1] / "installer" / "buchhaltung_installer.py")
inst = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inst)


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(f"-buchhaltung-main/{name}", text)
    return path


@pytest.fixture(autouse=True)
def _kein_echtes_programm(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "CONFIG", tmp_path / "cfg" / "einstellungen.json")
    monkeypatch.setattr(inst, "program_running", lambda: False)


def test_installieren_und_aktualisieren(tmp_path):
    folder = tmp_path / "Buchhaltung"
    v1 = _zip(tmp_path / "v1.zip", {"run.py": "v1", "app/alt.py": "alt", "app/main.py": "1", "requirements.txt": ""})
    assert inst.main(["--zip", str(v1), "--ordner", str(folder), "--ja", "--ohne-python"]) == 0
    assert (folder / "run.py").read_text() == "v1" and (folder / "app" / "alt.py").exists()
    assert inst.remembered_dir() == folder.resolve()

    # Daten, Schlüssel, Umgebung und eine fremde Datei des Benutzers
    (folder / "data").mkdir()
    (folder / "data" / "app.db").write_text("ECHT")
    (folder / ".env").write_text("SCHLUESSEL")
    (folder / ".venv").mkdir()
    (folder / "meine_notiz.txt").write_text("privat")

    v2 = _zip(tmp_path / "v2.zip", {"run.py": "v2", "app/main.py": "2", "requirements.txt": "",
                                    "data/app.db": "DARF NICHT", ".env": "DARF NICHT"})
    assert inst.main(["--zip", str(v2), "--ja", "--ohne-python"]) == 0      # Ordner gemerkt
    assert (folder / "run.py").read_text() == "v2" and (folder / "app" / "main.py").read_text() == "2"
    assert not (folder / "app" / "alt.py").exists()                         # entfernte Programmdatei weg
    assert (folder / "data" / "app.db").read_text() == "ECHT" and (folder / ".env").read_text() == "SCHLUESSEL"
    assert (folder / ".venv").is_dir() and (folder / "meine_notiz.txt").read_text() == "privat"
    with zipfile.ZipFile(folder / "alte_version" / "programm_vorher.zip") as z:
        assert z.read("run.py") == b"v1" and "app/alt.py" in z.namelist()


def test_kein_programm_im_paket_und_laufendes_programm(tmp_path, monkeypatch):
    bad = _zip(tmp_path / "bad.zip", {"README.md": "x"})
    assert inst.main(["--zip", str(bad), "--ordner", str(tmp_path / "A"), "--ja", "--ohne-python"]) == 1
    good = _zip(tmp_path / "ok.zip", {"run.py": "v1", "requirements.txt": ""})
    folder = tmp_path / "B"
    assert inst.main(["--zip", str(good), "--ordner", str(folder), "--ja", "--ohne-python"]) == 0
    monkeypatch.setattr(inst, "program_running", lambda: True)               # Update nur bei beendetem Programm
    assert inst.main(["--zip", str(good), "--ordner", str(folder), "--ja", "--ohne-python"]) == 1


def test_veroeffentlichung_schliesst_daten_und_schluessel_aus():
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", ".venv/", "data*/", "alte_version/", ".claude/"):
        assert pattern in ignore


def test_kein_zip_von_github(tmp_path):
    html = tmp_path / "seite.zip"
    html.write_text("<html>Anmelden</html>")
    assert inst.main(["--zip", str(html), "--ordner", str(tmp_path / "C"), "--ja", "--ohne-python"]) == 1


def test_neuer_ordner_trotz_vorhandener_installation(tmp_path, monkeypatch):
    good = _zip(tmp_path / "ok.zip", {"run.py": "v1", "requirements.txt": ""})
    old = tmp_path / "Alt"
    assert inst.main(["--zip", str(good), "--ordner", str(old), "--ja", "--ohne-python"]) == 0
    (old / "data").mkdir()
    (old / "data" / "app.db").write_text("ALT")
    new = tmp_path / "Neu"
    answers = iter(["2", str(new), "j", ""])                    # neuer Ordner, langer Testpfad ok, schließen
    monkeypatch.setattr("builtins.input", lambda *_: next(answers, ""))
    assert inst.main(["--zip", str(good), "--ohne-python"]) == 0
    assert (new / "run.py").exists() and not (new / "data").exists()       # frische Installation
    assert (old / "data" / "app.db").read_text() == "ALT"                   # alte bleibt unberührt
    assert not (old / "alte_version").exists()
    assert inst.remembered_dir() == new.resolve()

    # Enter = vorhandene (jetzt: neue) Installation aktualisieren
    answers = iter(["", "j", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers, ""))
    assert inst.main(["--zip", str(good), "--ohne-python"]) == 0
    assert (new / "alte_version" / "programm_vorher.zip").exists()


def test_vorschlag_fuer_neuen_ordner(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "DEFAULT_DIR", tmp_path / "Buchhaltung")
    (tmp_path / "Buchhaltung").mkdir()
    (tmp_path / "Buchhaltung" / "run.py").write_text("x")
    assert inst._free_suggestion(tmp_path / "Buchhaltung") == tmp_path / "Buchhaltung2"


def test_eingaben_mit_bom_und_relativem_pfad(tmp_path, monkeypatch):
    inst.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    old = tmp_path / "Alt"
    old.mkdir()
    (old / "run.py").write_text("x")
    inst.CONFIG.write_text('﻿{"ordner": "%s"}' % str(old).replace("\\", "\\\\"), encoding="utf-8")
    assert inst.remembered_dir() == old                                      # Einstellung mit BOM lesbar
    answers = iter(["﻿2", "nur_ein_name", str(tmp_path / "Neu")])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert inst.choose_dir(None, False) == tmp_path / "Neu"                  # relativer Pfad wird abgelehnt
