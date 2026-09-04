# README media — what's needed

Two placeholders in the main `README.md`, both referenced by relative path from repo root:

## `stock-demo.gif` (or `.png`)

The "what you actually get" hero image, right after the intro. Needs to show:

- The transcript page's **default state** — Solitude theme, nothing switched.
- A **plain browser tab**, no window manager chrome, no Television, no custom desktop theme around it.
- Ideally a short GIF of an actual exchange (hold key, talk, see the reply appear) rather than a static screenshot — more convincing that it's real and working, and GitHub autoplays GIFs inline with no click needed.

This is the "here's what a plain `git clone` gets you" image — it has to match that exactly, or it's misleading in the same way the whole point of the two-section split was meant to avoid.

## `styled-showcase.png` (or `.gif`)

The "Styled up" section, after Theming. This one's allowed to actually be Des's real, customized setup — Television-hosted windows, custom Omarchy theme, whatever looks good. The README text right below it already explains what it is and that it's not what ships by default; the image itself doesn't need any restraint, just needs to actually be that setup (not something further dressed up beyond what's real).

## Both

- Reasonable file size — GitHub renders large GIFs slowly; keep under a few MB if possible.
- PNG for static screenshots, GIF for anything showing motion/interaction (GitHub doesn't autoplay embedded video files the way it does GIFs).
