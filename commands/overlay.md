---
description: Generate share card (rate limited? run overlay.py --export --qr)
allowed-tools: Bash(python3 *)
---

First, check whether the session usage limit blocks this command:

!`python3 "${CLAUDE_PLUGIN_ROOT}/overlay.py" --rate-status 2>&1`

If the output above starts with `LIMIT REACHED`, show that message verbatim
and stop — do not run anything else.

Otherwise generate the share overlay for the current session:

```
python3 "${CLAUDE_PLUGIN_ROOT}/overlay.py" --export --qr
```

Then:

1. Show the QR code from the script output verbatim, inside a fenced code
   block so the alignment is preserved.
2. Reply with a one-line summary of the stats (lines added, thinking time,
   output tokens) and list both PNG paths.

Do not open the HTML preview or any image files. Do not modify any files.
If the script fails, show the error output.
$ARGUMENTS
