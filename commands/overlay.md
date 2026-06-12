---
description: Generate a Strava-style share card for this session
allowed-tools: Bash(python3 *), Bash(open *)
---

Generate the share overlay for the current session by running:

```
python3 "${CLAUDE_PLUGIN_ROOT}/overlay.py" --export --no-open --qr
```

Then:

1. Show the QR code from the script output verbatim, inside a fenced code
   block so the alignment is preserved.
2. Open the story card PNG (the first path after "PNG:") with `open <path>`.
3. Reply with a one-line summary of the stats (lines added, thinking time,
   output tokens) and list both PNG paths.

Do not modify any files. If the script fails, show the error output.
$ARGUMENTS
