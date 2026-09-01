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
directly in the format we ask for. A third, local-GPU option, CSM
(sesame/csm-1b via HuggingFace Transformers), sits below both cloud
engines and above Kokoro — no API key, no network per request, but
needs CUDA and a one-time `hf auth login` to accept the gated model.

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
CSM_RATE = 24000
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_pipe = None
_pipe_lock = threading.Lock()

_csm_model = None
_csm_processor = None
_csm_reference = None      # cached voice-anchor conversation turn, or None
_csm_lock = threading.Lock()


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


def _ensure_csm():
    """Lazy-load CSM (sesame/csm-1b) and its optional voice-anchor
    reference — first call downloads the model from HF (gated; needs
    `hf auth login` once) and puts it on GPU if available. Loaded under
    a lock so concurrent sentences don't race the load."""
    global _csm_model, _csm_processor, _csm_reference
    with _csm_lock:
        if _csm_model is None:
            import torch
            from transformers import AutoProcessor, CsmForConditionalGeneration
            device = "cuda" if torch.cuda.is_available() else "cpu"
            log(f"[mouth] loading CSM (device {device})...")
            _csm_processor = AutoProcessor.from_pretrained("sesame/csm-1b")
            _csm_model = CsmForConditionalGeneration.from_pretrained(
                "sesame/csm-1b", device_map=device)
            log("[mouth] CSM ready")
            ref_path = CFG["csm"].get("reference_audio") or ""
            if ref_path:
                _csm_reference = {
                    "role": str(CFG["csm"]["speaker"]),
                    "content": [
                        {"type": "text",
                         "text": CFG["csm"].get("reference_text") or ""},
                        {"type": "audio", "path": ref_path},
                    ],
                }
                log(f"[mouth] CSM voice reference loaded from {ref_path}")
    return _csm_model, _csm_processor


def _stream_csm(text: str):
    """CSM -> int16 PCM chunks at CSM_RATE, released as generation goes
    instead of waiting for the whole sentence. Raises on any failure;
    synth_stream's except clause is what falls back to Kokoro, same as
    the other engines.

    CSM's generate() calls streamer.put() once per ~80ms audio FRAME
    (codebook tokens), from inside its own sampling loop -- see
    transformers.models.csm.generation_csm.CsmGenerationMixin._sample.
    That's the real hook. Turning frames into audio incrementally is
    the part transformers doesn't give us for free: Mimi's decoder
    TRANSFORMER supports genuine incremental attention caching across
    decode() calls, but its final conv upsampling stack has no
    cross-call state at all -- every decode() call runs those causal
    convolutions as if preceded by silence. _CsmAudioStreamer below
    works around that the standard way for a codec with no exposed
    conv cache: each chunk re-decodes a small window of already-
    decoded trailing context plus the new frames, and keeps only the
    newly-decoded tail -- the context's own output is recomputed only
    to prime the convolutions with real history, then thrown away."""
    import queue as _queue
    import threading as _threading

    import torch
    from transformers.generation.streamers import BaseStreamer

    cfg = CFG["csm"]
    model, processor = _ensure_csm()
    speaker = str(cfg["speaker"])
    conversation = []
    if _csm_reference is not None:
        conversation.append(_csm_reference)
    conversation.append({"role": speaker, "content": [{"type": "text", "text": text}]})
    inputs = processor.apply_chat_template(
        conversation, tokenize=True, return_dict=True).to(model.device)
    # Mimi codec runs at 12.5Hz -> 80ms/token.
    max_new_tokens = max(1, int(cfg["max_audio_length_ms"]) // 80)

    class _CsmAudioStreamer(BaseStreamer):
        CHUNK_FRAMES = 8      # ~640ms of new audio released per chunk
        CONTEXT_FRAMES = 4    # trailing frames re-fed to prime the conv decoder

        def __init__(self, codec_model, eos_id, num_codebooks):
            self.codec_model = codec_model
            self.eos_id = eos_id
            self.num_codebooks = num_codebooks
            self.queue: _queue.Queue = _queue.Queue()
            self._frames: list = []
            self._decoded_through = 0

        def put(self, value):
            # generate()'s base machinery also calls streamer.put(input_ids)
            # once for the whole prompt before our per-frame pushes ever
            # start (transformers/generation/utils.py, the plain prompt
            # echo every streamer gets) -- that push is shaped (batch,
            # prompt_len), nothing like a single codebook frame, so it has
            # to be filtered out here rather than assumed away.
            if value.dim() != 2 or value.shape[-1] != self.num_codebooks:
                return
            frame = value[0]      # batch size is always 1 here
            if bool((frame == self.eos_id).all()):
                return             # the EOS frame itself carries no audio
            self._frames.append(frame)
            if len(self._frames) - self._decoded_through >= self.CHUNK_FRAMES:
                self._flush()

        def end(self):
            self._flush()
            self.queue.put(None)

        def _flush(self):
            new_count = len(self._frames) - self._decoded_through
            if new_count <= 0:
                return
            ctx_start = max(0, self._decoded_through - self.CONTEXT_FRAMES)
            window = self._frames[ctx_start:]
            # generate()'s own loop pushes streamer.put(next_tokens.cpu())
            # (see the docstring above), so window's frames are CPU
            # tensors -- move back to the codec's device before decoding.
            codes = torch.stack(window, dim=0).transpose(0, 1).unsqueeze(0).to(model.device)
            with torch.no_grad():
                out = self.codec_model.decode(audio_codes=codes, return_dict=True)
            audio = out.audio_values[0, 0]
            per_frame = audio.shape[-1] / len(window)
            drop = round((self._decoded_through - ctx_start) * per_frame)
            new_audio = audio[drop:]
            self._decoded_through = len(self._frames)
            if new_audio.numel():
                self.queue.put(new_audio.detach().cpu().numpy())

    streamer = _CsmAudioStreamer(model.codec_model, model.config.codebook_eos_token_id,
                                  model.config.num_codebooks)
    errors: list = []

    def _run():
        try:
            with torch.no_grad():
                model.generate(
                    **inputs,
                    output_audio=False,   # we decode incrementally ourselves
                    max_new_tokens=max_new_tokens,
                    do_sample=True,
                    temperature=float(cfg["temperature"]),
                    top_k=int(cfg["topk"]),
                    streamer=streamer,
                )
            # success: generate() already called streamer.end() for us
        except Exception as e:
            errors.append(e)
            streamer.queue.put(None)   # unblock the consumer below
        finally:
            torch.cuda.empty_cache()  # release fragmented cache; VRAM margin here is thin

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        while True:
            chunk = streamer.queue.get()
            if chunk is None:
                break
            audio_array = np.clip(chunk, -1.0, 1.0)
            yield (audio_array * 32767).astype(np.int16)
    finally:
        t.join()
    if errors:
        raise errors[0]


def synth_stream(text: str, timeout: float = 30.0):
    """One sentence -> yields (sample_rate, pcm_chunk) as the TTS
    renders. voice_mode is the explicit switch (the voice console's
    "switch to local/cloud voice"): "cloud" (the default) tries
    Cartesia then ElevenLabs; "local" tries CSM instead, skipping both
    cloud engines entirely. Kokoro is always the final fallback, on
    ANY failure in either mode. Degrade, never mute."""
    if CFG.get("voice_mode", "cloud") == "local":
        if CFG.get("csm", {}).get("enabled"):
            try:
                for pcm in _stream_csm(text):
                    yield CSM_RATE, pcm
                return
            except Exception as e:
                log(f"[mouth] csm failed ({str(e)[:60]}) — "
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
                    out.write(pcm[i:i + block])
                    # Re-check after the blocking write: a barge-in
                    # landing mid-block must not let feed_waveform
                    # re-assert "speaking" over a fresh "listening".
                    if self._stop.is_set():
                        return False
                    signals.feed_waveform(pcm[i:i + block])
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
