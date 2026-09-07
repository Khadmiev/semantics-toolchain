# Back up the Assistant Memory Postgres DB to a compressed custom-format dump.
# This dump holds EVERYTHING that must survive a machine move: the graph memory,
# oauth_clients (DCR registrations), and credentials/OAuth tokens.
#
# Usage:  .\scripts\backup-db.ps1 [-OutDir <path>]
# Restore with scripts\restore-db.ps1 on the target machine.

param(
    [string]$OutDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "backups")
)

$ErrorActionPreference = "Stop"
$container = "assistant_memory_db"

if (-not (docker ps --filter "name=$container" --format "{{.Names}}")) {
    Write-Error "container $container is not running - start the stack first (start-local.ps1)"
    exit 1
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outFile = Join-Path $OutDir "assistant_memory-$stamp.dump"

Write-Host "Dumping database (custom format)..."
docker exec $container pg_dump -U am -d assistant_memory -Fc -f /tmp/am.dump
docker cp "${container}:/tmp/am.dump" $outFile
docker exec $container rm -f /tmp/am.dump

$size = [math]::Round((Get-Item $outFile).Length / 1MB, 2)
Write-Host "Backup written: $outFile ($size MB)"
Write-Host "Copy this file to the new machine and run scripts\restore-db.ps1 -DumpFile <path>."
