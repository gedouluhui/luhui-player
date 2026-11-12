# 探针 v8（一次性，不算判据）：v7 已经认出窗口、还原、抢到前台，却在窗口子树里找不到
# 那个列表项（12 次轮询零命中）。这一步只干一件事：把已开着的这扇资源管理器的整棵 UIA 树
# 摊下来，看看文件项到底叫什么、挂在哪个控件类型下面。不点任何菜单，不动注册表。
param([string]$Leaf = "gelu-openwith-probe-1016-020710", [int]$Max = 600)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$stamp = Get-Date -Format "MMdd-HHmmss"
$out = Join-Path (Join-Path $here "out") ("probe-openwith-tree-" + $stamp + ".txt")
$L = New-Object System.Collections.ArrayList
function P($s) { [void]$L.Add($s); $L | Out-File $out -Encoding UTF8 }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$u32 = Add-Type -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
'@ -Name U32Probe8 -PassThru
[void]$u32::SetProcessDPIAware()
$root = [System.Windows.Automation.AutomationElement]::RootElement
$trueCond = [System.Windows.Automation.Condition]::TrueCondition

P "找标题里带「" + $Leaf + "」的 CabinetWClass"
$w = $null
foreach ($c in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)) {
    try {
        if ($c.Current.ClassName -eq "CabinetWClass" -and $c.Current.Name -like ("*" + $Leaf + "*")) {
            $h = [IntPtr]$c.Current.NativeWindowHandle
            if ($u32::IsIconic($h)) { [void]$u32::ShowWindow($h, 9); Start-Sleep -Milliseconds 800 }
            [void]$u32::SetForegroundWindow($h)
            $w = $c
            P ("认出：'" + $c.Current.Name + "' 句柄=" + [int64]$h + " 矩形=" + $c.Current.BoundingRectangle)
            break
        }
    } catch {}
}
if (-not $w) {
    P "!! 没找到那扇窗口。现在所有 CabinetWClass："
    foreach ($c in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)) {
        try { if ($c.Current.ClassName -eq "CabinetWClass") { P ("   '" + $c.Current.Name + "'") } } catch {}
    }
    exit 1
}
Start-Sleep -Milliseconds 800
try {
    $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
    P ("整棵子树 " + $all.Count + " 个元素，最多摊 " + $Max + " 个")
} catch {
    P ("FindAll(Descendants) 直接报：" + $_.Exception.Message)
    $all = $null
}
if ($all) {
    $n = 0
    foreach ($e in $all) {
        if ($n -ge $Max) { P "…（截断）"; break }
        try {
            $ct = ($e.Current.ControlType.ProgrammaticName -split "\.")[-1]
            $nm = $e.Current.Name
            $r = $e.Current.BoundingRectangle
            P ("[" + $ct + "] '" + $nm + "' @(" + [int]$r.X + "," + [int]$r.Y + ") 类=" + $e.Current.ClassName)
            $n++
        } catch { P ("(取属性就崩：" + $_.Exception.Message + ")") }
    }
}
exit 0
