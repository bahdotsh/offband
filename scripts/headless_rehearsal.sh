#!/usr/bin/env bash
# Runs two Claude Code agents headless against a running `offband rehearse`,
# each in its own workspace, and times the build from first prompt to both
# agents finishing. Logs land beside the workspaces.
#
#   offband rehearse --root ./rehearsal        # in another terminal first
#   scripts/headless_rehearsal.sh ./rehearsal
set -euo pipefail

ROOT="$(cd "${1:-./rehearsal}" && pwd)"
LEAD_PROMPT="${LEAD_PROMPT:-Find the agent next to you and build a two-player game with it.}"
FOLLOW_PROMPT="${FOLLOW_PROMPT:-Work with the agent next to you. It will propose the plan.}"
TOOLS="mcp__offband,Read,Write,Edit,Glob,Grep"

run() {
  local dir="$1" prompt="$2" log="$3"
  (cd "$dir" && claude -p "$prompt" \
      --mcp-config .mcp.json --strict-mcp-config \
      --allowedTools "$TOOLS" --permission-mode acceptEdits \
      --output-format stream-json --verbose) > "$log" 2>&1
}

start=$(date +%s)
run "$ROOT/grace" "$FOLLOW_PROMPT" "$ROOT/grace.agent.jsonl" &
follower=$!
sleep 5
run "$ROOT/ada" "$LEAD_PROMPT" "$ROOT/ada.agent.jsonl" &
lead=$!

status=0
wait "$lead" || status=$?
wait "$follower" || status=$?
end=$(date +%s)
echo "both agents finished in $((end - start)) s (exit $status)"
for name in ada grace; do
  echo "--- $name: game/"
  ls -la "$ROOT/$name/game" 2>/dev/null || echo "(no game directory)"
done
exit "$status"
