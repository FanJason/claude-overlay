#!/usr/bin/env python3
"""Intercept /overlay before it reaches the model (zero tokens, works when rate limited)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

OVERLAY_CMD = re.compile(r"^/overlay(\s|$)", re.IGNORECASE)


def plugin_root() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def is_overlay_prompt(prompt: str) -> bool:
    text = prompt.strip()
    if OVERLAY_CMD.match(text):
        return True
    if "overlay.py" in text and "--export" in text and "--qr" in text:
        return True
    return "Generate the share overlay for the current session" in text


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        print(json.dumps({}))
        return 0

    prompt = payload.get("user_prompt") or ""
    if not is_overlay_prompt(prompt):
        print(json.dumps({}))
        return 0

    overlay = plugin_root() / "overlay.py"
    result = subprocess.run(
        [sys.executable, str(overlay), "--export", "--qr"],
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()

    # Plain text for the terminal (preserves QR alignment).
    if output:
        print(output, file=sys.stderr)

    print(
        json.dumps(
            {
                "continue": False,
                "suppressOutput": True,
                "systemMessage": output or "overlay: generation failed",
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
