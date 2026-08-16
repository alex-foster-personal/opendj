#!/usr/bin/env bash

# test_fireworks_harness.sh
# Simulates the Orchestrator (Claude Code) delegating a task to the Fireworks worker harness (Aider).
# Usage: ./scripts/test_fireworks_harness.sh [TASK_FILE]

set -euo pipefail

TASK_FILE="${1:-.tmp/FIREWORKS_TASK.md}"

# Create a sample task if none provided
if [ ! -f "$TASK_FILE" ]; then
    mkdir -p .tmp
    cat <<EOF > "$TASK_FILE"
# Fireworks Harness Test Task
Please add a comment to the top of scripts/test_fireworks_harness.sh saying "# Harness verified working"
and then save it. Do not change any other logic.
EOF
    echo "Created sample task file at $TASK_FILE"
fi

if [ -z "${FIREWORKS_API_KEY:-}" ]; then
    echo "WARNING: FIREWORKS_API_KEY is not set. Aider will likely fail if it attempts to make a request."
    echo "Export it before running: export FIREWORKS_API_KEY=\"<your_key>\""
    # To run a true dry-run we would just exit here, but we'll try to run Aider if the user wants to see the failure mode or if they have the key in .env.
fi

# The model to use. For Fire Pass, these are typically specific router paths.
# We default to kimi-2.7-code but allow overriding via environment variable.
MODEL="${FIREWORKS_MODEL:-openai/accounts/fireworks/models/kimi-2.7-code}"
LOG_FILE=".tmp/fireworks_worker_$(date +%s).log"

echo "Spawning Fireworks worker with Aider..."
echo "Model: $MODEL"
echo "Task File: $TASK_FILE"
echo "Logging output to: $LOG_FILE"
echo "Orchestrator (Claude Code) will now wait until completion..."

# Run aider and redirect all output to the log to save Claude Code tokens
# --yes-always ensures Aider auto-commits and doesn't wait for human confirmation
aider --openai-api-base "https://api.fireworks.ai/inference/v1" \
      --model "$MODEL" \
      --message-file "$TASK_FILE" \
      --yes-always \
      --auto-commits \
      > "$LOG_FILE" 2>&1 || {
    EXIT_CODE=$?
    echo "Fireworks worker exited with code $EXIT_CODE. See $LOG_FILE for details."
    exit $EXIT_CODE
}

echo "Fireworks worker completed successfully."
echo "Orchestrator (Claude Code) can now run 'git diff HEAD~1' or 'pytest' to verify the work."