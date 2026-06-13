#!/usr/bin/env python3
"""SessionEnd hook — print the story share QR in the terminal when a session ends.

The QR must be written *synchronously, before this hook returns*. If we instead
detach a worker that writes a moment later (as an earlier version did), the card
lands after the shell has already drawn its prompt, leaving the cursor on a
blank line with no prompt under it — the user has to press Enter to get one back.
Writing before the hook returns means Claude Code is still in control of the
terminal and the shell draws its prompt cleanly *after* our output.

Synchronous printing is only safe because the slow part — the ~1.5s Chrome PNG
render — has been handed off to the share server (overlay.py --qr-defer): the
server process renders the card in the background while serving the page, so all
this hook does is compute stats and print the QR (~0.2s), comfortably inside the
hook budget.

Claude Code spawns hooks with no controlling terminal, so plain stdout doesn't
reach the user's screen and /dev/tty (a kernel alias that re-resolves the
*calling process's* controlling terminal) usually fails. We resolve the concrete
device (/dev/ttysNNN) of the nearest ancestor that has a tty — the Claude Code
process itself — and open it directly with O_NOCTTY. A concrete device fd writes
from any session (this is how wall(1) writes to other terminals).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

LOG_PATH = Path.home() / ".claude/claude-overlay-session-end.log"


def log(message: str) -> None:
    try:
        with LOG_PATH.open("a") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def plugin_root() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def build_overlay_cmd(payload: dict) -> list[str]:
    # The card aggregates every session in the current 4am-to-4am day, so the
    # specific session that just ended doesn't need to be passed — the default
    # (no --session) is the daily view, and it quietly no-ops on an empty day.
    # --qr-defer hands the Chrome render to the share server so this returns fast.
    overlay = plugin_root() / "overlay.py"
    return [sys.executable, str(overlay), "--qr-defer", "--quiet-if-empty"]


def ancestor_tty(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        return ""


def ancestor_ppid(pid: int) -> int:
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except OSError:
        return 0
    return int(out) if out.isdigit() else 0


def open_terminal() -> int | None:
    """Open the user's terminal for writing; None if there isn't one."""
    try:
        return os.open("/dev/tty", os.O_WRONLY)
    except OSError:
        pass  # no controlling terminal — the normal case under Claude Code

    pid = os.getppid()
    for _ in range(10):
        tty = ancestor_tty(pid)
        if tty and tty not in ("??", "?", "-"):
            dev = f"/dev/{tty}"
            try:
                fd = os.open(dev, os.O_WRONLY | os.O_NOCTTY)
            except OSError as exc:
                log(f"cannot open {dev}: {exc}")
                return None
            log(f"opened {dev} via ancestor pid {pid}")
            return fd
        pid = ancestor_ppid(pid)
        if pid <= 1:
            break
    log("no terminal found on ancestor chain")
    return None


def print_to_terminal(cmd: list[str]) -> bool:
    """Render the card and write it to the terminal, synchronously.

    Returns False (so the caller can fall back to inline stderr) when there is
    no terminal to write to. The overlay command is fast here because the Chrome
    render is deferred to the share server (see module docstring).
    """
    tty_fd = open_terminal()
    if tty_fd is None:
        log("no terminal — falling back to inline run")
        return False

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = (result.stdout + result.stderr).strip()
        if not output:
            log("overlay produced no output (empty session?)")
            return True  # terminal exists; nothing to show is not a failure
        banner = "\nclaude-overlay — share your session\n\n"
        os.write(tty_fd, (banner + output + "\n").encode("utf-8", "replace"))
        log("share card written to terminal synchronously")
    except Exception as exc:
        log(f"synchronous terminal write failed: {exc!r}")
    finally:
        os.close(tty_fd)
    return True


def run_inline(cmd: list[str]) -> None:
    """Best-effort fallback when there is no terminal (non-interactive)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        log(f"inline export raised {exc!r}")
        return

    output = (result.stdout + result.stderr).strip()
    if output:
        print("\nclaude-overlay — share your session\n", file=sys.stderr)
        print(output, file=sys.stderr, flush=True)
    else:
        log("inline export produced no output")


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    log(f"=== SessionEnd hook invoked === payload={raw.strip() or '(empty)'}")

    cmd = build_overlay_cmd(payload)
    if not print_to_terminal(cmd):
        run_inline(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
