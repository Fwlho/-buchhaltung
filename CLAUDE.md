# CLAUDE.md – Buchhaltung & Angebote (generalisierte Version)

Neuentwicklung eines Buchhaltungs- und Angebotsprogramms für **deutsche Kleinunternehmen**,
mandantenfähig, zunächst als lokale Testversion, später auf einem Server.

**Die vollständige Spezifikation steht in `KONZEPT.md` – vor jeder Arbeit lesen.**
Diese Datei enthält nur die Arbeitsregeln und wird bei Struktur- oder Konzeptänderungen mitgepflegt
(neue Module, Ordner, Befehle hier kurz eintragen).

## Kurzüberblick

- Stack: Python 3.12+, FastAPI, SQLAlchemy 2.0, Alembic, Jinja2 + HTMX, reportlab, pdfplumber, factur-x, defusedxml, Tesseract-OCR
- DB: SQLite lokal, PostgreSQL auf dem Server – nur über `DATABASE_URL` in `.env`
- Start lokal: `python run.py` (legt `.env` an, sichert, migriert, startet auf http://127.0.0.1:8000) bzw. `start.bat`
- Server: Docker (`Dockerfile`, `docker-compose.yml`: app, worker, PostgreSQL, Caddy/HTTPS; `deploy/`, Upload vom
  Windows-Rechner mit `deploy\hochladen.ps1 -Server root@IP` – überträgt nie `.env`/`data`). Anleitung für
  Einrichtung, Updates, Sicherung und Wiederherstellung: `BETRIEB.md`. Server-Sicherung `deploy/sicherung.sh` (bash): nur
  verschlüsselt (age an `BACKUP_AGE_EMPFAENGER`, bricht sonst ab) + rsync zur Storage Box; `deploy/wiederherstellen.sh`
  (`pruefen`/`zurueck`); Tests mit nachgebildeten Werkzeugen `tests/test_server_sicherung.py`.
  Härtung (11.11.2026): `docker-compose.yml` mit Netzen web/intern (internal, DB)/ausgang, `read_only`, `cap_drop: ALL`,
  `no-new-privileges`, Protokoll-Grenzen; `deploy/Caddyfile` Schutz-Kopfzeilen; `deploy/server-haertung.sh` (ufw, SSH nur
  Schlüssel mit Aussperr-Schutz, fail2ban, unattended-upgrades). Skripte immer mit LF-Zeilenenden (Upload wandelt um).
  Tests `tests/test_server_haertung.py` (wertet die Compose-Datei mit `docker compose config` aus). Die komplette Testsuite läuft auch gegen
  PostgreSQL (28.10.2026 grün); das Docker-Paket selbst ist noch nicht auf einem echten Server gelaufen.
- **GitHub + Installer** (seit 25.11.2026): `Clean Version` ist ein Git-Repository, Ziel `github.com/Fwlho/-buchhaltung`
  (Zweig `main`, öffentlich). `.gitignore` schließt `data*/`, `.env`, `.venv`, `.claude/`, `SICHERHEITSPRUEFUNG.md`,
  `PRUEFBERICHT.md` aus – vor jedem Push prüfen, dass keine Daten/Schlüssel dabei sind. CleanVersion1 (Echtdaten) nie
  hochladen. `installer/buchhaltung_installer.py` (nur Standardbibliothek) lädt `main` als ZIP, spielt den Code ein (nie
  `data`, `.env`, `.venv`; Liste `.installiert.json`, alter Stand in `alte_version/`), installiert bei Bedarf Python von
  python.org, legt `.venv` + Pakete + Desktop-Verknüpfung an. Bauen: `installer\bauen.bat` → `installer\dist\
  Buchhaltung-Installer.exe` (PyInstaller in `installer\.bau`). Tests `tests/test_installer.py`.
- Tests: `.venv\Scripts\python.exe -m pytest` (Tests nutzen eine eigene Temp-DB, siehe `tests/conftest.py`).
  Gegen PostgreSQL: `TEST_DATABASE_URL=postgresql+psycopg://…/buchhaltung_test` setzen (Name muss „test“ enthalten;
  lokal z. B. mit dem Paket `pgserver` in einer separaten Python-3.9-Umgebung). Bei Datenbank-Änderungen beide prüfen.
- Prüfbericht Sicherheit/DSGVO/GoBD mit offenen Punkten: `SICHERHEITSPRUEFUNG.md` (bei Erledigung dort abhaken).
- Neue Migration: `.venv\Scripts\python.exe -m alembic revision --autogenerate -m "..."` (gegen eine leere Hilfs-DB)

## Stand & Struktur (Schritte 1–10 fertig: Grundgerüst, Stammdaten, Eingangsrechnungen + Prüfbereich, Ausgangsrechnungen, Angebote, E-Mail, EÜR/Steuern/Übersicht, E-Rechnung, OCR/Bank/Mahnwesen/DATEV, Verträge/Sicherung/Server)

- **Migrationen unter SQLite:** `alembic/env.py` schaltet die Fremdschlüssel-Prüfung während der Migration
  AUS (sonst löscht ein Tabellen-Neuaufbau per ON DELETE CASCADE abhängige Zeilen) und prüft danach mit
  `PRAGMA foreign_key_check`. Test `tests/test_migrationen.py` migriert über vorhandene Daten – bei neuen
  Migrationen, die bestehende Tabellen ändern, dort Testdaten ergänzen.

- `app/database.py` – Engine/Session; **zentraler Mandanten-Filter** per ORM-Ereignis: Modelle mit
  `TenantMixin` werden automatisch nach `session.info["tenant_id"]` gefiltert, ohne aktive Firma → Fehler.
  `PendingMixin` filtert `pending = true` automatisch weg (Prüfbereich: `.execution_options(include_pending=True)`).
  Firma vorübergehend setzen: `tenant_scope(db, tenant_id)`. Der Filter greift auch bei Abfragen ohne
  ORM-Klasse (`select(func.count()).select_from(Model)`, `select(Model.__table__)`); Firmen-Tabellen in
  Unterabfragen/Aliasen ohne ORM-Klasse werden abgelehnt. Belege aus dem Prüfbereich laden: `belege.get()`.
- `app/deps.py` – Schutz-Abhängigkeiten: `public`, `require_user`, `require_platform_admin`,
  `require_module(modul, edit=…)`. Jede Route braucht eine (Test `tests/test_routen_schutz.py`). CSRF global.
- **Admin-Passwort** (zweites, persönliches Passwort): Firmenverwaltung und die Module in `deps.ADMIN_MODULES`
  (`einstellungen` inkl. Datensicherung, `benutzer`) verlangen eine Freigabe je Sitzung (`sessions.admin_until`,
  **5 Min.** ohne Nutzung, `require_admin_unlock`) – nicht während der ersten Einrichtung. Logik
  `app/services/admin_freigabe.py`, Seiten `app/routers/admin_freigabe.py` (`/admin-passwort`: festlegen mit
  Anmelde-Passwort, eingeben, vergessen per E-Mail-Code + Anmelde-Passwort, sperren), Ändern unter `/konto`.
  Muss sich vom Anmelde-Passwort unterscheiden; 5 Fehlversuche = 15 Min. Sperre. Ersteinrichtung fragt es mit ab.
  Tests: `login(..., admin=True)` gibt automatisch frei, Helfer `admin_freigabe(c)`; `tests/test_admin_passwort.py`.
- **Sitzungs-Regeln** (verschärft 25.11.2026): Abmeldung nach **30 Min.** ohne Aktivität (`anmeldung.SESSION_IDLE`),
  Admin-Freigabe 5 Min.; beim Programmstart enden ALLE Anmeldungen (`app/services/neustart.py`, aus `run.py` und im
  Docker-CMD). `base.html` lädt per `<meta http-equiv="refresh">` nach Ablauf nach `/abgelaufen` (prüft mit
  `find_session(touch=False)`, verlängert also nicht) → Anmeldung bzw. Admin-Passwort. Authenticator-App ist **Pflicht**
  (`ZWEI_FAKTOR_PFLICHT`, Standard an; conftest schaltet sie aus): ohne App nur `deps.APP_SETUP_PATHS` erreichbar
  (Weiterleitung `/konto?app=pflicht`), auch vor dem Einrichtungs-Assistenten. Inhaber/in kann die App einer Person
  zurücksetzen (`/benutzer/{id}/app-zuruecksetzen`, beendet deren Sitzungen). Tests `tests/test_sicherheit_sitzung.py`.
- Zugriffsmatrix `/benutzer/matrix` (alle Personen × Bereiche + Sonderrechte, bearbeitbar; gleiche Regeln wie
  „Rechte bearbeiten“ über `_change_problem`/`_apply_change` in `app/routers/benutzer.py`, alles-oder-nichts).
- `app/permissions.py` – Module, Rechtestufen (keine/lesen/bearbeiten), Sonderrechte, Rollen-Vorlagen.
- `app/forms.py` – gemeinsame Formularfelder (`Field`), Prüfung deutscher Eingaben, Anzeige; Makro `form_field` in `_macros.html`.
- `app/services/einstellungen.py` – alle Firmen-Einstellungen (Gruppen = Assistenten-Schritte), Prüfung, Audit.
- `app/services/stammdaten.py` + `app/routers/stammdaten.py` – Kunden (mit Objekt-Adressen), Lieferanten,
  Katalog. Nummern automatisch (Kunden ab 10000, Lieferanten ab 70000, einstellbar). Stammdaten werden
  **archiviert, nie gelöscht**. Kostensatz im Katalog nur mit Sonderrecht `kalkulation_intern`.
  Einzelpreise `Numeric(12,4)` (bis 4 Nachkommastellen), Anzeige-Filter `preis`.
- Belege: Modelle `Document`/`DocumentLine`/`Payment` (`app/models/belege.py`), Logik `app/services/belege.py`,
  Seiten `app/routers/belege.py` (`/eingangsrechnungen`, `/pruefbereich`). Ablauf: Upload → Prüfbereich
  (`pending`) → „Übernehmen“ → Entwurf → Festschreiben (Knopf oder automatisch bei erster Zahlung) → Zahlungen
  (Skonto erst beim Bezahlen) / Storno. Löschen nur für Entwürfe.
  **Doppelte Eingangsrechnungen** (entschieden 04.11.2026, nur Hinweis, kein Sperren): `belege.duplicate_map`/`duplicates_of`
  – gleicher Absender (Lieferant bzw. Name) + gleiche Nummer (ohne Trennzeichen) oder + gleiches Datum + gleicher Brutto;
  Storno und „Duplizieren“-Kopien zählen nicht. Hinweis „doppelt?“ beim Upload, in beiden Listen, auf der Belegseite und
  nach dem Speichern („Doppelte Werte erkannt, bitte prüfen!“). Gleiche Datei wird weiter hart abgelehnt (SHA-256).
  Tests `tests/test_doppelte_belege.py`.
- Ausgangsrechnungen: `app/services/rechnungen.py`, `app/routers/rechnungen.py` (`/rechnungen`),
  PDF `app/services/pdf_rechnung.py` (DIN 5008, GiroCode). Nummer wird **erst beim Festschreiben** über
  `nummernkreise.allocate()` (Tabelle `number_ranges`, lückenlos je Firma/Art/Jahr; überspringt schon vorhandene
  Nummern, z. B. hochgeladene alte Rechnungen wie 2026-100) vergeben; dabei wird das
  PDF einmal erzeugt und unverändert gespeichert (`ablage.save_bytes`, SHA-256) – später nie neu erzeugen.
  Korrektur nur per Stornorechnung (`kind = storno`, eigener Nummernkreis, `storno_of_id`). Pflichtangaben
  § 14 UStG: `rechnungen.missing_for_finalize()`. Optional Kopie in lokalen Ablage-Ordner.
  **Häkchen „Bezahlt“** (entschieden 10.11.2026) in der Rechnungsliste (Spalte „Bezahlt“) und auf der Rechnungsseite
  neben „Offen“: Datumsfeld (vorbelegt heute) + Kästchen → `belege.mark_paid` bucht den offenen Rest als Zahlung mit Notiz
  `CHECK_NOTE`; Häkchen entfernen (`unmark_paid`, Sonderrecht festschreiben) löscht nur diese Zahlungen, nie Bank-/
  Teilzahlungen. Routen `/rechnungen/{id}/bezahlt|unbezahlt`, Rücksprung `zurueck` nur unter `/rechnungen`. Die Häkchen-
  Formulare stehen außerhalb der Auswahl-Liste (Attribut `form=`). Tests `tests/test_bezahlt_haekchen.py`.
  Gemeinsame Bausteine `_bezahlt.html` (`paid_box`/`paid_forms`, `with context`), seit 12.11.2026 auch in der Liste der
  Eingangsrechnungen (`/eingangsrechnungen/{id}/bezahlt|unbezahlt`; Entwürfe erlaubt – die Zahlung schreibt fest).
  **Pfeile vorherige/nächste** in beiden Prüfbereichen (`belege._steps` → `step_prev/next/pos/total`, Reihenfolge wie
  die Liste; Klasse `.step-arrows`). Tests `tests/test_haken_und_pfeile.py`.
- **Alte Ausgangsrechnungen hochladen** (`app/services/rechnungs_import.py`, Seiten `app/routers/rechnungs_import.py`,
  `/rechnungen/import`, Router VOR `rechnungen` einbinden): nur PDF; Auslese über `belege.extract` (Kunden als Partner,
  bei E-Rechnung der Käufer); eigener Prüfbereich (`pending`, `direction = ausgang`, `source = "import"`).
  „Übernehmen“ (Sonderrecht festschreiben) = festgeschrieben mit unverändertem Original-PDF, je Steuersatz eine
  Pauschal-Position, optional „bezahlt am“ (Zahlung über `belege.add_payment`), Nummernkreis zieht nach
  (`nummernkreise.register_existing`/`counter_of`). Doppelte Nummern abgefangen (Index `uq_documents_own_number`).
  `belege.pending_documents/pending_count` zählen nur Eingangsbelege. Tests `tests/test_rechnungs_import.py`.
- **Eingangsordner** (`app/services/eingangsordner.py`, Einstellungsgruppe `ordner`): je Firma zwei frei wählbare
  Ordner (`ordner.eingang` → Prüfbereich über `belege.create_from_bytes(source="ordner")`, `ordner.ausgang` → Import-
  Prüfbereich). Danach verschieben, nie löschen: `verarbeitet/` bzw. `fehler/` + `<Datei>.grund.txt`. Dateien jünger als
  `MIN_AGE_SECONDS` warten (werden evtl. noch kopiert); `_lock` verhindert gleichzeitiges Einlesen. Automatisch im Worker
  (`ordner.aktiv`), Knöpfe `/pruefbereich/ordner-einlesen`, `/rechnungen/import/ordner-einlesen`. Nur lokal
  (`is_local`); auf dem Server ausgeblendet – dort E-Mail-Postfach + Mehrfach-Upload (entschieden 11.11.2026, kein SFTP).
  Tests `tests/test_eingangsordner.py`.
- Impressum/Datenschutz: Links auf die Website des Betreibers über `IMPRESSUM_URL`/`DATENSCHUTZ_URL` (.env, nur https),
  im Seitenfuß jeder Seite inkl. Anmeldung (`templating.legal_links`). Tests `tests/test_rechtliches.py`.
- Angebote: Modelle `app/models/angebote.py`, Kalkulation als reine Funktionen `app/services/calc.py`
  (Tests `tests/test_calc.py`, aus dem alten Angebotstool übernommen), Logik `app/services/angebote.py`,
  Seiten `app/routers/angebote.py` (`/angebote`), PDF `app/services/pdf_angebot.py` (nutzt die Bausteine aus
  `pdf_rechnung.py`: `branding`, `build`, `positions_table`, `sums_table`). Angebotsnummer wird beim Anlegen
  vergeben. Interne Kostensätze/Marge nur mit Sonderrecht `kalkulation_intern`, nie im PDF. „Wiederkehrende
  Leistungen“ per Einstellung `angebot.wiederkehrend` zuschaltbar; Angebot → Rechnung rechnet sie als einen
  Monat ab (`angebote.to_invoice`, setzt `documents.quote_id`).
- **Aufträge / Projektübersicht** (entschieden 17.11.2026 nach Skizze des Nutzers): Modelle `app/models/auftraege.py`
  (`orders`, `order_lines`, `order_tasks`, `documents.order_id`), Logik `app/services/auftraege.py`, Seiten
  `app/routers/auftraege.py` (`/auftraege`, Modul/Recht `auftraege`; Migration 0017 gibt bestehenden Benutzern die Stufe von
  „angebote“). Übersicht = je **Objekt** (Objekt-Adresse des Kunden, sonst Text aus dem Angebot, sonst Hauptadresse) eine
  aufklappbare Karte (`<details>`, ohne JavaScript; `?offen=<schlüssel>` hält sie offen) mit Aufträgen, Kunde/Adresse, Belegen,
  Notizen, Abhakliste. Reiter nach Stand (offen/arbeit/erledigt), Suche, Sortierung Datum/A–Z/Preis, Filter Einzel/Dauer.
  Nummer `nummern.auftrag` (AU-{JJJJ}-{NNN}) beim Anlegen. Auftrag entsteht aus Angebot (`from_quote`, Zeilen über
  `angebote.billing_lines` wie Angebot -> Rechnung; wiederkehrend = Dauerauftrag) oder von Hand; daraus Rechnungsentwurf
  (`to_invoice`, gemeinsame Hilfe `rechnungen.add_lines`) bzw. Vertrag (`to_contract`, nur Dauerauftrag). Stand setzt der
  Mensch; `suggest_done` schlägt „erledigt“ vor, wenn alle Rechnungen eines Einzelauftrags bezahlt sind. Preise nur mit Recht
  „angebote“ oder „rechnungen_ausgang“. Löschen nur ohne Rechnung/Vertrag. Tests `tests/test_auftraege.py`.
  **Automatisch einlesen** (entschieden 17.11.2026): `auftraege.sync` (beim Öffnen von `/auftraege` und im Worker, idempotent)
  macht aus jeder Ausgangsrechnung ohne Auftrag einen Einzelauftrag (`orders.source = "rechnung"`, Migration 0018; Rechnungen
  aus Angebot -> dessen Auftrag, aus Vertrag -> Dauerauftrag `source = "vertrag"`), aus angenommenen Angeboten ohne Auftrag
  einen Auftrag (`"angebot"`). Fehlt der Kunde (hochgeladene alte Rechnungen), wird er aus Name/Adresse angelegt bzw. ein
  gleichnamiger verwendet – die Rechnung selbst bleibt unverändert (nur `documents.order_id`), der Kunde hängt am Auftrag.
  Stand: bezahlt -> erledigt, sonst in Arbeit; Vertrag beendet -> erledigt. Datum/Nummer nach Rechnungsdatum. Leere
  Entwürfe warten; automatische Aufträge folgen ihrem Entwurf bis zum Festschreiben und verschwinden mit ihm.
- E-Mail: `app/services/imap.py` (Verbindung mit Zertifikatsprüfung, Ordner tolerant finden/anlegen/abonnieren,
  IMAP-UTF-7), `email_import.py` (Abruf → Prüfbereich, Duplikatschutz je Firma über Tabelle `email_import_log`,
  sicheres Verschieben; JEDE angesehene Mail wird protokolliert mit `status` importiert/ohne_anhang/abgelehnt –
  bekannte Mails werden nur am Kopf erkannt und zählen nicht zu `MAX_MAILS_PER_RUN`; „abgelehnt“ = nur
  unbrauchbare Anhänge → bleibt im Postfach, Meldung im Prüfbereich), `email_entwurf.py` (Entwurf per IMAP APPEND mit `\Draft` bzw. .eml – **nie versenden**;
  Platzhalter nur per Regex, kein `str.format`). **Direkt im Mailprogramm öffnen** (entschieden 12.11.2026, nur lokal):
  `app/services/mailprogramm.py` – Knöpfe „In Thunderbird/Outlook öffnen“ je installiertem Programm (Jinja-Global
  `mail_programs_available`); Thunderbird per `-compose` (Werte URL-kodiert in "…", damit Text keine Anhänge einschleusen
  kann), Outlook per .eml mit `X-Unsent: 1` + `/eml`. Anhänge als Klartext-Kopie im Temp-Ordner `buchhaltung_mail`
  (nach 3 Tagen gelöscht). conftest blendet Programme aus und verbietet echte Starts; Tests `tests/test_mailprogramm.py`
  (inkl. Nachbau von Thunderbirds Parser). Hintergrund-Abruf `app/worker.py` (Thread im Programm,
  `BACKGROUND_JOBS=false` schaltet ab; Server: `python -m app.worker`). Tests mit `tests/fake_imap.py`
  (in Tests `imap.connect` per monkeypatch ersetzen – nie ein echtes Postfach).
- Auswertungen: `app/services/auswertung.py` (EÜR nach Zahlungsdatum – jede Zahlung anteilig je Steuersatz;
  USt Ist/Soll, Vorsteuer nach Rechnungsdatum mit Skonto-Korrektur; UStVA-Kennzahlen 81/86/21/60/46/47/84/85/66/67/83;
  Kleinunternehmer-Grenzen; offene Posten; Monatsverlauf), Seiten `app/routers/auswertung.py` (`/euer`
  inkl. PDF/CSV, `/steuern`), Übersicht in `app/routers/start.py` (Finanzzahlen nur mit Recht „euer“).
  Erlöskonten und Anlage-EÜR-Positionen in `konten.py` (nur Bezeichnungen, keine Zeilennummern).
  Steuerseite: Übersicht aller Zeiträume mit Zeile „Jahr gesamt“ (`nr=0` = ganzes Jahr). Ist-Versteuerung: Hinweis
  „Achtung“ bei Zahlungen ohne Bankbeleg > 30 Tage nach Rechnungsdatum (`_late_unbacked_payments` – typisch: Häkchen
  „Bezahlt“ nachträglich mit Datum „heute“). Der Bankabgleich schlägt auch bezahlte Rechnungen ohne Bankbeleg vor
  (`bank.unbacked_payments`) und ERSETZT deren Zahlung durch die Bankzahlung (nur über den ganzen Betrag, nie vor dem
  Rechnungsdatum; gleiche Beträge → zeitlich nächste Rechnung). Tests `tests/test_ist_versteuerung.py`.
- **§ 13b UStG** (`app/services/steuerschuld.py`, entschieden 12.11.2026): Eingang = `documents.reverse_charge`
  inland/eu/drittland + `reverse_charge_rate` (19/7), Beträge mit 0 %, Steuer = Netto × Satz → Kz 46/47 (EU) bzw. 84/85,
  Vorsteuer Kz 67 (Kleinunternehmer: Steuer zahlen, kein Abzug; Steuerseite zeigt dann die Zeiträume mit § 13b).
  Vorauswahl beim Upload über Lieferant (`suppliers.reverse_charge`) oder erkannten § 13b-Text (gelb). Ausgang = Hinweis
  „13b“, Fall aus Land/USt-IdNr. des Kunden (`rechnungen.set_reverse_charge`; EU → Kz 21 + Liste Zusammenfassende
  Meldung, Text „Reverse charge“, eigene + Kunden-USt-IdNr. Pflicht; außerhalb der EU nicht festschreibbar);
  `customers.reverse_charge` wählt den Hinweis bei neuen Rechnungen vor. Nach Rechnungsdatum, unabhängig von Ist/Soll.
  DATEV: Eingang BU 94/91 + Hinweis „Sachverhalt prüfen“, EU-Erlöse 8336/4336. Tests `tests/test_paragraph_13b.py`.
- E-Rechnung: `app/services/erechnung.py` – Lesen (CII + UBL, auch aus PDF-Anhang; fremdes XML nur über
  `defusedxml`), Schreiben (`build_cii`, CII nach EN 16931 / XRechnung 3.0), Prüfung `validate` (XSD offline,
  **kein** Schematron – sendet Daten an Server), `embed` (Factur-X + sRGB-OutputIntent = PDF/A-3).
  Format je Rechnung: `format_for` (Leitweg-ID/Behörde → XRechnung, sonst Einstellung `rechnung.e_rechnung`);
  Pflichtangaben `erechnung.missing` fließen in `missing_for_finalize`. XRechnung-XML als eigene Datei
  (`documents.xml_file_id`). **Alle PDFs nutzen die eingebettete Schrift Vera** (`FONT`/`FONT_BOLD` in
  `pdf_rechnung.py`); jede `TableStyle` braucht `("FONTNAME", …, FONT)`, und keine reportlab-Grafik-Renderer
  verwenden (setzen Helvetica, verletzt PDF/A) – Test `test_pdf_a3_merkmale`.
- Beleg aus Datei anlegen (Upload und Mail-Anhang): `belege.create_from_bytes`; Dateien prüfen: `ablage.save_checked`.
  Auslese-Reihenfolge: E-Rechnungs-XML → PDF-Textebene → OCR (`app/services/ocr.py`, ruft das externe Programm
  Tesseract auf – Pfad `TESSERACT_CMD` in `.env` oder automatisch gesucht; fehlt es, bleibt es bei Handeingabe).
  OCR-Beträge sind immer „bitte prüfen“. In Tests OCR per monkeypatch nachbilden (`tests/test_ocr.py`).
- **Kontovorschläge / KI** (`app/services/kontierung.py`): 1. Lieferant (letztes Konto), 2. gelernt aus eigenen
  übernommenen Belegen (seltene gemeinsame Wörter, reine Funktion `learn`, lokal), 3. Claude – nur mit Einstellung
  `ki.aktiv` + Schlüssel (`ki.api_key` verschlüsselt oder `ANTHROPIC_API_KEY`). Stufe 3 läuft NIE in einer offenen
  Transaktion, sondern danach (`ki_nachtragen`: nach Upload, im Worker, Knopf `/belege/{id}/konto-vorschlagen`).
  Herkunft/Begründung in `documents.account_source`/`account_hint`; gelernt/KI = gelb (`review_fields` "account").
  Anbindung `app/services/ki.py` (`ask_json`: Modell `claude-opus-5-5`, JSON-Schema, `fallbacks="default"`,
  `minimize` entfernt IBAN/E-Mail/Telefon). In Tests `ki.ask_json` per monkeypatch ersetzen (`tests/test_kontierung.py`);
  `conftest` entfernt `ANTHROPIC_API_KEY`. Einstellungsgruppe `ki` (Datenschutz-Hinweis/AVV). `run.py` installiert
  fehlende Pakete aus `requirements.txt` beim Start nach. Geplant: Bankumsätze zuordnen.
- **KI-Belegprüfung** (`app/services/ki_auslese.py`, entschieden 12.11.2026 nach Konzept-Dokument des Nutzers): Claude liest
  EINEN Eingangsbeleg im Prüfbereich aus (ein Aufruf = ein Beleg, kein DB-Zugriff, keine Werkzeuge). Übermittelt wird der
  PDF-Text über `ki.minimize` (ohne IBAN/E-Mail/Telefon); nur Scans/Fotos ohne Textebene als Bild/PDF-Block (Fotos
  verkleinert). Dazu Kontenliste + eigener Firmenname. E-Rechnungen (`documents.e_invoice_format` gesetzt) nie. Antwort
  im JSON-Schema, `check()` (reine Funktion) rechnet alles nach: Netto×Satz/Summe/Gesamtbetrag, Datum (Zukunft, >10 Jahre),
  Konto in Liste, Pflichtangaben § 14 UStG über 250 € (darunter Kleinbetrag § 33 UStDV). Ampel je Feld in
  `documents.field_states` (ok/pruefen/fehlt; Speichern/Übernehmen leert sie). Protokoll-Tabelle `ki_readings` (Roh-Antwort,
  Prüfungen, eingetragene Felder, `final` beim Übernehmen über `record_takeover`) → Kasten „KI-Auslese“ mit Vergleich
  (`_ki_auslese.html`). Ablauf `nachtragen` (committet vor der Anfrage): automatisch nach Upload (erste 3 sofort,
  `UPLOAD_KI_NOW`), Eingangsordner, Worker; Einstellung `ki.auslesen` (Standard an, nur mit `ki.aktiv`+Schlüssel); Knopf
  `/belege/{id}/ki-auslesen` überschreibt bewusst, der Automatik-Lauf überschreibt nie von Menschen gespeicherte Werte
  (Audit „geaendert“). Lieferanten-Konto hat Vorrang. `ki.ask_json` nimmt Inhaltsblöcke + `effort`. Tests
  `tests/test_ki_auslese.py` (KI immer nachgebildet).
- Bankabgleich: Modelle `app/models/bank.py` (`bank_imports`, `bank_transactions` mit `fingerprint` gegen
  Doppel-Import), Einlesen `app/services/kontoauszug.py` (reine Funktionen: CSV mit Spalten-Zuordnung, CAMT
  052/053/054 auch als ZIP), Logik `app/services/bank.py` (Vorschläge, Zuordnung = normale Zahlung über
  `belege.add_payment(..., bank_transaction_id=)`, Kategorien `ust`/`privat`/`ignorieren`), Seiten
  `app/routers/bank.py` (`/bank`). CSV-Zuordnungen je Bank in Einstellung `bank.csv_zuordnungen`.
  Umsätze mit Kategorie `ust` zählen in der EÜR (`auswertung.vat_office_payments`).
- Mahnwesen: Modell `app/models/mahnwesen.py` (`dunnings`), Stufen/Fristen/Gebühren/Texte als Einstellungen
  (Gruppe `mahnwesen`, `einstellungen.DUNNING_LEVELS`), Logik `app/services/mahnwesen.py` (Gebühren kumuliert,
  optional Verzugszinsen), PDF `pdf_mahnung.py` (einmal erzeugt und gespeichert), Seiten
  `app/routers/mahnwesen.py` (`/mahnwesen`, E-Mail-Entwurf wie bei Rechnungen). Nummernkreis `mahnung`.
- DATEV-Export: `app/services/datev.py` (EXTF 700 Buchungsstapel, cp1252; Rechnungen über Personenkonten,
  BU 9/8 für Vorsteuer außer Automatikkonten; Zahlungen nur mit Einstellung `datev.zahlungen`; ZIP mit
  Belegen + Übersicht + LIESMICH), Seite `app/routers/export.py` (`/export`). Einstellungen Gruppe `datev`.
- `app/services/auslese.py` – PDF-Textauslese (pdfplumber) mit Unsicherheits-Markierung; `betraege.py` –
  Steuerzeilen ergänzen/prüfen, Skonto anteilig; `konten.py` – Vorschlagsliste Aufwandskonten SKR03/SKR04.
- `app/services/anmeldung.py` – Passwort, Fehlversuch-Sperre, E-Mail-Code, Sitzungen, Einladungen.
- **Authenticator-App (TOTP)** (`app/services/totp.py`, entschieden 11.11.2026): RFC 6238 selbst umgesetzt, QR als SVG über
  reportlab (`/konto/app/qr.svg`); Einrichten unter `/konto` (Passwort → QR → erster Code → 8 Notfall-Codes, nur als SHA-256
  gespeichert). Felder `users.totp_*` (Geheimnis Fernet-verschlüsselt, `totp_last_step` gegen Wiederverwenden). Mit aktiver
  App KEIN E-Mail-Code mehr (`/login/code` prüft `totp.check`), 5 Fehlversuche = 15 Min. Sperre. Tests
  `tests/test_authenticator_app.py`; conftest-`login()` funktioniert nur ohne App.
- `app/services/mail.py` – System-Mails; lokal ohne SMTP → `.eml` in `data/mail_ausgang/` + Konsole.
- **Verschlüsselte Ablage** (`app/services/tresor.py`, entschieden 11.11.2026): jede gespeicherte Datei AES-256-GCM, je
  Firma eigener Schlüssel (`tenants.file_key`, gewrappt mit HKDF aus `ENCRYPTION_KEY`), an den Ablagepfad gebunden.
  Dateien NUR über `ablage.read_bytes(stored, db)` bzw. `ablage.response(stored)` lesen – nie `absolute_path(...)`
  öffnen oder `FileResponse` nutzen. Altdateien ohne Kennung werden gelesen und beim Start verschlüsselt
  (`run.py` bzw. Docker-CMD: `python -m app.services.tresor`). SHA-256/Größe in `files` = Original. Ohne `.env` sind
  Dateien und Sicherungen unlesbar. Tests `tests/test_tresor.py`.
- `app/services/ablage.py` – Datei-Uploads je Firma (Magic-Bytes-Prüfung, SHA-256); Ausliefern gespeicherter Dateien nur über
  `ablage.serve_type` (XML als Text, CSP `sandbox` – fremde Dateien dürfen nie als Webseite mit Skript laufen).
- `app/formatting.py` – deutsches Zahlen-/Datumsformat (Jinja-Filter `geld`, `zahl`, `datum`, `datumzeit`).
- Oberfläche: Hauptmenü links in `base.html` (nach Themen gruppiert, Liste `nav_sections`; auf dem Handy über den
  Knopf „Menü“ ohne JavaScript). Variablennamen in `base.html` mit `nav_` beginnen – sonst überdecken sie
  gleichnamige Werte der Seiten. Einstellungs-Unterpunkte: `_einstellungen_nav.html` (Liste links).
- **Design „Tinte & Papier“** (12.11.2026): `app/static/style.css` ist aus dem Design-System (Claude-Artefakt „Buchhaltungstool
  Design-System“: Token, Bausteine, Markenbuch) erzeugt – Farben/Abstände/Radien als CSS-Variablen (`--accent`, `--card`, `--ok-bg` …),
  Hell-Thema Standard, Dunkel-Thema nur mit `data-theme="dark"` (noch nicht eingeschaltet). Neue Oberfläche zuerst mit
  vorhandenen Klassen und Variablen bauen, keine festen Hex-Farben in Vorlagen (E-Mails ausgenommen). Status immer Farbe UND Wort.
  Schrift IBM Plex unter `app/static/fonts/` (selbst ausgeliefert, nie von Google; Lizenz liegt dabei). Logo-Zeichen (Haken über
  Summenstrich) in `base.html` und `favicon.svg`; PDFs zeigen nur das Mandanten-Logo.
- PDF-Briefpapier (`pdf_rechnung._Page`): Text auf festen Positionen nie nach Zeichenzahl kürzen, sondern
  `wrap_text`/`fit_text` (gemessene Breite) verwenden – Test `tests/test_pdf_layout.py` prüft lange Namen.
  Firmenname zentriert, Logo links/rechts (`design.logo_position`), Falzmarken nur mit `design.falzmarken`.
  Fußzeile umbricht statt zu kürzen (`footer_layout` bestimmt auch das Inhaltsende). Zahlungsblock/GiroCode
  und Schlusstext über `BottomBlock` unten auf der Seite; Summen enden mit einer Linie über die ganze Breite.
- PDF-Vorlagen (Designs): Auswahl je Belegart in Einstellung `design.vorlage_rechnung|angebot|mahnung`.
  Verzeichnis `app/services/pdf_vorlagen.py` (ohne reportlab), Umsetzung `app/services/pdf_designs.py` (Klasse je
  Vorlage, erbt von `Klassisch`: `page_class`, `styles`, `positions_table`, `sums_table`, `payment_block`, `build`).
  Neue Vorlage = Klasse dort + Eintrag in `VORLAGEN`. render-Funktionen liefern Inhalte, Vorlagen nur das Aussehen.
  Pakete „Marine“ … „Terrakotta“ (aus Claude Design): ein Grundgerüst `PaketSeite` + `Stil` je Paket (`PAKETE`),
  Maße in Entwurfs-px (`px()`, A4 = 794 px). Farbe der Vorlage oder eigene Akzentfarbe (`design.vorlage_farbe`).
  Angebote der Pakete enthalten das Feld „Auftrag erteilt“ (`acceptance_block`). Schrift überall Vera (PDF/A).
  Muster-PDF ohne DB-Einträge: `pdf_muster.py`, Route `/einstellungen/design/muster?art=…&vorlage=…`.
  Tests `tests/test_pdf_vorlagen.py`.
- Verträge (wiederkehrende Leistungen, nur mit Einstellung `angebot.wiederkehrend`): Modelle
  `app/models/vertraege.py` (`recurring_contracts`, `recurring_contract_lines`, `documents.contract_id`), Logik
  `app/services/vertraege.py` (`from_quote`, `run_due` legt je fälligem Monat einen normalen Rechnungsentwurf an;
  Abrechnung je Vertrag „nachtraeglich“ = Vormonat oder „voraus“), Seiten `app/routers/vertraege.py` (`/vertraege`,
  Recht `rechnungen_ausgang`). `run_due` läuft im Hintergrund-Worker; Hinweis auf der Übersicht.
- Datensicherung: `app/services/sicherung.py` (lokal/SQLite: ZIP mit DB + Dateien unter `data/sicherungen/` beim
  Start in `run.py` vor der Migration, nur bei Änderungen, letzte 10 – `BACKUP_KEEP`), Firmen-Export
  `app/services/firmen_export.py` (alle Daten EINER Firma als JSON + Dateien, ohne Passwörter; nur Inhaber/in),
  Seite `app/routers/datensicherung.py` (`/datensicherung`). Server: `deploy/sicherung.sh` (pg_dump + Dateien).
- Plattform-Tabellen ohne `tenant_id`-Filter (users, memberships, invitations, …) → in Routen **immer
  ausdrücklich** nach `ctx.tenant_id` filtern. Neue Plattform-Tabelle in `test_plattform_tabellen_ausdruecklich_bekannt` eintragen.
- Entschieden: keine Altdaten-Übernahme aus alten Datenbanken (Rohversion für neue Firmen) – einzelne alte
  Ausgangsrechnungen lassen sich aber als PDF hochladen (siehe „Alte Ausgangsrechnungen“).
- **Zwei Ordner (seit 02.11.2026):** `E:\Programme\Buchhaltung\CleanVersion1` = Prototyp mit echten Firmendaten
  (Nutzer arbeitet damit), `E:\Programme\Buchhaltung\Clean Version` = gleicher Code ohne Echtdaten. JEDE Code-Änderung
  in beiden Ordnern umsetzen (Code-Dateien kopieren, nie `data/`, `.env`, `.venv`) und in beiden testen.

## Arbeitsregeln

1. **Sprache:** UI, Code-Kommentare, Commit-Texte und Erklärungen auf Deutsch. Der Auftraggeber ist
   kein Programmierer – Änderungen am Ende laienverständlich zusammenfassen.
2. **Zahlenformat:** immer deutsch (`1.234,56`, Dezimalkomma, Tausenderpunkt) in Eingabe und Anzeige;
   Datum `TT.MM.JJJJ`. Intern Geld nur als `Decimal`, nie `float`.
3. **Vor größeren Umbauten oder offenen Fachfragen nachfragen** (siehe „Offene Punkte“ in KONZEPT.md),
   nicht selbst entscheiden. Empfehlungen sind willkommen.
4. **Schema-Änderungen nur über neue Alembic-Revision** – nie Tabellen von Hand ändern.
5. **Mandantentrennung:** Jede fachliche Tabelle hat `tenant_id`; Abfragen nur über die zentralen
   Hilfsfunktionen, die automatisch nach Firma filtern. Neue Tabelle = Test für die Trennung.
6. **Rechteprüfung pro Modul** bei jeder Route (zentral über Abhängigkeit).
7. **Prüfbereich:** Automatisch importierte Belege (`pending = true`) zählen in keiner Auswertung,
   Liste, Statistik oder Export mit. Jede neue Auswertung filtert `pending = false`.
8. **Kein automatischer E-Mail-Versand an Kunden.** Nur Entwürfe (IMAP-Entwürfe-Ordner oder `.eml`).
   SMTP nur für System-Mails (Login-Code, Einladung).
9. **E-Mails beim Import nie löschen, nur verschieben** (MOVE, sonst COPY + Nachzählen + UID EXPUNGE;
   neue Ordner immer abonnieren).
10. **GoBD:** Festgeschriebene Belege sind unveränderbar, Korrektur nur per Storno; Änderungen ins Audit-Log.
11. **Keine Firmendaten im Code.** Alles Firmenspezifische kommt aus den Firmen-Einstellungen.
12. **Zugangsdaten verschlüsselt** speichern, Schlüssel nur in `.env` (nie committen).
13. **Keine Test-/Beispieldaten in echte Datenbanken** unter `data/`. Tests nutzen eigene Test-DB.
14. Nach Änderungen: `pytest` grün und Programm startet (`python run.py`).
15. Die alten Programme (Buchhaltungsprogramm, Angebotstool) sind nur **Referenz** – dort nichts ändern.
