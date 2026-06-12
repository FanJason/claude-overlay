#!/usr/bin/env python3
"""Claude Code statusline command that doubles as a telemetry tap.

Claude Code pipes a JSON session snapshot to the statusline command on every
turn. This script:
  1. appends the snapshot to ~/.claude-overlay/sessions/<session_id>.ndjson
     (the canonical data source for overlay.py — cost, API duration, lines)
  2. prints a one-line status for the terminal UI

Wire it up in ~/.claude/settings.json:
  "statusLine": {
    "type": "command",
    "command": "python3 /Users/jasonfan/Projects/claude-overlay/statusline.py"
  }
"""

import json
import sys
import time
from pathlib import Path

SNAPSHOT_DIR = Path.home() / ".claude-overlay" / "sessions"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("claude-overlay: bad statusline payload")
        return 0

    session_id = data.get("session_id", "unknown")
    data["_captured_at"] = time.time()

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with (SNAPSHOT_DIR / f"{session_id}.ndjson").open("a") as f:
        f.write(json.dumps(data) + "\n")

    model = (data.get("model") or {}).get("display_name", "Claude")
    cost = (data.get("cost") or {}) or {}
    usd = cost.get("total_cost_usd")
    added = cost.get("total_lines_added", 0)
    removed = cost.get("total_lines_removed", 0)
    api_ms = cost.get("total_api_duration_ms", 0)
    ctx = (data.get("context_window") or {}).get("used_percentage")

    parts = [model]
    if usd is not None:
        parts.append(f"${usd:.2f}")
    parts.append(f"+{added}/-{removed}")
    m, s = divmod(int(api_ms / 1000), 60)
    parts.append(f"think {m}m{s:02d}s")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")

    print(" | ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
