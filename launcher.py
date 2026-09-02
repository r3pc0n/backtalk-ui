#!/usr/bin/env python3
"""backtalk launcher — the one thing that's always running, so the
transcript page always has SOMETHING to talk to, whether or not a
call is active.

NOT part of backtalk.main — a separate, persistent process (installed
as a systemd user service, same shape as ai-visualizer.service) that
takes over port 8793 with a placeholder page ONLY while nothing else
needs it. The moment a real call starts, backtalk's own
transcript_server needs that port back, so this gets out of the way
automatically by watching backtalk's single-instance port (8791,
read-only probe, no side effects) rather than trying to track
subprocess state through the launch-talk.sh -> start.sh -> run.sh
chain.

The placeholder serves the SAME index.html the real transcript server
does, so the browser tab never has to navigate anywhere: the page's
own poll loop, and the boot-id reconnect logic already built for the
stale-tab-across-restart bug, handle "idle -> live" the same way they
already handle "old session -> new session" — nothing new needed
client-side beyond checking one flag in the response.

Stdlib only, same as transcript_server.py and every other local
server in this project — no dependency on backtalk's own venv, so it
runs with the system python3 (see the systemd unit).
"""
import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "backtalk" / "transcript_web"
LAUNCH_SCRIPT = HERE.parent / "launch-talk.sh"
LAUNCH_LOG = HERE / "logs" / "launcher-launch.log"
VIEWER_STATE_FILE = HERE / "logs" / "viewer-state.json"

PORT = 8793
INSTANCE_PORT = 8791   # backtalk.main's own single-instance mutex
POLL_S = 1.0

# Tracks the most recent /start attempt so a launch that dies before
# backtalk.main ever claims INSTANCE_PORT can be reported to the page
# instead of leaving it stuck showing "Starting..." forever -- see
# _watch_launch() below. Plain globals, not a class: this whole module
# is a single always-on loop, not something that needs instances.
_launch_proc = None      # the Popen for the current/last attempt, or None
_launch_failed = False   # set once _watch_launch() sees it die unstarted


def _record_viewer(source: str):
    """Best-effort: remember the most recent idle-period poll's origin
    (Television's webview vs. a plain browser) so start.sh can later
    decide whether a viewer already exists, and where, instead of
    always opening a new browser window. Only launcher.py ever writes
    this file -- it's the only thing that sees every poll while idle;
    by the time backtalk.main is actually up, the browser-opening
    decision has already been made. Written on every matching poll
    (last write wins), not just once, so the timestamp always reflects
    how recently someone was actually looking, not just first contact.
    Failure is silent by design -- a missing/stale file just means
    start.sh falls back to its original, always-safe behavior."""
    tmp = VIEWER_STATE_FILE.with_suffix(".tmp")
    try:
        VIEWER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"source": source, "ts": time.time()}))
        tmp.replace(VIEWER_STATE_FILE)
    except OSError:
        pass


def _call_active() -> bool:
    """True if backtalk.main already holds its single-instance port.

    A bind attempt, not a connect probe: backtalk's own mutex socket
    is a pure listen(1) with nothing ever calling accept() on it (see
    main.py's _claim_single_instance), so repeatedly connect()ing to
    it fights over that single-slot accept backlog and produces
    exactly the flapping false-negatives this comment is replacing —
    confirmed live (2026-09-01): the launcher saw active=False and
    kept retrying its own bind for the whole rest of a real call, even
    though backtalk.main plainly still held the port throughout. A
    bind attempt is the clean, stateless version of the same question
    ("is anything on this port") and never touches the backlog at
    all."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        return False   # nothing was using it
    except OSError:
        return True    # already bound by backtalk.main
    finally:
        s.close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; this runs unattended, always

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/transcript", "/transcript/"):
            self._serve_file(WEB_DIR / "index.html", "text/html")
        elif parsed.path == "/api/transcript":
            source = parse_qs(parsed.query).get("source", [None])[0]
            if source in ("television", "browser"):
                _record_viewer(source)
            body = {"idle": True}
            if _launch_failed:
                body["launch_failed"] = True
            self._send_json(200, body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global _launch_proc, _launch_failed
        parsed = urlparse(self.path)
        if parsed.path == "/start":
            try:
                LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
                logf = open(LAUNCH_LOG, "a")
                logf.write(
                    f"\n--- launch attempt {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                logf.flush()
                _launch_failed = False
                _launch_proc = subprocess.Popen(
                    [str(LAUNCH_SCRIPT)],
                    stdout=logf, stderr=logf,
                    start_new_session=True)
                logf.close()   # Popen already dup'd it; this is our copy
                self._send_json(202, {"ok": True})
            except OSError as e:
                self._send_json(500, {"error": str(e)})
        elif parsed.path == "/api/input":
            # No live session to route this to while idle. Accepted,
            # not a crash — the page's own postInput() already
            # swallows a failed request silently, so a settings-menu
            # button clicked while idle just harmlessly does nothing.
            self._send_json(503, {"error": "no active session"})
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _watch_launch(active: bool):
    """Reconcile the tracked /start attempt against reality: clear it on
    a real success, flag it once the whole launch-talk.sh -> start.sh ->
    backtalk.main chain has visibly died without ever claiming
    INSTANCE_PORT. `active` is this tick's _call_active() result, passed
    in rather than recomputed."""
    global _launch_proc, _launch_failed
    if active:
        _launch_proc = None
        return
    if _launch_proc is not None and _launch_proc.poll() is not None:
        print(f"[launcher] launch attempt exited (code "
              f"{_launch_proc.returncode}) without ever starting "
              f"backtalk.main — see {LAUNCH_LOG}", flush=True)
        _launch_failed = True
        _launch_proc = None


def main():
    server = None
    print("[launcher] watching for backtalk.main on port "
          f"{INSTANCE_PORT}...", flush=True)
    while True:
        active = _call_active()
        _watch_launch(active)
        if active and server is not None:
            print("[launcher] call started — releasing the port so "
                  "backtalk's own transcript server can take it",
                  flush=True)
            server.shutdown()
            server.server_close()
            server = None
        elif not active and server is None:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
            except OSError as e:
                # backtalk's own server (shutting down) or something
                # else still holds it — retry next tick, never crash.
                print(f"[launcher] port {PORT} busy, retrying: {e}",
                      flush=True)
                server = None
            else:
                threading.Thread(target=server.serve_forever,
                                 daemon=True).start()
                print(f"[launcher] idle placeholder up at "
                      f"http://127.0.0.1:{PORT}/", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
