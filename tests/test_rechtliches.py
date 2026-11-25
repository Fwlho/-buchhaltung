"""Impressum/Datenschutz-Links (Server-Einstellung) und Eingangsordner nur lokal."""
import pytest

from app.config import get_settings
from tests.test_eingangsrechnungen import _ready


@pytest.fixture
def links(monkeypatch):
    def setze(impressum: str, datenschutz: str):
        monkeypatch.setenv("IMPRESSUM_URL", impressum)
        monkeypatch.setenv("DATENSCHUTZ_URL", datenschutz)
        get_settings.cache_clear()

    yield setze
    monkeypatch.delenv("IMPRESSUM_URL", raising=False)
    monkeypatch.delenv("DATENSCHUTZ_URL", raising=False)
    get_settings.cache_clear()


def test_links_auf_anmeldeseite_und_jeder_seite(client, links):
    links("https://www.beispiel-firma.example/impressum", "https://www.beispiel-firma.example/datenschutz")
    page = client.get("/login").text
    assert 'href="https://www.beispiel-firma.example/impressum"' in page and ">Impressum<" in page
    assert 'href="https://www.beispiel-firma.example/datenschutz"' in page and ">Datenschutz<" in page
    _ready(client)
    assert ">Impressum<" in client.get("/rechnungen").text


def test_nur_echte_https_adressen(client, links):
    links("javascript:alert(1)", "www.ohne-protokoll.example")
    page = client.get("/login").text
    assert "javascript:" not in page and ">Impressum<" not in page and ">Datenschutz<" not in page


def test_ohne_einstellung_keine_links(client):
    assert ">Impressum<" not in client.get("/login").text


def test_eingangsordner_auf_dem_server_ausgeblendet(client, monkeypatch):
    _ready(client)
    assert "Eingangsordner" in client.get("/pruefbereich").text            # lokal sichtbar
    monkeypatch.setattr(type(get_settings()), "is_local", property(lambda self: False))
    assert "<h2>Eingangsordner</h2>" not in client.get("/pruefbereich").text
    assert "<h2>Eingangsordner</h2>" not in client.get("/rechnungen/import").text
