# claude-overlay

Share your Claude Code sessions. Two formats: a portrait
story card and a landscape footer strip — one terracotta route line
(cumulative output tokens over the session), three quiet stats.

No dependencies beyond Python 3 (preinstalled on macOS). Optional:
`pip3 install qrcode` for terminal QR codes.

## For users: install as a Claude Code plugin

This repo is a Claude Code plugin (see `.claude-plugin/plugin.json`). Once
published to a marketplace:

```
/plugin marketplace add <your-gh-org>/claude-overlay
/plugin install claude-overlay
```

Then, inside any session:

```
/overlay
```

Claude runs the generator for the current session, prints a scannable QR
code right in the terminal, opens the story card, and lists the exported
PNG paths. Scan the QR with your phone (same Wi-Fi network) to open the
cards and save them to your camera roll — a temporary local server makes
them available for 5 minutes, no upload involved.

To test the plugin locally without a marketplace:

```bash
claude --plugin-dir /path/to/claude-overlay
```

### Automatic cards at session end (zero tokens)

The plugin also ships a `SessionEnd` hook (`hooks/hooks.json`): every time a
session ends, it regenerates the share card for that exact session — no
model turn, no tokens, no interaction. The freshest cards are always at:

```
<plugin>/out/latest/story.png
<plugin>/out/latest/strip.png
```

Everything runs locally: the hook reads the session transcript from
`~/.claude/projects` and writes PNGs to the plugin's `out/` folder. Nothing
leaves your machine. If the export can't run (e.g. no Chromium-based
browser installed), the hook exits silently.

Tip: when you've hit your rate limit and `/overlay` can't respond, your
card is already waiting in `out/latest/`, or run the script directly —
it costs zero tokens.

## Quick start (script, no plugin)

Render an overlay for your most recent Claude Code session:

```bash
python3 overlay.py
```

This parses the session transcript from `~/.claude/projects`, writes a
self-contained HTML page to `out/`, and opens it in your browser.

Other invocations:

```bash
python3 overlay.py --list           # list recent sessions
python3 overlay.py --session <id>   # render a specific session
python3 overlay.py --no-open        # write the file, don't open it
python3 overlay.py --qr             # QR that opens the cards on your phone
```

`--qr` exports the PNGs, starts a temporary local server (5 minutes, LAN
only), and prints a QR code linking to a mobile share page — scan it with
your phone on the same Wi-Fi to save the cards. Pass `--share-url <url>`
to encode a custom link instead.

## Export share images

```bash
python3 overlay.py --export
```

Writes two PNGs to `out/` alongside the HTML:

- `overlay-<id>-story.png` — 760x1000 portrait story card
- `overlay-<id>-strip.png` — 1320x200 landscape footer strip

Both are 2x (retina) with transparent margins, ready to drop onto a
screenshot, social post, or stream layout. Export uses headless Chrome
(or Chromium/Edge/Brave/Arc — whichever is installed); if none is found,
open the HTML and screenshot manually.

## Better data: the statusline tap (optional, recommended)

Transcript parsing alone has to approximate two numbers:

- **Thinking time** — estimated from streamed-chunk timestamps
- **Lines added/removed** — recomputed from edit patches
- **Cost** — unavailable (transcripts don't carry it)

Claude Code pipes canonical totals (cost, API duration, line counts) to the
statusline command on every turn. `statusline.py` prints a normal status line
*and* archives each snapshot to `~/.claude-overlay/sessions/`, which
`overlay.py` automatically prefers over its approximations.

Enable it in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/claude-overlay/statusline.py"
  }
}
```

Restart Claude Code (or start a new session) and the tap begins capturing.
Snapshots are cumulative, so even a single one fixes up the totals.

## Data sources

| Metric | Primary source | Fallback |
| --- | --- | --- |
| Token route (the line) | transcript JSONL per-request usage | — |
| Lines added/removed | statusline `cost.total_lines_*` | transcript edit patches |
| Thinking time | statusline `cost.total_api_duration_ms` | chunk-timestamp spans |
| Output tokens | transcript usage (deduped by request) | — |
| Cost | statusline `cost.total_cost_usd` | omitted (shows tokens) |
| Prompts / tool calls / elapsed | transcript JSONL | — |
