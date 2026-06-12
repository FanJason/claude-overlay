#!/usr/bin/env python3
"""SessionEnd hook — print share QR in the terminal when a session ends."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def plugin_root() -> Path:
    import os

    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return 0

    overlay = plugin_root() / "overlay.py"
    cmd = [sys.executable, str(overlay), "--export", "--qr"]

    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        cmd.extend(["--transcript-path", tp])
    elif payload.get("session_id"):
        cmd.extend(["--session", str(payload["session_id"])[:8]])
    else:
        return 0

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception:
        return 0

    output = (result.stdout + result.stderr).strip()
    if not output:
        print(
            "claude-overlay: could not generate a share card for this session.",
            file=sys.stderr,
        )
        return 0

    # SessionEnd hooks only show stderr in the terminal — not stdout.
    print("\nclaude-overlay — share your session\n", file=sys.stderr)
    print(output, file=sys.stderr)
    print(file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
