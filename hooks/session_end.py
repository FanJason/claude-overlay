#!/usr/bin/env python3
"""SessionEnd hook — print the story share QR in the terminal when a session ends.

Claude Code gives plugin SessionEnd hooks a short budget (~1.5s) and then kills
the hook's process group, so the export (Chrome render + QR) must run in a
detached worker. The worker must NOT call setsid(): a process in a new session
cannot open /dev/tty, and on macOS even an inherited pty fd returns EIO when
written from a foreign session. Instead, this hook opens /dev/tty itself
(while it still has the controlling terminal), hands that fd to the worker as
stdout/stderr, and detaches with setpgrp() only — a new process group escapes
the group kill but stays in the terminal's session, so writes still land.

Claude Code additionally spawns hooks with no controlling terminal at all, so
/dev/tty (a kernel alias that re-resolves the *calling process's* controlling
terminal on every operation) usually fails even here. We then resolve the
concrete device (/dev/ttysNNN) of the nearest ancestor that has a tty — the
Claude Code process itself — and open it directly with O_NOCTTY. A concrete
device fd keeps working from any session (this is how wall(1) writes to other
terminals).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import textwrap
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
    overlay = plugin_root() / "overlay.py"
    cmd = [sys.executable, str(overlay), "--qr", "--quiet-if-empty"]

    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        cmd.extend(["--transcript-path", tp])
    elif payload.get("session_id"):
        cmd.extend(["--session", str(payload["session_id"])[:8]])

    return cmd


def worker_source(cmd: list[str]) -> str:
    return textwrap.dedent(
        f"""
        import subprocess
        import sys

        def log(message):
            try:
                with open({str(LOG_PATH)!r}, "a") as fh:
                    fh.write("worker: " + message + "\\n")
            except OSError:
                pass

        try:
            result = subprocess.run(
                {cmd!r},
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout + result.stderr).strip()
        except Exception as exc:
            log("overlay export raised " + repr(exc))
            raise SystemExit(0)

        if not output:
            log("overlay export produced no output (empty session?)")
            raise SystemExit(0)

        try:
            print("\\nclaude-overlay — share your session\\n", flush=True)
            print(output, flush=True)
            log("share card printed to terminal")
        except OSError as exc:
            log("terminal write failed: " + repr(exc))
        """
    ).strip()


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


def spawn_detached_export(cmd: list[str]) -> bool:
    tty_fd = open_terminal()
    if tty_fd is None:
        log("falling back to inline run")
        return False

    detach: dict = (
        {"process_group": 0}
        if sys.version_info >= (3, 11)
        else {"preexec_fn": os.setpgrp}
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", worker_source(cmd)],
            stdin=subprocess.DEVNULL,
            stdout=tty_fd,
            stderr=tty_fd,
            close_fds=True,
            **detach,
        )
    except OSError as exc:
        log(f"failed to spawn worker: {exc!r}")
        return False
    finally:
        os.close(tty_fd)

    log(f"spawned detached worker pid={proc.pid}")
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
    if not spawn_detached_export(cmd):
        run_inline(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
