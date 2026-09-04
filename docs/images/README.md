# README media

Both slots filled, real captures, both referenced by relative path from repo root in the main `README.md`.

## `stock-demo.gif` — done

The "what you actually get" hero image, right after the intro. 800×450, 31s, 2.1MB — converted from a real screen recording via a two-pass palette-optimized `ffmpeg` pass (`palettegen`/`paletteuse`, meaningfully better quality than a naive single-pass encode). Shows the transcript page's default state (Solitude theme, nothing switched) in a plain browser tab — this one has to keep matching a plain `git clone` + `./install.sh` result exactly, since it's the "here's what you actually get" promise. Recorded on Omarchy (the only test hardware available), captioned as such in the README.

## `styled-showcase.gif` — done

The "Styled up" section, after Theming. 760px wide, 8fps, 6.8MB (down from an unreduced 14.7MB at the source's native settings — GIF compression handles this busier, more colorful desktop scene far less efficiently than the flat stock-demo UI, so more aggressive settings were needed to keep the file size reasonable without losing legibility). Shows Des's real personal setup — Television hosting the backtalk transcript, ai-visualizer's radial face, and his own Vault Graph tool as separate themed windows, plus his own further-customized transcript with real character voices added. The README text right below it explains what's actually shown and that none of it ships by default.

## If either ever needs recapturing

- Two-pass palette conversion beats naive single-pass GIF encoding by a lot: `ffmpeg -i in.mp4 -vf "fps=N,scale=W:-1:flags=lanczos,palettegen" palette.png` then `ffmpeg -i in.mp4 -i palette.png -filter_complex "fps=N,scale=W:-1:flags=lanczos[x];[x][1:v]paletteuse" -loop 0 out.gif`.
- Busier/more colorful content (full-desktop captures) needs a lower fps and narrower scale to stay a reasonable size than flat UI-only captures do — check the actual output size before committing, don't assume the first pass's settings are fine.
- Keep it reasonable — GitHub renders large GIFs slowly, and every visitor downloads the full file just to see the README.
