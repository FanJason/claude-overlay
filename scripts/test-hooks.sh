#!/usr/bin/env bash
# Smoke-test plugin hooks without starting a full Claude session.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export CLAUDE_PLUGIN_ROOT="$ROOT"

TRANSCRIPT="${1:-}"
if [[ -z "$TRANSCRIPT" ]]; then
  TRANSCRIPT="$(ls -t "${HOME}/.claude/projects"/*/*.jsonl 2>/dev/null | head -1 || true)"
fi

if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]]; then
  echo "No transcript found. Pass a .jsonl path or run a Claude session first." >&2
  exit 1
fi

SESSION_ID="$(basename "$TRANSCRIPT" .jsonl)"
LOG="${HOME}/.claude/claude-overlay-session-end.log"

echo "=== claude-overlay hook smoke test ==="
echo "Plugin root:  $ROOT"
echo "Transcript:   $TRANSCRIPT"
echo "Session id:   $SESSION_ID"
echo

run_hook() {
  local name="$1"
  local script="$2"
  local payload="$3"
  echo "--- $name ---"
  if [[ "$script" == *.sh ]]; then
    printf '%s' "$payload" | CLAUDE_PLUGIN_ROOT="$ROOT" bash "$script"
  else
    printf '%s' "$payload" | CLAUDE_PLUGIN_ROOT="$ROOT" python3 "$script"
  fi
  echo "(exit $?)"
  echo
}

OVERLAY_PAYLOAD="$(jq -n \
  --arg sid "$SESSION_ID" \
  --arg tp "$TRANSCRIPT" \
  --arg prompt "/overlay" \
  '{session_id: $sid, transcript_path: $tp, hook_event_name: "UserPromptSubmit", user_prompt: $prompt, prompt: $prompt}')"

SESSION_END_PAYLOAD="$(jq -n \
  --arg sid "$SESSION_ID" \
  --arg tp "$TRANSCRIPT" \
  '{session_id: $sid, transcript_path: $tp, hook_event_name: "SessionEnd", reason: "other"}')"

run_hook "UserPromptSubmit (/overlay)" "$ROOT/hooks/prompt_submit.py" "$OVERLAY_PAYLOAD"
run_hook "SessionEnd (shell wrapper)" "$ROOT/hooks/run_session_end.sh" "$SESSION_END_PAYLOAD"

if [[ -f "$LOG" ]]; then
  echo "--- SessionEnd log (last 20 lines) ---"
  tail -20 "$LOG"
  echo
else
  echo "No SessionEnd log yet at $LOG"
fi

echo "Done. For a live test: ${ROOT}/scripts/dev-claude.sh"
echo "Then run /overlay or /exit and watch the log above."
