# Build ScopeHost as Windows exe (PyInstaller onedir)
# Usage: powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Output: release\ScopeHost\ScopeHost.exe

param([string]$Python = "python")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 支持常规 Python，也可显式指定 Python 路径。
if (Test-Path -LiteralPath "$PSScriptRoot\.runtime") {
    $env:PYTHONPATH = "$PSScriptRoot\.runtime" + [IO.Path]::PathSeparator + $env:PYTHONPATH
}
# 避免宿主 PATH 中其他软件的 ICU/Qt DLL 被误收集。
$scopeBuildPath = $env:PATH
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
try {
& $Python -m PyInstaller --noconfirm --clean `
    --distpath release `
    --workpath build\v2 `
    ScopeHost.spec
} finally {
    $env:PATH = $scopeBuildPath
}

if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED"; exit 1 }

# Smoke test: headless render (built-in simulator)
Write-Host "`n=== exe smoke test ==="
$env:PYTHONIOENCODING = "utf-8"
$smoke = Start-Process -FilePath "$PSScriptRoot\release\ScopeHost\ScopeHost.exe" `
    -ArgumentList '--headless --out release\preview.png' -WindowStyle Hidden -PassThru -Wait
if ($smoke.ExitCode -ne 0 -or -not (Test-Path -LiteralPath "$PSScriptRoot\release\preview.png")) {
    Write-Host "exe smoke FAILED (exit $($smoke.ExitCode))"
    exit 1
} else {
    Write-Host "exe smoke OK -> release\preview.png"
}
Copy-Item -LiteralPath "$PSScriptRoot\StartDemo.cmd" -Destination "$PSScriptRoot\release\ScopeHost\StartDemo.cmd"
Copy-Item -LiteralPath "$PSScriptRoot\StartDS1152E.cmd" -Destination "$PSScriptRoot\release\ScopeHost\StartDS1152E.cmd"
Copy-Item -LiteralPath "$PSScriptRoot\StartMSO44.cmd" -Destination "$PSScriptRoot\release\ScopeHost\StartMSO44.cmd"
Copy-Item -LiteralPath "$PSScriptRoot\docs\quickstart.md" -Destination "$PSScriptRoot\release\ScopeHost\使用说明.md"
Copy-Item -LiteralPath "$PSScriptRoot\docs\dlm3000.md" -Destination "$PSScriptRoot\release\ScopeHost\DLM3000连接说明.md"
Copy-Item -LiteralPath "$PSScriptRoot\docs\mso44.md" -Destination "$PSScriptRoot\release\ScopeHost\MSO44连接说明.md"
Copy-Item -LiteralPath "$PSScriptRoot\docs\visa_usb.md" -Destination "$PSScriptRoot\release\ScopeHost\VISA与USB连接说明.md"
Write-Host "`nDONE: release\ScopeHost\ScopeHost.exe"
