<#
.SYNOPSIS
  Build the OpenCraig agent-sandbox image (Windows / PowerShell).

.DESCRIPTION
  Mirrors scripts/build-sandbox.sh — same Dockerfile, same tag default.
  Useful when developing on Windows with Docker Desktop and you don't
  want to fall through to WSL just to run a build.

.PARAMETER Tag
  Image tag to apply. Default: opencraig/sandbox:py3.13

.PARAMETER NoCache
  Pass --no-cache to docker build.

.EXAMPLE
  .\scripts\build-sandbox.ps1
  .\scripts\build-sandbox.ps1 -Tag opencraig/sandbox:dev -NoCache
#>

[CmdletBinding()]
param(
    [string]$Tag = 'opencraig/sandbox:py3.13',
    [switch]$NoCache
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$context = Join-Path $repoRoot 'docker/sandbox'

if (-not (Test-Path "$context/Dockerfile")) {
    Write-Error "Dockerfile not found at $context/Dockerfile"
    exit 1
}

Write-Host "==> Building $Tag (context: $context)" -ForegroundColor Cyan

$env:DOCKER_BUILDKIT = '1'
$buildArgs = @('build', '-t', $Tag, '-f', "$context/Dockerfile")
if ($NoCache) { $buildArgs = @('build', '--no-cache', '-t', $Tag, '-f', "$context/Dockerfile") }
$buildArgs += $context

& docker @buildArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ''
Write-Host '==> Done. Image summary:' -ForegroundColor Cyan
docker image inspect $Tag --format '{{.RepoTags}}  {{.Size}} bytes  ({{.Created}})'

Write-Host ''
Write-Host '==> Smoke check: data-stack imports + claude binary present' -ForegroundColor Cyan
# Code execution is subprocess-based via the SDK's bundled Bash
# tool — jupyter / ipykernel are intentionally NOT installed in
# the image (see docker/sandbox/requirements.txt comment block).
docker run --rm $Tag python -c @'
import pandas, matplotlib, pdfplumber, openpyxl
print('pandas', pandas.__version__)
print('matplotlib', matplotlib.__version__)
print('pdfplumber', pdfplumber.__version__)
'@
docker run --rm $Tag sh -c "command -v claude >/dev/null && echo 'claude binary OK' || (echo 'MISSING: claude binary' >&2; exit 1)"
