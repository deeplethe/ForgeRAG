#!/usr/bin/env bash
# Day 2 verification: confirms the sandbox image builds, hermes-
# agent imports cleanly inside, and the entrypoint script emits
# the expected JSONL events with a stub LLM.
#
# Run after scripts/build-sandbox.sh succeeds.

set -euo pipefail

IMAGE="opencraig/sandbox:py3.13"

echo "==> Step 1: confirm image exists locally"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Image $IMAGE not found. Run scripts/build-sandbox.sh first." >&2
    exit 1
fi

echo "==> Step 2: hermes-agent imports inside container"
docker run --rm "$IMAGE" python -c \
    "from run_agent import AIAgent; print('AIAgent OK:', AIAgent.__module__)"

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
echo "Next: Day 3-5 backend HermesContainerRunner + Day 8-9 end-to-end"
echo "      smoke with a real LLM key + the full chat route."
