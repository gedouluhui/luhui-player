# 「打开方式」对话框可见性核对：把系统弹出来的那个列表里每一项的名字逐个抄下来，
# 看「LuHui」在不在。它证明的是用户那句话说的前半段——"根本都没有这个播放器的选项"。
#
# 为什么不能只截图目检：截图只能证明"有个列表"，证明不了"列表里那一行写的是谁"。
# 这里走 UI Automation（.NET 自带的 UIAutomationClient），把对话框子树里所有元素的
# Name 全抄进一个文本文件，再用字符串判定；截图只是留档，不是判据。
#
# 跑法（含中文，必须 -File，不许内联 -Command）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\e2e_openwith_dialog.ps1 [文件路径]
#
# 只读边界：这个脚本只"打开对话框 + 抄列表 + 按 ESC 关掉"。
# 不点任何一项、不勾「始终」、不动 UserChoice——那是要用户自己决定的默认程序。
param(
    [string]$Target = "",
    [string]$DumpDir = ""
)

$ErrorActionPreference = "Stop"
$AppName = "LuHui"                    # 列表里要出现的是这个（来自启动器的 FileDescription）
$Bogus   = "zzz绝不存在的程序名zzz"      # 反向对照：这条必须"找不到"，否则说明根本没在抄
$script:Fail = 0
$script:Env  = 0

function Say($tag, $what, $why) {
    if ($tag -eq "FAIL") { $script:Fail += 1 }
    if ($tag -eq "ENV")  { $script:Env  += 1 }
    if ($why) { Write-Output ("[{0}] {1} —— {2}" -f $tag, $what, $why) }
    else      { Write-Output ("[{0}] {1}" -f $tag, $what) }
}

# 150% 缩放的机器上，不声明 DPI 感知拿到的矩形是虚拟化的（和 e2e_openwith.py 同一课）
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$dpi = Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name U32Dpi -PassThru
try { [void]$dpi::SetProcessDPIAware() } catch {}

$here = $PSScriptRoot                       # tests 目录
$proj = Split-Path -Parent $here
if (-not $Target) { $Target = Join-Path (Join-Path $here "samples") "gelu-sample-3.mkv" }
$res = Resolve-Path $Target -ErrorAction SilentlyContinue
if (-not $res) { Say "ENV" "样本文件在不在" $Target; Write-Output "DONE"; exit 2 }
$Target = $res.Path
if (-not $DumpDir) { $DumpDir = Join-Path $here "out" }
if (-not (Test-Path $DumpDir)) { New-Item -ItemType Directory -Path $DumpDir | Out-Null }
$stamp = Get-Date -Format "MMdd-HHmmss"
$DumpPath = Join-Path $DumpDir ("e2e-openwith-dialog-" + $stamp + "-names.txt")
$PngPath  = Join-Path $DumpDir ("e2e-openwith-dialog-" + $stamp + ".png")

Say "NOTE" "对话框要针对这个文件弹" $Target
Say "NOTE" "列表名字抄到这里" $DumpPath

$root = [System.Windows.Automation.AutomationElement]::RootElement
$trueCond = [System.Windows.Automation.Condition]::TrueCondition

function TopWindows {
    # 返回当下所有顶层窗口，连 AutomationElement 一起留着（后面要拿它往下抄，
    # 只留 RuntimeId 的话还得按名字反查，名字会撞车、也可能空）
    $out = @()
    try {
        $all = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $trueCond)
        foreach ($w in $all) {
            try {
                $out += [pscustomobject]@{
                    Name      = $w.Current.Name
                    Class     = $w.Current.ClassName
                    Rect      = $w.Current.BoundingRectangle
                    RuntimeId = ($w.GetRuntimeId() -join ",")
                    AE        = $w
                }
            } catch {}
        }
    } catch { Say "NOTE" "枚举顶层窗口时报" $_.Exception.Message }
    return $out
}

$before = @(TopWindows)
$beforeIds = @($before | ForEach-Object { $_.RuntimeId })
Say "NOTE" "弹之前的顶层窗口数" ("桌面现有 " + $before.Count + " 扇")

# ---- 弹对话框：先走 Shell 的 openas 那个动作，落空再走 rundll32 的 OpenAs_RunDLL ------------
$openedBy = ""
try {
    $shell = New-Object -ComObject Shell.Application
    $dir  = Split-Path -Parent $Target
    $leaf = Split-Path -Leaf   $Target
    $item = $shell.Namespace($dir).ParseName($leaf)
    if ($item) { $item.InvokeVerb("openas"); $openedBy = "Shell.Application InvokeVerb(openas)" }
} catch { Say "NOTE" "InvokeVerb(openas) 没走通" $_.Exception.Message }

function Wait-New($seconds) {
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt $seconds) {
        Start-Sleep -Milliseconds 700
        $now = @(TopWindows)
        $fresh = @($now | Where-Object { $beforeIds -notcontains $_.RuntimeId })
        if ($fresh.Count -gt 0) { return ,$fresh }
    }
    return ,@()
}

$fresh = @(Wait-New 8)
if ($fresh.Count -eq 0) {
    Say "NOTE" "InvokeVerb 八秒内没等到新窗口，改走 rundll32 shell32.dll,OpenAs_RunDLL"
    Start-Process -FilePath "rundll32.exe" `
        -ArgumentList @("shell32.dll,OpenAs_RunDLL", ("`"" + $Target + "`"")) `
        -WindowStyle Normal -ErrorAction SilentlyContinue
    $openedBy = "rundll32 shell32.dll,OpenAs_RunDLL"
    $fresh = @(Wait-New 15)
}
if ($fresh.Count -eq 0) {
    Say "ENV" "对话框弹出来了" "两条路都没等到新顶层窗口（桌面不给窗口，不是注册没到位）"
    Say "FINAL" "对话框可见性核对不作数" "环境不给窗口"
    Write-Output "DONE"
    exit 2
}
Say "PASS" "对话框弹出来了" ("走的是：" + $openedBy + "；新窗口 " +
    (($fresh | ForEach-Object { "'" + $_.Name + "'/" + $_.Class }) -join "、"))

# ---- 抄列表：把每个新窗口的整棵子树的 Name 抄下来 ------------------------------------------
$lines = @()
$lines += "弹法：" + $openedBy
$lines += "针对文件：" + $Target
$lines += "抄的时间：" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
$hitWhere = ""
$hitNames = @()
foreach ($w in $fresh) {
    $kids = @()
    try {
        $all = $w.AE.FindAll([System.Windows.Automation.TreeScope]::Descendants, $trueCond)
        foreach ($e in $all) {
            try {
                $ct = ($e.Current.ControlType.ProgrammaticName -split "\.")[-1]
                $nm = $e.Current.Name
                if ($nm) { $kids += ("[" + $ct + "] " + $nm) } else { $kids += ("[" + $ct + "]") }
            } catch {}
        }
    } catch { $kids += ("(这扇窗的子树抄不动：" + $_.Exception.Message + ")") }
    $lines += ("== 窗口 “" + $w.Name + "” 类名 " + $w.Class + " 矩形 " + $w.Rect +
               " —— 子元素 " + $kids.Count + " 个 ==")
    $lines += $kids
    foreach ($k in $kids) {
        if ($k -like ("*" + $AppName + "*")) { $hitWhere = $w.Name; $hitNames += $k }
    }
}
$lines | Set-Content -Path $DumpPath -Encoding UTF8

$total = @($lines | Where-Object { $_ -match "^\[" }).Count
Say "NOTE" "对话框子树里抄到的带类型元素" ("共 " + $total + " 个，全文见 " + $DumpPath)

# ---- 判据①：列表里要有我们那一项 ----------------------------------------------------------
if ($hitNames.Count -gt 0) {
    Say "PASS" ("列表里看得见「" + $AppName + "」这一项") `
        ("出现在窗口 “" + $hitWhere + "”：" + (($hitNames | Select-Object -Unique) -join "；"))
} else {
    Say "FAIL" ("列表里看不见「" + $AppName + "」这一项") `
        ("抄到 " + $total + " 个元素名，没有一个含这三个字，见 " + $DumpPath)
}

# ---- 判据②（反向对照）：那条绝不存在的名字必须"找不到"，同时名单不许是空的 ------------------
# 只有"名单非空 且 假名字找不到"两件事一起成立，才说明这条 grep 真的在抄东西，
# 而不是因为压根没抄到东西而假绿。
$bogusFound = @($lines | Where-Object { $_ -like ("*" + $Bogus + "*") })
if ($total -ge 5 -and $bogusFound.Count -eq 0) {
    Say "PASS" "反向对照：绝不存在的程序名没被抄到，而名单非空" ("元素 " + $total + " 个")
} else {
    Say "FAIL" "反向对照失败（名单空／假名字也被抄到＝这本账是瞎的）" `
        ("元素 " + $total + " 个，假名命中 " + $bogusFound.Count + " 条")
}

# ---- 留档截图（不是判据，只是给人看一眼那个列表长什么样）------------------------------------
try {
    $r = $fresh[0].Rect
    if ($r.Width -gt 20 -and $r.Height -gt 20) {
        $bmp = New-Object System.Drawing.Bitmap ([int]$r.Width), [int]$r.Height
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen([int]$r.X, [int]$r.Y, 0, 0, $bmp.Size)
        $bmp.Save($PngPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $g.Dispose(); $bmp.Dispose()
        Say "NOTE" "对话框像素留档" ($PngPath + "（" + ([int]$r.Width) + "×" + ([int]$r.Height) + "）")
    } else {
        Say "NOTE" "对话框矩形太小，没截" ($r.ToString())
    }
} catch { Say "NOTE" "截图这条腿没走通（只影响留档，不影响判据）" $_.Exception.Message }

# ---- 收尾：按 ESC 关掉对话框，一个程序都没选、默认程序一点没动 -------------------------------
try {
    [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
    Start-Sleep -Milliseconds 900
} catch { Say "NOTE" "ESC 没发出去" $_.Exception.Message }
$freshIds = @($fresh | ForEach-Object { $_.RuntimeId })
$still = @(@(TopWindows) | Where-Object { $freshIds -contains $_.RuntimeId })
if ($still.Count -eq 0) {
    Say "PASS" "收尾：对话框已关掉，全程没选任何一项、没勾「始终」，默认程序一个字没动"
} else {
    Say "NOTE" "ESC 之后窗口还在（不强行杀，交给人看一眼）" `
        ((($still | ForEach-Object { $_.Name }) -join "、"))
}

$rc = 0
if ($script:Fail -gt 0) { $rc = 1 } elseif ($script:Env -gt 0) { $rc = 2 }
$tag = "全绿"
if ($rc -eq 1) { $tag = "有真失败" } elseif ($rc -eq 2) { $tag = "环境不给" }
Say "FINAL" ("对话框可见性：" + $tag) ("FAIL " + $script:Fail + " 条／ENV " + $script:Env + " 条 rc=" + $rc)
Write-Output "DONE"
exit $rc
