#!/usr/bin/env python3
"""SessionEnd hook — print story share QR in the terminal when a session ends."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def plugin_root() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def build_overlay_cmd(payload: dict) -> list[str]:
    overlay = plugin_root() / "overlay.py"
    cmd = [sys.executable, str(overlay), "--qr", "--quiet-if-empty"]

    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        cmd.extend(["--transcript-path", tp])
    elif payload.get("session_id"):
        cmd.extend(["--session", str(payload["session_id"])[:8]])

    return cmd


def spawn_detached_export(cmd: list[str]) -> bool:
    """Run overlay export detached so it survives SessionEnd's 1.5s plugin budget.

    Plugin-provided SessionEnd hooks do not raise Claude Code's session-end timeout
    budget, so synchronous export (Chrome + QR) is usually killed mid-run. We
    re-exec a tiny worker that writes directly to /dev/tty and return immediately.
    """
    worker = textwrap.dedent(
        f"""
        import subprocess
        import sys

        try:
            tty = open("/dev/tty", "w")
        except OSError:
            tty = sys.stderr

        print("\\nclaude-overlay — share your session\\n", file=tty, flush=True)
        try:
            result = subprocess.run(
                {cmd!r},
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            print(
                "claude-overlay: could not generate a share card for this session.",
                file=tty,
                flush=True,
            )
            raise SystemExit(0)

        output = (result.stdout + result.stderr).strip()
        if output:
            print(output, file=tty, flush=True)
        else:
            print(
                "claude-overlay: could not generate a share card for this session.",
                file=tty,
                flush=True,
            )
        """
    ).strip()

    try:
        subprocess.Popen(
            [sys.executable, "-c", worker],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        payload = {}

    cmd = build_overlay_cmd(payload)

    # Visible immediately — SessionEnd only shows stderr and may kill us quickly.
    print("claude-overlay: generating share card…", file=sys.stderr, flush=True)

    if spawn_detached_export(cmd):
        return 0

    # Fallback when /dev/tty detach fails (non-interactive): try inline, best effort.
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

    print("\nclaude-overlay — share your session\n", file=sys.stderr)
    print(output, file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
