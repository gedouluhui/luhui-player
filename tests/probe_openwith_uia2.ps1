# 探针 v2（一次性，不算判据）：换 rundll32 shell32.dll,OpenAs_RunDLL 这扇门弹「打开方式」，
# 并且把 WindowsTerminal 那种"标签标题变了"的噪声排掉（v1 就是被它骗成"对话框弹出来了"）。
param([string]$Target = "")
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $Target) { $Target = Join-Path (Join-Path $here "samples") "gelu-sample-3.mkv" }
$Target = (Resolve-Path $Target).Path
$out = Join-Path (Join-Path $here "out") ("probe-openwith-uia2-" + (Get-Date -Format "MMdd-HHmmss") + ".txt")
$L = New-Object System.Collections.ArrayList
function P($s) { [void]$L.Add($s) }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$dpi = Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name U32Dpi3 -PassThru
[void]$dpi::SetProcessDPIAware()

$root = [System.Windows.Automation.AutomationElement]::RootElement
$trueCond = [System.Windows.Automation.Condition]::TrueCondition
$NOISE = @("WindowsTerminal", "Qoder", "Code")     # 这些进程的窗口标题会变，RuntimeId 也会变
function Snap() {
    $o = @()
    foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)) {
        try {
            $p = $w.Current.ProcessId
            $pn = ""
            try { $pn = (Get-Process -Id $p -ErrorAction Stop).ProcessName } catch { $pn = "?" }
            $o += [pscustomobject]@{
                Id = ($w.GetRuntimeId() -join ","); Name = $w.Current.Name; Class = $w.Current.ClassName
                Rect = $w.Current.BoundingRectangle; Pid = $p; Proc = $pn; AE = $w }
        } catch {}
    }
    return $o
}
$before = @(Snap)
$beforeIds = @($before | ForEach-Object { $_.Id })
P ("弹之前顶层窗口 " + $before.Count + " 扇；噪声进程=" + ($NOISE -join ","))
P ("弹之前各窗口：" + ((($before | ForEach-Object { "'" + $_.Name + "'/" + $_.Proc + "/" + $_.Class }) -join "、")))

P ("走 rundll32 shell32.dll,OpenAs_RunDLL 针对 " + $Target)
Start-Process -FilePath "rundll32.exe" -ArgumentList @("shell32.dll,OpenAs_RunDLL", ("`"" + $Target + "`""))

for ($i = 1; $i -le 16; $i++) {
    Start-Sleep -Milliseconds 900
    $now = @(Snap)
    $fresh = @($now | Where-Object { ($beforeIds -notcontains $_.Id) -and ($NOISE -notcontains $_.Proc) })
    P ("---- 第 " + $i + " 次：现在 " + $now.Count + " 扇，新（去噪声）" + $fresh.Count + " 扇 ----")
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
                    $ct = ($e.Current.ControlType.ProgrammaticName -split "\.")[-1]
                    if ($nm) { if ($sample.Count -lt 60) { $sample += ($ct + "=" + $nm) } }
                    else { if ($sample.Count -lt 60) { $sample += ("[" + $ct + " 无名]") } }
                    if ($nm -like "*LuHui*") { $hit += 1 }
                } catch {}
            }
        } catch { $sample += ("子树抄不动：" + $_.Exception.Message) }
        P ("  新窗口 名='" + $w.Name + "' 类=" + $w.Class + " 进程=" + $w.Proc + "/" + $w.Pid +
           " 矩形=" + $w.Rect + " 子元素=" + $kids + " 命中LuHui=" + $hit)
        foreach ($s in $sample) { P ("      " + $s) }
    }
}
try { Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch {}
Start-Sleep -Milliseconds 800
P "已发 ESC"
$L | Out-File -FilePath $out -Encoding UTF8
Write-Output ("WROTE " + $out)
