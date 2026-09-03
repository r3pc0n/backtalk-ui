# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transcript UI — a small stdlib HTTP server that replaces the raw
terminal transcript window with a persistent web page: chat bubbles,
speaker separation, a plain text input with no keyboard-focus problems
to fight.

Deliberately stdlib-only (ThreadingHTTPServer), the same shape as
vault-graph and ai-visualizer, rather than adding a websocket
dependency: this venv has already broken once from a transitive
dependency regression (see backtalk.md's setuptools/pkg_resources
incident), and a chat transcript has none of the waveform's 15fps
urgency. The page polls a cursor endpoint instead, same pattern as
vault-graph's /api/activity?since=N.

Typed input POSTed here goes straight into the SAME typed_q the
terminal reader feeds (see main.py's _typed_reader) — that is what
makes it a first-class input path rather than a parallel one: every
bit of existing routing (pending permission asks, console verbs,
interrupts) already watches that queue and needs no new code to also
see this.

Runs on its own background thread inside backtalk's asyncio process.
Nothing here talks to the brain, mouth, or ears directly — it only
reads/writes the transcript buffer and the typed-input queue.
"""
import json
import queue
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backtalk.vlog import log

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "transcript_web"

# One random id per process, handed back on every /api/transcript
# response. The event buffer below is in-memory and its ids reset to 1
# on every restart, so a browser tab left open across one (a crash, a
# relaunch) would otherwise keep polling with a cursor from the OLD
# process — which the new process's low ids never catch up to, so it
# just looks stuck: green "connected" dot, nothing new ever appears.
# Confirmed live (2026-09-01): a tab open before a restart silently
# stopped updating after one. The client resets to since=0 the moment
# this id doesn't match what it saw last.
BOOT_ID = uuid.uuid4().hex

_MAX_EVENTS = 2000
_lock = threading.Lock()
_events: list[dict] = []
_next_id = 1

_typed_q: "queue.Queue[str] | None" = None   # set by start()

# Live session state (tier/effort/auto_approve) for the always-visible
# status pill and the settings panel's toggle/button highlighting.
# main.py owns these values and pushes changes here whenever one
# actually changes -- nothing here polls or infers state on its own.
_state: dict = {"tier": None, "effort": None, "auto_approve": None}


def set_state(**kwargs):
    """Update one or more live-state fields. Never raises, same rule as
    add_event -- a broken status readout must never take the voice
    line down."""
    try:
        with _lock:
            _state.update(kwargs)
    except Exception:
        pass


def add_event(speaker: str, text: str):
    """Append one transcript line. speaker: 'you' or 'assistant'. Never
    raises — a broken transcript feed must never take the voice down,
    the same rule vlog.log and the signal bus follow."""
    global _next_id
    if not text:
        return
    try:
        with _lock:
            event = {"id": _next_id, "speaker": speaker, "text": text,
                     "ts": time.time()}
            _next_id += 1
            _events.append(event)
            if len(_events) > _MAX_EVENTS:
                del _events[: len(_events) - _MAX_EVENTS]
    except Exception:
        pass


def _events_since(since_id: int):
    with _lock:
        return [e for e in _events if e["id"] > since_id]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep backtalk's console quiet; this runs unattended

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/transcript", "/transcript/"):
            # "/transcript/" exists so start.sh can launch this as its
            # own app window: Chrome's --app window class comes from
            # origin+path (port is NOT part of it), so serving only at
            # bare "/" would collide with vault-graph's window class —
            # both are 127.0.0.1 on a bare path, just different ports.
            # Confirmed live: launching "/" produced the exact same
            # chrome-127.0.0.1__-Default class vault-graph already
            # uses. "/" itself still works for a manual bookmark/visit.
            self._serve_file(WEB_DIR / "index.html", "text/html")
        elif parsed.path == "/api/transcript":
            since_raw = parse_qs(parsed.query).get("since", ["0"])[0]
            try:
                since_id = int(since_raw)
            except ValueError:
                since_id = 0
            with _lock:
                state = dict(_state)
            self._send_json(200, {"boot": BOOT_ID, "state": state,
                                  "events": _events_since(since_id)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/input":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        raw_body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return
        text = (str(payload.get("text") or "").strip()
                if isinstance(payload, dict) else "")
        if not text:
            self._send_json(400, {"error": "text must be non-empty"})
            return
        if _typed_q is not None:
            _typed_q.put(text)
        self._send_json(201, {"ok": True})

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


def start(typed_q: "queue.Queue[str]", port: int,
          open_browser: bool = False) -> bool:
    """Starts the transcript server on a background thread. Never
    raises — a UI that fails to bind must degrade, not take the voice
    line down (the same "never mute" rule mouth.py's engine chain
    follows). Returns True if it actually started."""
    global _typed_q
    _typed_q = typed_q
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        log(f"[transcript-ui] couldn't bind port {port}: {e} — "
            "UI unavailable this session")
        return False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    log(f"[transcript-ui] serving at {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return True
