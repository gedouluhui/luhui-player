# 探针 v9（一次性，不算判据）：v8 里那扇资源管理器**矩形正常、却没被最小化，UIA 子树只有
# 1 个元素**（标题栏脚手架，还在 -32284 屏外）。这不正常。这一步只判一件事：
# 到底是「我这趟进程的 UIA 全线看不见内容」，还是「只有 Explorer 这扇看不见」——
# 顺带量 DWMWA_CLOAKED（被别的虚拟桌面/挂起遮罩掉的窗口就是这种表现）。
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$stamp = Get-Date -Format "MMdd-HHmmss"
$out = Join-Path (Join-Path $here "out") ("probe-openwith-cloak-" + $stamp + ".txt")
$L = New-Object System.Collections.ArrayList
function P($s) { [void]$L.Add($s); $L | Out-File $out -Encoding UTF8 }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$u32 = Add-Type -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern int PutWindowTile(IntPtr h);
[DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out int v, int s);
'@ -Name U32Probe9 -PassThru
[void]$u32::SetProcessDPIAware()
$root = [System.Windows.Automation.AutomationElement]::RootElement
$trueCond = [System.Windows.Automation.Condition]::TrueCondition

P ("DWMWA_CLOAKED 的返回值：0=没遮 1=被别的虚拟桌面 cloak 2=挂起 cloak 4=实际不可见")
foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)) {
    try {
        $h = [IntPtr]$w.Current.NativeWindowHandle
        if ($h -eq [IntPtr]::Zero) { continue }
        $cl = 0; [void]$u32::DwmGetWindowAttribute($h, 14, [ref]$cl, 4)
        $ct = -1
        try { $ct = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond).Count }
        catch { $ct = -99 }
        $nm = $w.Current.Name
        $cn = $w.Current.ClassName
        if ($cn -eq "CabinetWClass" -or $ct -gt 0 -or $cl -ne 0) {
            P ("类=" + $cn + " cloak=" + $cl + " iconic=" + $u32::IsIconic($h) + " UIA子元素=" + $ct +
               " 矩形=" + $w.Current.BoundingRectangle + " 名='" + $nm + "'")
        }
    } catch {}
}
P "—— 用 UIA 从桌面根直接按名字找那个文件项 ——"
try {
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "probe-sample.mkv")
    $e = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    if ($e) { P ("找到了：类=" + $e.Current.ClassName + " 矩形=" + $e.Current.BoundingRectangle) }
    else { P "桌面上没有名字叫 probe-sample.mkv 的元素" }
} catch { P ("按名字找报错：" + $_.Exception.Message) }
try {
    $cond2 = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, "LuHui播放器")
    $e2 = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond2)
    if ($e2) { P ("顺带：桌面上有标题「LuHui播放器」的窗口（说明 App 还开着）") } else { P "顺带：桌面上没有标题「LuHui播放器」的窗口（端到端那趟已收干净）" }
} catch {}
exit 0
