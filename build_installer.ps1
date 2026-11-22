$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stage = Join-Path $root 'cache\installer_stage'
$exe = Join-Path $root 'dist\METEOR-X.exe'

if (-not (Test-Path -LiteralPath $exe)) {
    throw 'Build dist\METEOR-X.exe with build_portable.ps1 first.'
}

New-Item -ItemType Directory -Path $stage -Force | Out-Null
Copy-Item -LiteralPath $exe -Destination $stage -Force
foreach ($file in @('meteorrx.ini', 'README.md', 'START-HERE.txt', 'LICENSE')) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination $stage -Force
}
$driverStage = Join-Path $stage 'drivers'
New-Item -ItemType Directory -Path $driverStage -Force | Out-Null
Copy-Item -Path (Join-Path $root 'drivers\*') -Destination $driverStage -Recurse -Force

$compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $compiler) {
    throw 'Install Inno Setup 7 and add ISCC.exe to PATH.'
}
& $compiler.Source (Join-Path $root 'METEOR-X-Setup.iss')
if ($LASTEXITCODE -ne 0) {
    throw 'Installer build failed.'
}
