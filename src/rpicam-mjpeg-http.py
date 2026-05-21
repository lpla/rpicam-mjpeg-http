#!/usr/bin/env python3
import argparse
import http.server
import socketserver
import subprocess
import threading
import time
import sys
from typing import Optional


class CameraState:
    def __init__(self):
        self.cond = threading.Condition()
        self.frame: Optional[bytes] = None
        self.frame_id = 0
        self.running = True


def camera_worker(state: CameraState, args):
    cmd = [
        "/usr/bin/rpicam-vid",
        "-t", "0",
        "-n",
        "--codec", "mjpeg",
        "--width", str(args.width),
        "--height", str(args.height),
        "--framerate", str(args.framerate),
        "-o", "-",
    ]

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
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break

                buf.extend(chunk)

                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi < 0:
                        if len(buf) > 1024 * 1024:
                            del buf[:-2]
                        break

                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi < 0:
                        if soi > 0:
                            del buf[:soi]
                        break

                    frame = bytes(buf[soi:eoi + 2])
                    del buf[:eoi + 2]

                    with state.cond:
                        state.frame = frame
                        state.frame_id += 1
                        state.cond.notify_all()

        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if state.running:
            print("Camera process exited; restarting in 2s", file=sys.stderr, flush=True)
            time.sleep(2)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "rpicam-mjpeg-http/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (
            self.client_address[0],
            self.log_date_time_string(),
            fmt % args,
        ))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        path = self.path.lower()

        if "action=snapshot" in path or path.endswith("/snapshot.jpg"):
            return self.snapshot()

        if "action=stream" in path or path.endswith("/stream.mjpg") or path.endswith("/stream"):
            return self.stream()

        return self.index()

    def index(self):
        body = b"""<!doctype html>
<html>
<head><title>Raspberry Pi camera</title></head>
<body>
<h1>Raspberry Pi camera</h1>
<p><a href="/?action=stream">MJPEG stream</a></p>
<p><a href="/?action=snapshot">JPEG snapshot</a></p>
<img src="/?action=stream" style="max-width:100%;height:auto">
</body>
</html>
"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def get_frame(self, timeout=5):
        state = self.server.state
        deadline = time.time() + timeout

        with state.cond:
            while state.frame is None and time.time() < deadline:
                state.cond.wait(timeout=0.25)
            return state.frame

    def snapshot(self):
        frame = self.get_frame()

        if not frame:
            self.send_error(503, "No camera frame available yet")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(frame)

    def stream(self):
        boundary = "frame"
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        state = self.server.state
        last_id = -1

        try:
            while True:
                with state.cond:
                    state.cond.wait_for(lambda: state.frame is not None and state.frame_id != last_id, timeout=10)
                    frame = state.frame
                    last_id = state.frame_id

                if frame is None:
                    continue

                self.wfile.write(
                    (
                        f"--{boundary}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n"
                        "\r\n"
                    ).encode("ascii")
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError):
            return


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--framerate", type=int, default=5)
    args = parser.parse_args()

    state = CameraState()

    t = threading.Thread(target=camera_worker, args=(state, args), daemon=True)
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.state = state

    print(f"Serving MJPEG on http://{args.host}:{args.port}/", file=sys.stderr, flush=True)

    try:
        server.serve_forever()
    finally:
        state.running = False
        with state.cond:
            state.cond.notify_all()


if __name__ == "__main__":
    main()
