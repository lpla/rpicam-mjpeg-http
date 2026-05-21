#!/usr/bin/env python3
import argparse
import http.server
import signal
import socketserver
import subprocess
import sys
import threading
import time
from typing import Optional


class CameraState:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.frame: Optional[bytes] = None
        self.frame_id = 0
        self.running = True


def build_camera_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        args.rpicam_vid,
        "-t", "0",
        "-n",
        "--codec", "mjpeg",
        "--width", str(args.width),
        "--height", str(args.height),
        "--framerate", str(args.framerate),
    ]

    if args.quality is not None:
        cmd.extend(["--quality", str(args.quality)])

    cmd.extend(["-o", "-"])
    return cmd


def camera_worker(state: CameraState, args: argparse.Namespace) -> None:
    cmd = build_camera_command(args)

    while state.running:
        print("Starting camera:", " ".join(cmd), file=sys.stderr, flush=True)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )

        buf = bytearray()

        try:
            assert proc.stdout is not None

            while state.running:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break

                buf.extend(chunk)

                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        if len(buf) > 1024 * 1024:
                            del buf[:-2]
                        break

                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        break

                    frame = bytes(buf[start:end + 2])
                    del buf[:end + 2]

                    with state.cond:
                        state.frame = frame
                        state.frame_id += 1
                        state.cond.notify_all()

        except Exception as exc:
            print(f"Camera worker error: {exc}", file=sys.stderr, flush=True)

        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

        if state.running:
            print("Camera process exited; restarting in 2 seconds", file=sys.stderr, flush=True)
            time.sleep(2)


class MJPEGHandler(http.server.BaseHTTPRequestHandler):

    def do_HEAD(self):
        """Return headers for HTTP HEAD probes without streaming a body."""
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        action = parse_qs(parsed.query).get("action", [""])[0]

        # mjpg-streamer-compatible URLs:
        #   /?action=snapshot
        #   /?action=stream
        # plus a few direct aliases useful for probes and dashboards.
        if parsed.path in ("/stream", "/stream.mjpg", "/video.mjpg"):
            action = "stream"
        elif parsed.path in ("/snapshot", "/snapshot.jpg"):
            action = "snapshot"
        elif parsed.path not in ("", "/"):
            self.send_error(404, "Not found")
            return

        if action in ("", None):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        if action == "snapshot":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            return

        if action == "stream":
            boundary = globals().get("BOUNDARY", "FRAME")
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Age", "0")
            self.end_headers()
            return

        self.send_error(400, "Unsupported action")

    server_version = "rpicam-mjpeg-http/1.0"

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.send_index()
            return

        if self.path.startswith("/?action=snapshot") or self.path == "/snapshot.jpg":
            self.send_snapshot()
            return

        if self.path.startswith("/?action=stream") or self.path == "/stream.mjpg":
            self.send_stream()
            return

        self.send_error(404, "Not found")

    def send_index(self) -> None:
        html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Raspberry Pi Camera MJPEG Stream</title>
  <style>
    body { font-family: sans-serif; max-width: 900px; margin: 2rem auto; }
    img { max-width: 100%; height: auto; border: 1px solid #ccc; }
    code { background: #eee; padding: 0.1rem 0.25rem; }
  </style>
</head>
<body>
  <h1>Raspberry Pi Camera MJPEG Stream</h1>
  <p>Stream URL: <code>/?action=stream</code></p>
  <p>Snapshot URL: <code>/?action=snapshot</code></p>
  <img src="/?action=stream" alt="MJPEG stream">
</body>
</html>
"""
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_snapshot(self) -> None:
        state: CameraState = self.server.state  # type: ignore[attr-defined]

        with state.cond:
            if state.frame is None:
                state.cond.wait(timeout=5)
            frame = state.frame

        if frame is None:
            self.send_error(503, "No camera frame available yet")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(frame)

    def send_stream(self) -> None:
        state: CameraState = self.server.state  # type: ignore[attr-defined]
        boundary = "FRAME"

        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.end_headers()

        last_id = -1

        try:
            while state.running:
                with state.cond:
                    state.cond.wait_for(
                        lambda: state.frame_id != last_id or not state.running,
                        timeout=10,
                    )
                    if not state.running:
                        break
                    frame = state.frame
                    last_id = state.frame_id

                if frame is None:
                    continue

                self.wfile.write(f"--{boundary}\r\n".encode("ascii"))
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            f"{self.address_string()} - - [{self.log_date_time_string()}] {fmt % args}",
            file=sys.stderr,
            flush=True,
        )


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expose a Raspberry Pi camera as an mjpg-streamer-like HTTP MJPEG endpoint."
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port")
    parser.add_argument("--width", type=int, default=640, help="Camera frame width")
    parser.add_argument("--height", type=int, default=480, help="Camera frame height")
    parser.add_argument("--framerate", type=int, default=5, help="Camera frame rate")
    parser.add_argument(
        "--quality",
        type=int,
        default=None,
        choices=range(1, 101),
        metavar="1-100",
        help="MJPEG quality passed to rpicam-vid. Higher values increase image quality, bandwidth and CPU/memory pressure.",
    )
    parser.add_argument("--rpicam-vid", default="/usr/bin/rpicam-vid", help="Path to rpicam-vid")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = CameraState()

    def stop_handler(signum, frame) -> None:  # type: ignore[no-untyped-def]
        state.running = False
        with state.cond:
            state.cond.notify_all()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    worker = threading.Thread(target=camera_worker, args=(state, args), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((args.host, args.port), MJPEGHandler)
    server.state = state  # type: ignore[attr-defined]

    print(f"Serving MJPEG on http://{args.host}:{args.port}/", file=sys.stderr, flush=True)

    try:
        while state.running:
            server.handle_request()
    finally:
        state.running = False
        with state.cond:
            state.cond.notify_all()
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
