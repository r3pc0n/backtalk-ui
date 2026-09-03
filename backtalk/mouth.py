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
"""The mouth — streaming sentence-chunked TTS, played through one
long-lived output stream.

Default engine: Kokoro, in-process. Local, free, no server, no API key,
~0.2s to first audio once warm. Optional premium engines: ElevenLabs or
Cartesia, on YOUR key — read from the system keychain, never from a file
(see _get_elevenlabs_key / _get_cartesia_key) — with Kokoro as the
automatic fallback: the voice degrades instead of going mute if the
cloud fails. Cartesia wins if both are enabled at once (see
synth_stream). Cartesia needs no ffmpeg step — it hands back raw PCM
directly in the format we ask for. The local-mode equivalent is Pocket
TTS (kyutai-labs/pocket-tts) — CPU-only, no GPU contention, run as its
own local HTTP server in its own venv (see _ensure_pocket) rather than
imported into this one. A second local-GPU option, Chatterbox-Turbo
(Resemble AI), sits below Pocket TTS and above Kokoro — same
arm's-length local-server pattern as Pocket TTS, its own isolated venv
at ~/my-agent/chatterbox-tts (see _ensure_chatterbox). Replaces CSM as
of 2026-09-03, retired outright rather than left parked.

Sentences are synthesized one at a time and queued for playback, so the
first sentence is audible while later ones are still rendering. Playback
is cancellable mid-word: set the stop event and the speaker goes silent
within one audio block plus the device buffer (~0.15s).

HARD-WON AUDIO LAW #1 — ONE long-lived OutputStream, reused for every
sentence for the life of the process. A fresh stream per sentence gives
an audible onset blip or a beat of dead air on plenty of audio setups
(USB interfaces, Bluetooth, streaming mixers that latch onto each new
stream late). Proven by A/B test; do not "simplify" this away.

HARD-WON AUDIO LAW #2 — buffer ~0.75s of synthesized audio before a
sentence starts playing, so a slower machine never underruns into
slow-motion garble.
"""
import os
import queue
import re
import shutil
import sys
import tempfile
import threading

import numpy as np
import sounddevice as sd

from backtalk.config import CFG
from backtalk.vlog import log

KOKORO_RATE = 24000
EL_RATE = 44100
CARTESIA_RATE = 44100
CHATTERBOX_RATE = 24000
POCKET_RATE = 24000
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_pipe = None
_pipe_lock = threading.Lock()

_chatterbox_proc = None    # subprocess.Popen of chatterbox_server.py, or None
_chatterbox_lock = threading.Lock()

_pocket_proc = None        # subprocess.Popen of `pocket-tts serve`, or None
_pocket_file_httpd = None  # localhost static file server for the voice, or None
_pocket_lock = threading.Lock()


def _apply_gain(pcm: np.ndarray) -> np.ndarray:
    """Scale one int16 PCM block by CFG["volume"] (a percent, 100 =
    unchanged). The one place this needs to happen: every engine's
    audio passes through here on its way to the speaker (see
    Mouth._play_stream), so a single multiply covers all of them.
    Float32 round-trip + clip, not a raw int16 multiply, so a >100%
    boost clips cleanly instead of wrapping into ugly digital
    distortion."""
    vol = CFG.get("volume", 100)
    if vol == 100:
        return pcm
    gain = max(0.0, vol) / 100.0
    return np.clip(pcm.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


def _ensure_espeak():
    """kokoro phonemizes through system espeak-ng (its bundled loader
    ships a broken build path — found the hard way; upstream's own docs
    say install the system package). Help phonemizer find it in the
    usual homes when the env isn't already set."""
    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY"):
        return
    candidates = (
        "/opt/homebrew/lib/libespeak-ng.dylib",       # macOS arm64 (brew)
        "/usr/local/lib/libespeak-ng.dylib",          # macOS intel (brew)
        "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1",  # debian/ubuntu
        "/usr/lib/libespeak-ng.so.1",                 # other linux
        "C:\\Program Files\\eSpeak NG\\libespeak-ng.dll",       # windows
        "C:\\Program Files (x86)\\eSpeak NG\\libespeak-ng.dll",
    )
    for lib in candidates:
        if os.path.exists(lib):
            os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = lib
            break


# Every espeak library filename phonemizer might copy, on any platform. A
# directory holding exactly one of these and nothing else is a phonemizer
# scratch dir and is not plausibly anything else.
_ESPEAK_LIB_NAMES = (
    "espeak-ng.dll",
    "libespeak-ng.dll",
    "libespeak-ng.so",
    "libespeak-ng.so.1",
    "libespeak-ng.dylib",
)


def _is_orphan_espeak_tempdir(path: str) -> bool:
    """True only for a directory whose ENTIRE contents are one espeak
    library. That signature is what makes it safe to point a delete at a
    shared temp folder: one file, and its name is one of five."""
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    return len(entries) == 1 and entries[0] in _ESPEAK_LIB_NAMES


def _sweep_orphan_espeak_tempdirs():
    """Delete espeak scratch dirs left behind by previous runs.

    phonemizer copies the espeak shared library into a fresh temp dir for
    every backend it builds, because espeak-ng keeps its state in globals
    and the loader refuses the same file twice. Kokoro builds several
    backends, so ONE start leaves several behind.

    On POSIX that cleanup rides a finalizer and usually happens. On
    Windows phonemizer can only register it with atexit, and atexit does
    not run when a process is KILLED rather than exited -- so anything
    stopping the voice line by terminating it, which is most launchers and
    every supervisor, leaks every directory it ever made. Sixty had piled
    up on the machine where this was found, and fifteen were sitting on
    the author's own Mac when it was reviewed: the POSIX path is not as
    reliable as it looks either. The count only ever grows.

    Patching phonemizer where it is installed is not a fix, because the
    launcher runs a dependency sync that would overwrite it. Sweeping at
    our own startup bounds the total at one run's worth instead.

    Two things make deleting from a shared temp folder safe, and only the
    first is ours: the signature above is narrow enough that nothing else
    matches it, and anything we are not permitted to remove raises and is
    skipped. On Windows a loaded library cannot be deleted at all, so a
    live instance is protected by the OS rather than by us noticing it.
    POSIX does not work that way, but a process that has already mapped
    the library keeps it after the unlink, so a running instance is
    unharmed either way.
    """
    root = tempfile.gettempdir()
    swept = 0
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or not _is_orphan_espeak_tempdir(path):
            continue
        try:
            shutil.rmtree(path)
            swept += 1
        except OSError:
            pass          # in use, or not ours. Leaving it is correct.
    if swept:
        log(f"[mouth] swept {swept} orphaned espeak temp dir(s)")


def warm():
    """Load the Kokoro pipeline (first call downloads the model to the
    HF cache). Called at startup while the greeting text is composed."""
    global _pipe
    with _pipe_lock:
        if _pipe is None:
            _ensure_espeak()
            # Before kokoro makes this run's scratch dirs, clear the ones
            # earlier runs could not clean up on their way out.
            _sweep_orphan_espeak_tempdirs()
            from kokoro import KPipeline
            # The voice name's first letter IS the language pipeline:
            # a=American English, b=British English, e/f/h/i/j/p/z = the
            # other shipped languages. bm_lewis -> 'b'.
            lang = (CFG["voice"] or "bm_lewis")[0]
            log(f"[mouth] loading kokoro (lang '{lang}', "
                f"voice {CFG['voice']})...")
            _pipe = KPipeline(lang_code=lang)
            log("[mouth] voice ready")
    return _pipe


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_RE.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _stream_kokoro(text: str):
    """One sentence -> int16 PCM chunks at 24kHz, in-process."""
    pipe = warm()
    try:
        speed = float(CFG.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    for _, _, audio in pipe(text, voice=CFG["voice"], speed=speed):
        a = np.asarray(audio, dtype=np.float32)
        if a.size:
            yield (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)


def _stream_elevenlabs(text: str, timeout: float):
    """ElevenLabs -> ffmpeg streaming decode -> int16 PCM at 44.1kHz.

    THE ELEVENLABS DOCTRINE, learned the expensive way:
    - fetch mp3_44100_128 and decode locally (raw 44.1k PCM needs their
      Pro tier; the mp3 decode hides inside network wait anyway)
    - turbo model, stability 0.5, similarity 0.75
    - never the multilingual model for English, never style above 0 —
      both make delivery slow and dull
    - their site previews are MASTERED demo clips; raw API output never
      matches them, so master locally (the ffmpeg chain in config)
    ffmpeg reads stdin as we feed it, so playback still starts before
    synthesis finishes."""
    import subprocess

    import httpx

    el = CFG["elevenlabs"]
    key = _get_elevenlabs_key()
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/"
           f"{el['voice_id']}/stream?output_format=mp3_44100_128")
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "quiet", "-i", "pipe:0",
         "-af", el["master"],
         "-f", "s16le", "-ar", str(EL_RATE), "-ac", "1", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    feed_error: list = []

    def _feed():
        try:
            with httpx.stream("POST", url, headers={"xi-api-key": key},
                              json={"text": text, "model_id": el["model"],
                                    "voice_settings": {
                                        "stability": 0.5,
                                        "similarity_boost": 0.75}},
                              timeout=timeout) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(chunk_size=4096):
                    proc.stdin.write(chunk)
        except Exception as e:
            feed_error.append(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_feed, daemon=True)
    t.start()
    carry = b""
    got_audio = False
    while True:
        data = proc.stdout.read(8820)
        if not data:
            break
        data = carry + data
        usable = len(data) - (len(data) % 2)
        carry = data[usable:]
        if usable:
            got_audio = True
            yield np.frombuffer(data[:usable], dtype=np.int16)
    proc.wait(timeout=10)
    if feed_error and not got_audio:
        raise feed_error[0]


def _stream_cartesia(text: str, timeout: float):
    """Cartesia -> int16 PCM at CARTESIA_RATE, no decode step.

    Unlike ElevenLabs, we ask for raw pcm_s16le at the exact sample rate
    we play back, so the response body IS the PCM stream — stream the
    HTTP response straight into int16 chunks, no ffmpeg subprocess."""
    import httpx

    ca = CFG["cartesia"]
    key = _get_cartesia_key()
    body = {
        "model_id": ca["model"],
        "transcript": text,
        "voice": {"id": ca["voice_id"]},
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": CARTESIA_RATE,
        },
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Cartesia-Version": "2026-08-14",
    }
    carry = b""
    with httpx.stream("POST", "https://api.cartesia.ai/tts/bytes",
                      headers=headers, json=body, timeout=timeout) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes(chunk_size=4096):
            data = carry + chunk
            usable = len(data) - (len(data) % 2)
            carry = data[usable:]
            if usable:
                yield np.frombuffer(data[:usable], dtype=np.int16)


_el_key_cache: str | None = None
_ca_key_cache: str | None = None


def _el_key_slot() -> str:
    """The credential-store entry name, so someone who already keeps a key
    under their own name points at it instead of storing a second copy."""
    return str(CFG["elevenlabs"].get("key_slot") or "backtalk-elevenlabs")


def _ca_key_slot() -> str:
    return str(CFG["cartesia"].get("key_slot") or "backtalk-cartesia")


def _get_elevenlabs_key() -> str:
    """The API key, from the most secure store available — NEVER from a
    file in this repo. Lookup order:
      1. macOS Keychain, item `backtalk-elevenlabs` by default (change it
         with elevenlabs.key_slot) — seed it once with:
         security add-generic-password -a "$USER" -s backtalk-elevenlabs -T /usr/bin/security -w
         (it prompts for the secret; -T lets this code read it without a
         GUI prompt every launch)
      2. Linux secret-tool (libsecret):
         secret-tool store --label backtalk service backtalk-elevenlabs
      3. the ELEVENLABS_API_KEY environment variable — the last-resort
         fallback, and the only option on Windows for now. Know the
         tradeoff: an export line in a shell profile is a plaintext key
         on disk, which is exactly what the keychain path avoids."""
    global _el_key_cache
    if _el_key_cache is not None:
        return _el_key_cache
    import subprocess
    key = ""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["security", "find-generic-password",
                                "-s", _el_key_slot(), "-w"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                key = r.stdout.strip()
        elif sys.platform.startswith("linux"):
            from shutil import which
            if which("secret-tool"):
                r = subprocess.run(["secret-tool", "lookup", "service",
                                    _el_key_slot()],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    key = r.stdout.strip()
    except Exception:
        pass
    _el_key_cache = key or os.environ.get("ELEVENLABS_API_KEY", "")
    return _el_key_cache


def _elevenlabs_ready() -> bool:
    el = CFG["elevenlabs"]
    return bool(el.get("enabled") and el.get("voice_id")
                and _get_elevenlabs_key())


def _get_cartesia_key() -> str:
    """The API key, from the most secure store available — NEVER from a
    file in this repo. Same lookup order as _get_elevenlabs_key:
      1. macOS Keychain, item `backtalk-cartesia` by default (change it
         with cartesia.key_slot) — seed it once with:
         security add-generic-password -a "$USER" -s backtalk-cartesia -T /usr/bin/security -w
      2. Linux secret-tool (libsecret):
         secret-tool store --label backtalk service backtalk-cartesia
      3. the CARTESIA_API_KEY environment variable — last-resort
         fallback, same tradeoff as the ElevenLabs one: a plaintext key
         on disk instead of in the keychain."""
    global _ca_key_cache
    if _ca_key_cache is not None:
        return _ca_key_cache
    import subprocess
    key = ""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["security", "find-generic-password",
                                "-s", _ca_key_slot(), "-w"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                key = r.stdout.strip()
        elif sys.platform.startswith("linux"):
            from shutil import which
            if which("secret-tool"):
                r = subprocess.run(["secret-tool", "lookup", "service",
                                    _ca_key_slot()],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    key = r.stdout.strip()
    except Exception:
        pass
    _ca_key_cache = key or os.environ.get("CARTESIA_API_KEY", "")
    return _ca_key_cache


def _cartesia_ready() -> bool:
    ca = CFG["cartesia"]
    return bool(ca.get("enabled") and ca.get("voice_id")
                and _get_cartesia_key())


def _chatterbox_server_healthy(base_url: str) -> bool:
    import httpx
    try:
        return httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200
    except Exception:
        return False


def _ensure_chatterbox():
    """Lazy-start Chatterbox's local HTTP server -- its own process, in
    its own venv (chatterbox-tts/.venv), never imported into this one.
    Chatterbox pins torch==2.6.0; this venv runs 2.13.0+cu130, and
    mixing them risks exactly the transitive dependency regression that
    already happened once (see backtalk.md's setuptools incident) and
    again while THIS install was being set up (see the vault's
    Chatterbox TTS Engine note). Same arm's-length shape as
    _ensure_pocket, just with a hand-written server (Chatterbox ships
    no CLI/serve command the way pocket-tts does) — see
    chatterbox-tts/chatterbox_server.py.

    Replaces CSM as of 2026-09-03: CSM ran in-process (imported straight
    into this venv) rather than arm's-length, which is exactly the
    pattern this avoids. Retired outright per Des's call, not left
    parked.

    Swapped from Chatterbox-Turbo to the full model, same day, after a
    live head-to-head: VRAM difference measured negligible (~3.3GB vs
    ~3.4GB), but the full model exposes exaggeration/cfg_weight for
    tuning, which Turbo silently ignores. exaggeration is baked into
    voice conditioning at server startup (a restart is needed to change
    it); cfg_weight applies per-call so it's cheaper to retune, though
    this server still bakes it in at launch for simplicity — see
    chatterbox_server.py."""
    import subprocess
    import time
    from pathlib import Path

    global _chatterbox_proc
    with _chatterbox_lock:
        cfg = CFG["chatterbox"]
        repo_root = Path(__file__).resolve().parents[1]
        venv_python = cfg.get("python") or str(
            repo_root.parent / "chatterbox-tts" / ".venv" / "bin" / "python")
        server_script = cfg.get("server_script") or str(
            repo_root.parent / "chatterbox-tts" / "chatterbox_server.py")
        if not Path(venv_python).exists():
            raise RuntimeError(
                f"chatterbox venv python not found at {venv_python} — "
                f"set up ~/my-agent/chatterbox-tts/.venv or set chatterbox.python")

        ref_path = cfg.get("reference_audio") or ""
        if not ref_path:
            raise RuntimeError("chatterbox.reference_audio not set")

        base_url = cfg["url"].rstrip("/")
        if not _chatterbox_server_healthy(base_url):
            port = base_url.rsplit(":", 1)[-1]
            log(f"[mouth] starting chatterbox server on port {port}...")
            log_path = repo_root / "logs" / "chatterbox.log"
            log_path.parent.mkdir(exist_ok=True)
            # Read fresh off disk, not from the in-memory CFG global --
            # CFG is loaded once at process start and _write_config_key
            # is the only thing that ever mutates it live, which these
            # two keys never go through (there's no console verb for
            # them). Without this, tuning exaggeration/cfg_weight in
            # backtalk.json would silently do nothing until the whole
            # backtalk process restarts, defeating the point of tuning
            # without a restart. The subprocess itself already relaunches
            # fresh on every call here, so this just makes sure it
            # launches with the CURRENT values, not stale ones frozen at
            # import time.
            try:
                import json as _json
                from backtalk.config import CONFIG_PATH as _CONFIG_PATH
                _live_cfg = _json.loads(_CONFIG_PATH.read_text()).get("chatterbox", {})
            except Exception:
                _live_cfg = {}
            exaggeration = str(_live_cfg.get("exaggeration", cfg.get("exaggeration", 0.5)))
            cfg_weight = str(_live_cfg.get("cfg_weight", cfg.get("cfg_weight", 0.5)))
            with open(log_path, "ab") as logf:
                _chatterbox_proc = subprocess.Popen(
                    [venv_python, server_script, "--port", port, "--voice", ref_path,
                     "--exaggeration", exaggeration, "--cfg-weight", cfg_weight],
                    stdout=logf, stderr=subprocess.STDOUT)
            # Longer budget than pocket's 30s -- first-ever launch also
            # downloads model weights, not just loads them from disk.
            deadline = time.time() + 60
            while time.time() < deadline:
                if _chatterbox_server_healthy(base_url):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    f"chatterbox server did not become healthy within 60s "
                    f"(see {log_path})")
            log("[mouth] chatterbox ready")


def _shutdown_chatterbox():
    """Tear down what _ensure_chatterbox started, so backtalk exiting
    doesn't leave an orphaned chatterbox server (and its GPU memory)
    behind -- same reasoning as _shutdown_pocket."""
    import subprocess
    global _chatterbox_proc
    with _chatterbox_lock:
        if _chatterbox_proc is not None:
            _chatterbox_proc.terminate()
            try:
                _chatterbox_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _chatterbox_proc.kill()
            _chatterbox_proc = None


def _stream_chatterbox(text: str):
    """Chatterbox -> int16 PCM at CHATTERBOX_RATE. Blocks until the
    whole sentence is rendered server-side then returns it as one
    chunk -- the library exposes no token-streaming generate() the way
    Pocket TTS does, so there's no incremental-release benefit to
    chase here, unlike _stream_pocket. Same WAV-response parsing
    (skip past the "data" subchunk marker) either way.

    voice resolves fresh from CFG on every call, same pattern
    _stream_pocket already uses for CFG["pocket"]["voice"] -- a
    character switch takes effect on the very next sentence, no
    restart. The server caches each character's conditioning after
    first use (see chatterbox_server.py's _conds_for)."""
    import httpx

    cfg = CFG["chatterbox"]
    _ensure_chatterbox()
    base_url = cfg["url"].rstrip("/")
    voice = cfg.get("voice", "samantha")
    r = httpx.post(f"{base_url}/tts", data={"text": text, "voice": voice}, timeout=60.0)
    r.raise_for_status()
    data = r.content
    idx = data.find(b"data")
    if idx == -1 or len(data) < idx + 8:
        raise RuntimeError("chatterbox response missing WAV data chunk")
    pcm = data[idx + 8:]
    usable = len(pcm) - (len(pcm) % 2)
    if usable:
        yield np.frombuffer(pcm[:usable], dtype=np.int16)


def _pocket_server_healthy(base_url: str) -> bool:
    import httpx
    try:
        return httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200
    except Exception:
        return False


def _ensure_pocket():
    """Lazy-start Pocket TTS (kyutai-labs/pocket-tts) and the cloned
    voice it needs. Two things this owns and must also tear down (see
    _shutdown_pocket, called from Mouth.shutdown): the `pocket-tts
    serve` subprocess itself, and a tiny localhost-only static file
    server that exists ONLY so pocket-tts's voice_url can fetch the
    exported voice — its HTTP API takes a URL (http://, https://, or
    hf://) or a raw file upload, never a bare local path (confirmed
    against the live server's /openapi.json, since the docs don't spell
    the API out). Uploading the raw reference audio on every call works
    too, but re-embeds the voice from scratch each time — measured at
    ~4s/sentence, vs ~1.4s/sentence once exported to .safetensors and
    served locally by URL, which is why this exists instead of a
    simpler one-shot upload.

    Deliberately NOT a dependency of backtalk's own venv: pocket-tts
    pulls its own (CPU-only) torch, and mixing that into backtalk's
    GPU-torch venv risks the exact transitive setuptools/CUDA
    regression the CSM install caused once already (see backtalk.md,
    "installing ML deps silently broke the real entrypoint"). It runs
    from its own separate venv as a separate process instead, reached
    only over HTTP — same arm's-length relationship Cartesia/ElevenLabs
    have, just local instead of cloud."""
    import functools
    import subprocess
    import time
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    global _pocket_proc, _pocket_file_httpd
    with _pocket_lock:
        cfg = CFG["pocket"]
        repo_root = Path(__file__).resolve().parents[1]
        voices_dir = repo_root / "voices"
        voices_dir.mkdir(exist_ok=True)
        voice = cfg["voice"]
        safet = voices_dir / f"{voice}.safetensors"
        pocket_bin = cfg.get("bin") or str(
            repo_root.parent / "pocket-tts" / ".venv" / "bin" / "pocket-tts")
        if not Path(pocket_bin).exists():
            raise RuntimeError(
                f"pocket-tts binary not found at {pocket_bin} — install it "
                f"(see backtalk.md) or set pocket.bin to its real path")

        if not safet.exists():
            ref = cfg.get("reference_audio") or ""
            if not ref:
                raise RuntimeError(
                    f"pocket.reference_audio not set and {safet.name} "
                    f"hasn't been exported yet")
            wav = voices_dir / f"{voice}-reference.wav"
            log(f"[mouth] exporting Pocket TTS voice '{voice}' from {ref}...")
            # pocket-tts needs soundfile to read anything but plain WAV
            # (confirmed live: raw .mp3 upload 500s with
            # "ImportError: soundfile is required...") — converting once
            # here avoids adding that dependency to either venv.
            subprocess.run(
                ["ffmpeg", "-y", "-i", ref, "-ar", "24000", "-ac", "1", str(wav)],
                check=True, capture_output=True)
            subprocess.run([pocket_bin, "export-voice", str(wav), str(safet)],
                           check=True, capture_output=True)
            log(f"[mouth] Pocket TTS voice '{voice}' exported to {safet.name}")

        base_url = cfg["url"].rstrip("/")
        if not _pocket_server_healthy(base_url):
            port = base_url.rsplit(":", 1)[-1]
            log(f"[mouth] starting pocket-tts serve on port {port}...")
            log_path = repo_root / "logs" / "pocket-tts.log"
            log_path.parent.mkdir(exist_ok=True)
            with open(log_path, "ab") as logf:
                _pocket_proc = subprocess.Popen(
                    [pocket_bin, "serve", "--port", port],
                    stdout=logf, stderr=subprocess.STDOUT)
            deadline = time.time() + 30
            while time.time() < deadline:
                if _pocket_server_healthy(base_url):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    f"pocket-tts serve did not become healthy within 30s "
                    f"(see {log_path})")
            log("[mouth] pocket-tts ready")

        if _pocket_file_httpd is None:
            handler = functools.partial(SimpleHTTPRequestHandler, directory=str(voices_dir))
            _pocket_file_httpd = ThreadingHTTPServer(
                ("127.0.0.1", int(cfg["file_port"])), handler)
            threading.Thread(target=_pocket_file_httpd.serve_forever, daemon=True).start()
            log(f"[mouth] pocket voice file server up on 127.0.0.1:{cfg['file_port']}")


def _shutdown_pocket():
    """Tear down what _ensure_pocket started, so backtalk exiting
    doesn't leave an orphaned pocket-tts server or file thread behind —
    the same class of leak the CSM shutdown fix closed for the GPU and
    the instance-port lock (2026-09-01)."""
    import subprocess
    global _pocket_proc, _pocket_file_httpd
    with _pocket_lock:
        if _pocket_file_httpd is not None:
            _pocket_file_httpd.shutdown()
            _pocket_file_httpd = None
        if _pocket_proc is not None:
            _pocket_proc.terminate()
            try:
                _pocket_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _pocket_proc.kill()
            _pocket_proc = None


def _stream_pocket(text: str):
    """Pocket TTS -> int16 PCM at POCKET_RATE.

    Its /tts response is a real WAV (confirmed against the live
    server, not the docs, which don't specify this), sent over chunked
    transfer. Skip the RIFF/fmt header once by scanning for the "data"
    subchunk marker, then it's raw s16le PCM same as Cartesia's
    response — the rest of this mirrors _stream_cartesia exactly."""
    import httpx

    cfg = CFG["pocket"]
    _ensure_pocket()
    base_url = cfg["url"].rstrip("/")
    voice_url = f"http://127.0.0.1:{cfg['file_port']}/{cfg['voice']}.safetensors"
    header_buf = b""
    past_header = False
    carry = b""
    with httpx.stream("POST", f"{base_url}/tts",
                      data={"text": text, "voice_url": voice_url},
                      timeout=30.0) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes(chunk_size=4096):
            if not past_header:
                header_buf += chunk
                idx = header_buf.find(b"data")
                if idx == -1 or len(header_buf) < idx + 8:
                    continue
                chunk = header_buf[idx + 8:]  # past "data" + its 4-byte size field
                past_header = True
                if not chunk:
                    continue
            data = carry + chunk
            usable = len(data) - (len(data) % 2)
            carry = data[usable:]
            if usable:
                yield np.frombuffer(data[:usable], dtype=np.int16)


def synth_stream(text: str, timeout: float = 30.0):
    """One sentence -> yields (sample_rate, pcm_chunk) as the TTS
    renders. voice_mode is the explicit three-way switch (the voice
    console's "switch to cartesia/pocket/chatterbox voice"): "cartesia"
    (the default) tries Cartesia then ElevenLabs; "pocket" tries Pocket
    TTS only; "chatterbox" tries Chatterbox only — no crossover between
    the three, picking one means getting that one. Kokoro is always the
    silent final fallback, on ANY failure in any mode (or if the
    selected engine's config block is disabled). Degrade, never mute."""
    mode = CFG.get("voice_mode", "cartesia")
    if mode == "pocket":
        if CFG.get("pocket", {}).get("enabled"):
            try:
                for pcm in _stream_pocket(text):
                    yield POCKET_RATE, pcm
                return
            except Exception as e:
                log(f"[mouth] pocket failed ({str(e)[:60]}) — "
                    f"falling back to {CFG['voice']}")
        else:
            log("[mouth] pocket selected but disabled — "
                f"falling back to {CFG['voice']}")
    elif mode == "chatterbox":
        if CFG.get("chatterbox", {}).get("enabled"):
            try:
                for pcm in _stream_chatterbox(text):
                    yield CHATTERBOX_RATE, pcm
                return
            except Exception as e:
                log(f"[mouth] chatterbox failed ({str(e)[:60]}) — "
                    f"falling back to {CFG['voice']}")
        else:
            log("[mouth] chatterbox selected but disabled — "
                f"falling back to {CFG['voice']}")
    else:
        if _cartesia_ready():
            try:
                for pcm in _stream_cartesia(text, timeout):
                    yield CARTESIA_RATE, pcm
                return
            except Exception as e:
                log(f"[mouth] cartesia failed ({str(e)[:60]}) — "
                    f"falling back to {CFG['voice']}")
        if _elevenlabs_ready():
            try:
                for pcm in _stream_elevenlabs(text, timeout):
                    yield EL_RATE, pcm
                return
            except Exception as e:
                log(f"[mouth] elevenlabs failed ({str(e)[:60]}) — "
                    f"falling back to {CFG['voice']}")
    for pcm in _stream_kokoro(text):
        yield KOKORO_RATE, pcm


class Mouth:
    def __init__(self):
        from backtalk.ducking import Ducker
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._speaking = threading.Event()
        # The one persistent output stream (audio law #1).
        # Worker-thread-only — never touch from other threads.
        self._out: sd.OutputStream | None = None
        self._out_rate: int | None = None
        self.ducker = Ducker()  # public: PTT ducks for the USER's voice too
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    def say(self, text: str):
        """Queue text (split to sentences) for speech."""
        for s in split_sentences(text):
            self._q.put((s, None))

    def say_chunk(self, text: str, directions=None):
        """Queue text as ONE TTS request, no sentence splitting — fuller
        chunks get livelier prosody (single short sentences come out
        dull).

        `directions` are the stage directions this chunk carried. They are
        published on the signal bus when this chunk's audio STARTS, which
        is why they travel with it instead of firing at parse time."""
        text = text.strip()
        if text:
            self._q.put((text, directions or None))

    def shut_up(self):
        """Barge-in: stop current playback and flush everything queued."""
        self._stop.set()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def shutdown(self):
        """Exit path: stop playback and restore the music SYNCHRONOUSLY
        (the debounced restore timer dies with the process otherwise)."""
        self.shut_up()
        self.ducker.restore_now()
        _shutdown_pocket()
        _shutdown_chatterbox()

    def wait_done(self, timeout: float | None = None):
        """Block until the queue is drained and playback finished."""
        import time
        deadline = None if timeout is None else time.time() + timeout
        while (not self._q.empty()) or self._speaking.is_set():
            time.sleep(0.05)
            if deadline and time.time() > deadline:
                return

    def _run(self):
        from backtalk import signals
        while True:
            item = self._q.get()
            sentence, directions = item if isinstance(item, tuple) else (item, None)
            if not sentence:
                continue
            self._stop.clear()
            self._speaking.set()
            self.ducker.speech_start()
            signals.static_stop()     # thinking sound dies when speech starts
            signals.set_state("speaking")
            try:
                self._play_stream(sentence, directions)
            except Exception as e:
                log(f"[mouth] synth/play error: {e}")
            finally:
                if self._q.empty():
                    self._speaking.clear()
                    # The reply has genuinely stopped talking, as opposed to
                    # the gap between two sentences of the same reply.
                    signals.reply_done()
                    self.ducker.speech_end()
                    signals.set_state("idle")

    def _get_out(self, rate: int) -> sd.OutputStream:
        """The long-lived stream (audio law #1). Reopened only when the
        sample rate changes (ElevenLabs 44.1k <-> Kokoro 24k fallback:
        rare, costs at most one blip on the switch)."""
        if self._out is not None and self._out_rate == rate:
            # Guarded, because the stream can die UNDER us: the ears
            # rebuild the whole audio system to recover from a device
            # change (see ears._reopen_after_device_change), and that
            # closes every open stream including this one. Touching a
            # dead stream raises rather than returning False, so the
            # check has to be the try, not an `if`. Falling through
            # rebuilds it, which is what the rest of this method does.
            try:
                if not self._out.active:
                    self._out.start()
                return self._out
            except Exception:
                log("[mouth] the output stream went away, reopening")
        self._drop_out()
        self._out = sd.OutputStream(samplerate=rate, channels=1, dtype="int16")
        self._out_rate = rate
        self._out.start()
        return self._out

    def _cut(self):
        """Barge-in cut: stop feeding audio and pad the line with a beat
        of silence — the stream itself NEVER stops (an abort+restart here
        re-triggers the onset blip on latch-happy audio setups). Cost:
        the device buffer (~0.1s) plays out after the kill order — half a
        syllable of tail."""
        try:
            zeros = np.zeros(2205, dtype=np.int16)
            for _ in range(3):
                self._out.write(zeros)
        except Exception:
            self._drop_out()

    def _drop_out(self):
        """Close and forget the stream — the next sentence reopens
        fresh. The self-heal path for device errors (interface
        unplugged, audio mixer restarted)."""
        if self._out is not None:
            try:
                self._out.close(ignore_errors=True)
            except Exception:
                pass
        self._out = None
        self._out_rate = None

    def _play_stream(self, sentence: str, directions=None, block: int = 2205,
                     prebuffer_s: float = 0.75):
        """Stream-synthesize and play with the head-start buffer (audio
        law #2). stop() reacts ~50ms. The sample rate comes from
        whichever engine actually answered."""
        from backtalk import signals
        gen = synth_stream(sentence)
        head: list = []
        banked = 0
        rate = None
        for rate_, pcm in gen:
            rate = rate_
            head.append(pcm)
            banked += len(pcm)
            if banked >= int(rate * prebuffer_s):
                break
        if rate is None:
            return
        try:
            out = self._get_out(rate)
            # AUDIO STARTS HERE: the head buffer is full and the first write
            # is next. Publishing now is what puts a screen cue on the spoken
            # word rather than seconds ahead of it.
            if directions:
                from backtalk import signals as _sig
                _sig.direction(directions)

            def _write(pcm):
                for i in range(0, len(pcm), block):
                    if self._stop.is_set():
                        return False
                    chunk = _apply_gain(pcm[i:i + block])
                    out.write(chunk)
                    # Re-check after the blocking write: a barge-in
                    # landing mid-block must not let feed_waveform
                    # re-assert "speaking" over a fresh "listening".
                    if self._stop.is_set():
                        return False
                    signals.feed_waveform(chunk)
                return True
            for pcm in head:
                if not _write(pcm):
                    self._cut()
                    return
            for _, pcm in gen:
                if not _write(pcm):
                    self._cut()
                    return
        except Exception:
            self._drop_out()
            raise


if __name__ == "__main__":
    m = Mouth()
    m.say(sys.argv[1] if len(sys.argv) > 1 else
          "Voice check. The mouth is alive, and it is very good to be heard.")
    m.wait_done(timeout=60)
    m.shutdown()  # else a pocket-tts (or future subprocess-backed engine) test run orphans it
