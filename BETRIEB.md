# Betrieb: Start, Datensicherung, Wiederherstellung, Server

## 1. Lokal (Testversion auf diesem Rechner)

- **Starten:** `start.bat` doppelklicken (oder `python run.py`). Das Programm läuft dann unter
  http://127.0.0.1:8000. Beenden: Fenster schließen bzw. Strg+C.
- **Texterkennung (OCR) für Scans und Fotos:** kostenloses Programm *Tesseract* installieren
  (Windows-Installer „Tesseract-OCR“, bei der Installation die Sprache **Deutsch** mit auswählen).
  Das Programm findet es unter `C:\Program Files\Tesseract-OCR` automatisch; anderer Ort → in der `.env`
  `TESSERACT_CMD=…\tesseract.exe` eintragen. Ohne Tesseract werden Scans von Hand erfasst.

### Datensicherung (automatisch)
Bei jedem Start legt das Programm **vor** dem Datenbank-Update eine Sicherung an – aber nur, wenn sich seit der
letzten Sicherung etwas geändert hat:

- Ordner: `data\sicherungen\`, Dateien `sicherung_JJJJ-MM-TT_hh-mm-ss.zip` (Datenbank + alle Belege/PDFs)
- Es bleiben die letzten **10** Sicherungen (änderbar in der `.env`: `BACKUP_KEEP=20`).
- **Wichtig:** Kopieren Sie regelmäßig (z. B. wöchentlich) die neueste ZIP-Datei auf einen USB-Stick oder in
  einen Cloud-Ordner. Eine Sicherung auf derselben Festplatte hilft nicht, wenn die Festplatte kaputtgeht.
- Zusätzlich: die Datei `.env` einmal sicher aufbewahren (ohne sie lassen sich gespeicherte Postfach-Passwörter
  nicht mehr entschlüsseln).

### Wiederherstellen (lokal)
1. Programm beenden.
2. Den Ordner `data` zur Sicherheit umbenennen, z. B. in `data_alt`.
3. Neuen, leeren Ordner `data` anlegen und die gewünschte `sicherung_….zip` hinein entpacken.
   Danach liegen dort `app.db` und der Ordner `files`.
4. Programm starten – fertig. (Den Ordner `data_alt` erst löschen, wenn alles stimmt.)

### Firmen-Export
Einstellungen → „Datensicherung & Export“ → „Firmen-Export herunterladen“ (nur Inhaber/in): alle Daten der Firma
als ZIP (JSON-Dateien + alle Belege). Für Datenschutz-Auskünfte oder einen Wechsel. Für die Steuerberatung gibt es
den DATEV-Export (Menü „Export“).

## 2. Server (Docker)

Bestandteile (Datei `docker-compose.yml`): **app** (das Programm), **worker** (E-Mail-Abruf, Rechnungsentwürfe aus
Verträgen), **db** (PostgreSQL-Datenbank), **caddy** (holt automatisch das HTTPS-Zertifikat).

### Voraussetzungen
- Linux-Server bei einem Anbieter **in der EU** (DSGVO), mit Docker und dem Docker-Compose-Plugin.
  Größe für den Anfang: 2 CPU-Kerne, 4 GB Arbeitsspeicher, 40 GB SSD reichen für mehrere kleine Firmen.
  Beispiel (Stand Oktober 2026, Preise vor Bestellung prüfen): Hetzner Cloud CX23 in Nürnberg/Falkenstein,
  ca. 5,49 € netto/Monat zzgl. IPv4-Adresse; Betriebssystem Ubuntu 24.04 oder Debian 12 – beim Anlegen die
  „Docker CE“-App wählen, dann ist Docker schon installiert. Den AV-Vertrag schließt man in der Hetzner-Konsole ab.
- Eine Domain bzw. Subdomain, deren DNS-Eintrag (A/AAAA) auf den Server zeigt; Ports 80 und 443 offen.
- Ein SMTP-Postfach für System-Mails (Login-Codes, Einladungen).
- Mit dem Anbieter einen **Vertrag zur Auftragsverarbeitung (AV-Vertrag)** abschließen; wer das Programm für
  andere Firmen betreibt, schließt mit diesen ebenfalls einen AV-Vertrag.

### Server absichern (zuerst, einmalig)
1. **SSH-Schlüssel statt Passwort** – auf dem Windows-Rechner in PowerShell:
   ```powershell
   ssh-keygen -t ed25519                                  # Enter drücken; ein Passwort für den Schlüssel ist empfohlen
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@IHRE-SERVER-IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```
   (Bei Hetzner kann man den öffentlichen Schlüssel auch schon beim Anlegen des Servers hinterlegen.)
   Danach `ssh root@IHRE-SERVER-IP` testen – es darf **kein** Server-Passwort mehr abgefragt werden.
2. Programm hochladen (siehe unten), dann auf dem Server den Grundschutz einschalten:
   ```sh
   cd /opt/buchhaltung && bash deploy/server-haertung.sh
   ```
   Das Skript aktualisiert alles, schaltet **automatische Sicherheitsupdates** ein (bei Bedarf Neustart um 04:30 –
   das Programm startet von selbst wieder), eine **Firewall** (nur SSH, 80, 443), **SSH nur mit Schlüssel** (nur wenn
   schon ein Schlüssel hinterlegt ist – sonst überspringt es diesen Schritt, damit Sie sich nicht aussperren),
   eine **Sperre bei Passwort-Raten** (fail2ban) und den Uhrzeit-Abgleich; die `.env` ist danach nur für root lesbar.
   **Wichtig:** Das aktuelle SSH-Fenster offen lassen und in einem **zweiten** Fenster prüfen, ob die Anmeldung noch
   klappt, bevor Sie das erste schließen.
3. In der Hetzner-Konsole zusätzlich die **Cloud-Firewall** mit denselben Regeln (22, 80, 443) anlegen – doppelt hält
   besser.

Die Container sind ebenfalls gehärtet (`docker-compose.yml`): Die Datenbank liegt in einem Netz **ohne Internet** und
ist nur für Programm und Hintergrund-Prozess erreichbar, alle Programm-Container laufen ohne Sonderrechte mit
schreibgeschütztem Dateisystem, und die Protokolle sind auf 5 × 10 MB je Dienst begrenzt.

### Einrichtung
Programm vom Windows-Rechner hochladen (PowerShell im Programmordner; überträgt nie `.env` oder `data`):
```powershell
.\deploy\hochladen.ps1 -Server root@IHRE-SERVER-IP
```
Dann auf dem Server (`ssh root@IHRE-SERVER-IP`):
```sh
cd /opt/buchhaltung
cp .env.server.example .env
python3 deploy/neue-schluessel.py >> .env
nano .env                            # DOMAIN, SMTP-Zugang, IMPRESSUM_URL und DATENSCHUTZ_URL eintragen
docker compose up -d --build
docker compose logs -f app           # warten bis „Application startup complete“
```
Danach im Browser `https://IHRE-DOMAIN/ersteinrichtung?token=SETUP_TOKEN` öffnen (den Wert von `SETUP_TOKEN`
aus der `.env`) und den ersten Zugang (Plattform-Admin + erste Firma) anlegen.

**Impressum und Datenschutz:** Sobald das Programm im Internet erreichbar ist, muss die Anmeldeseite auf Impressum und
Datenschutzerklärung verlinken. Dafür in der `.env` die Adressen Ihrer Website eintragen (`IMPRESSUM_URL=https://…`,
`DATENSCHUTZ_URL=https://…`); die Links erscheinen dann unten auf jeder Seite. Die Datenschutzerklärung muss auch das
Programm abdecken (Anmeldedaten, Sitzungs-Cookies, Belege der Firmen, Hoster, ggf. KI-Vorschläge) – bitte rechtlich
prüfen lassen.

### Updates
```sh
cd /opt/buchhaltung
bash deploy/sicherung.sh             # vorher sichern
# neue Programmdateien vom Windows-Rechner aus hochladen: .\deploy\hochladen.ps1 -Server root@IHRE-SERVER-IP
docker compose up -d --build         # Datenbank wird beim Start automatisch migriert
```

### Datensicherung (Server) – verschlüsselt und außer Haus
`deploy/sicherung.sh` sichert jede Nacht Datenbank und alle Belege **verschlüsselt** (Programm „age“) nach
`sicherungen/` und spiegelt den Ordner auf eine **Storage Box an einem anderen Standort**. Verschlüsselt wird an
einen *öffentlichen* Schlüssel: Der Server kann Sicherungen schreiben, aber **nicht lesen**. Den *privaten*
Schlüssel zum Wiederherstellen haben nur Sie. Ohne gültigen Schlüssel in der `.env` bricht die Sicherung ab –
unverschlüsselte Sicherungen entstehen nie.

**Einmalig einrichten** (auf dem Server, `ssh root@IHRE-SERVER-IP`):
1. Werkzeuge installieren: `apt install -y age rsync`
2. Schlüsselpaar erzeugen: `age-keygen -o /root/sicherungsschluessel.txt`
   - Die Zeile `# public key: age1…` zeigt den **öffentlichen** Schlüssel → in die `.env` als
     `BACKUP_AGE_EMPFAENGER=age1…` eintragen.
   - Die Datei `/root/sicherungsschluessel.txt` (privater Schlüssel `AGE-SECRET-KEY-…`) **herunterladen und
     ausdrucken bzw. auf einem Stick im Schrank aufbewahren** – getrennt von der `.env`-Kopie. Danach auf dem
     Server **löschen**: `shred -u /root/sicherungsschluessel.txt`
3. Storage Box bestellen (Hetzner-Konsole, Standort **anders** als der Server, z. B. Server Nürnberg → Box
   Falkenstein), dort „SSH-Support“ einschalten und **automatische Schnappschüsse** aktivieren (z. B. täglich,
   30 behalten). Die Schnappschüsse kann ein Angreifer auf dem Server nicht löschen.
4. Eigenen SSH-Schlüssel für die Box anlegen und hinterlegen:
   ```sh
   ssh-keygen -t ed25519 -N "" -f /root/.ssh/storagebox
   cat /root/.ssh/storagebox.pub | ssh -p 23 uXXXXX@uXXXXX.your-storagebox.de install-ssh-key
   ```
5. In die `.env` eintragen: `BACKUP_ZIEL_SSH=uXXXXX@uXXXXX.your-storagebox.de` (optional
   `BACKUP_ZIEL_ORDNER`, `BACKUP_TAGE`), dann einmal von Hand testen: `bash deploy/sicherung.sh`
6. Täglich per cron (`crontab -e`):
   ```
   30 2 * * * cd /opt/buchhaltung && bash deploy/sicherung.sh >> sicherungen/protokoll.txt 2>&1
   ```
   Ob die letzte Sicherung lief, steht in `sicherungen/letzte_sicherung.txt` bzw. `sicherungen/protokoll.txt`.

### Schlüssel-Datei `.env` – unbedingt getrennt aufbewahren
Alle Belege (Rechnungen, Scans, Logos) liegen **verschlüsselt** auf dem Server, je Firma mit eigenem Schlüssel.
Diese Firmenschlüssel sind wiederum mit dem `ENCRYPTION_KEY` aus der `.env` verschlüsselt. **Geht die `.env`
verloren, sind alle Belege – auch in jeder Sicherung – unwiederbringlich verloren.** Deshalb:
- die `.env` (bzw. mindestens `ENCRYPTION_KEY`) ausgedruckt oder in einem Passwort-Tresor **außerhalb** des Servers
  aufbewahren, nie zusammen mit den Sicherungen;
- `ENCRYPTION_KEY` nie ändern (sonst sind alle vorhandenen Belege nicht mehr lesbar).

### Wiederherstellen (Server)
Den privaten Schlüssel vorübergehend auf den Server kopieren (z. B. `scp sicherungsschluessel.txt root@IP:/root/`).
`STAND` ist Datum/Uhrzeit aus dem Dateinamen, z. B. `2026-11-11_02-30`.
```sh
cd /opt/buchhaltung
bash deploy/wiederherstellen.sh pruefen /root/sicherungsschluessel.txt STAND   # nur prüfen, ändert nichts
bash deploy/wiederherstellen.sh zurueck /root/sicherungsschluessel.txt STAND   # ersetzt Datenbank und Belege
shred -u /root/sicherungsschluessel.txt                                         # Schlüssel danach wieder entfernen
```
Liegt die Sicherung nicht mehr auf dem Server (z. B. neuer Server), vorher von der Storage Box holen:
`rsync -a -e "ssh -p 23 -i /root/.ssh/storagebox" uXXXXX@uXXXXX.your-storagebox.de:buchhaltung/ sicherungen/`
Für einen neuen Server außerdem die **alte `.env`** (wegen `ENCRYPTION_KEY`) wieder einspielen.

**Probe:** Mindestens alle drei Monate `wiederherstellen.sh pruefen` mit einer aktuellen Sicherung ausführen
(ändert nichts) und einmal im Jahr eine komplette Wiederherstellung auf einem Testserver durchspielen.

### Hinweise
- Vor jedem Update fremde Bausteine auf bekannte Lücken prüfen (auf dem Windows-Rechner im Programmordner):
  `.venv\Scripts\python.exe -m pip install pip-audit` (einmalig), dann `.venv\Scripts\python.exe -m pip_audit -r requirements.txt`.
- Das Programm selbst lauscht nur im internen Docker-Netz; von außen ist nur Caddy (HTTPS) erreichbar.
- Protokolle: `docker compose logs app`, `docker compose logs worker`.
- Löschfristen: Rechnungen 8 Jahre, Bücher 10 Jahre (GoBD) – Firmen erst danach endgültig löschen.
