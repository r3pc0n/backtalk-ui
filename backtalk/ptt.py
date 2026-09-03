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
"""Hold-to-talk — a global key listener.

HOLD the key -> mic opens. RELEASE -> mic closes and the utterance is
processed. The button IS the voice-activity detector, which is why this
mode is speaker-safe with no headphones: the mic simply isn't open while
the assistant talks, unless you press the key — and pressing while it
talks interrupts it.

THE KEY-REPEAT TRAP (the bug that kills every naive build): the OS fires
on_press events CONTINUOUSLY while a key is held. Without the held-state
filter below, every repeat reads as a fresh press and keeps cancelling
the reply before it can speak.

AND THE HALF THAT TRAP HIDES: some keyboards send auto-repeat as full
DOWN/UP PAIRS rather than the repeated DOWN-only stream. Filtering the
presses and trusting every release then breaks the OTHER way -- a single
hold is chopped into dozens of ~50ms recordings, each too short to
transcribe, and the whole thing is SILENT. No exception, no log line,
nothing to search for; it simply reads as "the microphone does not work".
Measured in the field on a Logitech MX Mechanical through a Bolt
receiver: one 2.6-second hold produced 186 key events and about fifty
recordings. So a release is never trusted on sight -- see is_held().

macOS needs Input Monitoring permission for the hosting terminal
(System Settings -> Privacy & Security -> Input Monitoring). Windows
works out of the box; some Linux desktops need the user in the `input`
group or an X11 session.

NATIVE WAYLAND (Hyprland, Sway, GNOME Wayland, ...): pynput has exactly
two Linux backends and neither works unprivileged here. Its default
backend rides XWayland's X11 record extension, which only sees events
from X11/XWayland client windows -- a native Wayland window's keypresses
never reach it, so the listener starts cleanly and silently sees nothing,
key-independent. Its other backend (uinput) needs a real console and
root. So on Linux this module reads /dev/input/event* directly via
evdev instead (see PTTListener below): kernel-level, so it works
identically on X11 and Wayland, and only needs the user in the `input`
group (no root, no grab -- it's a passive read, so the key still reaches
whatever app has focus too). Falls back to pynput if evdev can't find a
usable keyboard device.
"""
import sys
import threading
import time


def resolve_key(name: str):
    """'home' / 'f13' / 'right_alt' / any single character -> pynput key."""
    name = (name or "home").strip().lower()
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    # Friendly names -> pynput's names. pynput calls the right option key
    # alt_r, not right_alt; the docs speak human, this map translates.
    # (Field-caught: right_alt silently fell back to home, which Mac
    # laptops cannot press, so the voice looked healthy and never fired.)
    aliases = {
        "right_alt": "alt_r", "left_alt": "alt_l",
        "right_option": "alt_r", "left_option": "alt_l",
        "right_ctrl": "ctrl_r", "left_ctrl": "ctrl_l",
        "right_cmd": "cmd_r", "left_cmd": "cmd_l",
        "right_shift": "shift_r", "left_shift": "shift_l",
    }
    name = aliases.get(name, name)
    try:
        return getattr(keyboard.Key, name)
    except AttributeError:
        print(f"[ptt] unknown key {name!r} — falling back to 'home'",
              flush=True)
        return keyboard.Key.home


class _ReleaseGraceMixin:
    """The debounce logic (see THE KEY-REPEAT TRAP above), shared by both
    backends so a release is never trusted until it survives the grace
    window unchallenged by a following press."""
    RELEASE_GRACE = 0.12

    def _init_grace(self):
        self._held = False
        self._release_t = None
        self._press_evt = threading.Event()

    def _on_press(self):
        self._release_t = None
        if not self._held:                      # filter key-repeat
            self._held = True
            self._press_evt.set()

    def _on_release(self):
        # PROVISIONAL. Believed only if no press follows; see _settle().
        self._release_t = time.monotonic()

    def _settle(self):
        """Commit a release that has stood unchallenged for the grace window."""
        r = self._release_t
        if self._held and r is not None and \
                time.monotonic() - r >= self.RELEASE_GRACE:
            self._held = False
            self._release_t = None

    def wait_press(self):
        """Block until the key goes DOWN (one event per physical press)."""
        # Settled on a loop, not once. A release landing after the last
        # is_held() poll leaves _held provisionally True, and a single
        # settle-then-wait would then block forever: the next press is
        # filtered as key-repeat, so nothing ever sets the event again.
        while True:
            self._settle()
            if self._press_evt.wait(timeout=self.RELEASE_GRACE):
                self._press_evt.clear()
                return

    def is_held(self) -> bool:
        self._settle()
        return self._held


class _PynputPTTListener(_ReleaseGraceMixin):
    def __init__(self, key="home"):
        # Imported here, not at module load, so an X-less environment
        # (no DISPLAY -- e.g. this module loaded from a systemd --user
        # service with no desktop session vars) never crashes the whole
        # process on import. evdev is the real backend on Linux; pynput
        # is only reached at all if evdev's own constructor already
        # failed, so paying its import cost eagerly buys nothing here.
        global keyboard
        from pynput import keyboard
        self._key = resolve_key(key) if isinstance(key, str) else key
        self._init_grace()
        self._listener = keyboard.Listener(on_press=self._handle_press,
                                           on_release=self._handle_release)
        self._listener.daemon = True
        self._listener.start()

    def _handle_press(self, k):
        if k == self._key:
            self._on_press()

    def _handle_release(self, k):
        if k == self._key:
            self._on_release()


def _resolve_evdev_key(name, ecodes):
    """'home' / 'f13' / 'right_alt' / any single character -> evdev KEY_* code."""
    name = (name or "home").strip().lower()
    if len(name) == 1 and name.isalnum():
        code = getattr(ecodes, f"KEY_{name.upper()}", None)
        if code is not None:
            return code
    aliases = {
        "right_alt": "RIGHTALT", "left_alt": "LEFTALT",
        "right_option": "RIGHTALT", "left_option": "LEFTALT",
        "right_ctrl": "RIGHTCTRL", "left_ctrl": "LEFTCTRL",
        "right_cmd": "RIGHTMETA", "left_cmd": "LEFTMETA",
        "right_shift": "RIGHTSHIFT", "left_shift": "LEFTSHIFT",
    }
    key_name = aliases.get(name, name.upper())
    code = getattr(ecodes, f"KEY_{key_name}", None)
    if code is None:
        raise ValueError(f"unknown key {name!r} for the evdev backend")
    return code


def _find_evdev_keyboards(key_code):
    """Every readable input device that actually exposes this key -- a
    keyboard usually shows up as several device nodes (the report ships
    a Keychron dongle presenting FOUR: a plain keyboard interface, a
    consumer-control node, and more), and only some of them carry the
    real key table. Reading them all and letting whichever one fires
    win costs nothing and removes the guesswork."""
    import evdev
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
        except OSError:
            continue
        if key_code in caps:
            devices.append(dev)
    return devices


class _EvdevPTTListener(_ReleaseGraceMixin):
    """Kernel-level key listener via /dev/input -- works on X11 and native
    Wayland alike. Read-only: never grabs the device, so the key still
    reaches whatever window has focus, same as pynput's passive hook."""

    def __init__(self, key="home"):
        import evdev
        self._evdev = evdev
        self._key_code = _resolve_evdev_key(key, evdev.ecodes)
        self._devices = _find_evdev_keyboards(self._key_code)
        if not self._devices:
            raise RuntimeError(
                f"no readable /dev/input device exposes key {key!r} "
                "(check you're in the 'input' group)")
        self._init_grace()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        import select
        devs = {d.fd: d for d in self._devices}
        while not self._stop:
            try:
                ready, _, _ = select.select(devs.keys(), [], [], 0.5)
            except (OSError, ValueError):
                return
            for fd in ready:
                dev = devs[fd]
                try:
                    for event in dev.read():
                        if event.type != self._evdev.ecodes.EV_KEY or \
                                event.code != self._key_code:
                            continue
                        if event.value == 1:        # down
                            self._on_press()
                        elif event.value == 0:      # up
                            self._on_release()
                        # value == 2 is kernel autorepeat -- ignored; the
                        # held-state filter above already covers it.
                except OSError:
                    continue


def PTTListener(key="home"):
    """Factory: evdev on Linux (works under Wayland; see the module
    docstring), pynput everywhere else or if evdev can't find a device."""
    if sys.platform.startswith("linux"):
        try:
            return _EvdevPTTListener(key)
        except Exception as e:
            print(f"[ptt] evdev backend unavailable ({e}) "
                  "— falling back to pynput", flush=True)
    return _PynputPTTListener(key)
