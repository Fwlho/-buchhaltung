"""Firmen-Einstellungen: welche es gibt, Prüfung der Eingaben, Lesen/Speichern.

Alles Firmenspezifische (Name, Adresse, Bank, Design, Postfach …) wird hier
definiert und in der Tabelle tenant_settings je Firma gespeichert. Die Gruppen
sind zugleich die Schritte des Einrichtungs-Assistenten.
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forms import Field, display_value, parse_field
from app.models import TenantSetting
from app.security import decrypt
from app.services import audit, pdf_vorlagen
from app.services.nummernkreise import NUMBER_TYPES, format_number, format_problem

# Festgeschriebene Belege sind unveränderbar (GoBD) – ihr PDF wird nie neu erzeugt
_VORLAGE_HILFE = {"rechnung": "Gilt für neu festgeschriebene Rechnungen; bestehende behalten ihr Aussehen.",
                  "mahnung": "Gilt für neue Mahnungen; bestehende behalten ihr Aussehen."}


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    intro: str
    fields: tuple[Field, ...] = field(default_factory=tuple)

    def field(self, key: str) -> Field:
        return next(f for f in self.fields if f.key == key)


RECHTSFORMEN = (
    ("Einzelunternehmen", "Einzelunternehmen"),
    ("Freiberufler", "Freiberufler/in"),
    ("e.K.", "eingetragener Kaufmann (e.K.)"),
    ("GbR", "GbR"),
    ("UG (haftungsbeschränkt)", "UG (haftungsbeschränkt)"),
    ("GmbH", "GmbH"),
    ("Sonstige", "Sonstige"),
)

_number_fields: list[Field] = []
for _typ, _label, _default in NUMBER_TYPES:
    _number_fields.append(Field(f"nummern.{_typ}.format", f"{_label}: Format", "numformat", _default, required=True))
    _number_fields.append(
        Field(f"nummern.{_typ}.start", f"{_label}: erste Nummer im Jahr", "int", 1, required=True, min=1, max=999999)
    )
_number_fields.append(
    Field("nummern.kunde.start", "Erste Kundennummer (Debitor)", "int", 10000, required=True, min=1, max=999999999,
          help="Für den DATEV-Export üblich: Kunden 10000–69999.")
)
_number_fields.append(
    Field("nummern.lieferant.start", "Erste Lieferantennummer (Kreditor)", "int", 70000, required=True, min=1,
          max=999999999, help="Für den DATEV-Export üblich: Lieferanten 70000–99999.")
)

DUNNING_LEVELS = (
    # (Stufe, Name, Zahlungsfrist in Tagen, Gebühr, Text)
    (1, "Zahlungserinnerung", 7, Decimal("0.00"),
     "sicher haben Sie unsere Rechnung {nummer} vom {datum} übersehen. Der Betrag von {betrag} war am {faellig} "
     "fällig. Bitte überweisen Sie den offenen Betrag bis zum {frist}."),
    (2, "1. Mahnung", 7, Decimal("5.00"),
     "leider konnten wir zu unserer Rechnung {nummer} vom {datum} bis heute keinen Zahlungseingang feststellen. "
     "Bitte überweisen Sie den Gesamtbetrag von {gesamt} bis spätestens {frist}."),
    (3, "2. Mahnung", 7, Decimal("5.00"),
     "trotz unserer Erinnerungen ist die Rechnung {nummer} vom {datum} weiterhin offen. Bitte überweisen Sie den "
     "Gesamtbetrag von {gesamt} bis spätestens {frist}. Nach Ablauf dieser Frist behalten wir uns weitere "
     "Schritte vor."),
)
_dunning_fields: list[Field] = []
for _nr, _name, _days, _fee, _text in DUNNING_LEVELS:
    _dunning_fields += [
        Field(f"mahnung.stufe{_nr}.name", f"Stufe {_nr}: Bezeichnung", default=_name, required=True),
        Field(f"mahnung.stufe{_nr}.frist", f"Stufe {_nr}: Zahlungsfrist (Tage)", "int", _days, required=True,
              min=1, max=90),
        Field(f"mahnung.stufe{_nr}.gebuehr", f"Stufe {_nr}: Mahngebühr in €", "decimal", _fee, min=0,
              help="Leer = keine Gebühr." if _nr == 1 else ""),
        Field(f"mahnung.stufe{_nr}.text", f"Stufe {_nr}: Text (nach „Sehr geehrte Damen und Herren,“)",
              "textarea", _text, required=True),
    ]

GROUPS: tuple[Group, ...] = (
    Group(
        "firma", "Firmendaten",
        "Diese Angaben erscheinen im Kopf und in der Fußzeile Ihrer Rechnungen und Angebote.",
        (
            Field("firma.name", "Firmenname", required=True),
            Field("firma.rechtsform", "Rechtsform", "choice", "Einzelunternehmen", required=True, choices=RECHTSFORMEN),
            Field("firma.inhaber", "Inhaber/in bzw. Geschäftsführer/in", required=True),
            Field("firma.strasse", "Straße und Hausnummer", required=True),
            Field("firma.plz", "PLZ", required=True),
            Field("firma.ort", "Ort", required=True),
            Field("firma.land", "Land", default="Deutschland", required=True),
            Field("firma.telefon", "Telefon"),
            Field("firma.email", "E-Mail", "email"),
            Field("firma.website", "Webseite"),
            Field("firma.handelsregister", "Handelsregister (optional)",
                  placeholder="z. B. Amtsgericht Musterstadt, HRB 12345"),
        ),
    ),
    Group(
        "steuer", "Steuer",
        "Auf Rechnungen muss die Steuernummer oder die USt-IdNr. stehen. "
        "Das Programm ersetzt keine Steuerberatung.",
        (
            Field("steuer.steuernummer", "Steuernummer"),
            Field("steuer.ust_id", "USt-IdNr.", "ustid", placeholder="DE123456789"),
            Field("steuer.kleinunternehmer", "Kleinunternehmer nach § 19 UStG (keine Umsatzsteuer ausweisen)", "bool", False),
            Field("steuer.versteuerung", "Versteuerung", "choice", "ist", required=True,
                  choices=(("ist", "Ist-Versteuerung (nach Zahlungseingang)"),
                           ("soll", "Soll-Versteuerung (nach Rechnungsdatum)")),
                  help="Die meisten Kleinunternehmen nutzen die Ist-Versteuerung."),
            Field("steuer.voranmeldung", "Umsatzsteuer-Voranmeldung", "choice", "quartalsweise", required=True,
                  choices=(("monatlich", "monatlich"), ("quartalsweise", "quartalsweise"), ("jaehrlich", "jährlich / keine"))),
        ),
    ),
    Group(
        "bank", "Bankverbindung",
        "Erscheint auf Rechnungen und im GiroCode (QR-Code zum Bezahlen).",
        (
            Field("bank.kontoinhaber", "Kontoinhaber/in", required=True),
            Field("bank.iban", "IBAN", "iban", required=True),
            Field("bank.bic", "BIC", "bic"),
            Field("bank.name", "Bank"),
        ),
    ),
    Group(
        "konten", "Kontenrahmen",
        "Nach diesem Kontenrahmen werden Einnahmen und Ausgaben später den Konten zugeordnet "
        "(wichtig für den DATEV-Export). Fragen Sie im Zweifel Ihre Steuerberatung.",
        (
            Field("konten.rahmen", "Kontenrahmen", "choice", "SKR03", required=True,
                  choices=(("SKR03", "SKR03 (verbreitet bei Kleinunternehmen)"), ("SKR04", "SKR04"))),
        ),
    ),
    Group(
        "nummern", "Nummernkreise",
        "Format der Belegnummern. Platzhalter: {JJJJ} Jahr, {JJ} Jahr zweistellig, {MM} Monat, "
        "{NNN} laufende Nummer (Anzahl N = Stellen). Die Nummern beginnen jedes Jahr neu.",
        tuple(_number_fields),
    ),
    Group(
        "design", "Design & Rechnungstexte",
        "Aussehen und Standardwerte Ihrer Rechnungen und Angebote.",
        (
            *(Field(pdf_vorlagen.setting_key(art), f"PDF-Vorlage {label}", "choice", pdf_vorlagen.STANDARD,
                    choices=pdf_vorlagen.choices(art), help=_VORLAGE_HILFE.get(art, ""))
              for art, label in pdf_vorlagen.ARTEN.items()),
            Field("design.vorlage_farbe", "Farbe der PDF-Vorlagen", "choice", "vorlage",
                  choices=(("vorlage", "Farbe der jeweiligen Vorlage (z. B. Marine, Bordeaux)"),
                           ("akzent", "eigene Akzentfarbe (siehe unten)")),
                  help="Gilt nicht für „Klassisch“ – diese Vorlage nutzt immer die Akzentfarbe."),
            Field("design.logo", "Logo (PNG oder JPG)", "logo"),
            Field("design.logo_position", "Logo im Briefkopf", "choice", "rechts",
                  choices=(("rechts", "rechts (Firmenname in der Mitte)"), ("links", "links (Firmenname in der Mitte)")),
                  help="Nur für die Vorlage „Klassisch“ – die anderen Vorlagen legen die Position selbst fest."),
            Field("design.falzmarken", "Falz- und Lochmarken am linken Rand drucken", "bool", False,
                  help="Kleine Striche zum Falten für Fensterumschläge und zum Lochen. Nur nötig, wenn Sie "
                       "Rechnungen ausdrucken und per Post verschicken."),
            Field("design.akzentfarbe", "Akzentfarbe", "color", "#1f5fbf", required=True),
            Field("design.fusszeile", "Zusätzlicher Fußzeilen-Text", "textarea"),
            Field("design.danke_text", "Dankes-Text unter der Rechnung", "textarea", "Vielen Dank für Ihren Auftrag!"),
            Field("design.zahlungsziel_tage", "Zahlungsziel (Tage)", "int", 14, required=True, min=0, max=365),
            Field("design.ust_satz", "Standard-Umsatzsteuersatz", "choice", "19", required=True,
                  choices=(("19", "19 %"), ("7", "7 %"), ("0", "0 %"))),
            Field("rechnung.einleitung", "Einleitungstext auf Rechnungen", "textarea",
                  "Für unsere Leistungen berechnen wir Ihnen:"),
            Field("rechnung.e_rechnung", "E-Rechnung", "choice", "zugferd", required=True,
                  choices=(("zugferd", "ZUGFeRD – Rechnungsdaten im PDF eingebettet (empfohlen)"),
                           ("xrechnung", "XRechnung – zusätzlich eigene XML-Datei"),
                           ("keine", "keine E-Rechnung (nur PDF)")),
                  help="Kunden mit Leitweg-ID (Behörden) erhalten immer eine XRechnung. Ab 2027/2028 ist die "
                       "E-Rechnung zwischen Unternehmen schrittweise Pflicht."),
            Field("rechnung.zahlungs_qr", "QR-Code zum Bezahlen auf Rechnungen", "choice", "girocode", required=True,
                  choices=(("girocode", "GiroCode (Überweisung per Banking-App)"), ("keiner", "kein QR-Code"))),
            Field("rechnung.ablage_ordner", "Ablage-Ordner für Rechnungs-PDFs (optional, nur lokale Version)",
                  help="Zusätzliche Kopie jeder festgeschriebenen Rechnung unter <Ordner>/<Jahr>/<MM Monat>/<Kunde>/. "
                       "Leer lassen = keine Kopie. Das Original bleibt immer im Programm gespeichert.",
                  placeholder=r"z. B. D:\Buchhaltung\Rechnungen"),
        ),
    ),
    Group(
        "angebote", "Angebote",
        "Standardwerte für neue Angebote. Alles optional und jederzeit änderbar.",
        (
            Field("angebot.gueltig_tage", "Angebote gültig für … Tage", "int", 30, required=True, min=1, max=365),
            Field("angebot.einleitung", "Einleitungstext", "textarea",
                  "Gerne unterbreiten wir Ihnen folgendes Angebot:"),
            Field("angebot.schluss", "Schlusstext", "textarea",
                  "Wir freuen uns auf Ihren Auftrag. Bei Fragen stehen wir Ihnen gerne zur Verfügung."),
            Field("angebot.wiederkehrend", "Modul „Wiederkehrende Leistungen“ verwenden "
                  "(Intervall, Einsätze pro Monat, Monats- und Vertragswert)", "bool", False),
        ),
    ),
    Group(
        "email", "E-Mail",
        "Postfach für den Rechnungs-Import und für E-Mail-Entwürfe. Das Programm versendet "
        "selbst keine Mails an Kunden – es legt nur Entwürfe in Ihren Entwürfe-Ordner. "
        "Passwörter werden verschlüsselt gespeichert. Alles optional, auch später einstellbar.",
        (
            Field("imap.aktiv", "Rechnungs-Mails automatisch abrufen (alle … Minuten, siehe unten)", "bool", False,
                  help="Für E-Mail-Entwürfe genügt es, Server, Benutzername und Passwort einzutragen."),
            Field("imap.host", "IMAP-Server", placeholder="z. B. imap.example.de"),
            Field("imap.port", "IMAP-Port", "int", 993, min=1, max=65535),
            Field("imap.ssl", "Verschlüsselt (SSL/TLS)", "bool", True),
            Field("imap.benutzer", "Benutzername"),
            Field("imap.passwort", "Passwort", "secret"),
            Field("imap.ordner_eingang", "Ordner mit Rechnungs-Mails", default="Rechnungen"),
            Field("imap.ordner_nach_import", "Ordner nach Import", default="Verarbeitet",
                  help="Leer lassen = Mails nach dem Import nicht verschieben. Mails werden nie gelöscht."),
            Field("imap.ordner_entwuerfe", "Entwürfe-Ordner", help="Leer lassen = automatisch erkennen."),
            Field("imap.intervall_minuten", "Abruf alle … Minuten", "int", 10, min=1, max=1440),
            Field("email.absender", "Absender-Adresse für Entwürfe", "email"),
            Field("rechnung.mail_betreff", "Betreff für Rechnungs-Mails", default="Rechnung {nummer}",
                  help="Platzhalter: {nummer} {datum} {monat} {betrag} {faellig} {kunde} {firma} {kontoinhaber}"),
            Field("rechnung.mail_text", "Text für Rechnungs-Mails", "textarea",
                  "Guten Tag,\n\nanbei erhalten Sie unsere Rechnung {nummer} vom {datum} über {betrag}, "
                  "zahlbar bis {faellig}.\n\nMit freundlichen Grüßen\n{firma}"),
            Field("angebot.mail_betreff", "Betreff für Angebots-Mails", default="Angebot {nummer}"),
            Field("angebot.mail_text", "Text für Angebots-Mails", "textarea",
                  "Guten Tag,\n\nanbei erhalten Sie unser Angebot {nummer} vom {datum} über {betrag}.\n\n"
                  "Mit freundlichen Grüßen\n{firma}"),
            Field("smtp.host", "SMTP-Server (optional, nur für Login-Codes und Einladungen)"),
            Field("smtp.port", "SMTP-Port", "int", 465, min=1, max=65535),
            Field("smtp.ssl", "SMTP verschlüsselt (SSL/TLS, sonst STARTTLS)", "bool", True),
            Field("smtp.benutzer", "SMTP-Benutzername"),
            Field("smtp.passwort", "SMTP-Passwort", "secret"),
            Field("smtp.absender", "SMTP-Absender-Adresse", "email"),
        ),
    ),
    Group(
        "ordner", "Eingangsordner",
        "Zwei Ordner, in die Sie Rechnungen einfach hineinlegen (auch über ein Netzlaufwerk). Das Programm liest "
        "sie ein und legt sie in den passenden Prüfbereich: Eingangsrechnungen (PDF, Foto, E-Rechnung) bzw. alte "
        "Ausgangsrechnungen (PDF). Eingelesene Dateien wandern in den Unterordner „verarbeitet“, Problemfälle mit "
        "Begründung nach „fehler“ – es wird nie etwas gelöscht. Nur in der lokalen Version – auf dem Server "
        "Rechnungen bitte an das E-Mail-Postfach weiterleiten (Einstellungen → E-Mail) oder mehrere Dateien auf "
        "einmal hochladen.",
        (
            Field("ordner.aktiv", "Ordner automatisch jede Minute einlesen (solange das Programm läuft)", "bool", False,
                  help="Ohne Haken nur über den Knopf „Jetzt einlesen“ im jeweiligen Prüfbereich."),
            Field("ordner.eingang", "Ordner für Eingangsrechnungen", placeholder=r"z. B. E:\Rechnungen\Eingang"),
            Field("ordner.ausgang", "Ordner für alte Ausgangsrechnungen", placeholder=r"z. B. E:\Rechnungen\Ausgang"),
        ),
    ),
    Group(
        "mahnwesen", "Mahnwesen",
        "Drei Stufen für überfällige Rechnungen. Platzhalter in den Texten: {nummer} {datum} {faellig} {betrag} "
        "(offener Rechnungsbetrag) {gesamt} (inkl. Gebühren und Zinsen) {frist} {kunde} {firma} {stufe}. "
        "Mahnungen werden nie automatisch versendet.",
        tuple(_dunning_fields) + (
            Field("mahnung.karenz_tage", "Mahnung vorschlagen … Tage nach Fälligkeit bzw. Fristablauf", "int", 3,
                  required=True, min=0, max=90),
            Field("mahnung.zinssatz", "Verzugszinsen in % pro Jahr (leer = keine)", "decimal", min=0,
                  help="Gesetzlich: Basiszinssatz + 9 Prozentpunkte bei Geschäftskunden, + 5 bei Privatkunden "
                       "(§ 288 BGB). Der Basiszinssatz ändert sich halbjährlich – bitte selbst aktuell halten."),
            Field("mahnung.mail_betreff", "Betreff für Mahnungs-Mails", default="{stufe} zur Rechnung {nummer}"),
            Field("mahnung.mail_text", "Text für Mahnungs-Mails", "textarea",
                  "Guten Tag,\n\nanbei erhalten Sie unsere {stufe} zur Rechnung {nummer} vom {datum}. "
                  "Bitte überweisen Sie {gesamt} bis zum {frist}.\n\nMit freundlichen Grüßen\n{firma}"),
        ),
    ),
    Group(
        "datev", "DATEV-Export",
        "Angaben für den Export an Ihre Steuerberatung. Berater- und Mandantennummer erhalten Sie von dort. "
        "Alles optional, auch später einstellbar.",
        (
            Field("datev.berater", "Beraternummer", "int", min=1001, max=9999999),
            Field("datev.mandant", "Mandantennummer", "int", min=1, max=99999),
            Field("datev.zahlungen", "Zahlungen mit exportieren (Geldkonto an Kunde/Lieferant)", "bool", False,
                  help="Nur ankreuzen, wenn Ihre Steuerberatung die Kontoauszüge NICHT selbst von der Bank "
                       "bekommt – sonst würden Zahlungen doppelt gebucht."),
            Field("datev.geldkonto", "Geldkonto (Bank)", "digits",
                  help="Leer = 1200 (SKR03) bzw. 1800 (SKR04)."),
            Field("datev.sammelkreditor", "Sammelkonto für Lieferanten ohne Stammdaten", "digits", "99999",
                  required=True),
            Field("datev.sammeldebitor", "Sammelkonto für Kunden ohne Kundennummer", "digits", "69999",
                  required=True),
        ),
    ),
    Group(
        "ki", "KI-Vorschläge",
        "Das Programm schlägt bei Eingangsrechnungen das Konto vor. Zuerst lernt es aus Ihren eigenen Buchungen "
        "(ohne Internet). Optional hilft zusätzlich die KI „Claude“ der Firma Anthropic: Sie liest jeden neuen "
        "Beleg im Prüfbereich aus (Absender, Datum, Nummer, Beträge je Steuersatz, Konto), das Programm rechnet "
        "alles nach und markiert die Felder grün (sicher), gelb (bitte prüfen) oder rot (fehlt/widersprüchlich). "
        "Gebucht wird erst, wenn ein Mensch „Übernehmen“ klickt. "
        "Datenschutz: Übermittelt wird je Aufruf nur dieser eine Beleg – der Belegtext ohne Bankverbindung, "
        "E-Mail und Telefon; nur bei Scans und Fotos ohne Text das Bild selbst – sowie Ihre Kontenliste und Ihr "
        "Firmenname, auch in die USA. Bitte vorher einen Auftragsverarbeitungsvertrag (AVV/DPA) mit Anthropic "
        "abschließen und dies im Verzeichnis der Verarbeitungstätigkeiten vermerken.",
        (
            Field("ki.lernen", "Aus eigenen Buchungen lernen (ohne Internet)", "bool", True),
            Field("ki.aktiv", "Zusätzlich KI-Vorschläge mit Claude verwenden (Daten werden an Anthropic übermittelt)",
                  "bool", False),
            Field("ki.auslesen", "Neue Belege im Prüfbereich automatisch per KI auslesen (sonst nur per Knopf)",
                  "bool", True),
            Field("ki.api_key", "API-Schlüssel für Claude", "secret",
                  help="Erhalten Sie nach Anmeldung unter console.anthropic.com (kostenpflichtig, je Beleg "
                       "etwa 1–2 Cent). Wird verschlüsselt gespeichert. Alternativ ANTHROPIC_API_KEY in der .env."),
        ),
    ),
)

GROUPS_BY_KEY = {g.key: g for g in GROUPS}
FIELDS_BY_KEY = {f.key: f for g in GROUPS for f in g.fields}
SECRET_KEYS = frozenset(k for k, f in FIELDS_BY_KEY.items() if f.type == "secret")


# ------------------------------------------------------------------ Lesen
def _rows(db: Session) -> dict[str, TenantSetting]:
    # Der Firmen-Filter wird automatisch angehängt (siehe app/database.py).
    return {row.key: row for row in db.scalars(select(TenantSetting))}


def load(db: Session) -> dict[str, Any]:
    """Alle Einstellungen der aktiven Firma inkl. Standardwerten.
    Geheimnisse bleiben verschlüsselt (siehe get_secret)."""
    values = {key: f.default for key, f in FIELDS_BY_KEY.items()}
    for key, row in _rows(db).items():
        values[key] = row.value
    return values


def get(db: Session, key: str) -> Any:
    return load(db).get(key)


def get_secret(db: Session, key: str) -> str:
    stored = get(db, key)
    return decrypt(stored) if stored else ""


def set_values(db: Session, values: Mapping[str, Any]) -> None:
    """Werte direkt setzen (ohne Prüfung) – für interne Vorbelegung."""
    rows = _rows(db)
    for key, value in values.items():
        if key in rows:
            rows[key].value = value
        else:
            db.add(TenantSetting(key=key, value=value))


def form_values(db: Session, group: Group) -> dict[str, str]:
    values = load(db)
    return {f.key: display_value(f, values.get(f.key)) for f in group.fields}


def secrets_present(db: Session) -> set[str]:
    values = load(db)
    return {k for k in SECRET_KEYS if values.get(k)}


def number_examples(values: Mapping[str, Any], year: int, month: int) -> dict[str, str]:
    result = {}
    for typ, _label, _default in NUMBER_TYPES:
        fmt = values.get(f"nummern.{typ}.format") or ""
        start = values.get(f"nummern.{typ}.start") or 1
        if format_problem(str(fmt)) is None:
            try:
                result[typ] = format_number(str(fmt), year, month, int(start))
            except (TypeError, ValueError):
                pass
    return result


# ---------------------------------------------------------------- Prüfen
def _group_checks(group: Group, new: dict[str, Any], errors: dict[str, str]) -> None:
    if group.key == "steuer":
        if not new.get("steuer.steuernummer") and not new.get("steuer.ust_id"):
            errors["steuer.steuernummer"] = "Bitte Steuernummer oder USt-IdNr. angeben (Pflichtangabe auf Rechnungen)."
    if group.key == "design" and new.get("rechnung.ablage_ordner"):
        from pathlib import Path

        from app.config import get_settings

        if not get_settings().is_local:
            errors["rechnung.ablage_ordner"] = "Nur in der lokalen Version möglich – auf dem Server bitte den ZIP-Export nutzen."
        elif not Path(new["rechnung.ablage_ordner"]).is_absolute():
            errors["rechnung.ablage_ordner"] = r"Bitte einen vollständigen Pfad angeben, z. B. D:\Buchhaltung\Rechnungen."
    if group.key == "ki" and new.get("ki.aktiv") and not new.get("ki.api_key") \
            and not os.environ.get("ANTHROPIC_API_KEY"):
        errors["ki.api_key"] = "Für KI-Vorschläge nötig."
    if group.key == "ordner":
        from app.services import eingangsordner

        for key, problem in eingangsordner.setting_problems(new.get("ordner.eingang"), new.get("ordner.ausgang"),
                                                            bool(new.get("ordner.aktiv"))).items():
            errors[key] = problem
    if group.key == "email":
        if new.get("imap.aktiv"):
            for key in ("imap.host", "imap.benutzer", "imap.passwort", "imap.ordner_eingang"):
                if not new.get(key):
                    errors[key] = "Für die Postfach-Anbindung nötig."
        if new.get("smtp.host"):
            for key in ("smtp.benutzer", "smtp.passwort", "smtp.absender"):
                if not new.get(key):
                    errors[key] = "Für den SMTP-Versand nötig."


def save_group(
    db: Session,
    group: Group,
    form: Mapping[str, str],
    *,
    user_id: int | None,
    ip: str | None = None,
) -> dict[str, str]:
    """Prüft und speichert eine Gruppe. Gibt Fehler je Feld zurück (leer = gespeichert).
    Änderungen werden ins Änderungsprotokoll geschrieben. Ruft KEIN commit auf."""
    old_values = load(db)
    new_values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in group.fields:
        if f.type == "logo":
            continue
        value, error = parse_field(f, form, old_values.get(f.key))
        if isinstance(value, Decimal):
            value = str(value)  # JSON-Spalte: Decimal als Text speichern
        if error:
            errors[f.key] = error
        new_values[f.key] = value
    _group_checks(group, new_values, errors)
    if errors:
        return errors

    # Standardwerte können Decimal sein, gespeicherte Werte sind Text – gleich vergleichen
    old_subset = {k: str(v) if isinstance(v := old_values.get(k), Decimal) else v for k in new_values}
    changes = audit.diff(old_subset, new_values, SECRET_KEYS)
    if changes:
        set_values(db, new_values)
        audit.log(
            db, action="geaendert", entity="einstellungen", entity_id=group.key,
            tenant_id=db.info.get("tenant_id"), user_id=user_id, changes=changes, ip=ip,
        )
    return {}


def missing_required(db: Session) -> list[str]:
    """Titel der Gruppen, in denen Pflichtangaben fehlen (für den Assistenten)."""
    values = load(db)
    missing = []
    for group in GROUPS:
        errors: dict[str, str] = {}
        for f in group.fields:
            if f.required and values.get(f.key) in (None, ""):
                errors[f.key] = "fehlt"
        _group_checks(group, values, errors)
        if errors:
            missing.append(group.title)
    return missing
