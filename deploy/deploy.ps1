# Deploy the usd_bridge Houdini 22 package.
#
# Copies deploy/usd_bridge.json into $HOME\Documents\houdini22.0\packages\.
# The json's "path" points back at this repo, so the Python/shelf load live from
# here - editing package\usd_bridge\... and restarting Houdini is enough, no re-copy.
#
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1

$ErrorActionPreference = "Stop"

$repoRoot   = Split-Path -Parent $PSScriptRoot
$pkgDir     = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "houdini22.0\packages"
$dstJson    = Join-Path $pkgDir "usd_bridge.json"

if (-not (Test-Path $pkgDir))  { New-Item -ItemType Directory -Force -Path $pkgDir | Out-Null }

# Generate the Houdini package manifest pointing at THIS clone of the repo.
# Adjust ZBRUSH_EXEC_PATH / USD_BRIDGE_CACHE here (or in the generated file) if needed.
$pkgPath  = ($repoRoot -replace '\\', '/') + '/package/usd_bridge'
$manifest = @"
{
    "enable": true,
    "path": "$pkgPath",
    "env": [
        {
            "var": "ZBRUSH_EXEC_PATH",
            "value": "C:/Program Files/Maxon ZBrush 2026/ZBrush.exe"
        },
        {
            "var": "USD_BRIDGE_CACHE",
            "value": "C:/usd_bridge_cache"
        }
    ]
}
"@
Set-Content -Path $dstJson -Value $manifest -Encoding utf8
Write-Host "Deployed:" -ForegroundColor Green
Write-Host "  $dstJson"
Write-Host ""
Write-Host "Package payload (loaded live from the repo):"
Write-Host "  $repoRoot\package\usd_bridge"
Write-Host ""

# --- Blender add-on (copied, not live: re-run deploy after editing it) -------
$srcAddon  = Join-Path $repoRoot "package\blender\addons\usd_bridge_blender.py"
$blAddons  = Join-Path $env:APPDATA "Blender Foundation\Blender\5.2\scripts\addons"
if (Test-Path $srcAddon) {
    if (-not (Test-Path $blAddons)) { New-Item -ItemType Directory -Force -Path $blAddons | Out-Null }
    Copy-Item -Path $srcAddon -Destination (Join-Path $blAddons "usd_bridge_blender.py") -Force
    Write-Host "Blender add-on deployed:" -ForegroundColor Green
    Write-Host "  $blAddons\usd_bridge_blender.py"
    Write-Host "  Enable once: Blender > Edit > Preferences > Add-ons > 'USD Portal'" -ForegroundColor Cyan
    Write-Host ""
}

# --- Maya agent (copied, not live: re-run deploy after editing it) -----------
$srcMaya   = Join-Path $repoRoot "package\maya\scripts\usd_bridge_maya.py"
$mayaDir   = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "maya\2026\scripts"
if (Test-Path $srcMaya) {
    if (-not (Test-Path $mayaDir)) { New-Item -ItemType Directory -Force -Path $mayaDir | Out-Null }
    Copy-Item -Path $srcMaya -Destination (Join-Path $mayaDir "usd_bridge_maya.py") -Force
    $userSetup = Join-Path $mayaDir "userSetup.py"
    $startLine = "import usd_bridge_maya; usd_bridge_maya.start()"
    $needLine  = $true
    if (Test-Path $userSetup) {
        if (Select-String -Path $userSetup -Pattern "usd_bridge_maya" -Quiet) { $needLine = $false }
    }
    if ($needLine) {
        Add-Content -Path $userSetup -Encoding utf8 -Value @"

try:
    $startLine
except Exception:
    pass
"@
    }
    Write-Host "Maya agent deployed:" -ForegroundColor Green
    Write-Host "  $mayaDir\usd_bridge_maya.py  (+ userSetup.py autostart)"
    Write-Host ""
}

Write-Host "Restart Houdini 22, then find the 'USD Portal' shelf (+ tab -> Shelves)." -ForegroundColor Cyan
