#!/usr/bin/env bash
# SessionEnd entrypoint — logs hook invocations for debugging, then runs session_end.py.
set -euo pipefail

LOG="${HOME}/.claude/claude-overlay-session-end.log"
INPUT="$(cat)"
{
  echo "=== $(date -Iseconds) ==="
  echo "$INPUT"
} >>"$LOG"

ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [[ -z "$ROOT" ]]; then
  ROOT="$(python3 - <<'PY'
import json
from pathlib import Path

path = Path.home() / ".claude/plugins/installed_plugins.json"
try:
    data = json.loads(path.read_text())
    print(data["plugins"]["claude-overlay@claude-overlay"][0]["installPath"])
except Exception:
    print("")
PY
)"
fi

if [[ -z "$ROOT" || ! -f "${ROOT}/hooks/session_end.py" ]]; then
  echo "claude-overlay: could not locate plugin install path." >>"$LOG"
  exit 0
fi

printf '%s' "$INPUT" | exec python3 "${ROOT}/hooks/session_end.py"
