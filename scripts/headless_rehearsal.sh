#!/usr/bin/env bash
# Runs Claude Code agents headless against a running `offband rehearse`, one
# per workspace, and times the build from first prompt to every agent
# finishing. The first workspace is the lead, the rest follow. Logs land
# beside the workspaces.
#
#   offband rehearse --root ./rehearsal --names Ada Grace Linus   # in another terminal first
#   scripts/headless_rehearsal.sh ./rehearsal ada grace linus
#
# With only the root, it runs ada and grace.
set -euo pipefail

ROOT="$(cd "${1:-./rehearsal}" && pwd)"
shift || true
NAMES=("$@")
[ ${#NAMES[@]} -ge 2 ] || NAMES=(ada grace)
OTHERS=$(( ${#NAMES[@]} - 1 ))
if [ "$OTHERS" -eq 1 ]; then
  DEFAULT_LEAD="Find the agent next to you and build a two-player game with it."
else
  DEFAULT_LEAD="Find the $OTHERS agents next to you and build a two-player game with them."
fi
LEAD_PROMPT="${LEAD_PROMPT:-$DEFAULT_LEAD}"
FOLLOW_PROMPT="${FOLLOW_PROMPT:-Work with the agents next to you. The lead will propose the plan.}"
TOOLS="mcp__offband,Read,Write,Edit,Glob,Grep"

run() {
  local dir="$1" prompt="$2" log="$3"
  (cd "$dir" && claude -p "$prompt" \
      --mcp-config .mcp.json --strict-mcp-config \
      --allowedTools "$TOOLS" --permission-mode acceptEdits \
      --output-format stream-json --verbose) > "$log" 2>&1
}

start=$(date +%s)
pids=()
for name in "${NAMES[@]:1}"; do
  run "$ROOT/$name" "$FOLLOW_PROMPT" "$ROOT/$name.agent.jsonl" &
  pids+=($!)
done
sleep 5
run "$ROOT/${NAMES[0]}" "$LEAD_PROMPT" "$ROOT/${NAMES[0]}.agent.jsonl" &
pids+=($!)

status=0
for pid in "${pids[@]}"; do
  wait "$pid" || status=$?
done
end=$(date +%s)
echo "all ${#NAMES[@]} agents finished in $((end - start)) s (exit $status)"
for name in "${NAMES[@]}"; do
  echo "--- $name: game/"
  ls -la "$ROOT/$name/game" 2>/dev/null || echo "(no game directory)"
done
exit "$status"
