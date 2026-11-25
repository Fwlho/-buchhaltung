#!/bin/bash
# Tägliche, VERSCHLÜSSELTE Sicherung auf dem Server: Datenbank (pg_dump) + alle gespeicherten Dateien.
# Aufruf aus dem Programmordner, z. B. per cron jeden Tag um 2:30 Uhr (siehe BETRIEB.md):
#   30 2 * * * cd /opt/buchhaltung && bash deploy/sicherung.sh >> sicherungen/protokoll.txt 2>&1
#
# Verschlüsselt wird mit „age“ an einen ÖFFENTLICHEN Schlüssel (BACKUP_AGE_EMPFAENGER in der .env). Der Server kann
# die Sicherungen damit nur schreiben, nicht lesen – den privaten Schlüssel zum Wiederherstellen hat nur der Inhaber.
# Unverschlüsselt landet nie etwas auf der Platte (direkt aus pg_dump/tar in age geleitet).
# Danach wird der Sicherungsordner auf die Storage Box (anderer Standort) gespiegelt, wenn BACKUP_ZIEL_SSH gesetzt ist.
#
# Einstellungen in der .env:
#   BACKUP_AGE_EMPFAENGER   öffentlicher age-Schlüssel (age1…), Pflicht – ohne ihn bricht die Sicherung ab
#   BACKUP_ZIEL_SSH         z. B. u123456@u123456.your-storagebox.de (leer = nur lokal, mit Warnung)
#   BACKUP_ZIEL_ORDNER      Ordner auf der Storage Box (Standard: buchhaltung)
#   BACKUP_SSH_PORT         Standard 23 (Hetzner Storage Box)
#   BACKUP_SSH_SCHLUESSEL   Standard /root/.ssh/storagebox
#   BACKUP_TAGE             Aufbewahrung auf dem Server in Tagen (Standard 30)
set -euo pipefail
cd "$(dirname "$0")/.."

wert() {  # Wert aus der .env lesen (ohne die Datei als Skript auszuführen)
    local v
    v="$(grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
    v="${v%\"}"; v="${v#\"}"
    printf '%s' "${v:-$2}"
}

EMPFAENGER="$(wert BACKUP_AGE_EMPFAENGER "")"
ZIEL_SSH="$(wert BACKUP_ZIEL_SSH "")"
ZIEL_ORDNER="$(wert BACKUP_ZIEL_ORDNER buchhaltung)"
SSH_PORT="$(wert BACKUP_SSH_PORT 23)"
SSH_SCHLUESSEL="$(wert BACKUP_SSH_SCHLUESSEL /root/.ssh/storagebox)"
TAGE="$(wert BACKUP_TAGE 30)"
ZIEL="${BACKUP_ORDNER:-./sicherungen}"
STAND="$(date +%Y-%m-%d_%H-%M)"

if [[ ! "$EMPFAENGER" =~ ^age1[0-9a-z]{58}$ ]]; then
    echo "$(date '+%d.%m.%Y %H:%M') FEHLER: BACKUP_AGE_EMPFAENGER fehlt oder ist kein age-Schlüssel – keine Sicherung (siehe BETRIEB.md)." >&2
    exit 2
fi
command -v age >/dev/null || { echo "FEHLER: Programm „age“ fehlt (apt install age)." >&2; exit 2; }

mkdir -p "$ZIEL"
chmod 700 "$ZIEL"
umask 077

# In Teil-Dateien schreiben und erst danach umbenennen – so liegt nie eine halbe Sicherung im Ordner.
# pipefail: Scheitert pg_dump/tar, scheitert die ganze Zeile und die Teil-Datei wird verworfen.
sichern() {  # $1 = Zieldatei, Rest = Befehl
    local datei="$1"; shift
    if "$@" | age -r "$EMPFAENGER" > "$datei.teil"; then
        mv "$datei.teil" "$datei"
    else
        rm -f "$datei.teil"
        echo "$(date '+%d.%m.%Y %H:%M') FEHLER bei $datei" >&2
        exit 1
    fi
}
sichern "$ZIEL/datenbank_$STAND.dump.age" docker compose exec -T db pg_dump -U buchhaltung -d buchhaltung -Fc
sichern "$ZIEL/dateien_$STAND.tar.gz.age" docker compose exec -T app tar -czf - -C /daten .

# Prüfsummen (zum Erkennen beschädigter Kopien – nicht geheim)
( cd "$ZIEL" && sha256sum "datenbank_$STAND.dump.age" "dateien_$STAND.tar.gz.age" > "pruefsummen_$STAND.txt" )

find "$ZIEL" -maxdepth 1 \( -name 'datenbank_*' -o -name 'dateien_*' -o -name 'pruefsummen_*' \) -mtime +"$TAGE" -delete
# Alte UNVERSCHLÜSSELTE Sicherungen aus der Zeit vor der Verschlüsselung entfernen
find "$ZIEL" -maxdepth 1 \( -name 'datenbank_*.dump' -o -name 'dateien_*.tar.gz' \) -delete

if [ -n "$ZIEL_SSH" ]; then
    # Spiegeln: löscht dort, was hier gelöscht wurde. Gegen Löschen durch einen Angreifer schützen die
    # automatischen Schnappschüsse der Storage Box (in der Hetzner-Konsole einschalten, siehe BETRIEB.md).
    rsync -a --delete -e "ssh -p $SSH_PORT -i $SSH_SCHLUESSEL -o BatchMode=yes" "$ZIEL/" "$ZIEL_SSH:$ZIEL_ORDNER/"
    echo "$(date '+%d.%m.%Y %H:%M') Sicherung $STAND fertig und nach $ZIEL_SSH übertragen"
else
    echo "$(date '+%d.%m.%Y %H:%M') WARNUNG: Sicherung $STAND fertig, aber BACKUP_ZIEL_SSH fehlt – liegt nur auf diesem Server!"
fi
date '+%d.%m.%Y %H:%M' > "$ZIEL/letzte_sicherung.txt"
