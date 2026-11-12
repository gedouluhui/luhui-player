# 探针 v6（一次性，不算判据）：前三扇门都量不到"打开方式"那个列表，只剩最真的一条路——
# 开一扇资源管理器、选中文件、按菜单键（VK_APPS）把上下文菜单叫出来，把整棵 UIA 树摊下来。
#
# 为什么可以这么干而不危险：目标文件是**临时目录里自己抄的一份**（probe-sample.mkv），
# 不在用户任何真实目录里。菜单里那些"删除/重命名/剪切"就算点歪，最多删掉我自己那份抄本。
param([string]$Sample = "")
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $Sample) { $Sample = Join-Path (Join-Path $here "samples") "gelu-sample-3.mkv" }
$Sample = (Resolve-Path $Sample).Path
$stamp = Get-Date -Format "MMdd-HHmmss"
$tmp = Join-Path $env:TEMP ("gelu-openwith-probe-" + $stamp)
New-Item -ItemType Directory -Path $tmp | Out-Null
Copy-Item $Sample (Join-Path $tmp "probe-sample.mkv")
$leaf = "probe-sample.mkv"
$out = Join-Path (Join-Path $here "out") ("probe-openwith-explorer-" + $stamp + ".txt")
$L = New-Object System.Collections.ArrayList
function P($s) { [void]$L.Add($s) }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
$dpi = Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name U32Dpi6 -PassThru
[void]$dpi::SetProcessDPIAware()
$kb = Add-Type -MemberDefinition @'
[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
'@ -Name KbdProbe -PassThru

$root = [System.Windows.Automation.AutomationElement]::RootElement
$trueCond = [System.Windows.Automation.Condition]::TrueCondition
function Snap() {
    $o = @()
    foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)) {
        try {
            $p = $w.Current.ProcessId
            $pn = "?"
            try { $pn = (Get-Process -Id $p -ErrorAction Stop).ProcessName } catch {}
            $o += [pscustomobject]@{ Id = ($w.GetRuntimeId() -join ","); Name = $w.Current.Name
                Class = $w.Current.ClassName; Rect = $w.Current.BoundingRectangle; Proc = $pn; Pid = $p; AE = $w }
        } catch {}
    }
    return $o
}
function Tree($w, $max) {
    P ("  == 窗口 名='" + $w.Name + "' 类=" + $w.Class + " 进程=" + $w.Proc + " 矩形=" + $w.Rect + " ==")
    try {
        $all = $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
        P ("     子元素 " + $all.Count + " 个")
        $n = 0
        foreach ($e in $all) {
            if ($n -ge $max) { P "     …（截断）"; break }
            try {
                $ct = ($e.Current.ControlType.ProgrammaticName -split "\.")[-1]
                $nm = $e.Current.Name
                $inv = ""
                try { if ($e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern) -ne $null) { $inv = " 可Invoke" } } catch {}
                if ($nm) { P ("     [" + $ct + "]" + $inv + " " + $nm) } else { P ("     [" + $ct + "]" + $inv) }
                $n++
            } catch {}
        }
    } catch { P ("     子树抄不动：" + $_.Exception.Message) }
}

P ("临时目录（里面只有我自己抄的一份样本）：" + $tmp)
$ids0 = @( @(Snap) | ForEach-Object { $_.Id } )
# 必须带 /n：不带的话 Win11 会把新文件夹开成**已有资源管理器窗口里的一个标签页**
# （实测第一版就是这么栽的：12 次轮询零新窗口，因为根本没有新窗口）。
Start-Process explorer.exe -ArgumentList ("/n," + $tmp)
$expl = $null
for ($i = 1; $i -le 14 -and -not $expl; $i++) {
    Start-Sleep -Milliseconds 900
    $now = @(Snap)
    $cand = @($now | Where-Object { ($ids0 -notcontains $_.Id) -and $_.Class -eq "CabinetWindow" })
    if ($cand.Count -eq 0) {
        # 退一步：按标题认（/n 有时复用宿主窗口，标题会换成这个临时文件夹名）
        $cand = @($now | Where-Object { $_.Class -eq "CabinetWindow" -and $_.Name -like ("*" + (Split-Path -Leaf $tmp) + "*") })
    }
    P ("等第 " + $i + " 次：新 CabinetWindow " + $cand.Count + " 扇；现有 CabinetWindow " +
       (@($now | Where-Object { $_.Class -eq "CabinetWindow" }).Count) + " 扇")
    if ($cand.Count -gt 0) { $expl = $cand[0] }
}
if (-not $expl) { P "没按「新窗口/标题」认出资源管理器窗口，继续往下：改到所有 CabinetWindow 里直接找那个文件" }
else { P ("资源管理器窗口：" + $expl.Name + " / " + $expl.Class + " / " + $expl.Rect) }

# 选中那个文件，然后按菜单键把上下文菜单叫出来（键盘比鼠标准）
$item = $null
for ($i = 1; $i -le 10 -and -not $item; $i++) {
    Start-Sleep -Milliseconds 700
    # 在所有 CabinetWindow 里找（/n 有时还是复用宿主，只认第一扇会漏）
    foreach ($cw in @( @(Snap) | Where-Object { $_.Class -eq "CabinetWindow" })) {
        try {
            $all = $cw.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
            foreach ($e in $all) {
                try { if ($e.Current.Name -like "probe-sample*") { $item = $e; $expl = $cw; break } } catch {}
            }
        } catch {}
        if ($item) { break }
    }
    P ("找列表项第 " + $i + " 次：" + $(if ($item) { "找到了" } else { "还没有" }))
}
if (-not $item) { P "!! 列表项没找到，探针结束"; $L | Out-File $out -Encoding UTF8; Write-Output ("WROTE " + $out); exit 1 }
P ("列表项类型=" + ($item.Current.ControlType.ProgrammaticName) + " 矩形=" + $item.Current.BoundingRectangle)
try { $sel = $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern); $sel.Invoke() ; P "已 SelectionItemPattern.Select()" } catch { P ("Select 失败：" + $_.Exception.Message) }
try { [void]$kb::SetForegroundWindow($expl.AE.Current.NativeWindowHandle) } catch {}
Start-Sleep -Milliseconds 500
P ("当前前台窗口句柄=" + [int64]$kb::GetForegroundWindow() + " 期望=" + $expl.AE.Current.NativeWindowHandle)
$VK_APPS = 0x12
$kb::keybd_event($VK_APPS, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 120
$kb::keybd_event($VK_APPS, 0, 2, [UIntPtr]::Zero)
P "已按菜单键 VK_APPS"
$ids1 = @( @(Snap) | ForEach-Object { $_.Id } )
$fresh = @()
for ($i = 1; $i -le 8; $i++) {
    Start-Sleep -Milliseconds 800
    $fresh = @( @(Snap) | Where-Object { ($ids1 -notcontains $_.Id) })
    P ("菜单轮询第 " + $i + " 次：新窗口 " + $fresh.Count + " 扇（全进程，没排噪声）")
    if ($fresh.Count -gt 0) { break }
}
foreach ($w in $fresh) { Tree $w 220 }
$kb::keybd_event(0x1B, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 100   # ESC
$kb::keybd_event(0x1B, 0, 2, [UIntPtr]::Zero)
try { [void]$kb::PostMessage } catch {}
$L | Out-File $out -Encoding UTF8
Write-Output ("WROTE " + $out)
# 收尾：只关我们自己开的那扇 explorer，删掉临时目录
try { $expl.AE.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close() } catch { P ("关 explorer 窗口失败：" + $_.Exception.Message) }
Start-Sleep -Milliseconds 600
try { Remove-Item -Recurse -Force $tmp -ErrorAction Stop; P ("临时目录已删：" + $tmp) } catch { P ("临时目录没删掉（自己看一眼）：" + $_.Exception.Message) }
$L | Out-File $out -Encoding UTF8
Write-Output ("收尾完，最终留档 " + $out)
