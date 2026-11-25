# Konzept: Buchhaltung & Angebote für Kleinunternehmen (generalisiert)

> Spezifikation für die Neuentwicklung mit Claude Code. Grundlage ist das
> bewährte Buchhaltungsprogramm + Angebotstool von Gebäudeservice FH – jetzt
> **ohne feste Firmendaten**, **mandantenfähig** und **server-tauglich**.
> Arbeitsregeln für Claude Code stehen in `CLAUDE.md`.

---

## 1. Ziel & Rahmen

| Thema | Entscheidung |
|---|---|
| Zielgruppe | Deutsche Kleinunternehmen (Einzelunternehmen, kleine GmbH/UG), Gewinnermittlung per **EÜR** |
| Steuer | Regelbesteuerung **und** Kleinunternehmerregelung (§ 19 UStG) je Firma umschaltbar |
| Betrieb | **Phase 1:** lokale Testversion (127.0.0.1). **Später:** Server im Internet |
| Mandanten | **Mandantenfähig von Anfang an** – mehrere Firmen streng getrennt in einer Installation |
| Firmen anlegen | **Nur durch den Plattform-Admin** (Betreiber); Firmen-Inhaber laden ihre Mitarbeiter per Einladung ein |
| Umfang | **Ein Programm** – Buchhaltung und Angebote sind Module mit gemeinsamem Kundenstamm |
| Sprache/Format | UI Deutsch; Zahlen immer deutsch: `1.234,56 €`, Datum `TT.MM.JJJJ` – in Eingabe **und** Anzeige |

Nicht-Ziele (vorerst): doppelte Buchführung/Bilanzierung (HGB-Bilanz), Lohnbuchhaltung,
Lager/Warenwirtschaft, Österreich/Schweiz, Selbstregistrierung mit Abo-Abrechnung
(Konzept aber so anlegen, dass sie nachrüstbar ist).

## 2. Technik

| Bereich | Vorgabe |
|---|---|
| Backend | Python 3.12+, **FastAPI**, uvicorn |
| Datenbank | **SQLAlchemy 2.0** + **Alembic** (jede Schema-Änderung = neue Revision) |
| DB-System | **SQLite** lokal, **PostgreSQL** auf dem Server – umschaltbar nur über `DATABASE_URL` in `.env` |
| Oberfläche | Serverseitig gerenderte **Jinja2**-Seiten + **HTMX** für Teil-Aktualisierungen, eigenes schlichtes CSS |
| PDF | reportlab (Rechnung, Angebot, Mahnung, EÜR-Bericht) |
| PDF-Auslese | pdfplumber (Textebene) + **OCR** (Tesseract, `deu`) für Scans/Fotos |
| E-Rechnung | Lesen/Schreiben **ZUGFeRD/Factur-X** (PDF/A-3 mit XML) und **XRechnung** (UBL/CII), z. B. Bibliothek `factur-x` + eigene XML-Erzeugung; Validierung gegen die offiziellen Schemata. **Umgesetzt (Schritt 8):** Schreiben als CII (EN 16931 bzw. XRechnung 3.0), XSD-Prüfung offline + eigene Rechenregeln; die Schematron-Prüfung der Bibliothek wird NICHT genutzt (würde Daten an einen Prüfserver senden). PDF/A-3: eingebettete Schriften (Vera), sRGB-Farbprofil, XMP – nicht mit veraPDF geprüft. Lesen: CII und UBL, als Datei oder im PDF eingebettet, über defusedxml. |
| Geldbeträge | Immer `Decimal` / `Numeric(12,2)`, kaufmännisch runden (ROUND_HALF_UP) – **nie float** |
| Hintergrundjobs | Phase 1: Thread/APScheduler im Prozess. Server: eigener Worker-Prozess (Konzept so trennen, dass das leicht geht) |
| Start lokal | `python run.py` → legt `.env` an (SECRET_KEY, Verschlüsselungsschlüssel), `alembic upgrade head`, startet uvicorn auf 127.0.0.1:8000; zusätzlich `start.bat` für Doppelklick |
| Server (später) | Docker-Compose (App, Worker, PostgreSQL, Reverse-Proxy mit HTTPS) – *Vorschlag, noch zu bestätigen* |

### Projektstruktur (Vorschlag)

```
app/
  main.py            FastAPI-App, Fehlerseiten, /health, Setup-Gate
  config.py          Einstellungen aus .env
  database.py        Engine/Session, Mandanten-Filter
  models/            ORM-Modelle je Bereich
  services/          Fachlogik ohne Web-Bezug (calc, steuern, euer, nummernkreise,
                     erechnung, extraktion, ocr, email_import, email_entwurf,
                     datev_export, bankabgleich, mahnwesen, ablage, backup)
  routers/           Seiten/Endpunkte je Modul
  templates/  static/
alembic/
tests/               pytest – Kalkulation, Steuer, Nummernkreise, Mandantentrennung …
run.py  start.bat  requirements.txt  .env.example
```

## 3. Mandanten & Sicherheit

- Tabelle `tenants` (Firma). **Jede fachliche Tabelle hat `tenant_id`** (Pflicht, Index).
- Mandanten-Trennung **zentral** erzwingen (z. B. Request-Abhängigkeit, die die aktive
  Firma setzt, + Hilfsfunktion/Query-Filter, der automatisch nach `tenant_id` filtert).
  Pflicht-Test: Ein Nutzer von Firma A sieht/ändert unter keinen Umständen Daten von Firma B.
- Dateien (PDFs, Logos) je Firma getrennt: `data/files/<tenant_id>/…`, Dateinamen mit UUID.
- Ein Benutzer kann (später) zu mehreren Firmen gehören → Tabelle `memberships`
  (user, tenant, rolle, rechte). Firmenwechsel oben in der Navigation.
- **Plattform-Admin** (Betreiber) ist getrennt von Firmenrollen: legt Firmen + ersten
  Inhaber an, sperrt Firmen, sieht keine Buchhaltungsdaten der Firmen (nur Verwaltung).
- **Zugangsdaten verschlüsselt** speichern (IMAP/SMTP-Passwörter, z. B. Fernet mit
  Schlüssel aus `.env`) – nicht mehr im Klartext wie bisher in `auth_store.json`.
- Passwörter: Argon2/bcrypt. Sessions serverseitig, CSRF-Schutz, sichere Cookies
  (auf dem Server `Secure` + HTTPS), Login-Rate-Limit.
- Login mit **2. Faktor per E-Mail-Code** (wie bisher; HTML-Mail im Firmendesign,
  Text-Fallback). Optional später TOTP-App.
- Uploads: nur echte PDFs/Bilder (Magic Bytes), max. 25 MB, Dateinamen bereinigen.

## 4. Benutzer, Rollen & Rechte

Rechte **pro Modul** (lesen / bearbeiten), Rollen sind nur Vorlagen und pro Benutzer feinjustierbar:

| Rolle | Standardrechte |
|---|---|
| Inhaber | alles inkl. Einstellungen, Benutzerverwaltung, Festschreiben |
| Buchhaltung | Rechnungen, Prüfbereich, Kunden, Steuern, EÜR, Bank, Mahnwesen, Export |
| Mitarbeiter | Angebote, Kunden (bearbeiten), eigene Rechnungen erstellen (kein Festschreiben) |
| Steuerberater | alles **nur lesen** + DATEV-Export |

Module: `dashboard`, `rechnungen_eingang`, `rechnungen_ausgang`, `pruefbereich`,
`kunden`, `angebote`, `katalog`, `euer`, `steuern`, `bank`, `mahnwesen`, `export`,
`einstellungen`, `benutzer`. **Jede Route prüft ihr Modulrecht** (zentral, nicht vergessen!).

Neue Benutzer: Einladung per Link (zeitlich begrenzt) durch Inhaber. Das bisherige
Vier-Augen-Prinzip (Freischaltcode durch IT) entfällt bzw. wird durch die Einladung ersetzt.

**Admin-Passwort (entschieden 29.10.2026):** Einstellungen (inkl. Datensicherung), Benutzerverwaltung und
Firmenverwaltung verlangen zusätzlich ein **persönliches zweites Passwort** je Person (anders als das
Anmelde-Passwort). Die Freigabe gilt je Anmeldung und endet nach 15 Minuten ohne Nutzung eines Admin-Bereichs.
Vergessen → neues mit E-Mail-Code + Anmelde-Passwort. Während der ersten Einrichtung noch nicht nötig.

**Zugriffsmatrix (entschieden 29.10.2026):** Seite mit allen Personen der Firma × Bereichen und Sonderrechten,
direkt bearbeitbar (Inhaber fest mit allen Rechten; niemand vergibt mehr, als er selbst hat).

## 5. Einrichtung & Firmen-Einstellungen

Beim ersten Start lokal: Assistent legt Plattform-Admin + erste Firma an.
Auf dem Server: Admin legt Firma an, Inhaber durchläuft beim ersten Login den
**Einrichtungs-Assistenten**:

1. Firmendaten: Name, Rechtsform, Adresse, Inhaber/Geschäftsführer, Handelsregister (optional)
2. Steuer: Steuernummer, USt-IdNr., **Regelbesteuerung / Kleinunternehmer**,
   Ist-/Soll-Versteuerung, Voranmeldungszeitraum (monatlich/quartalsweise/jährlich)
3. Bank: Kontoinhaber, IBAN (Prüfziffer prüfen), BIC, Bank
4. Kontenrahmen: **SKR03 oder SKR04**
5. Nummernkreise: Format + Startwert je Jahr (Rechnung, Angebot, Mahnung, Storno)
6. Design: Logo, Akzentfarbe, Fußzeile, Danke-Text, Zahlungsziel, Standard-USt-Satz
7. E-Mail: IMAP-Postfach (Abruf + Entwürfe-Ordner), optional SMTP nur für Login-Codes

Alle Werte später unter **Einstellungen** änderbar (wie heute: Gruppen, Vorschau-PDF ohne
Speichern). Nichts Firmenspezifisches im Code – alles in der Tabelle `tenant_settings`.

## 6. Module (Version 1)

### 6.1 Dashboard
Kennzahlen im gewählten Zeitraum: Einnahmen/Ausgaben/Überschuss (netto+brutto),
offene Posten (Forderungen/Verbindlichkeiten), überfällige Rechnungen, Anzahl im
Prüfbereich, USt-Zahllast laufender Zeitraum, Monatsverlauf.

### 6.2 Eingangsrechnungen (Ausgaben)
- Upload (mehrere Dateien), Felder automatisch auslesen: Nummer, Datum, Fälligkeit,
  Absender, Netto, USt-Satz/-Betrag, Brutto. Reihenfolge: **E-Rechnung-XML** (exakt) →
  PDF-Textebene → **OCR**. Unsicher erkannte Felder → Markierung „bitte prüfen“.
- Detailansicht mit **PDF-Vorschau neben dem Formular**; fehlende Steuerwerte werden aus
  den übrigen berechnet; **Aufteilung auf mehrere USt-Sätze** + Skonto (wie heute).
- Kategorie/Konto (SKR03/04) zuordnen; Vorschlag anhand früherer Rechnungen desselben Lieferanten.
- Lieferanten-Stamm (analog Kunden).
- Status: offen / bezahlt (Datum) / storniert. Mehrfachauswahl: duplizieren, drucken, ZIP-Download.

- **Entschieden (28.10.2026):**
  - Auch **von Hand hochgeladene** Eingangsrechnungen landen zuerst im **Prüfbereich** und zählen erst
    nach „Übernehmen“ mit.
  - **Festschreiben** per Knopf (Sonderrecht „festschreiben“); spätestens beim Erfassen der ersten Zahlung
    wird automatisch festgeschrieben.
  - **Skonto**: Rechnung wird mit vollem Betrag erfasst, Skonto-Satz/-Frist gespeichert; der Abzug erfolgt
    erst beim Bezahlen (Vorsteuer wird anteilig korrigiert).
- **Umgesetzt (Schritt 9): OCR** über das kostenlose Programm Tesseract (lokal, keine Daten an fremde Server),
  bevorzugt Deutsch. Gilt für Fotos und PDFs ohne Textebene. Ist Tesseract nicht installiert, läuft alles wie
  bisher mit Handeingabe. Per OCR gelesene Beträge sind immer „bitte prüfen“.

### 6.3 Prüfbereich (Kernkonzept – unbedingt beibehalten!)
- Alles, was **automatisch** ins Programm kommt (E-Mail-Import, später ggf. Upload-Ordner),
  landet mit `pending = true` im **Prüfbereich**.
- Solche Belege zählen **nicht** zu Rechnungslisten, EÜR, Steuern, Kundenstatistik, Export,
  bis ein Mensch sie prüft und „Übernehmen“ klickt.
- **Jede Auswertung/Abfrage muss `pending = false` filtern** – am besten über eine zentrale
  Query-Funktion, damit es nicht vergessen werden kann. Test dafür schreiben.

### 6.4 E-Mail-Rechnungsimport (IMAP)
Bewährte Regeln aus dem Original 1:1 übernehmen:
- Postfach, Eingangsordner und „Ordner nach Import“ je Firma einstellbar; Abruf im
  Hintergrund (Standard 10 Min.) + Button „Jetzt abrufen“.
- **Mails werden niemals gelöscht, nur verschoben.** IMAP `MOVE`; falls nicht unterstützt:
  COPY → Zielordner nachzählen → erst dann `\Deleted` + `UID EXPUNGE`. Kontrolle schlägt fehl →
  alles bleibt liegen. Leeres Feld „Ordner nach Import“ = nicht verschieben.
  Umgesetzt (Schritt 6) strenger als im Altprogramm: Kann der Server weder MOVE noch UIDPLUS, wird gar
  nicht verschoben (kein pauschales EXPUNGE, das fremde gelöschte Mails mitnehmen würde).
- Neue Ordner mit echtem Server-Trennzeichen anlegen **und abonnieren** (`SUBSCRIBE`),
  sonst wirken die Mails im Webmail „verschwunden“.
- Ordner-Auflösung tolerant (`INBOX.X` / `INBOX/X`, Groß/Klein, eingefügte `imap://`-Adressen).
- Duplikatschutz über Message-ID (je Firma). TLS-Zertifikatsprüfung. Ordnernamen bereinigen (IMAP-Injection).
- Anhänge: PDF, ZUGFeRD-PDF, XRechnung-XML. Diagnose-Seite: zeigt Server-Ordner + Mailanzahl.

### 6.5 Ausgangsrechnungen (eigene Rechnungen)
- Erstellen aus: leer, **Angebot**, Duplikat, wiederkehrende Vorlage.
- Positionen: **Titel (Pflicht, fett) + Beschreibung (optional)**, Menge, Einheit, Einzelpreis,
  USt-Satz je Position, Rabatt; Leistungszeitraum/-datum, Objekt-/Leistungsadresse.
- Pflichtangaben § 14 UStG prüfen, bevor festgeschrieben werden kann.
- Kleinunternehmer: keine USt ausweisen, Hinweistext § 19 UStG automatisch.
  Weitere Hinweise wählbar (z. B. § 13b Reverse-Charge, § 35a Haushaltsnahe Dienstleistung – Lohnanteil ausweisen).
- Ausgabe: PDF im Firmendesign (DIN-5008-Layout), **GiroCode** (Standard) / eigenes QR-Bild / keiner.
  **E-Rechnung:** ZUGFeRD (PDF + XML) als Standard, XRechnung wählbar (z. B. Behörden, Leitweg-ID).
- Nummernkreis lückenlos, Format einstellbar (Standard `JJJJ-NNN`).
- Ablage-Kopie in Ordnerstruktur `<Jahr>/<MM Monat>/<Kunde>/` (lokal: Ordner; Server:
  stattdessen ZIP-Download je Zeitraum) – Fehler blockieren das Erstellen nicht.

### 6.6 GoBD: Festschreibung, Storno, Protokoll
- Zustände: **Entwurf** (frei änderbar, löschbar) → **festgeschrieben** (unveränderbar, Nummer vergeben).
- Korrektur nur über **Stornorechnung** (Gegenbuchung mit Bezug) + ggf. neue Rechnung.
- Belege (Eingang wie Ausgang) **nicht löschbar**, solange die Aufbewahrungsfrist
  (i. d. R. 8–10 Jahre) läuft; nur Entwürfe und ungeprüfte Prüfbereich-Einträge sind löschbar
  (mit Protokolleintrag).
- **Änderungsprotokoll** (wer, wann, was, alt/neu) für alle buchhaltungsrelevanten Daten.
- Original-Dateien unverändert speichern + Prüfsumme (SHA-256).
- Verfahrensdokumentation als Vorlage/Text im Programm (später).

### 6.7 Kunden (gemeinsam für Angebote + Rechnungen)
Name, Ansprechpartner, Adresse, abweichende Rechnungsadresse, mehrere Objekt-/Leistungsadressen,
E-Mail, Telefon, USt-IdNr., Leitweg-ID (Behörden), Kundennummer/Debitorenkonto, Notizen.
Detailseite: Angebote, Rechnungen, offene Beträge, Umsatz (nur geprüfte Belege!).

### 6.8 Angebote
- Basis: bewährte Kalkulation des Angebotstools, generalisiert.
- Positionen aus **Artikel-/Leistungskatalog** (Einheiten: Stück, Std., m², m, pauschal, …;
  Preis netto, USt-Satz, Beschreibungstext, optional interner Kostensatz).
- Abrechnungsarten je Position: Menge × Preis | Stunden × Satz × Personen | Pauschal.
- Zusatzkosten-Blöcke (Anfahrt, Material, Entsorgung, frei benennbar).
- **Modul „Wiederkehrende Leistungen“ (zuschaltbar):** Intervall (täglich ≈ 21, wöchentlich ≈ 4,33,
  monatlich, individuell) → Einsätze/Monat, Monats-/Jahres-/Vertragswert; daraus später
  **automatische Rechnungs-Entwürfe** je Abrechnungszeitraum (nie automatisch versenden).
- Status: Entwurf, versendet, angenommen, abgelehnt, abgelaufen. **Angebot → Rechnung per Klick.**
- Interne Kalkulation (Kosten, Marge) nur für berechtigte Rollen sichtbar, nie im PDF.
- Kalkulation als reine, getestete Funktionen (`services/calc.py`), Tests aus dem Original übernehmen.
- **Entschieden/umgesetzt (Schritt 10, 28.10.2026):** Aus einem Angebot mit wiederkehrenden Positionen entsteht
  per Klick ein **Vertrag** (Monatsbetrag je Position, Laufzeit aus dem Angebot; auch leer für einen Kunden anlegbar).
  Abrechnung **je Vertrag wählbar**: am Monatsanfang für den Vormonat (Standard) oder im Voraus für den laufenden
  Monat. Der Hintergrund-Prozess legt je fälligem Monat einen **normalen Rechnungsentwurf** an (Rechnungsliste,
  Hinweis auf der Übersicht – nicht im Prüfbereich); verpasste Monate werden nachgeholt (höchstens 12 auf einmal).
  Festschreiben und Versenden bleiben Handarbeit. Verträge lassen sich pausieren und enden automatisch.

### 6.9 EÜR & Auswertungen
- Einnahmen-Überschuss-Rechnung nach Kategorien (**SKR03/SKR04**), Zeitraum frei / Monat /
  Quartal / Jahr; Zuordnung zu den Zeilen der Anlage EÜR (Vorschlag).
- Ist-Prinzip (Zufluss/Abfluss nach Zahlungsdatum) – Ausnahme 10-Tage-Regel für regelmäßige Zahlungen beachten (Hinweis).
- Export als PDF und CSV.

- **Umgesetzt (Schritt 7):** Einnahmen/Ausgaben nach Zahlungsdatum, je Zahlung anteilig auf die
  Steuersätze verteilt. Zuordnung zu Positionen der Anlage EÜR nur nach Bezeichnung (Zeilennummern ändern
  sich jährlich). USt-Zahlungen an das bzw. Erstattungen vom Finanzamt kommen aus dem Bankabgleich (Schritt 9).

### 6.10 Steuern
- **Entschieden (28.10.2026):** Ist- **und** Soll-Versteuerung werden unterstützt (je Firmeneinstellung).
  Vorsteuer in beiden Fällen nach Rechnungsdatum (§ 15 UStG), Skonto-Korrektur im Zahlungszeitraum.
- **Entschieden:** Finanzkennzahlen auf der Übersicht sieht nur, wer das Recht „EÜR“ hat.
- Umsatzsteuer, Vorsteuer, Zahllast/Erstattung je Voranmeldungszeitraum, nach Steuersätzen
  aufgeschlüsselt; Kennzahlen passend zur UStVA vorbereiten (kein ELSTER-Versand in V1).
- Ist- oder Soll-Versteuerung je nach Firmeneinstellung. Kleinunternehmer: Umsatzgrenzen-Monitor.
- Hinweis: ersetzt keine Steuerberatung.

### 6.11 Bankabgleich
- Import Kontoauszug **CSV** (gängige Banken, Spalten-Zuordnung speicherbar) und **CAMT.053**.
- Automatischer Vorschlag Zahlung ↔ offene Rechnung (Betrag, Rechnungsnummer im Verwendungszweck, Name).
  Nutzer bestätigt → Rechnung „bezahlt“ mit Datum. Teilzahlungen möglich. Keine Bank-Online-Anbindung in V1.
- **Umgesetzt (Schritt 9):** Eine Zuordnung ist eine normale Zahlung mit Verweis auf den Bankumsatz; ein Umsatz
  kann auf mehrere Rechnungen verteilt werden (Sammelüberweisung). Doppelt importierte Umsätze (überlappende
  Auszüge) werden erkannt. „Sichere“ Vorschläge (Rechnungsnummer + Betrag) per Knopf auf einmal übernehmen.
  Umsätze ohne Rechnung: „Umsatzsteuer an/vom Finanzamt“ (zählt in der EÜR), „privat“, „nicht relevant“.
  Gezahlte Mahngebühren/Zinsen werden noch nicht gesondert gebucht (Zahlung nur bis zum Rechnungsbetrag).

### 6.12 Mahnwesen
- Überfällige Rechnungen auflisten; Stufen (Zahlungserinnerung, 1. und 2. Mahnung) mit Fristen und
  Texten je Firma; optional Mahngebühr/Verzugszinsen.
- Mahnung als PDF + **E-Mail-Entwurf** (siehe 6.14) – nie automatisch versenden.
- **Umgesetzt (Schritt 9):** Drei Stufen als Firmen-Einstellungen (statt eigener Tabelle `dunning_levels`):
  Zahlungserinnerung (0 €), 1. Mahnung (5 €), 2. Mahnung (5 €), je 7 Tage Frist – alles änderbar. Gebühren
  summieren sich; Verzugszinsen optional (Zinssatz selbst pflegen, Basiszins ändert sich halbjährlich).
  Mahnnummer aus dem Nummernkreis „Mahnung“, PDF wird einmal erzeugt und gespeichert.
  Empfehlung, bitte prüfen: Standardwerte (Gebühren/Fristen) mit der eigenen Praxis abgleichen.

### 6.13 Export für Steuerberater
- **DATEV-Buchungsstapel (CSV, EXTF-Format)** je Zeitraum, Konten nach SKR03/04, inkl. Belegverweis.
- Zusätzlich ZIP mit allen Beleg-PDFs des Zeitraums + einfache CSV-Übersicht.
- Nur geprüfte, festgeschriebene Belege.
- **Umgesetzt (Schritt 9):** EXTF-Format 700, Rechnungen über Personenkonten (Kundennummer = Debitor,
  Lieferantennummer = Kreditor, Sammelkonten 69999/99999 ohne Stammdaten), Vorsteuer per BU-Schlüssel 9/8
  (nicht bei Automatikkonten, nicht bei Kleinunternehmern). Zahlungen nur auf Wunsch (Einstellung), damit nichts
  doppelt gebucht wird, wenn die Steuerberatung die Kontoauszüge selbst bekommt. Ein Zeitraum = ein Kalenderjahr.
  Noch offen: Export der Kunden-/Lieferanten-Stammdaten im DATEV-Format; ersten Import mit der Steuerberatung testen.

### 6.14 E-Mail-Entwürfe (Grundsatz: KEIN automatischer Versand)
- Programm erstellt die Mail (Betreff/Text aus Vorlage mit Platzhaltern `{nummer}`, `{monat}`,
  `{kunde}`, `{firma}`, `{kontoinhaber}` …, Anhang PDF/E-Rechnung) und legt sie per IMAP `APPEND`
  mit Flag `\Draft` in den **Entwürfe-Ordner des Firmenpostfachs**. Der Nutzer sendet selbst aus
  seinem Mailprogramm. Funktioniert lokal und auf dem Server gleich.
- Fallback: `.eml`-Datei herunterladen.
- Absender-Adresse je Firma einstellbar. Status „Entwurf erstellt am …“ an der Rechnung.
- SMTP wird **nur** für System-Mails (Login-Code, Einladung) genutzt.

### 6.15 Datensicherung
- Lokal: automatische Sicherung bei Start (nur bei Änderungen, letzte 10 Stände) von DB + Dateien.
- Server: täglicher DB-Dump + Dateien, Aufbewahrung konfigurierbar, Wiederherstellungs-Anleitung.
- Firmen-Export (alle Daten einer Firma als ZIP) – wichtig für Datenschutz/Kündigung.
- **Umgesetzt (Schritt 10):** Lokal ZIP (Datenbank + Dateien) beim Start **vor** dem Datenbank-Update, nur bei
  Änderungen, letzte 10. Server: `deploy/sicherung.sh` (pg_dump + Dateien, 30 Tage, per cron). Firmen-Export als ZIP
  mit JSON je Tabelle + allen Dateien, ohne Passwörter, nur für Inhaber/in. Anleitung inkl. Wiederherstellung:
  `BETRIEB.md`.

## 7. Datenmodell (Überblick)

Alle Tabellen außer Plattform-Tabellen mit `tenant_id`, `created_at`, `updated_at`.

| Tabelle | Inhalt |
|---|---|
| `tenants`, `tenant_settings` | Firma, alle Einstellungen (Key/Value oder JSON, typisiert) |
| `users`, `memberships`, `sessions`, `invitations`, `login_codes` | Benutzer & Zugriff |
| `customers`, `customer_addresses`, `suppliers` | Stammdaten |
| `catalog_items` | Artikel/Leistungen |
| `quotes`, `quote_positions`, `quote_cost_items` | Angebote |
| `recurring_contracts` | wiederkehrende Leistungen (aus Angebot) |
| `documents` | Belege: `direction` (eingang/ausgang), `kind` (rechnung/storno/gutschrift), `state` (entwurf/festgeschrieben), `pending`, `status` (offen/bezahlt/storniert), Beträge, `account` (SKR), `customer_id`/`supplier_id`, `storno_of_id`, `needs_review` |
| `document_lines` | Positionen inkl. USt-Satz (statt JSON wie bisher) |
| `files` | gespeicherte Dateien inkl. SHA-256, Typ (pdf/zugferd/xrechnung/bild) |
| `payments`, `bank_transactions`, `bank_imports` | Zahlungen & Kontoauszüge |
| `dunning_levels`, `dunnings` | Mahnwesen |
| `number_ranges` | Nummernkreise je Firma/Typ/Jahr (gesperrt vergeben, lückenlos) |
| `email_accounts`, `email_import_log` | Postfächer (verschlüsselt), Import-Duplikatschutz |
| `audit_log` | Änderungsprotokoll |

## 8. Übernahme aus dem bestehenden Programm

- Funktionen/Regeln aus Abschnitt 6.3, 6.4, 6.5 sind erprobt – Logik übernehmen, Code neu strukturieren.
- Alte Programme dienen nur als **Referenz** (lesend) und werden nicht verändert.
- **Entschieden (28.10.2026): Keine Altdaten-Übernahme.** Das neue Programm ist eine Rohversion
  für neue Unternehmen; es gibt kein Import-Werkzeug für die alten Datenbanken.
- **Ergänzt (02.11.2026): Alte Ausgangsrechnungen als PDF hochladen.** Archiv + Auswertungen (zählen nach dem
  Übernehmen wie eigene Rechnungen), Original-PDF unverändert, Auslesen + Prüfen, „bezahlt am“ beim Übernehmen,
  Nummernkreis zieht automatisch nach, doppelte Nummern werden abgelehnt.
- Entfällt: Windows-Zwischenablage (pywin32), Thunderbird/Outlook-Entwurf per Kommandozeile,
  Base64-Logo im Code, feste Firmendaten in `config_rechnung.py`.

## 9. Umsetzungs-Reihenfolge (Vorschlag)

1. Grundgerüst: Projekt, Config, DB/Alembic, Mandanten, Plattform-Admin, Login + E-Mail-Code, Rollen/Rechte, Einrichtungs-Assistent, Einstellungen
2. Kunden, Lieferanten, Katalog
3. Belege: Eingangsrechnungen + Auslese (PDF-Text), Prüfbereich, Detailansicht mit Vorschau
4. Ausgangsrechnungen: Erstellung, PDF, Nummernkreise, Festschreibung/Storno, Audit-Log
5. Angebote + Kalkulation + Angebot → Rechnung
6. E-Mail-Import (IMAP) + E-Mail-Entwürfe (IMAP APPEND)
7. EÜR, Steuern, Dashboard
8. E-Rechnung (ZUGFeRD/XRechnung lesen + schreiben)
9. OCR, Bankabgleich, Mahnwesen, DATEV-Export
10. Wiederkehrende Leistungen, Backup/Firmen-Export, Server-Betrieb (Docker, PostgreSQL, HTTPS)

Nach jedem Schritt: Tests grün, Programm startet, kurze laienverständliche Zusammenfassung.

## 10. Rechtlicher Hintergrund (Stand Okt. 2026, vor Umsetzung prüfen)

- **E-Rechnung (B2B, Inland):** Empfang seit 01.01.2025 Pflicht. Ausstellung: bis Ende 2026
  Übergang; 2027 Pflicht für Firmen mit Vorjahresumsatz > 800.000 €; ab 01.01.2028 für alle
  (Kleinunternehmer sind von der Ausstellungspflicht ausgenommen, müssen aber empfangen können).
- **GoBD:** Unveränderbarkeit, Nachvollziehbarkeit, Aufbewahrung (Rechnungen i. d. R. 8 Jahre, Bücher 10 Jahre).
- **§ 14 UStG** Pflichtangaben, **§ 19 UStG** Kleinunternehmer (Grenzen 25.000 € Vorjahr / 100.000 € lfd. Jahr).
- **DSGVO** (Server-Betrieb): AV-Vertrag mit Firmen, Hosting in der EU, Löschkonzept nach Fristablauf.

Das Programm ist ein Hilfsmittel und ersetzt keine Steuerberatung.

## 11. Offene Punkte (vor bzw. während der Umsetzung klären)

- Server-Hosting: **Docker entschieden (28.10.2026)** – Paket aus Programm, Hintergrund-Prozess, PostgreSQL und
  Caddy (automatisches HTTPS) liegt bei (`docker-compose.yml`, `BETRIEB.md`), aber noch nicht auf einem echten
  Server getestet. Offen: Anbieter (EU), Domain, externes Backup-Ziel.
- Preis-/Abo-Modell und Selbstregistrierung (später)
- Branchen-Vorlagen für den Katalog (Reinigung, Handwerk, Beratung) beim Einrichten?
- ~~TOTP-App als 2. Faktor zusätzlich zum E-Mail-Code?~~ **Umgesetzt 11.11.2026** als Alternative: mit eingerichteter App
  nur noch App-Code oder Notfall-Code, kein E-Mail-Code.
- **Eingangsordner (entschieden 04.11.2026):** je Firma zwei frei wählbare Ordner für Eingangs- und alte
  Ausgangsrechnungen; Einlesen jede Minute + Knopf; Dateien danach nach „verarbeitet“/„fehler“ verschieben, nie löschen.
  Auf dem Server (entschieden 11.11.2026): kein SFTP – Rechnungen per E-Mail-Postfach oder Mehrfach-Upload.
- **KI-Vorschläge (entschieden 02.11.2026):** erst aus eigenen Buchungen lernen (lokal), bei Unbekanntem Claude
  (Anthropic, je Firma abschaltbar, AVV nötig). Vorschläge werden nur vorausgefüllt und gelb markiert, ein Mensch
  bestätigt. Umfang in drei Schritten: 1. Konten bei Eingangsrechnungen (fertig), 2. Bankumsätze zuordnen,
  3. Belege per KI auslesen (bei schlechten Scans).
