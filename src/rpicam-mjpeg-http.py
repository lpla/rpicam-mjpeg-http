#!/usr/bin/env python3
import argparse
import http.server
import signal
import subprocess
import sys
import threading
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse


BOUNDARY = "FRAME"


class CameraState:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.frame: Optional[bytes] = None
        self.frame_id = 0
        self.running = True
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.process_lock = threading.Lock()

    def set_process(self, proc: Optional[subprocess.Popen[bytes]]) -> None:
        with self.process_lock:
            self.process = proc

    def stop(self) -> None:
        with self.cond:
            self.running = False
            self.cond.notify_all()

        with self.process_lock:
            proc = self.process

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            except ProcessLookupError:
                pass


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

        proc: Optional[subprocess.Popen[bytes]] = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=0,
            )
            state.set_process(proc)

            assert proc.stdout is not None
            buf = bytearray()

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
            if state.running:
                print(f"Camera worker error: {exc}", file=sys.stderr, flush=True)

        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                except ProcessLookupError:
                    pass

            state.set_process(None)

        if state.running:
            print("Camera process exited; restarting in 2 seconds", file=sys.stderr, flush=True)
            time.sleep(2)


class MJPEGHandler(http.server.BaseHTTPRequestHandler):
    server_version = "rpicam-mjpeg-http/1.0"

    @property
    def state(self) -> CameraState:
        return self.server.state  # type: ignore[attr-defined]

    def _resolve_action(self) -> tuple[str, str]:
        parsed = urlparse(self.path)
        action = parse_qs(parsed.query).get("action", [""])[0]

        if parsed.path in ("", "/", "/index.html"):
            return parsed.path, action

        if parsed.path in ("/stream", "/stream.mjpg", "/video.mjpg"):
            return parsed.path, "stream"

        if parsed.path in ("/snapshot", "/snapshot.jpg"):
            return parsed.path, "snapshot"

        return parsed.path, action

    def do_HEAD(self) -> None:
        path, action = self._resolve_action()

        if path not in ("", "/", "/index.html", "/stream", "/stream.mjpg", "/video.mjpg", "/snapshot", "/snapshot.jpg"):
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
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Age", "0")
            self.end_headers()
            return

        self.send_error(400, "Unsupported action")

    def do_GET(self) -> None:
        path, action = self._resolve_action()

        if path in ("", "/", "/index.html") and action in ("", None):
            self.send_index()
            return

        if action == "snapshot":
            self.send_snapshot()
            return

        if action == "stream":
            self.send_stream()
            return

        self.send_error(404, "Not found")

    def send_index(self) -> None:
        body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>rpicam MJPEG HTTP</title>
</head>
<body>
  <h1>rpicam MJPEG HTTP</h1>
  <ul>
    <li><a href="/?action=stream">MJPEG stream</a></li>
    <li><a href="/?action=snapshot">JPEG snapshot</a></li>
    <li><a href="/stream.mjpg">MJPEG stream alias</a></li>
    <li><a href="/snapshot.jpg">JPEG snapshot alias</a></li>
  </ul>
  <img src="/?action=stream" alt="MJPEG stream">
</body>
</html>
""".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def wait_for_frame(self, timeout: float = 10.0) -> Optional[bytes]:
        deadline = time.monotonic() + timeout

        with self.state.cond:
            while self.state.running and self.state.frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.state.cond.wait(timeout=remaining)

            return self.state.frame

    def send_snapshot(self) -> None:
        frame = self.wait_for_frame(timeout=10.0)

        if frame is None:
            self.send_error(503, "No camera frame available")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(frame)

    def send_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Age", "0")
        self.end_headers()

        last_frame_id = -1

        while self.state.running:
            with self.state.cond:
                self.state.cond.wait_for(
                    lambda: not self.state.running or self.state.frame_id != last_frame_id,
                    timeout=10.0,
                )

                if not self.state.running:
                    break

                frame = self.state.frame
                last_frame_id = self.state.frame_id

            if frame is None:
                continue

            try:
                self.wfile.write(
                    b"--" + BOUNDARY.encode("ascii") + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, state: CameraState):
        super().__init__(server_address, RequestHandlerClass)
        self.state = state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expose rpicam-vid MJPEG output over HTTP.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--framerate", type=int, default=5)
    parser.add_argument("--quality", type=int, default=50)
    parser.add_argument("--rpicam-vid", default="/usr/bin/rpicam-vid")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = CameraState()

    camera_thread = threading.Thread(
        target=camera_worker,
        args=(state, args),
        name="camera-worker",
        daemon=False,
    )
    camera_thread.start()

    server = ThreadingHTTPServer((args.host, args.port), MJPEGHandler, state)

    shutdown_started = threading.Event()

    def request_shutdown(signum, frame) -> None:
        if shutdown_started.is_set():
            return

        shutdown_started.set()
        print(f"Received signal {signum}; shutting down", file=sys.stderr, flush=True)

        state.stop()

        # server.shutdown() must not be called from the serve_forever thread itself.
        threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    print(f"Serving MJPEG on http://{args.host}:{args.port}/", file=sys.stderr, flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        state.stop()
        server.server_close()
        camera_thread.join(timeout=5)

    print("Shutdown complete", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
