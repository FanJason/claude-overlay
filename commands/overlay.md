---
description: Generate share card for this session (zero tokens via hook)
allowed-tools: Bash(python3 *)
---

Generate the share overlay for the current session:

```
python3 "${CLAUDE_PLUGIN_ROOT}/overlay.py" --export --qr
```

Then:

1. Show the QR code from the script output verbatim, inside a fenced code
   block so the alignment is preserved.
2. Reply with a one-line summary of the stats (lines added, thinking time,
   output tokens) and the Story path.

Do not open the HTML preview or any image files. Do not modify any files.
If the script fails, show the error output.
$ARGUMENTS
