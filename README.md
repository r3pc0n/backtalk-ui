# backtalk-ui

> **Aimed mainly at people already running [Jared Rhodenizer](https://jaredrhod.com)'s full stack** ([fullstack-agent](https://github.com/jaredrhod/fullstack-agent): memory, voice, face, hands) who want a different voice interface than stock backtalk — an explicit Local/Cloud engine choice instead of a fixed pair, config-driven custom voice characters instead of hardcoded ones, and a bundled theme picker for the transcript UI. Worth being upfront about what this actually is: under the hood it's the **complete, independent backtalk application**, not a small patch — full credit and thanks to Jared for the original, and it stays AGPL-3.0-or-later, same as his. See **Install** below for how to swap it in without redoing your existing setup, and **Credits**/**License** for the rest.

**Runs on:** Claude Code only; the voice is built on Claude's agent SDK. The $20 Pro plan is enough.

Talk to your Claude Code agent out loud. Hold a key, say the thing, and it answers through your speakers in a real voice about a second later, with all its tools, your project context, and its own personality.

The hearing and the built-in voice run local: free, offline models on your machine, no voice API keys required, no per-word costs. The brain is the Claude Code you already have — talking works like any other session and uses your plan's usage, nothing extra to buy.

## What it does

- **Hold a key, talk, release.** Your words are transcribed locally and handed to a live Claude Code session. The reply is spoken sentence by sentence as it's generated, with first audio in about 1 to 2 seconds on warm turns. Prefer no button at all? **Hands-free listening** is one spoken sentence away ("go hands free"), and the key keeps working there as your interrupt.
- **It's YOUR agent talking.** The session runs in the folder whose CLAUDE.md defines your assistant: same name, same personality, same memory as your terminal sessions. backtalk-ui has no personality of its own; it's a mouth and ears for whoever you already have.
- **Interrupt it.** Press the key while it's talking and it shuts up and listens. No headphones needed, because the mic only opens while you hold the key, so it never hears the speakers.
- **Type instead whenever you want.** Typing in the terminal, or in the transcript web page (see below), is the same conversation, and the reply is still spoken.
- **It asks before it acts, in plain words.** When your agent wants to do something real, it asks out loud the way a person would and waits. An exact spoken yes approves; anything else denies, with your words passed back as the reason. Prefer auto-approve? Say "stop asking for permission" and confirm.
- **The voice console.** Session control by voice, so you never go back to the keyboard: "clear the session", "compact the session", "switch to the deep model" / "back to the fast model", "set effort to low" (or medium, high, max), "usage report", "go hands free" / "push to talk mode", "stop asking for permission" / "start asking again", "switch to cloud voice" / "switch to local voice", "switch voice to `<name>`" for any character you've set up. Exact phrases, spoken alone.
- **A persistent transcript web page**, not a raw terminal: settings for model tier, reasoning effort, engine, theme, and volume, plus a hang-up button and a typed-input box. Themed with a bundled palette (see **Theming** below).
- **Music ducks while it speaks** (Spotify, macOS) and comes back up after.
- **It thinks out loud.** While the agent works, you hear a processing sound, so a pause never reads as a dead line. Silence it with `"thinking_sound": ""` in the config.

## Install

### Already running the full Jarvis stack (fullstack-agent)?

This swaps `backtalk-ui` in for stock backtalk, keeping your memory, face, and hands untouched, and keeping `fullstack-agent/update.sh`'s "update everything" working correctly. Hang up any active call first — do this with backtalk not running:

```
mv backtalk backtalk.old              # from your agent's home folder
git clone https://github.com/r3pc0n/backtalk-ui backtalk
cp backtalk.old/backtalk.json backtalk/backtalk.json
cd backtalk && ./install.sh
```

Then confirm it (all good, nothing to change if you cloned exactly as above):

```
git -C backtalk remote -v
```

Should show only `origin` → `backtalk-ui`. **Don't add an `upstream` remote pointing at Jared's original backtalk** if you ever set one up for reference — `fullstack-agent/update.sh` specifically prefers `upstream` over `origin` when both exist (it's built for tracking a *real* upstream project from a private backup fork), so an `upstream` remote here would make "update everything" quietly pull Jared's stock backtalk back over this fork's changes. Just `origin` is correct and exactly what a plain clone gives you.

Once you've confirmed it works, `rm -rf backtalk.old`.

### Starting fresh (no existing backtalk install)

```
git clone https://github.com/r3pc0n/backtalk-ui
cd backtalk-ui
./install.sh
```

Either way, the installer sets up a Python environment, the local speech-to-text and voice models, and the one system library they need. First run downloads the models (about 1 GB total); everything after is instant. Prerequisites: [Claude Code](https://claude.com/claude-code) with a Claude subscription, and `uv` (the installer offers to install it).

**The easy way to configure it:** open the folder in Claude Code and say *"read backtalk.md and set me up."*

**The manual way:** copy `backtalk.json.example` to `backtalk.json` (your copy is untracked, so updates never touch it), then edit it. Point `agent_dir` at the folder whose CLAUDE.md is your agent, set `name` to your agent's name, pick a `ptt_key`. Then:

```
./run.sh
```

Hold the key. Talk. Let go.

## Windows

Setup runs through the wizard instead of the shell scripts (`install.sh` and `run.sh` are Mac and Linux). Open this folder in Claude Code and say *"read backtalk.md and set me up"*: the wizard installs uv, espeak-ng, the environment, and the models natively, then launches with `uv run python -m backtalk.main`. The ElevenLabs key lives in the `ELEVENLABS_API_KEY` environment variable on Windows for now. Hit something rough? `TROUBLESHOOTING.md` carries the known quirks.

## The voice

**Zero-config default: Kokoro.** Local, offline, no accounts, no per-word costs, and honestly a bit computer-sounding. The default voice is `bm_lewis`, a British male with exactly the butler register. Around 60 voices ship free; set `"voice"` in `backtalk.json` (the first letter picks the language: `a` is American, `b` is British, and there are Spanish, French, Hindi, Italian, Japanese, Portuguese, and Chinese voices too). This works the moment you clone the repo — nothing to set up.

**Local vs. Cloud, an explicit pick, not a silent chain.** Say "switch to local voice" or "switch to cloud voice" (or use the Engine buttons in the transcript page). Whichever you're not on stays fully configured and ready — switching is instant, no restart.

- **Local**: Kokoro by default. Optionally upgrade to **Pocket TTS** ([kyutai-labs/pocket-tts](https://github.com/kyutai-labs/pocket-tts)) for a cloned voice instead of a preset — CPU-only, free, no GPU needed, but it's a separate install (its own venv, kept isolated from backtalk's own dependencies on purpose — see `mouth.py`'s `_ensure_pocket` for why). Set it up yourself:
  1. `pip install pocket-tts --extra-index-url https://download.pytorch.org/whl/cpu` into its own venv at `../pocket-tts/.venv` (a sibling folder to this repo).
  2. Set `pocket.enabled: true` and `pocket.reference_audio` to a short clip (5s+) of the voice you want cloned, in `backtalk.json`.
  3. Say "switch to local voice" — the first sentence takes a few extra seconds to export the clone, everything after is fast.
- **Cloud**: Cartesia (preferred if both are set up) then ElevenLabs, whichever you configure. Real cloud TTS quality, needs your own API key either way. Trying to switch to cloud before either is configured gets you a spoken reminder to come back here instead of silently failing.
  - **Cartesia**: sign up at [play.cartesia.ai](https://play.cartesia.ai), set `cartesia.enabled: true` and `cartesia.voice_id` in `backtalk.json`. Key goes in the OS keychain, never a file: macOS `security add-generic-password -a "$USER" -s backtalk-cartesia -T /usr/bin/security -w`, Linux `secret-tool store --label backtalk service backtalk-cartesia`, or the `CARTESIA_API_KEY` environment variable as a last resort.
  - **ElevenLabs**: set `elevenlabs.enabled: true` and `elevenlabs.voice_id`, needs `ffmpeg` installed. Same keychain pattern, service name `backtalk-elevenlabs` (or `ELEVENLABS_API_KEY`).

Kokoro is always the silent last-resort fallback in either mode — if your chosen engine fails or isn't set up, the voice degrades instead of going mute, and `logs/backtalk.log` records why.

### Custom voice characters

`CFG["voices"]` in `backtalk.json` is empty by default — this is config, not code, so adding a character doesn't mean editing Python:

```json
"voices": {
  "aria": {
    "cartesia_voice_id": "your-cartesia-voice-id-here",
    "label": "Aria"
  }
}
```

Both keys are optional — `label` defaults to the name capitalized, and `cartesia_voice_id` only matters if you've cloned that character on Cartesia. Once it's in `backtalk.json`, "switch voice to aria" works immediately by voice or typed command, no restart.

Pocket TTS needs no entry in `voices` at all — it clones whichever name you set `pocket.voice` to, straight from a `voices/<name>.safetensors` file (exported automatically from `pocket.reference_audio` the first time it's needed). Give a name both a safetensors file *and* a `voices` entry with a `cartesia_voice_id`, and switching characters moves both engines together, whichever one is actually live.

## Theming

The transcript page ships with **Solitude** as its default theme — picked deliberately as the most neutral of the bundled set, not a personal favorite. Open the settings panel (the hamburger icon) and use the **Theme** dropdown to pick from 28 more, all bundled, no network request: Atelier, Batou, Bauhaus, Catppuccin (and Latte), Ethereal, Everforest, Flexoki Light, Greek Noir, Gruvbox (and Material), Hackerman, Kanagawa, Last Horizon, Lumon, Lupine, Matrix, Matte Black, Mechanoonna, Miasma, Nord, Osaka Jade, Retro 82, Ristretto, Rosé Pine, Tokyo Night, Vantablack, White. Your pick is saved per-browser (`localStorage`), not in `backtalk.json`.

This fork doesn't include any automatic desktop-theme sync (an earlier build had one wired to a specific personal setup — not something that would have worked for anyone else, so it was left out rather than shipped broken). If you want your transcript page to follow your actual desktop theme live, that's a build-it-yourself project for now.

## Give it a face (optional)

backtalk-ui writes tiny state files while it listens, thinks, and speaks, so anything can watch them and react in real time.

- **[ai-visualizer](https://github.com/jaredrhod/ai-visualizer)** is a matching face: full-screen visualizers that perform your actual conversation. Point its `bus_dir` at this folder (or set `signals_dir` here to its folder).
- **[barehands](https://github.com/jaredrhod/barehands)**: point `barehands_state_dir` at its `state/` folder and the on-screen ring becomes your agent's face.

Both are Jared's own projects, unrelated to what changed in this fork — they work the same way here as in the original.

## The fine print that matters

- **Usage:** every spoken turn is a real Claude Code turn, so a long voice session uses your plan the same way a long typing session does.
- **Permissions: ask first, auto-approve by choice.** The default is `"ask"`: gated actions get a spoken permission check, silence for about 75 seconds means no. `"bypassPermissions"` is auto-approve. Say "stop asking for permission" / "start asking again" for a flip that saves itself.
- **Two microphone modes.** Push to talk (the default): the mic is closed except while you hold the key. Hands-free listening: always listening with voice detection, "go hands free" / "push to talk mode" switches live.
- **The talk key works on native Wayland, not just X11.** `backtalk/ptt.py` reads keyboard events straight from `/dev/input` (evdev) on Linux, which works identically under X11 and native Wayland compositors (Hyprland, Sway, GNOME Wayland) — needs membership in the `input` group, no root, and never grabs the key (it still reaches whatever window has focus too). Falls back to `pynput` automatically on macOS, Windows, or if evdev can't find a usable device. Read-only either way: it compares each event against the one key you configured and discards the rest, stores nothing, writes nothing.
- **Pin the microphone if you wear a headset.** Set `"mic_device"` in `backtalk.json` to the input you want, by name, so the mic doesn't jump to a headset's narrowband profile the moment one connects.
- Something misbehaving? `TROUBLESHOOTING.md` covers the classics, and `logs/backtalk.log` has the receipts.

## Credits

This fork's own changes aside, everything below is unchanged from the original and stays credited exactly as Jared wrote it:

Speech recognition by [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) running [OpenAI Whisper](https://github.com/openai/whisper) models (MIT). Voice by [Kokoro](https://github.com/hexgrad/kokoro) (Apache 2.0) with [espeak-ng](https://github.com/espeak-ng/espeak-ng) (GPL-3.0, used as a system tool) for phonemization. Built on the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview).

**backtalk itself** is [Jared Rhodenizer](https://jaredrhod.com)'s work — this fork changes how voices are picked and configured, not the core loop underneath. If you want the original, unmodified project (installer wizard, ElevenLabs-focused setup, his own video series and Discord community), it's at [github.com/jaredrhod/backtalk](https://github.com/jaredrhod/backtalk).

## Updating

```
./update.sh
```

(`update.bat` on Windows.) Shows what changed before applying it. Your config, your keys, and your agent's identity live outside the tracked files, so updates never touch them.

## License

Copyright (c) 2026 Jared Rhodenizer. Fork changes copyright (c) 2026 Youri Jan Olie.

Licensed under the GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later), same as the original. **Use it in your business, commercially, for free.** Run it, change it, build your workflow on top of it, and charge for the work you do with it. The one rule is that it stays open: if you hand it to someone else, or run a modified version as a service other people use, your version ships under this same license with its source available. Full terms are in the LICENSE file and at https://www.gnu.org/licenses/agpl-3.0.html
