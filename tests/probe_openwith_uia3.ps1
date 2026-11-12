# 探针 v3（一次性，不算判据）：把资源管理器给这个文件提供的**动作文案清单**整个摊出来，
# 再逐个试着InvokeVerb，看到底哪一条能弹出「打开方式」。
# 为什么要摊：v1/v2 实测 InvokeVerb("openas") 与 rundll32 OpenAs_RunDLL 在这台 24H2 上
# 都不弹任何窗口（16 次轮询零新窗口），所以不能再猜动词名，得先看盘上真相。
param([string]$Target = "")
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $Target) { $Target = Join-Path (Join-Path $here "samples") "gelu-sample-3.mkv" }
$Target = (Resolve-Path $Target).Path
$out = Join-Path (Join-Path $here "out") ("probe-openwith-uia3-" + (Get-Date -Format "MMdd-HHmmss") + ".txt")
$L = New-Object System.Collections.ArrayList
function P($s) { [void]$L.Add($s); }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$dpi = Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name U32Dpi4 -PassThru
[void]$dpi::SetProcessDPIAware()
$root = [System.Windows.Automation.AutomationElement]::RootElement
$trueCond = [System.Windows.Automation.Condition]::TrueCondition
$NOISE = @("WindowsTerminal", "Qoder", "Code")
function Snap() {
    $o = @()
    foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)) {
        try {
            $p = $w.Current.ProcessId
            $pn = "?"
            try { $pn = (Get-Process -Id $p -ErrorAction Stop).ProcessName } catch {}
            $o += [pscustomobject]@{ Id = ($w.GetRuntimeId() -join ","); Name = $w.Current.Name
                Class = $w.Current.ClassName; Rect = $w.Current.BoundingRectangle; Proc = $pn; AE = $w }
        } catch {}
    }
    return $o
}
function Dump-New($ids, $label) {
    $now = @(Snap)
    $fresh = @($now | Where-Object { ($ids -notcontains $_.Id) -and ($NOISE -notcontains $_.Proc) })
    P ("  [" + $label + "] 现在 " + $now.Count + " 扇，新（去噪声）" + $fresh.Count + " 扇")
    foreach ($w in $fresh) {
        $kids = 0; $hit = 0
        try {
            $all = $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
            $kids = $all.Count
            foreach ($e in $all) { try { if ($e.Current.Name -like "*LuHui*") { $hit += 1 } } catch {} }
        } catch {}
        P ("      新窗口 名='" + $w.Name + "' 类=" + $w.Class + " 进程=" + $w.Proc +
           " 矩形=" + $w.Rect + " 子元素=" + $kids + " 命中LuHui=" + $hit)
    }
    return $fresh
}

$shell = New-Object -ComObject Shell.Application
$dir  = Split-Path -Parent $Target
$leaf = Split-Path -Leaf   $Target
$item = $shell.Namespace($dir).ParseName($leaf)
P ("针对文件 " + $Target)
P "---- 这个文件的动词清单（文案原样抄，含 & 助记符）----"
$verbs = @()
foreach ($v in $item.Verbs()) { $verbs += $v.Name; P ("  动词: [" + $v.Name + "]") }

foreach ($vn in $verbs) {
    $clean = ($vn -replace "&", "")
    if (-not ($clean -match "打开方式" -or $clean -match "Open with" -or $clean -match "openas")) { continue }
    P ("---- 试 InvokeVerb([" + $vn + "]) ----")
    $ids = @( @(Snap) | ForEach-Object { $_.Id } )
    try { $item.InvokeVerb($vn) } catch { P ("  报错：" + $_.Exception.Message); continue }
    Start-Sleep -Milliseconds 2500
    $f = @(Dump-New $ids "弹后")
    if ($f.Count -gt 0) {
        P "  >>> 弹出来了，抄一下列表内容"
        foreach ($w in $f) {
            try {
                $all = $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
                foreach ($e in $all) {
                    try {
                        $nm = $e.Current.Name
                        $ct = ($e.Current.ControlType.ProgrammaticName -split "\.")[-1]
                        if ($nm) { P ("      " + $ct + "=" + $nm) }
                    } catch {}
                }
            } catch {}
        }
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
        P "  已发 ESC"
        break
    }
}
$L | Out-File -FilePath $out -Encoding UTF8
Write-Output ("WROTE " + $out)
