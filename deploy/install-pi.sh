#!/usr/bin/env bash
# Install the Airlog recorder as a systemd service on a Raspberry Pi (Pi OS Bookworm, 64-bit).
#
#   cd airlog && sudo ./deploy/install-pi.sh
#
# Options (environment variables):
#   AIRLOG_USER   user to run as            (default: the user who ran sudo)
#   DATA_DIR      archive + grabs location  (default: ~AIRLOG_USER/Airlog — point at a USB SSD if you have one)
#   PORT          web UI port               (default: 8080)
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi

AIRLOG_USER="${AIRLOG_USER:-${SUDO_USER:-pi}}"
USER_HOME="$(getent passwd "$AIRLOG_USER" | cut -d: -f6)"
DATA_DIR="${DATA_DIR:-$USER_HOME/Airlog}"
PORT="${PORT:-8080}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/airlog
CFG_DIR="$USER_HOME/.config/airlog"
CFG="$CFG_DIR/config.json"

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-flask python3-waitress alsa-utils

echo "==> App -> $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
cp -r "$SRC/airlog" "$APP_DIR/"

echo "==> Permissions: $AIRLOG_USER in 'audio' group"
usermod -aG audio "$AIRLOG_USER"
mkdir -p "$DATA_DIR/Archive" "$DATA_DIR/Grabs" "$CFG_DIR"
chown -R "$AIRLOG_USER:" "$DATA_DIR" "$CFG_DIR"

# Grab buffer: keep the constant raw-audio writes off the SD card.
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [[ $MEM_MB -ge 3500 ]]; then
  SPOOL=/dev/shm/airlog; BUFFER=60          # RAM: 60 min ≈ 690 MB at 48 kHz stereo (lost on reboot)
else
  SPOOL="$DATA_DIR/.spool"; BUFFER=60
fi

if [[ ! -f "$CFG" ]]; then
  echo "==> Writing $CFG"
  cat > "$CFG" <<JSON
{
  "station_name": "Station",
  "source_type": "stream",
  "stream_url": "",
  "device_backend": "alsa",
  "archive_dir": "$DATA_DIR/Archive",
  "grabs_dir": "$DATA_DIR/Grabs",
  "spool_dir": "$SPOOL",
  "buffer_minutes": $BUFFER,
  "web_port": $PORT
}
JSON
  chown "$AIRLOG_USER:" "$CFG"
else
  echo "==> Keeping existing $CFG"
fi

echo "==> systemd service"
cat > /etc/systemd/system/airlog.service <<UNIT
[Unit]
Description=Airlog radio recorder
# The Pi has no RTC: wait for NTP so file names and grab times are right after a reboot.
Wants=network-online.target time-sync.target
After=network-online.target time-sync.target sound.target

[Service]
User=$AIRLOG_USER
Group=audio
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m airlog.server --config $CFG
Restart=always
RestartSec=3
Nice=-5

[Install]
WantedBy=multi-user.target
UNIT

systemctl enable --now systemd-time-wait-sync.service >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable airlog.service
systemctl restart airlog.service

IP=$(hostname -I | awk '{print $1}')
echo
echo "Airlog is running.  Open:  http://$IP:$PORT/"
echo "Set the source in Settings.  Logs:  journalctl -u airlog -f"
echo "Grab buffer: $SPOOL ($BUFFER min)"
