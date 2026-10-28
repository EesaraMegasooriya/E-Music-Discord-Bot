#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
LAVALINK_VERSION="4.2.2"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Missing $PROJECT_DIR/.env"
  echo "Copy .env.example to .env and fill in the credentials first."
  exit 1
fi

echo "Installing system packages..."
sudo apt-get update
sudo apt-get install -y openjdk-21-jre-headless python3 python3-venv curl

JAVA_MAJOR="$(java -version 2>&1 | awk -F '[\".]' '/version/ {print $2; exit}')"
if [[ -z "$JAVA_MAJOR" || "$JAVA_MAJOR" -lt 17 ]]; then
  echo "Lavalink requires Java 17 or newer; the installed Java is not compatible."
  exit 1
fi

echo "Creating the Python environment..."
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

if [[ ! -f "$PROJECT_DIR/lavalink/Lavalink.jar" ]]; then
  echo "Downloading Lavalink $LAVALINK_VERSION..."
  curl --fail --location --progress-bar \
    "https://github.com/lavalink-devs/Lavalink/releases/download/${LAVALINK_VERSION}/Lavalink.jar" \
    --output "$PROJECT_DIR/lavalink/Lavalink.jar"
fi

chmod 700 "$PROJECT_DIR/deploy/start-bot.sh" "$PROJECT_DIR/deploy/start-lavalink.sh"
chmod 600 "$PROJECT_DIR/.env"

echo "Installing systemd services..."
sudo tee /etc/systemd/system/emusic-lavalink.service >/dev/null <<EOF
[Unit]
Description=E Music Lavalink audio server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR/lavalink
ExecStart=$PROJECT_DIR/deploy/start-lavalink.sh
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/emusic-bot.service >/dev/null <<EOF
[Unit]
Description=E Music Discord bot
After=network-online.target emusic-lavalink.service
Wants=network-online.target
Requires=emusic-lavalink.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/deploy/start-bot.sh
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now emusic-lavalink.service

echo "Waiting for Lavalink to open port 2333..."
for _ in {1..60}; do
  if (echo >/dev/tcp/127.0.0.1/2333) 2>/dev/null; then
    break
  fi
  sleep 1
done

if ! (echo >/dev/tcp/127.0.0.1/2333) 2>/dev/null; then
  echo "Lavalink did not become ready. Check: sudo journalctl -u emusic-lavalink -n 100"
  exit 1
fi

sudo systemctl enable --now emusic-bot.service

echo
echo "E Music is installed and running."
echo "Status: sudo systemctl status emusic-lavalink emusic-bot"
echo "Logs:   sudo journalctl -u emusic-lavalink -u emusic-bot -f"
