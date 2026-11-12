# 探针 v7（一次性，不算判据）：门还是那四扇（openas 动词 / OpenAs_RunDLL / SHOpenWithDialog /
# OpenWith.exe），在这台 26300 上实测全哑。只剩最笨也最真的一条：真开一扇资源管理器，
# 选中文件，按菜单键把上下文菜单叫出来，再用 UIA 把「打开方式」那一层摊下来。
#
# 安全性：目标文件是**临时目录里自己抄的一份**（probe-sample.mkv），不在用户任何真实目录里，
# 菜单里「删除/重命名/剪切」就算点歪，最多毁掉我自己那份抄本。全程不写注册表。
#
# v6 为什么白跑：过滤器写的是 "CabinetWindow"，而资源管理器真正的窗口类是 **CabinetWClass**
# （ctypes 枚举桌面窗口时看到的），14 次轮询当然次次 0 扇；而且 /n 开出来的那两扇是
# 最小化在 (-32000,-32000) 的，就算认出来了也拿不到能用的矩形。v7 两处都改。
param([string]$Sample = "", [int]$MaxNodes = 400)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $Sample) { $Sample = Join-Path (Join-Path $here "samples") "gelu-sample-3.mkv" }
$Sample = (Resolve-Path $Sample).Path
$stamp = Get-Date -Format "MMdd-HHmmss"
$tmp = Join-Path $env:TEMP ("gelu-openwith-probe-" + $stamp)
New-Item -ItemType Directory -Path $tmp | Out-Null
Copy-Item $Sample (Join-Path $tmp "probe-sample.mkv")
$leaf = Split-Path -Leaf $tmp
$out = Join-Path (Join-Path $here "out") ("probe-openwith-explorer-" + $stamp + ".txt")
$L = New-Object System.Collections.ArrayList
function P($s) { [void]$L.Add($s); $L | Out-File $out -Encoding UTF8 }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
$u32 = Add-Type -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
'@ -Name U32Probe7 -PassThru
[void]$u32::SetProcessDPIAware()
$SW_RESTORE = 9
function Tap([byte]$vk) {
    $u32::keybd_event($vk, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 90
    $u32::keybd_event($vk, 0, 2, [UIntPtr]::Zero)
}

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
                Class = $w.Current.ClassName; Rect = $w.Current.BoundingRectangle; Proc = $pn; Pid = $p
                H = [IntPtr]$w.Current.NativeWindowHandle; AE = $w }
        } catch {}
    }
    return $o
}
function Tree($w, $max) {
    P ("  == 窗口 名='" + $w.Name + "' 类=" + $w.Class + " 进程=" + $w.Proc + "/" + $w.Pid +
       " 句柄=" + [int64]$w.H + " 矩形=" + $w.Rect + " ==")
    try {
        $all = $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
        P ("     子元素 " + $all.Count + " 个")
        $n = 0
        foreach ($e in $all) {
            if ($n -ge $max) { P "     …（截断）"; break }
            try {
                $ct = ($e.Current.ControlType.ProgrammaticName -split "\.")[-1]
                $nm = $e.Current.Name
                $extra = ""
                try { if ($e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern) -ne $null) { $extra += " 可Invoke" } } catch {}
                try { if ($e.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern) -ne $null) { $extra += " 可展开" } } catch {}
                $r = $e.Current.BoundingRectangle
                if ($nm) { P ("     [" + $ct + "]" + $extra + " '" + $nm + "' @" + ($r.X.ToString("0") + "," + $r.Y.ToString("0"))) }
                else { P ("     [" + $ct + "]" + $extra + " @(" + ($r.X.ToString("0") + "," + $r.Y.ToString("0")) + ")") }
                $n++
            } catch {}
        }
    } catch { P ("     子树抄不动：" + $_.Exception.Message) }
}

P ("临时目录（里面只有我自己抄的一份样本）：" + $tmp)
$ids0 = @( @(Snap) | ForEach-Object { $_.Id } )
Start-Process explorer.exe -ArgumentList ("/n," + $tmp)

$expl = $null
for ($i = 1; $i -le 16 -and -not $expl; $i++) {
    Start-Sleep -Milliseconds 900
    $now = @(Snap)
    $cands = @($now | Where-Object { $_.Class -eq "CabinetWClass" -and $_.Name -like ("*" + $leaf + "*") })
    if ($cands.Count -eq 0) {
        $cands = @($now | Where-Object { ($ids0 -notcontains $_.Id) -and $_.Class -eq "CabinetWClass" })
    }
    P ("等窗口第 " + $i + " 次：认出 CabinetWClass " + $cands.Count +
       " 扇（本趟临时目录标题优先）；顶层窗口共 " + $now.Count + " 扇")
    if ($cands.Count -gt 0) { $expl = $cands[0] }
}
if (-not $expl) {
    P "!! 没认出资源管理器窗口，探针结束"
    P "现在所有 CabinetWClass："
    foreach ($w in @( @(Snap) | Where-Object { $_.Class -eq "CabinetWClass" })) { P ("   '" + $w.Name + "' " + $w.Rect) }
    exit 1
}
P ("认出窗口 '" + $expl.Name + "' 句柄=" + [int64]$expl.H + " 矩形（还原前）=" + $expl.Rect)

# 最小化的窗口矩形是 (-32000,...)，UIA 拿到也点不动：先还原再重取
if ($u32::IsIconic($expl.H)) {
    [void]$u32::ShowWindow($expl.H, $SW_RESTORE)
    P "本来是最小化 → 已 ShowWindow(SW_RESTORE)"
    Start-Sleep -Milliseconds 900
}
[void]$u32::SetForegroundWindow($expl.H)
Start-Sleep -Milliseconds 800
$expl = @( @(Snap) | Where-Object { $_.H -eq $expl.H } )[0]
if (-not $expl) { P "!! 还原后窗口认不回来了，探针结束"; exit 1 }
P ("矩形（还原后）=" + $expl.Rect + " 前台=" + [int64]$u32::GetForegroundWindow() + " 期望=" + [int64]$expl.H)

$item = $null
for ($i = 1; $i -le 12 -and -not $item; $i++) {
    Start-Sleep -Milliseconds 600
    foreach ($cw in @( @(Snap) | Where-Object { $_.Class -eq "CabinetWClass" })) {
        try {
            foreach ($e in $cw.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)) {
                try { if ($e.Current.Name -like "probe-sample*") { $item = $e; $expl = $cw; break } } catch {}
            }
        } catch {}
        if ($item) { break }
    }
    P ("找列表项第 " + $i + " 次：" + $(if ($item) { "找到了" } else { "还没有" }))
}
if (-not $item) { P "!! 列表项没找到，探针结束"; exit 1 }
P ("列表项 " + ($item.Current.ControlType.ProgrammaticName) + " 矩形=" + $item.Current.BoundingRectangle)
try {
    $sel = $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $sel.Invoke(); P "已 SelectionItemPattern.Select()"
} catch { P ("Select 失败：" + $_.Exception.Message) }
try { $item.SetFocus(); P "已 SetFocus 到列表项" } catch { P ("SetFocus 失败：" + $_.Exception.Message) }
[void]$u32::SetForegroundWindow($expl.H)
Start-Sleep -Milliseconds 600

$ids1 = @( @(Snap) | ForEach-Object { $_.Id } )
Tap 0x12
P "已按菜单键 VK_APPS(0x12)"
$fresh = @()
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Milliseconds 800
    $fresh = @( @(Snap) | Where-Object { ($ids1 -notcontains $_.Id) })
    P ("上下文菜单轮询第 " + $i + " 次：新顶层窗口 " + $fresh.Count + " 扇")
    if ($fresh.Count -gt 0) { break }
}
if ($fresh.Count -eq 0) { P "!! 按了菜单键没有任何新顶层窗口（菜单可能画在别的宿主里），探针结束"; exit 1 }
P "---- 第一层（上下文菜单本体）----"
foreach ($w in $fresh) { Tree $w $MaxNodes }

# 找「打开方式」那一项，展开它
$target = $null
foreach ($w in $fresh) {
    try {
        foreach ($e in $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)) {
            try {
                $nm = $e.Current.Name
                if ($nm -and ($nm -like "*打开方式*" -or $nm -like "*Open with*")) { $target = $e; break }
            } catch {}
        }
    } catch {}
    if ($target) { break }
}
if (-not $target) {
    P "!! 第一层里没有「打开方式」这一项，探针结束（上面已有整棵树，够判读）"
    Tap 0x1B
    exit 1
}
$tr = $target.Current.BoundingRectangle
P ("找到「" + $target.Current.Name + "」 " + ($target.Current.ControlType.ProgrammaticName) +
   " 矩形=(" + [int]$tr.X + "," + [int]$tr.Y + "," + [int]$tr.Width + "x" + [int]$tr.Height + ")")
$ids2 = @( @(Snap) | ForEach-Object { $_.Id } )
$opened = "无"
try {
    $ec = $target.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    $ec.Expand(); $opened = "ExpandCollapsePattern.Expand()"
} catch { $opened = "" }
if ($opened -ne "无" -and -not $opened.Contains("Expand()")) { $opened = "无" }
if ($opened -eq "无") {
    try {
        $iv = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $iv.Invoke(); $opened = "InvokePattern.Invoke()"
    } catch {}
}
if ($opened -eq "无") {
    # 键盘出路：把焦点给那一项，再按右箭头展开子菜单
    try { $target.SetFocus(); [void]$u32::SetForegroundWindow($expl.H); Tap 0x27; $opened = "SetFocus + 右箭头(0x27)" }
    catch { $opened = "三条路都没走通" }
}
P ("展开「打开方式」用的是：" + $opened)
$fresh2 = @()
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Milliseconds 800
    $fresh2 = @( @(Snap) | Where-Object { ($ids2 -notcontains $_.Id) })
    P ("子菜单轮询第 " + $i + " 次：又新出 " + $fresh2.Count + " 扇")
    if ($fresh2.Count -gt 0) { break }
}
P "---- 第二层（打开方式列出来的候选）----"
foreach ($w in $fresh2) { Tree $w $MaxNodes }

$hit = @()
foreach ($w in @($fresh) + @($fresh2)) {
    try {
        foreach ($e in $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)) {
            try { if ($e.Current.Name -like "*LuHui*") { $hit += ($w.Class + " :: " + $e.Current.Name) } } catch {}
        }
    } catch {}
}
P ("命中「LuHui」的项：" + $(if ($hit.Count) { $hit -join " | " } else { "零个" }))
Tap 0x1B
Tap 0x1B
P "已发两次 ESC 收菜单"
exit 0
