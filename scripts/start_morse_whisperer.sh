#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/morse-whisperer-pi"
PY="$APP_DIR/venv/bin/python"
SPLASH="$APP_DIR/morse_whisperer_splash.py"
LOG_DIR="/var/log/morse-whisperer"
LOG_FILE="$LOG_DIR/service.log"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"

# From here on, log everything.
exec >> "$LOG_FILE" 2>&1

cd "$APP_DIR"

echo "============================================================"
echo "[BOOT] The Morse Whisperer starting at $(date -Is)"
echo "[BOOT] user=$(id)"
echo "[BOOT] cwd=$(pwd)"
echo "[BOOT] python=$PY"
echo "============================================================"

for i in $(seq 1 40); do
    if [ -e /dev/fb1 ]; then
        echo "[BOOT] /dev/fb1 ready"
        break
    fi

    echo "[BOOT] waiting for /dev/fb1... ${i}/40"
    sleep 0.25
done

if [ ! -e /dev/fb1 ]; then
    echo "[BOOT] WARNING: /dev/fb1 not found; splash will try configured framebuffer candidates"
fi

exec "$PY" -u "$SPLASH" --no-touch
