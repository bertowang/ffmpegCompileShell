# Windows 一键编译（MSVC），产物 -> ..\TGCapture3rd
param(
    [string]$Libs = "all",
    [string]$Abi = "x64",
    [int]$Jobs = 0
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Join-Path (Split-Path -Parent $PSScriptRoot) "")

python build.py bootstrap --platform windows --install
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$extra = @()
if ($Jobs -gt 0) { $extra = @("-j", "$Jobs") }
python build.py build --platform windows --libs $Libs --abi $Abi @extra
exit $LASTEXITCODE
