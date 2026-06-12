#!/usr/bin/env bash
# Start Claude Code with this repo as a local plugin (overrides marketplace install).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Plugin SessionEnd hooks use a ~1.5s budget unless this is set.
export CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS="${CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS:-120000}"

echo "claude-overlay dev: loading plugin from ${ROOT}" >&2
echo "  SessionEnd log: ~/.claude/claude-overlay-session-end.log" >&2
echo "  Hook smoke test: ${ROOT}/scripts/test-hooks.sh" >&2
echo "  In Claude: /hooks → SessionEnd should list claude-overlay" >&2
echo >&2

exec claude --plugin-dir "$ROOT" "$@"
