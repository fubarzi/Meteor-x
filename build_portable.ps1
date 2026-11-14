$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
& $pythonExe -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
& $pythonExe -m PyInstaller --noconfirm METEOR-X.spec
if ($LASTEXITCODE -ne 0) { throw "METEOR-X build failed" }
Copy-Item -LiteralPath (Join-Path $projectRoot "drivers") -Destination (Join-Path $projectRoot "dist") -Recurse -Force
Write-Host "Built dist\METEOR-X.exe with bundled drivers."
