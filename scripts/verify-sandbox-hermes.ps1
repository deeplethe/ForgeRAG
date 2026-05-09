# Day 2 verification: confirms the sandbox image builds, hermes-
# agent imports cleanly inside, and the entrypoint script emits
# the expected JSONL events with a stub LLM.
#
# Run after `scripts/build-sandbox.ps1` succeeds.
#
# This script is what you SHOULD run before claiming Day 2 done.
# Until then, treat the in-container path as "structurally
# scaffolded but not exercised end-to-end".

$ErrorActionPreference = "Stop"

$Image = "opencraig/sandbox:py3.13"

Write-Host "==> Step 1: confirm image exists locally" -ForegroundColor Cyan
docker image inspect $Image | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Image $Image not found. Run scripts/build-sandbox.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "==> Step 2: hermes-agent imports inside container" -ForegroundColor Cyan
docker run --rm $Image python -c "from run_agent import AIAgent; print('AIAgent OK:', AIAgent.__module__)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "hermes-agent didn't import — check the requirements.txt + image rebuild." -ForegroundColor Red
    exit 1
}

Write-Host "==> Step 3: opencraig_run_turn.py present at the expected path" -ForegroundColor Cyan
docker run --rm $Image test -f /opt/opencraig/opencraig_run_turn.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "Entrypoint script missing — Dockerfile COPY didn't land." -ForegroundColor Red
    exit 1
}

Write-Host "==> Step 4: entrypoint emits MissingInput error when no message env" -ForegroundColor Cyan
$out = docker run --rm $Image python /opt/opencraig/opencraig_run_turn.py
Write-Host $out
if ($out -notmatch '"kind": ?"error"') {
    Write-Host "Expected an error event when OPENCRAIG_USER_MESSAGE is unset." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "All structural checks passed. The container can run the entrypoint." -ForegroundColor Green
Write-Host "Next: Day 3-5 backend HermesContainerRunner + Day 8-9 end-to-end" -ForegroundColor Green
Write-Host "      smoke with a real LLM key + the full chat route." -ForegroundColor Green
