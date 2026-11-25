"""Server-Sicherung (deploy/sicherung.sh, deploy/wiederherstellen.sh) mit nachgebildeten Werkzeugen.

docker, age und rsync werden durch kleine Ersatz-Skripte ersetzt – geprüft wird der Ablauf: nie unverschlüsselt,
Abbruch ohne Schlüssel, keine halben Sicherungen, Übertragung zur Storage Box, Probe-Wiederherstellung.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import BASE_DIR

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash nicht vorhanden")
SCHLUESSEL = "age1" + "q" * 58

STUBS = {
    # docker compose exec -T db pg_dump … | docker … tar … | pg_restore --list | stop/start/run
    "docker": r'''#!/bin/bash
echo "docker $*" >> "$PROTOKOLL"
case "$*" in
  *pg_dump*) [ -n "${DOCKER_FEHLER:-}" ] && exit 1; printf 'PGDMP-GEHEIM' ;;
  *"tar -czf"*) printf 'TAR-GEHEIM' ;;
  *"pg_restore --list"*) cat > /dev/null; echo "1; 0 0 TABLE DATA public documents" ;;
  *) cat > /dev/null 2>&1 || true ;;
esac
''',
    # age -r EMPFAENGER (stdin -> stdout) bzw. age -d -i SCHLUESSEL DATEI
    "age": r'''#!/bin/bash
if [ "$1" = "-r" ]; then printf 'AGE[%s]\n' "$2"; cat; else tail -n +2 "$4"; fi
''',
    "rsync": '#!/bin/bash\necho "rsync $*" >> "$PROTOKOLL"\n',
    # tar -tzf - für die Probe-Wiederherstellung
    "tar": '#!/bin/bash\ncat > /dev/null; echo "1/beleg.pdf"; echo "1/"\n',
}


def _setup(tmp_path: Path, env_lines: list[str]) -> tuple[Path, dict]:
    root = tmp_path / "server"
    (root / "deploy").mkdir(parents=True)
    for name in ("sicherung.sh", "wiederherstellen.sh"):
        (root / "deploy" / name).write_bytes((BASE_DIR / "deploy" / name).read_bytes().replace(b"\r\n", b"\n"))
    (root / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8", newline="\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(body, encoding="utf-8", newline="\n")
        os.chmod(stubs / name, 0o755)
    env = {**os.environ, "PROTOKOLL": (tmp_path / "protokoll.txt").as_posix()}
    env["PATH"] = stubs.as_posix() + os.pathsep + env.get("PATH", "")
    return root, env


def _run(root: Path, env: dict, *args: str, eingabe: str = "") -> subprocess.CompletedProcess:
    script = "sicherung.sh" if not args else "wiederherstellen.sh"
    return subprocess.run([BASH, (root / "deploy" / script).as_posix(), *args], env=env, input=eingabe,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def _files(root: Path) -> list[str]:
    folder = root / "sicherungen"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_ohne_schluessel_keine_sicherung(tmp_path):
    root, env = _setup(tmp_path, ["DOMAIN=x"])
    r = _run(root, env)
    assert r.returncode == 2 and "BACKUP_AGE_EMPFAENGER fehlt" in r.stderr
    assert _files(root) == []
    root, env = _setup(tmp_path / "b", [f"BACKUP_AGE_EMPFAENGER=kein-schluessel"])
    assert _run(root, env).returncode == 2


def test_verschluesselt_und_zur_storage_box(tmp_path):
    root, env = _setup(tmp_path, [f"BACKUP_AGE_EMPFAENGER={SCHLUESSEL}",
                                  "BACKUP_ZIEL_SSH=u123@u123.your-storagebox.de"])
    alt = root / "sicherungen"
    alt.mkdir()
    (alt / "datenbank_2026-01-01_02-30.dump").write_text("ALT-UNVERSCHLUESSELT")
    r = _run(root, env)
    assert r.returncode == 0, r.stderr
    names = _files(root)
    db = next(n for n in names if n.startswith("datenbank_") and n.endswith(".dump.age"))
    files = next(n for n in names if n.startswith("dateien_") and n.endswith(".tar.gz.age"))
    for name in (db, files):
        content = (root / "sicherungen" / name).read_text()
        assert content.startswith(f"AGE[{SCHLUESSEL}]")          # nur verschlüsselt geschrieben
    assert not [n for n in names if n.endswith(".teil") or n.endswith(".dump")]   # alte Klartext-Sicherung weg
    assert any(n.startswith("pruefsummen_") for n in names) and "letzte_sicherung.txt" in names
    log = (tmp_path / "protokoll.txt").read_text()
    assert "rsync -a --delete -e ssh -p 23 -i /root/.ssh/storagebox -o BatchMode=yes" in log
    assert "u123@u123.your-storagebox.de:buchhaltung/" in log
    assert "nach u123@u123.your-storagebox.de übertragen" in r.stdout


def test_fehler_im_datenbank_abzug_hinterlaesst_nichts(tmp_path):
    root, env = _setup(tmp_path, [f"BACKUP_AGE_EMPFAENGER={SCHLUESSEL}"])
    env["DOCKER_FEHLER"] = "1"
    r = _run(root, env)
    assert r.returncode == 1 and "FEHLER" in r.stderr
    assert _files(root) == []


def test_warnung_ohne_storage_box(tmp_path):
    root, env = _setup(tmp_path, [f"BACKUP_AGE_EMPFAENGER={SCHLUESSEL}"])
    r = _run(root, env)
    assert r.returncode == 0 and "WARNUNG" in r.stdout and "liegt nur auf diesem Server" in r.stdout
    assert "rsync" not in (tmp_path / "protokoll.txt").read_text()


def test_wiederherstellen_pruefen_und_sicherheitsabfrage(tmp_path):
    root, env = _setup(tmp_path, [f"BACKUP_AGE_EMPFAENGER={SCHLUESSEL}"])
    assert _run(root, env).returncode == 0
    stand = next(n for n in _files(root) if n.startswith("datenbank_"))[len("datenbank_"):-len(".dump.age")]
    key = tmp_path / "schluessel.txt"
    key.write_text("AGE-SECRET-KEY-TEST")
    r = _run(root, env, "pruefen", key.as_posix(), stand)
    assert r.returncode == 0 and "lesbar: 1 Tabellen, 1 Dateien" in r.stdout, r.stderr
    log = (tmp_path / "protokoll.txt").read_text()
    assert "compose stop" not in log                       # Prüfen ändert nichts
    r = _run(root, env, "zurueck", key.as_posix(), stand, eingabe="nein\n")
    assert r.returncode == 1 and "Abgebrochen" in r.stdout
    assert "compose stop" not in (tmp_path / "protokoll.txt").read_text()
    r = _run(root, env, "zurueck", key.as_posix(), "2000-01-01_00-00")
    assert r.returncode == 2 and "fehlt" in r.stderr
