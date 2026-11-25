# Lädt das Programm von diesem Windows-Rechner auf den Server (erste Einrichtung und Updates).
# Aufruf im Programmordner (PowerShell):   .\deploy\hochladen.ps1 -Server root@IHRE-SERVER-IP
# Übertragen werden NUR die Programmdateien – nie die .env, nie der Ordner data (lokale Daten).
# Auf dem Server bleiben .env, Datenbank, Dateien und Sicherungen unangetastet.
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [switch]$NurPaket   # nur das Paket bauen und den Inhalt anzeigen (zum Prüfen), nichts hochladen
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$teile = @("app", "alembic", "alembic.ini", "requirements.txt", "Dockerfile", ".dockerignore", "docker-compose.yml",
           "deploy", "BETRIEB.md", ".env.server.example")
$paket = Join-Path $env:TEMP "buchhaltung-paket.tar.gz"

tar -czf $paket -C $root --exclude=__pycache__ --exclude=*.pyc @teile
if ($LASTEXITCODE -ne 0) { throw "Das Paket konnte nicht gebaut werden." }
if ($NurPaket) {
    tar -tzf $paket
    Write-Host "Paket: $paket"
    return
}

Write-Host "Lade das Programm hoch nach $Server ..."
scp $paket "${Server}:/tmp/buchhaltung-paket.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "Hochladen fehlgeschlagen." }
# Alte Programmdateien ersetzen (gelöschte Dateien sollen nicht liegen bleiben); .env und Daten bleiben.
# Skripte bekommen Linux-Zeilenenden (Windows-Editoren speichern sie sonst mit CR LF - bash bricht dann ab).
ssh $Server "mkdir -p /opt/buchhaltung && cd /opt/buchhaltung && rm -rf app alembic deploy && tar -xzf /tmp/buchhaltung-paket.tar.gz && rm /tmp/buchhaltung-paket.tar.gz && sed -i 's/\r$//' deploy/*.sh deploy/Caddyfile && echo 'Programm liegt in /opt/buchhaltung'"
if ($LASTEXITCODE -ne 0) { throw "Entpacken auf dem Server fehlgeschlagen." }
Remove-Item $paket
Write-Host "Fertig. Weiter mit den Schritten in BETRIEB.md."
