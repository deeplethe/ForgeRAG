# Sandbox image structural verification: confirms the image builds,
# the Claude Agent SDK imports cleanly inside the container, the
# bundled `claude` binary is on PATH, and the entrypoint script
# emits the expected JSONL error event when run without the required
# env vars (smoke without a real LLM call).
#
# Run after scripts/build-sandbox.ps1 succeeds.

$ErrorActionPreference = "Stop"

$Image = "opencraig/sandbox:py3.13"

Write-Host "==> Step 1: confirm image exists locally" -ForegroundColor Cyan
docker image inspect $Image | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Image $Image not found. Run scripts/build-sandbox.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "==> Step 2: claude-agent-sdk imports + bundled binary on PATH" -ForegroundColor Cyan
docker run --rm $Image bash -lc "python -c 'import claude_agent_sdk; print(\""SDK\"", claude_agent_sdk.__version__)' && which claude && claude --version"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Claude Agent SDK / claude binary not available — check requirements.txt + Dockerfile symlink." -ForegroundColor Red
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
Write-Host "Next: Day 3-5 backend ClaudeContainerRunner + Day 8-9 end-to-end" -ForegroundColor Green
Write-Host "      smoke with a real LLM key + the full chat route." -ForegroundColor Green
