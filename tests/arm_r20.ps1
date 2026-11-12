# Arm the R20 watchdog detached from this shell's job, and leave its PID on disk.
# ASCII only on purpose (memory: .ps1 with Chinese needs a UTF-8 BOM). The repo path is
# derived from $PSScriptRoot, so the 赛博鸡蛋 directory name never appears in this file.
param([string]$Tag = 'watch-r20-2')
$ErrorActionPreference = 'Stop'
$py = (Get-Command python.exe).Source
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $repo 'tests\out'
$stdout = Join-Path $out ($Tag + '.stdout.txt')
$stderr = Join-Path $out ($Tag + '.stderr.txt')
$pidfile = Join-Path $out ($Tag + '.pid')
$p = Start-Process -FilePath $py -ArgumentList @('-u', 'tests\watch_fullscreen_r20.py') `
    -WorkingDirectory $repo -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
Set-Content -Path $pidfile -Value $p.Id -Encoding ascii
Write-Output ('PID=' + $p.Id + ' stdout=' + $stdout)
