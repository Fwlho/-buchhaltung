"""PDF-Vorlagen (Designs) für Rechnung, Angebot und Mahnung: Verzeichnis und Auswahl.

Die Firma wählt je Belegart eine Vorlage (Einstellungen → Design, Schlüssel ``design.vorlage_<art>``).
Neue Vorlage: Klasse in ``pdf_designs.py`` anlegen (erbt von ``Klassisch``) und hier in ``VORLAGEN`` eintragen.
Dieses Modul importiert bewusst kein reportlab – die Einstellungen brauchen nur die Auswahlliste.
"""
from dataclasses import dataclass

ARTEN = {"rechnung": "Rechnung", "angebot": "Angebot", "mahnung": "Mahnung"}
STANDARD = "klassisch"


@dataclass(frozen=True)
class Vorlage:
    key: str
    name: str
    beschreibung: str
    arten: tuple[str, ...] = tuple(ARTEN)


VORLAGEN: tuple[Vorlage, ...] = (
    Vorlage("klassisch", "Klassisch",
            "Firmenname zentriert, Logo links oder rechts, Linien in der Akzentfarbe (DIN 5008)"),
    # Pakete aus Claude Design (Umsetzung: pdf_designs.PAKETE)
    Vorlage("marine", "Marine", "Logo rechts, getönter Tabellenkopf, schlichte Fußzeile"),
    Vorlage("petrol", "Petrol", "Farbband oben, Belegart in Großbuchstaben, Gesamtbetrag als Farbbalken"),
    Vorlage("bordeaux", "Bordeaux", "Logo mittig, Doppellinien, zentrierte Fußzeile – klassisch-elegant"),
    Vorlage("anthrazit", "Anthrazit", "Sehr reduziert, Belegart klein über der Nummer"),
    Vorlage("tannengruen", "Tannengrün", "Getönte Flächen, Fußzeile als farbiges Band"),
    Vorlage("indigo", "Indigo", "Tabellenkopf als Farbfläche, Fußzeile mit Überschriften"),
    Vorlage("bronze", "Bronze", "Belegart kursiv im Kopf, Linien in Bronze"),
    Vorlage("pflaume", "Pflaume", "Infoblock im getönten Kasten, getöntes Fußband"),
    Vorlage("stahlblau", "Stahlblau", "Firmenangaben im Briefkopf rechts, Fußzeile in drei Spalten"),
    Vorlage("terrakotta", "Terrakotta", "Farbstreifen am rechten Rand, Positionen abwechselnd getönt"),
)


def choices(art: str) -> tuple[tuple[str, str], ...]:
    return tuple((v.key, v.name) for v in VORLAGEN if art in v.arten)


def setting_key(art: str) -> str:
    return f"design.vorlage_{art}"


def key_for(values: dict, art: str, wanted: str | None = None) -> str:
    """Gewünschte bzw. eingestellte Vorlage; unbekannte (z. B. entfernte) fallen auf „Klassisch“ zurück."""
    valid = {key for key, _ in choices(art)}
    for key in (wanted, values.get(setting_key(art))):
        if key in valid:
            return key
    return STANDARD


def design(values: dict, art: str, wanted: str | None = None):
    """Die Vorlage (Objekt mit styles/positions_table/sums_table/payment_block/build) für diese Belegart."""
    from app.services import pdf_designs

    return pdf_designs.DESIGNS[key_for(values, art, wanted)]()
