#!/bin/sh
set -eu

SCRIPT_SRC="./src/rpicam-mjpeg-http.py"
SERVICE_SRC="./systemd/rpicam-mjpeg-http.service"

SCRIPT_DST="/usr/local/bin/rpicam-mjpeg-http.py"
SERVICE_DST="/etc/systemd/system/rpicam-mjpeg-http.service"

if [ ! -f "$SCRIPT_SRC" ]; then
  echo "Missing $SCRIPT_SRC" >&2
  exit 1
fi

if [ ! -f "$SERVICE_SRC" ]; then
  echo "Missing $SERVICE_SRC" >&2
  exit 1
fi

python3 - <<PY
from pathlib import Path
p = Path("$SCRIPT_SRC")
compile(p.read_text(), str(p), "exec")
print("Python syntax OK")
PY

sudo install -o root -g root -m 0755 "$SCRIPT_SRC" "$SCRIPT_DST"
sudo install -o root -g root -m 0644 "$SERVICE_SRC" "$SERVICE_DST"

sudo systemctl daemon-reload
sudo systemctl enable rpicam-mjpeg-http.service
sudo systemctl restart rpicam-mjpeg-http.service

echo
echo "Installed:"
systemctl --no-pager --full status rpicam-mjpeg-http.service | sed -n '1,100p'
