#!/usr/bin/env bash
# Build the OpenCraig agent-sandbox image.
#
#   scripts/build-sandbox.sh           # default: opencraig/sandbox:py3.13
#   scripts/build-sandbox.sh --tag opencraig/sandbox:dev
#   scripts/build-sandbox.sh --no-cache
#
# Wraps `docker build` with two conveniences:
#   1. resolves repo root regardless of where the script is invoked
#   2. uses BuildKit so the multi-arch curl+install RUN stages stream
#      progress incrementally (otherwise apt-get install libreoffice
#      looks frozen for 90s on a fresh layer)
#
# After build, prints a short tag + size summary so you can see the
# image landed at the expected ~1.5 GB rather than ballooning.

set -euo pipefail

TAG="${TAG:-opencraig/sandbox:py3.13}"
NO_CACHE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --no-cache) NO_CACHE="--no-cache"; shift ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \?//'
      exit 0 ;;
    *)
      echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Resolve repo root from this script's location so callers from any
# subdir get the right context dir.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." &>/dev/null && pwd)"
CONTEXT="$REPO_ROOT/docker/sandbox"

if [[ ! -f "$CONTEXT/Dockerfile" ]]; then
  echo "error: $CONTEXT/Dockerfile not found" >&2
  exit 1
fi

echo "==> Building $TAG (context: $CONTEXT)"

DOCKER_BUILDKIT=1 docker build \
  $NO_CACHE \
  -t "$TAG" \
  -f "$CONTEXT/Dockerfile" \
  "$CONTEXT"

echo
echo "==> Done. Image summary:"
docker image inspect "$TAG" --format '{{.RepoTags}}  {{.Size}} bytes  ({{.Created}})'

echo
echo "==> Smoke check: data-stack imports + claude binary present"
# Code execution is subprocess-based via the SDK's bundled Bash
# tool — jupyter / ipykernel are intentionally NOT installed in
# the image (see docker/sandbox/requirements.txt comment block).
docker run --rm "$TAG" python -c "
import pandas, matplotlib, pdfplumber, openpyxl
print('pandas', pandas.__version__)
print('matplotlib', matplotlib.__version__)
print('pdfplumber', pdfplumber.__version__)
"
docker run --rm "$TAG" sh -c "command -v claude >/dev/null && echo 'claude binary OK' || (echo 'MISSING: claude binary' >&2; exit 1)"
