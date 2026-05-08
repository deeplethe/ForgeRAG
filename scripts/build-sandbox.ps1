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
Write-Host '==> Smoke check: kernel + pandas import' -ForegroundColor Cyan
docker run --rm $Tag python -c @'
import ipykernel, jupyter_client, pandas, matplotlib, pdfplumber
print('kernel/pandas/plt/pdfplumber import OK')
print('python', __import__('sys').version.split()[0])
print('pandas', pandas.__version__)
'@
