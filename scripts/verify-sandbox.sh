#!/usr/bin/env bash
# Sandbox image structural verification: confirms the image builds,
# the Claude Agent SDK imports cleanly inside the container, the
# bundled `claude` binary is on PATH, and the entrypoint script
# emits the expected JSONL error event when run without the required
# env vars (smoke without a real LLM call).
#
# Run after scripts/build-sandbox.sh succeeds.

set -euo pipefail

IMAGE="opencraig/sandbox:py3.13"

echo "==> Step 1: confirm image exists locally"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Image $IMAGE not found. Run scripts/build-sandbox.sh first." >&2
    exit 1
fi

echo "==> Step 2: claude-agent-sdk imports + bundled binary on PATH"
docker run --rm "$IMAGE" bash -lc \
    "python -c 'import claude_agent_sdk; print(\"SDK\", claude_agent_sdk.__version__)' && which claude && claude --version"

echo "==> Step 3: opencraig_run_turn.py present at the expected path"
docker run --rm "$IMAGE" test -f /opt/opencraig/opencraig_run_turn.py

echo "==> Step 4: entrypoint emits MissingInput error when no message env"
out=$(docker run --rm "$IMAGE" python /opt/opencraig/opencraig_run_turn.py)
echo "$out"
if ! echo "$out" | grep -q '"kind": *"error"'; then
    echo "Expected an error event when OPENCRAIG_USER_MESSAGE is unset." >&2
    exit 1
fi

echo
echo "All structural checks passed. The container can run the entrypoint."
echo "Next: Day 3-5 backend ClaudeContainerRunner + Day 8-9 end-to-end"
echo "      smoke with a real LLM key + the full chat route."
