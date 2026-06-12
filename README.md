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

Claude runs the generator for the current session and prints a scannable QR
code in the terminal. Scan it with your phone (same Wi-Fi network) to open
a share page with the story card — press and hold the image and tap Save to Photos. A temporary local server makes them available
for 5 minutes, no upload involved. Nothing is opened automatically in the browser.

**`/overlay` uses zero model tokens.** A `UserPromptSubmit` hook intercepts
the command before it reaches Claude, runs `overlay.py` locally, prints the
QR in your terminal, and skips the model turn — so it still works when
you've hit your session usage limit.

Fallback if the hook isn't loaded (run directly, also zero tokens):

```bash
python3 overlay.py --export --qr
```

Or in Claude Code bash mode: `!python3 overlay.py --export --qr`

Check limit status: `python3 overlay.py --rate-status`

To test the plugin locally without a marketplace:

```bash
./scripts/dev-claude.sh
```

That runs `claude --plugin-dir` pointed at this repo. Local `--plugin-dir`
copies **override** the installed marketplace version for the same plugin
name, so edits here take effect on the next Claude restart (no `/plugin update`
needed). Use `./scripts/test-hooks.sh` to smoke-test hooks without a session.

**Local dev loop**

1. Edit plugin files in this repo.
2. `./scripts/test-hooks.sh` — pipes fake hook JSON to `prompt_submit.py` and
   `run_session_end.sh` (writes to `~/.claude/claude-overlay-session-end.log`).
3. `./scripts/dev-claude.sh` — interactive Claude with the local plugin.
4. In Claude: `/hooks` → confirm `SessionEnd` lists `claude-overlay`.
5. Run `/overlay` or `/exit`; tail the log:

   ```bash
   tail -f ~/.claude/claude-overlay-session-end.log
   ```

6. Optional debug: `CLAUDE_CODE_DEBUG=1 ./scripts/dev-claude.sh --debug hooks`

Or manually:

```bash
claude --plugin-dir /path/to/claude-overlay
```

### Share card when you exit (zero tokens)

When a session ends (`/exit`, closing the terminal, etc.), a `SessionEnd`
hook runs `overlay.py --qr` and prints stats, the Story path, and a scannable
QR to your terminal. The QR opens the story card only. Nothing opens in the
browser. If the ending session has no output yet, it falls back to your most
recent session in the same project.

**Troubleshooting:** Plugin `SessionEnd` hooks are limited to a ~1.5s budget by
Claude Code, so this hook detaches the export and writes the QR directly to
your terminal (`/dev/tty`) after exit. If you still see nothing, restart Claude
Code after `/plugin update` (hooks reload on restart), then exit with `/exit`.
Closing the terminal window can kill the hook before the QR finishes.

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
python3 overlay.py --no-open        # write HTML, don't open browser
python3 overlay.py --qr             # QR in terminal (no browser open)
```

`--qr` exports the story PNG, starts a temporary local server (5 minutes, LAN
only), and prints a QR code linking to the story card — press and hold the
image and tap Save to Photos. Use `--export` to also write the footer strip
PNG. Pass `--share-url <url>` to encode a custom link instead.

## Export share images

```bash
python3 overlay.py --export
```

Writes two PNGs to `out/` alongside the HTML:

- `overlay-<id>-story.png` — portrait story card
- `overlay-<id>-strip.png` — landscape footer strip (auto-trimmed, transparent)

Both are 2x (retina) with transparent backgrounds, ready to drop onto a
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
