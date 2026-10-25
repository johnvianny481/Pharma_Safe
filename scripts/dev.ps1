<#
.SYNOPSIS
  Windows equivalent of the Makefile, for machines without GNU make.
.EXAMPLE
  ./scripts/dev.ps1 up
  ./scripts/dev.ps1 ci
#>
param([Parameter(Position = 0)][string]$Target = "help")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$UI = Join-Path $Root "apps/review_ui"
$PySrc = @("packages/domain/src", "packages/platform/src", "apps/api/src", "apps/ai_gateway/src",
           "apps/workflow_worker/src", "apps/ai_worker/src", "apps/submission_worker/src")
$SbomNpm = "@cyclonedx/cyclonedx-npm@6.0.1"

# Run a native command and stop on a non-zero exit code. Windows PowerShell 5.1 would turn
# any stderr line into a terminating error under "Stop", so judge by exit code instead.
function Run {
    $ErrorActionPreference = "Continue"
    $exe, $rest = $args
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "'$($args -join ' ')' failed with exit code $LASTEXITCODE" }
}
function Compose { Run docker compose --env-file .env -f deploy/docker-compose.yml @args }
function InUI([scriptblock]$Block) { Push-Location $UI; try { & $Block } finally { Pop-Location } }

$targets = [ordered]@{
    env          = { if (-not (Test-Path .env)) { Copy-Item .env.example .env; "Created .env from .env.example" } }
    up           = { & $targets.env; Compose up -d --wait --wait-timeout 600; Compose ps }
    down         = { Compose down }
    reset        = { Compose down -v }
    logs         = { Compose logs -f --tail=100 }
    ps           = { Compose ps }
    sync         = { Run uv sync --frozen }
    install      = { Run uv sync --frozen; InUI { Run npm ci } }
    lint         = { Run uv run ruff check .; Run uv run ruff format --check .; InUI { Run npm run lint } }
    fmt          = { Run uv run ruff check --fix .; Run uv run ruff format . }
    typecheck    = { Run uv run mypy @PySrc; InUI { Run npm run typecheck } }
    test         = {
        Run uv run pytest packages/domain --cov=pharmasafe_domain --cov-fail-under=85
        Run uv run pytest
        InUI { Run npm test }
    }
    security     = {
        New-Item -ItemType Directory -Force reports | Out-Null
        Run uv run bandit -c pyproject.toml -r packages apps -q
        Run uv export --frozen --no-dev --no-emit-workspace --no-hashes -o reports/requirements.txt -q
        Run uv run pip-audit --strict -r reports/requirements.txt
        InUI { Run npm audit --audit-level=high }
    }
    sbom         = {
        New-Item -ItemType Directory -Force sbom | Out-Null
        Run uv export --frozen --no-dev --no-emit-workspace --no-hashes -o sbom/python-requirements.txt -q
        Run uv run cyclonedx-py requirements sbom/python-requirements.txt --of JSON -o sbom/python.cdx.json
        InUI { Run npx --yes $SbomNpm --omit dev --output-file ../../sbom/review-ui.cdx.json }
    }
    ci           = { foreach ($t in "lint", "typecheck", "test", "security", "sbom") { & $targets[$t] } }
    migrate      = { Run uv run alembic -c apps/api/alembic.ini upgrade head }
    seed         = { Run uv run python -m pharmasafe_api.tenancy.seed }
    "verify-audit" = { Run uv run python -m pharmasafe_api.audit.verify_job }
    "email-intake" = { Run uv run python -m pharmasafe_api.intake.email_intake }
    api          = { Run uv run uvicorn pharmasafe_api.main:app --reload --port 8000 }
    "ai-gateway" = { Run uv run uvicorn pharmasafe_ai_gateway.main:app --reload --port 8001 }
    help         = { "Targets: " + ($targets.Keys -join ", ") }
}

if (-not $targets.Contains($Target)) { throw "Unknown target '$Target'. $(& $targets.help)" }
& $targets[$Target]
