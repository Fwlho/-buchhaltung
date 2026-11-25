#!/bin/bash
# Grundschutz für den Server (Ubuntu 24.04 / Debian 12) – einmal nach dem Anlegen als root ausführen, darf wiederholt
# werden. Aufruf im Programmordner:   bash deploy/server-haertung.sh
# Anderen SSH-Port verwenden:          SSH_PORT=2222 bash deploy/server-haertung.sh   (vorher in sshd einstellen)
#
# 1 Updates · 2 automatische Sicherheitsupdates (Neustart nachts) · 3 Firewall (nur SSH, 80, 443)
# 4 SSH nur mit Schlüssel (nur wenn schon ein Schlüssel hinterlegt ist – sonst würde man sich aussperren)
# 5 Sperre bei Passwort-Raten (fail2ban) · 6 Uhrzeit-Abgleich (für Anmelde-Codes) und Dateirechte
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen." >&2; exit 2; }
SSH_PORT="${SSH_PORT:-22}"
PROGRAMM="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

echo "1/6 Pakete aktualisieren …"
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install unattended-upgrades ufw fail2ban python3-systemd age rsync

echo "2/6 Automatische Sicherheitsupdates (Neustart bei Bedarf um 04:30, die Dienste starten von selbst wieder) …"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
cat > /etc/apt/apt.conf.d/52buchhaltung-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF

echo "3/6 Firewall: nur SSH ($SSH_PORT), HTTP und HTTPS …"
ufw default deny incoming
ufw default allow outgoing
ufw allow "$SSH_PORT"/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable
# Hinweis: Docker öffnet veröffentlichte Ports an ufw vorbei – deshalb veröffentlicht docker-compose.yml nur 80/443.

echo "4/6 SSH nur noch mit Schlüssel …"
if [ -s /root/.ssh/authorized_keys ] || compgen -G "/home/*/.ssh/authorized_keys" > /dev/null; then
    # „10-“: wird vor 50-cloud-init.conf gelesen, das beim Anbieter oft Passwörter erlaubt (es gilt der erste Treffer)
    cat > /etc/ssh/sshd_config.d/10-buchhaltung.conf <<EOF
Port $SSH_PORT
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
EOF
    sshd -t
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
else
    echo "   ÜBERSPRUNGEN: Kein SSH-Schlüssel hinterlegt – sonst würden Sie sich aussperren."
    echo "   Erst einen Schlüssel einrichten (BETRIEB.md, „Server absichern“), dann dieses Skript erneut ausführen."
fi

echo "5/6 Sperre bei Passwort-Raten (fail2ban) …"
cat > /etc/fail2ban/jail.d/buchhaltung.conf <<EOF
[sshd]
enabled = true
port = $SSH_PORT
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "6/6 Uhrzeit-Abgleich und Dateirechte …"
timedatectl set-ntp true || true
if [ -f "$PROGRAMM/.env" ]; then chown root:root "$PROGRAMM/.env"; chmod 600 "$PROGRAMM/.env"; fi
if [ -d "$PROGRAMM/sicherungen" ]; then chmod 700 "$PROGRAMM/sicherungen"; fi

echo
echo "Fertig. Kontrolle:"
echo "  ufw status verbose                 # nur $SSH_PORT, 80, 443 offen"
echo "  fail2ban-client status sshd        # Sperre aktiv"
echo "  sshd -T | grep -Ei 'passwordauth|permitroot'"
