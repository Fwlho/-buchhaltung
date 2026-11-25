#!/bin/bash
# Verschlüsselte Sicherung prüfen oder zurückspielen (Gegenstück zu deploy/sicherung.sh). Aufruf im Programmordner:
#
#   bash deploy/wiederherstellen.sh pruefen  SCHLUESSELDATEI STAND   # nur entschlüsseln und lesen – ändert NICHTS
#   bash deploy/wiederherstellen.sh zurueck  SCHLUESSELDATEI STAND   # ERSETZT Datenbank und Dateien durch die Sicherung
#
# SCHLUESSELDATEI = privater age-Schlüssel (AGE-SECRET-KEY-…), den nur der Inhaber hat – nach dem Gebrauch wieder vom
# Server löschen. STAND = Datum/Uhrzeit aus dem Dateinamen, z. B. 2026-11-11_02-30.
# Liegen die Dateien nicht mehr auf dem Server, vorher von der Storage Box holen (siehe BETRIEB.md).
set -euo pipefail
cd "$(dirname "$0")/.."

MODUS="${1:-}"; SCHLUESSEL="${2:-}"; STAND="${3:-}"
ZIEL="${BACKUP_ORDNER:-./sicherungen}"
DB="$ZIEL/datenbank_$STAND.dump.age"
DATEIEN="$ZIEL/dateien_$STAND.tar.gz.age"

if [[ "$MODUS" != "pruefen" && "$MODUS" != "zurueck" ]] || [ -z "$SCHLUESSEL" ] || [ -z "$STAND" ]; then
    sed -n '2,9p' "$0"; exit 2
fi
[ -f "$SCHLUESSEL" ] || { echo "Schlüsseldatei $SCHLUESSEL fehlt." >&2; exit 2; }
for f in "$DB" "$DATEIEN"; do [ -f "$f" ] || { echo "Sicherung $f fehlt." >&2; exit 2; }; done
if [ -f "$ZIEL/pruefsummen_$STAND.txt" ]; then
    ( cd "$ZIEL" && sha256sum -c --quiet "pruefsummen_$STAND.txt" ) || { echo "Prüfsummen stimmen nicht – Datei beschädigt?" >&2; exit 1; }
fi

if [ "$MODUS" = "pruefen" ]; then
    tabellen="$(age -d -i "$SCHLUESSEL" "$DB" | docker compose exec -T db pg_restore --list | grep -c 'TABLE DATA' || true)"
    dateien="$(age -d -i "$SCHLUESSEL" "$DATEIEN" | tar -tzf - | grep -vc '/$' || true)"
    echo "Sicherung $STAND lesbar: $tabellen Tabellen, $dateien Dateien. (Nichts verändert.)"
    exit 0
fi

echo "ACHTUNG: Datenbank und alle Dateien werden durch die Sicherung vom $STAND ERSETZT."
if [ "${4:-}" != "--ja" ]; then
    read -r -p "Zum Fortfahren WIEDERHERSTELLEN eintippen: " antwort
    [ "$antwort" = "WIEDERHERSTELLEN" ] || { echo "Abgebrochen."; exit 1; }
fi
# Vorher entschlüsseln und prüfen – erst wenn beides lesbar ist, wird etwas überschrieben
age -d -i "$SCHLUESSEL" "$DB" | docker compose exec -T db pg_restore --list > /dev/null
age -d -i "$SCHLUESSEL" "$DATEIEN" | tar -tzf - > /dev/null

docker compose stop app worker
age -d -i "$SCHLUESSEL" "$DB" | docker compose exec -T db pg_restore -U buchhaltung -d buchhaltung --clean --if-exists
age -d -i "$SCHLUESSEL" "$DATEIEN" | docker compose run --rm --no-deps -T app sh -c "rm -rf /daten/* && tar -xzf - -C /daten"
docker compose start app worker
echo "Wiederhergestellt: Stand $STAND. Bitte die Schlüsseldatei jetzt wieder vom Server löschen."
