# rpicam-mjpeg-http

Small HTTP MJPEG wrapper around `rpicam-vid` for Raspberry Pi camera setups that previously depended on `mjpg-streamer`.

It exposes a browser-friendly MJPEG stream and JPEG snapshot endpoint while using the modern Raspberry Pi camera stack underneath.

## Why

Recent Raspberry Pi OS / camera-stack updates make old `mjpg-streamer`-based setups increasingly fragile or obsolete. Some home dashboards, Grafana panels, MotionEye configurations and browser bookmarks still expect a simple HTTP MJPEG endpoint.

This project provides that compatibility layer:

- Browser page: `http://<raspberrypi-ip>:8080/`
- MJPEG stream: `http://<raspberrypi-ip>:8080/?action=stream`
- JPEG snapshot: `http://<raspberrypi-ip>:8080/?action=snapshot`

Alternative aliases are also available:

- Stream: `http://<raspberrypi-ip>:8080/stream.mjpg`
- Snapshot: `http://<raspberrypi-ip>:8080/snapshot.jpg`

## Requirements

- Raspberry Pi OS with `rpicam-vid`
- A camera supported by the modern libcamera/rpicam stack
- Python 3
- systemd

Check MJPEG and quality support:

    rpicam-vid --help 2>&1 | grep -Ei 'codec|mjpeg|quality|h264'

A short manual camera test:

    timeout 8s rpicam-vid \
      -t 5000 \
      -n \
      --codec mjpeg \
      --width 640 \
      --height 480 \
      --framerate 5 \
      --quality 50 \
      -o /tmp/rpicam-test.mjpg

    file /tmp/rpicam-test.mjpg

## Install

Clone the repository on the Raspberry Pi:

    git clone https://github.com/lpla/rpicam-mjpeg-http.git
    cd rpicam-mjpeg-http
    ./install.sh

The default systemd unit runs:

    /usr/local/bin/rpicam-mjpeg-http.py --host 0.0.0.0 --port 8080 --width 640 --height 480 --framerate 5 --quality 50

This conservative default is intended for low-power Raspberry Pi devices.

## Quality, resolution and frame-rate tuning

The `--quality` option is passed directly to `rpicam-vid` for MJPEG output. Higher values produce better JPEG frames, but also increase bandwidth, CPU use and memory pressure.

Conservative profile:

    /usr/local/bin/rpicam-mjpeg-http.py --host 0.0.0.0 --port 8080 --width 640 --height 480 --framerate 5 --quality 50

Better quality at the same resolution:

    /usr/local/bin/rpicam-mjpeg-http.py --host 0.0.0.0 --port 8080 --width 640 --height 480 --framerate 10 --quality 75

Higher-quality profile for more capable devices:

    /usr/local/bin/rpicam-mjpeg-http.py --host 0.0.0.0 --port 8080 --width 1280 --height 720 --framerate 10 --quality 85

Very high values such as `--quality 90` or above can be useful on stronger devices or wired LANs, but are usually excessive for small headless Raspberry Pi boards.

To override the systemd command:

    sudo systemctl edit rpicam-mjpeg-http.service

Example override:

    [Service]
    ExecStart=
    ExecStart=/usr/local/bin/rpicam-mjpeg-http.py --host 0.0.0.0 --port 8080 --width 1280 --height 720 --framerate 10 --quality 85

Then reload:

    sudo systemctl daemon-reload
    sudo systemctl restart rpicam-mjpeg-http.service

## Replace an older raw H264 TCP service

If you previously had a raw H264 service such as `rpicam-stream.service` on port 8888, disable it after confirming the MJPEG service works:

    sudo systemctl disable --now rpicam-stream.service

Check the final camera state:

    systemctl list-units --type=service --all --no-pager \
      | grep -Ei 'rpicam|mjpg|camera|video|stream' || true

    sudo ss -ltnp | grep -E ':8080|:8888' || true

## Test

From the Raspberry Pi:

    curl -fsS --max-time 5 http://127.0.0.1:8080/ | sed -n '1,20p'

    rm -f /tmp/camera-snapshot.jpg
    curl -fsS --max-time 10 'http://127.0.0.1:8080/?action=snapshot' -o /tmp/camera-snapshot.jpg
    file /tmp/camera-snapshot.jpg
    ls -lh /tmp/camera-snapshot.jpg

From another machine in the LAN:

    curl -v --max-time 8 http://<raspberrypi-ip>:8080/
    curl -v --max-time 10 'http://<raspberrypi-ip>:8080/?action=snapshot' -o /tmp/camera-snapshot.jpg

## MotionEye

Add the camera as a network MJPEG camera using:

    http://<raspberrypi-ip>:8080/?action=stream

## Grafana

Use the same stream URL in an HTML/image/video-compatible panel, depending on the panel type:

    http://<raspberrypi-ip>:8080/?action=stream

For panels that expect a still image, use:

    http://<raspberrypi-ip>:8080/?action=snapshot

## Resource profile

The service keeps one `rpicam-vid --codec mjpeg` process running and serves the latest frames over HTTP. On low-memory Raspberry Pi devices, use modest defaults such as:

- 640x480
- 5 fps
- quality 50

## Logs

    systemctl status rpicam-mjpeg-http.service
    journalctl -u rpicam-mjpeg-http.service -f

## Security note

This service is intended for trusted LAN use. It has no authentication or TLS. Do not expose it directly to the public Internet.
