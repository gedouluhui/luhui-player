# 探针（一次性，不算判据）：弹出「打开方式」之后，把桌面上所有新出现的顶层窗口连同
# 类名/进程/矩形/子元素数摊出来，并对每个窗口整棵子树搜「LuHui」，看到底谁装着那个列表。
param([string]$Target = "")
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$proj = Split-Path -Parent $here
if (-not $Target) { $Target = Join-Path (Join-Path $here "samples") "gelu-sample-3.mkv" }
$Target = (Resolve-Path $Target).Path
$out = Join-Path (Join-Path $here "out") ("probe-openwith-uia-" + (Get-Date -Format "MMdd-HHmmss") + ".txt")
$L = New-Object System.Collections.ArrayList
function P($s) { [void]$L.Add($s) }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$dpi = Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name U32Dpi2 -PassThru
[void]$dpi::SetProcessDPIAware()

$root = [System.Windows.Automation.AutomationElement]::RootElement
$trueCond = [System.Windows.Automation.Condition]::TrueCondition
function Snap() {
    $o = @()
    foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)) {
        try {
            $pid2 = $w.Current.ProcessId
            $pname = ""
            try { $pname = (Get-Process -Id $pid2 -ErrorAction Stop).ProcessName } catch { $pname = "?" }
            $o += [pscustomobject]@{
                Id = ($w.GetRuntimeId() -join ","); Name = $w.Current.Name; Class = $w.Current.ClassName
                Rect = $w.Current.BoundingRectangle; Pid = $pid2; Proc = $pname; AE = $w }
        } catch {}
    }
    return $o
}
$before = @(Snap)
P ("弹之前顶层窗口 " + $before.Count + " 扇")
$beforeIds = @($before | ForEach-Object { $_.Id })

$shell = New-Object -ComObject Shell.Application
$item = $shell.Namespace((Split-Path -Parent $Target)).ParseName((Split-Path -Leaf $Target))
P ("InvokeVerb(openas) 针对 " + $Target)
$item.InvokeVerb("openas")

for ($i = 1; $i -le 14; $i++) {
    Start-Sleep -Milliseconds 900
    $now = @(Snap)
    $fresh = @($now | Where-Object { $beforeIds -notcontains $_.Id })
    P ("---- 第 " + $i + " 次：现在 " + $now.Count + " 扇，新 " + $fresh.Count + " 扇 ----")
    foreach ($w in $fresh) {
        $kids = 0
        $hit = 0
        $sample = @()
        try {
            $all = $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
            $kids = $all.Count
            foreach ($e in $all) {
                try {
                    $nm = $e.Current.Name
                    if ($nm -and $nm.Length -gt 0 -and $sample.Count -lt 40) {
                        $ct = ($e.Current.ControlType.ProgrammaticName -split "\.")[-1]
                        $sample += ($ct + "=" + $nm)
                    }
                    if ($nm -like "*LuHui*") { $hit += 1 }
                } catch {}
            }
        } catch { $sample += ("子树抄不动：" + $_.Exception.Message) }
        P ("  新窗口 名='" + $w.Name + "' 类=" + $w.Class + " 进程=" + $w.Proc + "/" + $w.Pid +
           " 矩形=" + $w.Rect + " 子元素=" + $kids + " 命中LuHui=" + $hit)
        foreach ($s in $sample) { P ("      " + $s) }
    }
    if ($fresh.Count -gt 0) {
        $anyHit = $false
        foreach ($w in $fresh) { }
    }
}
try { Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch {}
Start-Sleep -Milliseconds 600
P "已发 ESC"
$L | Out-File -FilePath $out -Encoding UTF8
Write-Output ("WROTE " + $out)
