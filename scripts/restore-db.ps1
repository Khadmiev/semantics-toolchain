# Restore an Assistant Memory Postgres dump into a fresh stack (target machine).
# Run AFTER the db container is up but BEFORE relying on the app. It drops and
# recreates all objects from the dump (--clean --if-exists), including the graph,
# oauth_clients, and credentials/OAuth tokens.
#
# Usage:  .\scripts\restore-db.ps1 -DumpFile .\backups\assistant_memory-YYYYMMDD-HHMMSS.dump

param(
    [Parameter(Mandatory = $true)][string]$DumpFile
)

$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $PSScriptRoot
$container = "assistant_memory_db"

if (-not (Test-Path $DumpFile)) { Write-Error "dump not found: $DumpFile"; exit 1 }

Write-Host "Ensuring db container is up..."
docker compose --project-directory $proj up -d db | Out-Null
for ($i = 0; $i -lt 30; $i++) {
    if ((docker inspect -f '{{.State.Health.Status}}' $container 2>$null) -eq 'healthy') { break }
    Start-Sleep 2
}

Write-Host "Copying dump into container..."
docker cp $DumpFile "${container}:/tmp/restore.dump"

Write-Host "Restoring (drops + recreates objects)..."
docker exec $container pg_restore -U am -d assistant_memory --clean --if-exists --no-owner /tmp/restore.dump
docker exec $container rm -f /tmp/restore.dump

Write-Host "Restore complete. Start the full stack with scripts\start-local.ps1"
Write-Host "(alembic upgrade head will be a no-op since the schema came from the dump)."
