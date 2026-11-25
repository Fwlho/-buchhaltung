"""Server-Härtung: Docker-Konfiguration (Netze, Rechte, Protokolle), Caddy-Kopfzeilen, Skripte."""
import json
import shutil
import subprocess

import pytest

from app.config import BASE_DIR

DOCKER = shutil.which("docker")
BASH = shutil.which("bash")


@pytest.fixture(scope="module")
def compose(tmp_path_factory):
    """docker-compose.yml so auswerten, wie Docker sie liest (startet nichts, braucht keinen laufenden Docker-Dienst)."""
    if DOCKER is None:
        pytest.skip("Docker-Kommandozeile nicht vorhanden")
    folder = tmp_path_factory.mktemp("compose")
    (folder / "deploy").mkdir()
    shutil.copy(BASE_DIR / "docker-compose.yml", folder)
    shutil.copy(BASE_DIR / "deploy" / "Caddyfile", folder / "deploy")
    (folder / ".env").write_text("DOMAIN=test.example\nPOSTGRES_PASSWORD=x\n", encoding="utf-8")
    r = subprocess.run([DOCKER, "compose", "config", "--format", "json"], cwd=folder, capture_output=True, text=True,
                       timeout=60, env={**__import__("os").environ, "DOMAIN": "test.example", "POSTGRES_PASSWORD": "x"})
    if r.returncode != 0:
        pytest.fail(r.stderr)
    return json.loads(r.stdout)


def test_datenbank_nur_im_internen_netz(compose):
    services = compose["services"]
    assert compose["networks"]["intern"]["internal"] is True
    assert set(services["db"]["networks"]) == {"intern"}
    assert set(services["caddy"]["networks"]) == {"web"}            # Caddy erreicht die Datenbank nicht
    assert set(services["app"]["networks"]) == {"web", "intern", "ausgang"}
    assert set(services["worker"]["networks"]) == {"intern", "ausgang"}
    # Nur Caddy ist von außen erreichbar
    assert [name for name, s in services.items() if s.get("ports")] == ["caddy"]
    assert sorted(str(p["published"]) for p in services["caddy"]["ports"]) == ["443", "443", "80"]


def test_container_ohne_sonderrechte_und_schreibgeschuetzt(compose):
    services = compose["services"]
    for name in ("app", "worker", "caddy"):
        s = services[name]
        assert s["read_only"] is True, name
        assert s["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in s["security_opt"], name
    assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]
    assert "no-new-privileges:true" in services["db"]["security_opt"]
    for name in ("app", "worker"):                                    # nur /daten beschreibbar (+ /tmp im Speicher)
        assert [v["target"] for v in services[name]["volumes"]] == ["/daten"]
        assert any(str(t).startswith("/tmp") for t in services[name]["tmpfs"])


def test_protokolle_begrenzt(compose):
    for name, s in compose["services"].items():
        assert s["logging"]["options"] == {"max-size": "10m", "max-file": "5"}, name


def test_caddy_schutz_kopfzeilen():
    text = (BASE_DIR / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    for needle in ('Strict-Transport-Security "max-age=31536000; includeSubDomains"', "Permissions-Policy",
                   "X-Content-Type-Options", "-Server", "header_up X-Forwarded-For {remote_host}"):
        assert needle in text, needle


@pytest.mark.skipif(BASH is None, reason="bash nicht vorhanden")
@pytest.mark.parametrize("script", ["sicherung.sh", "wiederherstellen.sh", "server-haertung.sh"])
def test_skripte_syntaktisch_korrekt(script, tmp_path):
    source = (BASE_DIR / "deploy" / script).read_bytes().replace(b"\r\n", b"\n")
    (tmp_path / script).write_bytes(source)
    r = subprocess.run([BASH, "-n", (tmp_path / script).as_posix()], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert b"set -euo pipefail" in source


def test_haertung_sperrt_niemanden_aus():
    text = (BASE_DIR / "deploy" / "server-haertung.sh").read_text(encoding="utf-8")
    # Passwort-Login wird nur abgeschaltet, wenn schon ein SSH-Schlüssel hinterlegt ist
    guard = text.index("authorized_keys")
    assert guard < text.index("PasswordAuthentication no") < text.index("ÜBERSPRUNGEN")
    assert "sshd -t" in text                                          # Konfiguration vor dem Neuladen prüfen
    assert "ufw default deny incoming" in text and "ufw allow 443/tcp" in text
    assert "10-buchhaltung.conf" in text                              # vor 50-cloud-init.conf


def test_zeilenenden_fuer_linux():
    """Skripte müssen auf dem Server mit LF-Zeilenenden ankommen (sonst: „$'\\r': command not found“)."""
    for path in [*(BASE_DIR / "deploy").glob("*.sh"), BASE_DIR / "deploy" / "Caddyfile"]:
        assert b"\r\n" not in path.read_bytes(), path.name
    # … und zur Sicherheit wandelt das Upload-Skript sie auf dem Server noch einmal um
    assert "sed -i 's/\\r$//'" in (BASE_DIR / "deploy" / "hochladen.ps1").read_text(encoding="utf-8")
