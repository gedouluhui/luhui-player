"""设计令牌与全局样式（石墨绿 + 暖象牙 + 芦荟绿）。

这不是"换一组色号"：上一版把**主色当主按钮底色**，又在页签、开关、进度条上重复铺满，
整窗像一套默认控件换了蓝色。这一版把颜色分成三层：

  · 表面层（BG / BG_RAISE / BG_HOVER / BG_ACTIVE）——近黑、带一点绿灰，靠明度分层；
  · 文字层（TEXT / TEXT_DIM / TEXT_FAINT）——暖象牙白，不是纯白，长时间看不刺眼；
  · 信号层（ACCENT / PRIMARY_FILL）——**主色只做状态**（选中、进度、开关、当前条目），
    主操作（打开、确认）用象牙色实心块压深墨字。一块屏幕上同时出现大面积彩色底，
    是"廉价"最直接的来源；把彩色收成一枚瘦进度条和一两个小点，画面才有呼吸。

三条容易踩的规矩，都在这文件里踩过：
  1. QSS 的注释只认 /* */，写 # 会被当成一个 ID 选择器，解析器从那一行起放弃整张表
     ——表现是"主题静默失效、控件回到 Fusion 默认灰"，比报错更难查。
  2. 主色必须在运行时生效：qss() 里的 {ACCENT} 是**构建字符串那一刻**的模块全局值，
     所以 set_accent() 要先改全局再重新生成并 setStyleSheet，光改配置文件没用。
  3. 由 2 派生：qss() 整段是 f-string，所以**注释里也不能出现裸的花括号**
     （写 "QScrollBar:vertical{background:...}" 会被当成替换字段，import 就 NameError）。
     要引用规则就只写选择器名，或者把花括号写成 {{ }}。
"""
from __future__ import annotations

import os
import sys
import weakref
from dataclasses import dataclass

from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPalette

# ---- 表面 ----
BG = "#0B0D0C"            # 窗口底：比纯黑暖一点，视频画面四周不会显得发蓝
BG_DEEP = "#080A09"       # 标题栏/控制条：比窗口再深一档，把画面夹在中间
BG_RAISE = "#121513"      # 抬起的面板（菜单、提示、空状态回执）
BG_HOVER = "#1A1E1B"
BG_ACTIVE = "#242925"
BORDER = "#2B322C"
BORDER_SOFT = "#191E1A"
SIDEBAR_BG = "#0E110F"    # 侧栏：与窗口同族但略亮，靠一条左侧发丝线分开

# ---- 文字 ----
TEXT = "#F2EFE6"          # 暖象牙：正文与标题
TEXT_DIM = "#A8AFA5"      # 次要：读数、说明、未选中页签
# 更弱：占位、禁用、拖拽提示。这个值不是随手挑的：媒体库 10px 元数据、播放列表序号、
# 空状态提示都拿它当**正文**读，旧值 #727A70 在悬停底上只有 3.80:1（AA 要 4.5）。
# 现值的对比度：窗口底 6.07 / 侧栏 5.91 / 悬停底 5.25 / 选中底 4.61 / 面板 5.72。
TEXT_FAINT = "#8A9286"

# ---- 信号 ----
ACCENT_DEFAULT = "#8BC9A3"      # 芦荟绿
DANGER = "#E06A64"
OK = "#8BC9A3"

# 主操作：一块象牙色实心。为什么不做成主色底：
# 六个预设色当大面积底时，饱和度和明度都会把画面拉回"彩色软件"那一档；
# 而象牙色是这套文字色的延长，按在深底上像一块被灯打亮的字幕板，安静但明确。
PRIMARY_FILL = "#F2EFE6"
PRIMARY_FILL_HOVER = "#FFFFFF"
PRIMARY_FILL_DOWN = "#D9D3C3"
PRIMARY_INK = "#10130E"         # 象牙底上的字与图标：近黑带绿，不用纯黑

# 主题色的可选值：设置页从这一份取，别在 UI 里另抄一遍色号。
# 六支都压过明度、带一点灰：它们只用来做状态，不铺大面积，所以可以比品牌色更克制。
ACCENTS: tuple[tuple[str, str], ...] = (
    ("#8BC9A3", "芦荟"),
    ("#D2B27C", "香槟"),
    ("#8FB3D9", "雾蓝"),
    ("#C99AAE", "藕荷"),
    ("#A79BD9", "鸢尾"),
    ("#B9BDB4", "石墨"),
)

# 上一版的六支主色：老配置里存的是这些值。迁移到同色相的新档，而不是让用户
# 打开设置页看见"当前 #4C8DFF"这种像坏了一样的兜底项。
LEGACY_ACCENTS: dict[str, str] = {
    "#4C8DFF": "#8FB3D9",
    "#7C5CFF": "#A79BD9",
    "#12B886": "#8BC9A3",
    "#F76707": "#D2B27C",
    "#ED6A9C": "#C99AAE",
    "#A6ABB8": "#B9BDB4",
}


def normalize_accent(color: str) -> str:
    """老色号 → 这一版同色相的档位；非老色号原样返回。"""
    key = str(color or "").strip().upper()
    return LEGACY_ACCENTS.get(key, str(color or ""))


def is_accent(color: str) -> bool:
    """能不能当主色用：既要在预设里，也要 QColor 认。"""
    c = normalize_accent(color)
    return any(str(c).upper() == v.upper() for v, _ in ACCENTS) and QColor(c).isValid()

# 这几份是运行时可变的"同一个主色的三种用途"（set_accent 一次算全），其余令牌是常量
ACCENT = ACCENT_DEFAULT          # 原色：状态图形件（进度条、滑块、选中下划线、logo）
ACCENT_FILL = "#4E705B"          # 当**底色**铺白字用（开关"开"那一档；对比度闸门压深的绿）
ACCENT_FILL_HOVER = "#567B64"    # 上面那个的悬停档，仍保证白字达标
ACCENT_FILL_DOWN = "#48694F"     # 按下档：必须比静态档更深，"按下去了"不能靠猜
ACCENT_TEXT = "#8BC9A3"          # 当**文字/细线**用在深色板上（选中页签、当前曲目）

RADIUS = {"window": 14, "panel": 14, "card": 10, "chip": 8, "pill": 999}

# 滑轨的两件尺寸：槽厚 + 把手直径。这两个数在三个地方互相押：
#   ① qss() 的 groove 高、② qss() 的 handle margin（= −(直径−槽厚)/2）、
#   ③ form_rows 给滑轨控件设的最小高度（把手画不出控件矩形以外）。
# 槽取偶数 2：-(14-2)/2 = -6，把手内容区正好 14×14，border-radius 7 才是一颗正圆。
# 取 3 时 margin 只能落到 -5（QSS 不吃半像素），高 13、半径 7 > 高/2，
# 实测 Qt 会直接退回方角——屏幕上那排白方块就是这么来的。
GROOVE_H = 2
SLIDER_HANDLE = 14

# 布尔项的开关（轨道的宽高 + 描边）。轨道尺寸必须和 packaging/make_ui_assets.py 里
# 画把手用的 SW_W/SW_H 同一对数：那两张 PNG 是"整条轨道内容区"的画布，Qt 把 image 缩放
# 进内容区时按等比铺，两边数字一旦不同，把手就会偏心或被裁。
SWITCH_W, SWITCH_H, SWITCH_BORDER = 38, 22, 1


@dataclass
class Metrics:
    title_h = 54
    bar_h = 84
    scrub_h = 32
    side_w = 300
    btn = 36
    btn_s = 30
    gutter = 18
    big_play = 48


M = Metrics()

# 三套字体。Qt 的 QSS 认 CSS 字体栈，QFont 不认——代码里自绘的地方用下面三个
# 构造函数拿同一族，别在委托里再抄一遍字体名（抄了就会和样式表漂开）。
FONT_UI = '"Noto Sans SC", "Microsoft YaHei UI", "Segoe UI Variable Text", sans-serif'
FONT_DISPLAY = '"Noto Serif SC", "Source Han Serif SC", "Noto Serif CJK SC", serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "Lucida Console", monospace'


def _qfont(families: list[str], px: int, weight: int = 400) -> QFont:
    f = QFont()
    f.setFamilies(families)
    f.setPixelSize(int(px))
    f.setWeight(int(weight))
    return f


def font_ui(px: int, weight: int = 400) -> QFont:
    return _qfont(["Noto Sans SC", "Microsoft YaHei UI", "Segoe UI Variable Text"], px, weight)


def font_display(px: int, weight: int = 500) -> QFont:
    return _qfont(["Noto Serif SC", "Source Han Serif SC"], px, weight)


def font_mono(px: int, weight: int = 400) -> QFont:
    return _qfont(["Cascadia Mono", "Consolas", "Lucida Console"], px, weight)


def _lin(hexs: str) -> float:
    """相对亮度（WCAG 定义：逐通道 gamma 线性化后加权）。"""
    c = QColor(hexs)
    ch = []
    for v in (c.redF(), c.greenF(), c.blueF()):
        ch.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def contrast(fg: str, bg: str) -> float:
    """两色对比度（1~21）。放在主题这边而不是只放测试里，是因为**派生令牌要用同一把尺**：
    set_accent 按对比度往下找填充色、往上找文字色，检查用的又是另一套算法的话，
    "算出来达标"和"检查说达标"会各说各话。"""
    a, b = _lin(fg), _lin(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


WHITE = "#FFFFFF"
FILL_REST = 5.2     # 主色当**底色**铺白字时，静态档要的余量
FILL_HOVER = 4.5    # 悬停亮一档之后不许跌破的线
TEXT_MIN = 4.5      # 主色当**文字**用时要求的对比（按它最常落的那块底 BG_ACTIVE 算）


def _fit_fill(accent: str) -> tuple[str, str]:
    """(静态填充, 悬停填充)：从用户选的主色出发，往**深**走到达标为止。

    为什么不直接把主色铺上去再写白字：主色是"身份色"，六预设里有三支铺白字
    只有 2.7~3.6:1，12px 的字压在上面就是"看得见有东西、看不清写的什么"。
    往深走而不是往浅走，是因为要保住的是**色相**：加深只是把亮度压下去，
    换成补色或加黑洗成灰会让六种主题色看起来像同一个。
    """
    c = QColor(accent)
    for _ in range(40):
        if contrast(WHITE, c.name()) >= FILL_REST:
            break
        c = c.darker(106)
    rest = c.name()
    h = QColor(rest)
    for _ in range(40):
        nxt = h.lighter(105)
        if contrast(WHITE, nxt.name()) < FILL_HOVER:
            break
        h = nxt
    return rest, h.name()


def _fit_text(accent: str, bg: str = BG_ACTIVE) -> str:
    """主色当文字/细线用：往**亮**走到达标（深底上压深字是那三支主色的另一面）。

    `bg` 给的是"这行字最常落的那块底"：主色文字落在选中条上（BG_ACTIVE），
    而警告文字落在面板上（BG_RAISE）。拿错底就会算出一个**看着达标、屏幕上不达标**的色号。
    """
    c = QColor(accent)
    for _ in range(40):
        if contrast(c.name(), bg) >= TEXT_MIN:
            break
        c = c.lighter(108)
    return c.name()


# 危险色的三份用途，和主色那几份是同一个道理（见 `set_accent`）：
# `DANGER` 是**身份色**——关闭按钮 hover 那整块底铺白字；
# `DANGER_DOWN` 是按下档（同一色相压深，否则按下和悬停看起来一样）；
# `DANGER_TEXT` 是**读得清**那一份——快捷键冲突红字、滤镜被内核扔掉的警告行。
DANGER_DOWN = QColor(DANGER).darker(116).name()
DANGER_TEXT = _fit_text(DANGER, BG_RAISE)


def switch_path(on: bool) -> str:
    """开关把手图片在盘上的路径（正斜杠，QSS 认这个）；文件不在则返回空串。

    QSS 一旦点名 `QCheckBox::indicator`，Qt 就不画原生那一样东西了，而 QSS 里画自己的
    图形只有 `image: url(盘上的文件)` 一条路。开关必须**两张**：把手要"关在左、开在右"，
    而 QSS 没有把 image 在 indicator 里左右挪的写法，所以左右两颗分别由
    `packaging/make_ui_assets.py` 画在各自画布上。
    """
    return _asset("switch-on.png" if on else "switch-off.png")


def _bundle_base() -> str:
    """只读资源根：源码运行时是项目根，打包后是 sys._MEIPASS（onedir 的 _internal/）。

    不能拿 `__file__` 往上跳：冻结后模块 __file__ 指向哪一层由打包器决定，
    而 _MEIPASS 是 Qt 资源与 fonts/ui 图片共同的、有保证的根。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(getattr(sys, "_MEIPASS", "")
                              or os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _asset(name: str) -> str:
    path = os.path.normpath(os.path.join(_bundle_base(), "packaging", "assets", "ui", name))
    return path.replace("\\", "/") if os.path.isfile(path) else ""


# 随包字体：界面在没装 Noto 的机器上会退回微软雅黑/通用衬线，字号与字重都对不上。
# 字体是 SIL OFL 1.1，允许随应用分发（许可证放在同目录 OFL.txt）。
_FONT_FILES: tuple[tuple[str, str], ...] = (
    ("Noto Sans SC", "NotoSansSC-Regular.otf"),
    ("Noto Sans SC", "NotoSansSC-Medium.otf"),
    ("Noto Sans SC", "NotoSansSC-Bold.otf"),
    ("Noto Serif SC", "NotoSerifSC-Regular.ttf"),
    ("Noto Serif SC", "NotoSerifSC-Medium.ttf"),
)


def install_fonts() -> int:
    """把随包字体注册进 Qt；返回真正新注册的文件数。

    先按家族整体判断：本机已装该族时，同一族的所有字重都不再注册——既省启动时解析
    50MB 字体，也避免"应用里只有 Regular、标题的 Medium/Bold 被合成出来"。
    打包机上通常没有 Noto，就会把三个字重 + 衬线变量字体全装上。
    单个字体坏掉不许把窗口带倒——界面退回字体栈里的后备族仍可读。
    """
    have = set(QFontDatabase().families())
    groups: dict[str, list[str]] = {}
    for family, filename in _FONT_FILES:
        groups.setdefault(family, []).append(filename)
    added = 0
    root = os.path.join(_bundle_base(), "packaging", "assets", "fonts")
    for family, names in groups.items():
        if family in have:
            continue
        for filename in names:
            path = os.path.join(root, filename)
            if not os.path.isfile(path):
                continue
            try:
                if QFontDatabase.addApplicationFont(path) >= 0:
                    added += 1
            except Exception:
                pass
    return added


def _img_decl(path: str) -> str:
    """把裸路径拼成一条 QSS 声明；路径为空就返回空串（宁可不画把手也不把主题带崩）。"""
    return f'image: url("{path}");' if path else ""


def palette() -> QPalette:
    """给 Qt 自己画的那些零碎准备一份深色调色板。

    为什么 QSS 之外还要这一份：下拉框与自旋框的箭头、焦点框、占位文字、列表选中高亮，
    走的是 **QPalette**，QSS 点不到它们。不覆盖就会出现"深色界面里混着浅色主题的零件"。
    """
    pal = QPalette()
    put = pal.setColor
    put(QPalette.Window, QColor(BG))
    put(QPalette.Base, QColor(BG_RAISE))
    put(QPalette.AlternateBase, QColor(BG_HOVER))
    put(QPalette.ToolTipBase, QColor(BG_ACTIVE))
    put(QPalette.ToolTipText, QColor(TEXT))
    put(QPalette.Text, QColor(TEXT))
    put(QPalette.WindowText, QColor(TEXT))
    put(QPalette.Button, QColor(BG_RAISE))
    put(QPalette.ButtonText, QColor(TEXT))
    put(QPalette.BrightText, QColor(WHITE))
    put(QPalette.Highlight, QColor(ACCENT_FILL))
    put(QPalette.HighlightedText, QColor(WHITE))
    put(QPalette.Link, QColor(ACCENT_TEXT))
    put(QPalette.PlaceholderText, QColor(TEXT_FAINT))
    for grp in (QPalette.Disabled,):
        put(grp, QPalette.Text, QColor(TEXT_FAINT))
        put(grp, QPalette.WindowText, QColor(TEXT_FAINT))
        put(grp, QPalette.ButtonText, QColor(TEXT_FAINT))
    return pal


def install(app, accent: str) -> None:
    """装配层唯一该记的三步：随包字体、深色调色板、主色样式表。"""
    install_fonts()
    sheet = set_accent(str(accent))          # 先改令牌
    app.setPalette(palette())               # 再让调色板读到改过的 ACCENT_FILL
    app.setStyleSheet(sheet)


# 主色是**全局令牌**，而 QSS 只管得了样式表能画到的那一半：QIcon 的像素、自绘文字、
# inline stylesheet 都不在它的管辖内。设置页换色时逐个去问每个控件"你刷新了吗"很容易
# 漏（漏了就是"只有底色变了"）；这里反过来让关心主色的控件登记一个回调。
# 绑定方法用弱引用：窗口/控件销毁后钩子自动作废，不许把整棵 QObject 树吊在模块级。
_ACCENT_HOOKS: list[tuple[str, object]] = []


def on_accent(fn) -> None:
    """登记"主色变了"的回调（无参）。同一个对象重复登记不会去重，登记方自己保证一次。"""
    try:
        ref = weakref.WeakMethod(fn)
    except TypeError:
        _ACCENT_HOOKS.append(("strong", fn))
    else:
        _ACCENT_HOOKS.append(("weak", ref))


def notify_accent() -> None:
    """主色落地之后叫一遍所有登记过的回调；单个回调炸了不许影响别家。"""
    alive: list[tuple[str, object]] = []
    for kind, ref in list(_ACCENT_HOOKS):
        fn = ref() if kind == "weak" else ref
        if fn is None:
            continue
        alive.append((kind, ref))
        try:
            fn()
        except Exception:
            pass
    _ACCENT_HOOKS[:] = alive


def set_accent(color: str) -> str:
    """改主色令牌并返回新的全局样式表；调用方负责 app.setStyleSheet(返回值)。

    一个主色在这里会派生出**三份**用途不同的值（原色/填充/文字），因为"色相身份"和
    "铺上去还得读得清"是两件事。
    """
    global ACCENT, ACCENT_FILL, ACCENT_FILL_HOVER, ACCENT_FILL_DOWN, ACCENT_TEXT
    color = normalize_accent(color)
    c = QColor(color or ACCENT_DEFAULT)
    if not c.isValid():
        c = QColor(ACCENT_DEFAULT)
    ACCENT = c.name()
    ACCENT_FILL, ACCENT_FILL_HOVER = _fit_fill(ACCENT)
    # 按下档从填充档**往深**走一档：Qt 的 QSS 没有 darken()，六档主色各自压深才是
    # "同一色相、按下去更实"；直接复用 HOVER 会让按下看起来像悬停。
    ACCENT_FILL_DOWN = QColor(ACCENT_FILL).darker(108).name()
    ACCENT_TEXT = _fit_text(ACCENT)
    return qss()


def qss() -> str:
    sw_off = _img_decl(switch_path(False))
    sw_on = _img_decl(switch_path(True))
    chevron_down = _img_decl(_asset("chevron-down.png"))
    chevron_up = _img_decl(_asset("chevron-up.png"))
    chevron_right = _img_decl(_asset("chevron-right.png"))
    check = _img_decl(_asset("check.png"))
    return f"""
* {{ outline: none; font-family: {FONT_UI}; }}
QWidget {{ color: {TEXT}; font-size: 13px; background: transparent; }}
QToolTip {{
    background: {BG_RAISE}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 6px 10px; border-radius: 8px; font-size: 12px;
}}
/* 根节点不写 border-radius：无边框窗口的客户区被 chrome 扩到整个窗口矩形，
   圆角由 DWM 在窗口层裁（见 ui/chrome.py），CSS 再画一层只会和 DWM 的半径打架。 */
#Root {{ background: {BG}; border: 1px solid {BORDER_SOFT}; }}
#TitleBar {{ background: {BG_DEEP}; border-bottom: 1px solid {BORDER_SOFT}; }}
#DragRegion {{ background: transparent; }}
/* 品牌字与当前片名：品牌是"这家店叫什么"，片名是"现在在放什么"。
   两者中间隔一条竖向发丝线——不是装饰，是让两个信息不粘成一个长句。 */
#BrandTitle {{ color: {TEXT}; font-size: 12px; font-weight: 600; letter-spacing: 1.4px; }}
#BrandSep {{ background: {BORDER}; }}
#NowTitle {{ color: {TEXT_DIM}; font-size: 12px; letter-spacing: .2px; }}
#AppTitle {{ color: {TEXT_DIM}; font-size: 12px; letter-spacing: .3px; }}
/* 表单行的标题（「画面比例」「旋转」「左右镜像」……）：这一行**在讲什么**，是这一行的主信息，
   所以走正文色＋正文号。 */
#RowLabel {{ color: {TEXT}; font-size: 13px; }}
/* 状态读数（「滤镜链：format、hflip」「A-B：未设置」）：报的是内核**现在**怎样，
   和「这一项叫什么」的行标题不是一类信息。 */
#Status {{ color: {TEXT_DIM}; font-size: 13px; }}
#DocTitle {{ color: {TEXT}; font-size: 14px; font-weight: 600; letter-spacing: .2px; }}
/* 对话框里真正的那行标题：比页内小标题大一档，和副标题（HeroHint）成对出现。 */
#DialogTitle {{ color: {TEXT}; font-size: 18px; font-weight: 600; letter-spacing: .2px; }}
/* 夹在控件行里的说明词（「帧率」）：只做定位，不和行标题争字号。 */
#InlineLabel {{ color: {TEXT_DIM}; font-size: 12px; }}
/* 对话框标题下的那一行解释：比 HeroHint 亮一档。它是"这一页怎么用"，
   不是空状态里可有可无的脚注；12px 的 faint 灰在大屏上读起来很吃力。 */
#DialogHint {{ color: {TEXT_DIM}; font-size: 12px; letter-spacing: .2px; }}
#PathEdit {{ font-family: {FONT_MONO}; font-size: 12px; }}
/* 对话框自己铺底：通用那条 `QWidget {{ background: transparent; }}` 只管子控件，
   顶层对话框不点名的话，那块背板靠的是 Qt 拿**调色板**填的后备缓冲。 */
QDialog {{ background: {BG}; }}
#ControlsBar {{ background: {BG_DEEP}; border-top: 1px solid {BORDER_SOFT}; }}
#ChromeHost {{ background: {BG_DEEP}; }}
#TimeCode {{ color: {TEXT_DIM}; font-family: {FONT_MONO}; font-size: 12px; letter-spacing: .4px; }}
#Scrubber {{ background: transparent; }}
#EmptyPage {{ background: {BG}; }}
/* 空状态上那句"这次为什么没放起来"：一条窄窄的回执，不是报错弹窗。
   左侧那道 2px 主色边是它唯一的彩色——比整块警告底安静得多。 */
#EmptyNotice {{ color: {TEXT_DIM}; font-size: 12px; background: {BG_RAISE};
                border: 1px solid {BORDER_SOFT}; border-left: 2px solid {ACCENT};
                border-radius: {RADIUS['card']}px; padding: 10px 14px; }}
/* 侧栏是一整块连续表面：面板自己不再各画一个带框的盒子（上一版 #Panel 的细边
   把侧栏切成"一堆卡片"，每一页看起来像另一套软件）。 */
#Sidebar {{ background: {SIDEBAR_BG}; border-left: 1px solid {BORDER_SOFT}; }}
#Panel {{ background: transparent; border: none; }}
#PanelTitle {{ color: {TEXT}; font-size: 15px; font-weight: 600; letter-spacing: .2px; }}
#PanelCount {{ color: {TEXT_FAINT}; font-size: 11px; font-family: {FONT_MONO};
              background: {BG_HOVER}; border-radius: {RADIUS['pill']}px; padding: 2px 8px; }}
/* 分组间距写在标签自己身上，而不是布局里插一条 addSpacing：统计页整段隐藏时，
   插进去的固定空白不会跟着藏，页面上会留下一条 60px 的"没有内容的缝"。 */
#SectionTitle {{ color: {TEXT_DIM}; font-size: 11px; font-weight: 600; letter-spacing: 1.5px;
                padding-top: 12px; }}
#EmptyTitle {{ color: {TEXT}; font-size: 15px; font-weight: 600; letter-spacing: .2px; }}
/* 首屏：衬线大字是唯一允许"大"的东西。它不是装饰，是把"这里什么都没放"说成一句邀请，
   而不是一块黑底上三个居中的小字。 */
#Eyebrow {{ color: {ACCENT_TEXT}; font-size: 11px; font-weight: 600; letter-spacing: 3px; }}
#HeroTitle {{ color: {TEXT}; font-family: {FONT_DISPLAY}; font-size: 40px; font-weight: 500;
              letter-spacing: 1px; }}
#HeroSub {{ color: {TEXT_DIM}; font-size: 13px; letter-spacing: .2px; }}
#HeroHint {{ color: {TEXT_FAINT}; font-size: 12px; letter-spacing: .2px; }}
/* 侧栏各页共用的滚动区：viewport 是 Qt 内部造的 QWidget，靠上面那条通用 QWidget
   规则匹配不稳，所以点名给它透明底＋无边框。滚动条不必单独上色——全局那一条
   已经把槽画成透明、把手画成低对比的发丝，压在连续侧栏面上不会切出一根浅柱。 */
#TweakScroll, #StatsScroll, #TransformScroll, #AudioEqScroll {{ background: transparent; border: none; }}
#TweakScroll > QWidget, #StatsScroll > QWidget, #TransformScroll > QWidget,
#AudioEqScroll > QWidget {{ background: transparent; }}
/* 统计页的读数：右对齐、等宽数字，避免一秒一刷时整列左右跳动 */
#StatValue {{ color: {TEXT}; font-size: 12px; font-family: {FONT_MONO}; }}
/* 页签：一条安静的文字导航，选中态用一道 2px 主色下划线，而不是一整块灰底胶囊。
   横向 padding 仍然是 0——左右留白由 main_window 的 `_layout_tabs()` 按"文字宽 + 2×留白"
   钉在每个按钮的固定宽度上；写在 QSS 里会多算一层 Qt 给 QToolButton 的 11px 松弛。 */
#SidebarTabs {{ background: transparent; border-bottom: 1px solid {BORDER_SOFT}; }}
#TabBtn {{ background: transparent; border: none; border-bottom: 2px solid transparent;
          color: {TEXT_FAINT}; font-size: 12px; font-weight: 500; padding: 10px 0px 8px; }}
#TabBtn:hover {{ color: {TEXT}; }}
#TabBtn:checked {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
/* 列表与网格的条目底由自绘委托负责（播放列表行、媒体库卡片）。QSS 只清理默认的
   选中块/左边框，否则它会从圆角卡片边上戳出来。 */
QListWidget, QListView {{ background: transparent; border: none; color: {TEXT}; font-size: 13px; }}
#LibraryGrid::item {{ padding: 0px; border: none; background: transparent; color: {TEXT_DIM}; }}
#LibraryGrid::item:hover, #LibraryGrid::item:selected {{ background: transparent; border: none; }}
#PlaylistList::item {{ padding: 0px; border: none; background: transparent; }}
#PlaylistList::item:hover, #PlaylistList::item:selected {{ background: transparent; border: none; }}
#CaptionBtn {{ background: transparent; border: none; border-radius: 8px; color: {TEXT_DIM}; }}
#CaptionBtn:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
#CaptionBtn:pressed {{ background: {BG_ACTIVE}; color: {TEXT}; }}
#CloseBtn:hover {{ background: {DANGER}; color: #FFFFFF; }}
#CloseBtn:pressed {{ background: {DANGER_DOWN}; color: #FFFFFF; }}
#ToolBtn {{ background: transparent; border: none; border-radius: {RADIUS['card']}px; color: {TEXT_DIM}; }}
#ToolBtn:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
#ToolBtn:pressed {{ background: {BG_ACTIVE}; }}
#ToolBtn:checked {{ background: {BG_ACTIVE}; color: {ACCENT_TEXT}; }}
#ToolBtn:disabled {{ color: {TEXT_FAINT}; }}
/* 主播放键：整条控制条唯一一块象牙色实心。它不是"更大的图标按钮"，是这一行的锚。 */
#BigPlay {{ background: {PRIMARY_FILL}; border: none; border-radius: {M.big_play // 2}px;
           color: {PRIMARY_INK}; }}
#BigPlay:hover {{ background: {PRIMARY_FILL_HOVER}; }}
#BigPlay:pressed {{ background: {PRIMARY_FILL_DOWN}; }}
#BigPlay:disabled {{ background: {BG_HOVER}; color: {TEXT_FAINT}; }}
/* 数值读数：一行里最常被扫的是右边这个数，所以给它等宽数字和一条细边，
   让它像仪表盘上的读数而不是一个可编辑输入框。底色只用 7/255 的暖白，够分层、
   又不会在每个滑轨右侧排出一列灰方块。 */
#Chip {{ background: rgba(242, 239, 230, 7); border: 1px solid rgba(242, 239, 230, 20);
        border-radius: 6px; color: {TEXT}; font-family: {FONT_MONO}; font-size: 12px;
        padding: 2px 8px; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(242, 239, 230, 34); border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(242, 239, 230, 78); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 6px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(242, 239, 230, 34); border-radius: 3px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: rgba(242, 239, 230, 78); }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; border: none; }}
QLineEdit {{
    background: {BG_HOVER}; border: 1px solid {BORDER_SOFT}; border-radius: {RADIUS['card']}px;
    padding: 7px 12px; color: {TEXT}; selection-background-color: {ACCENT_FILL};
    selection-color: {WHITE};
}}
QLineEdit:hover {{ border-color: {BORDER}; }}
QLineEdit:focus {{ border-color: {ACCENT}; background: {BG_ACTIVE}; }}
/* 媒体库/播放列表的搜索框是**胶囊**：它不是"填一个值"，是"在筛这一页"。 */
#SearchField {{ border-radius: {RADIUS['pill']}px; padding: 7px 14px; }}
/* 下拉：值本身是正文（TEXT），不是灰提示；右侧留出 26px 给那枚矢量小箭头。
   没有箭头时它看起来和只读文本框一模一样——"这里能点开"这件事不能只靠悬停才发现。 */
QComboBox {{
    background: {BG_HOVER}; border: 1px solid rgba(242, 239, 230, 16); border-radius: 8px;
    padding: 6px 26px 6px 10px; color: {TEXT}; font-size: 12px; min-height: 20px;
}}
QComboBox:hover {{ border-color: rgba(242, 239, 230, 40); background: {BG_ACTIVE}; }}
QComboBox:focus {{ border-color: {ACCENT}; background: {BG_ACTIVE}; }}
/* 媒体库「最近播放」档会把排序锁上：它是过滤而不是排序，留着能点等于撒谎。
   颜色必须点名，否则 QSS 里那条 `color: TEXT` 会盖过 QPalette 的禁用色。 */
QComboBox:disabled {{ color: {TEXT_FAINT}; background: transparent;
                     border-color: rgba(242, 239, 230, 10); }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ {chevron_down} width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {BG_RAISE}; border: 1px solid {BORDER}; border-radius: {RADIUS['chip']}px;
    color: {TEXT}; padding: 4px; outline: none;
}}
QComboBox QAbstractItemView::item {{
    min-height: 28px; padding: 2px 10px; border-radius: 6px;
}}
QComboBox QAbstractItemView::item:selected {{ background: {BG_ACTIVE}; color: {TEXT}; }}
QPushButton {{
    background: transparent; border: 1px solid {BORDER}; border-radius: {RADIUS['card']}px;
    padding: 8px 20px; color: {TEXT}; font-weight: 500;
}}
QPushButton:hover {{ background: {BG_HOVER}; border-color: {BORDER}; }}
QPushButton:pressed {{ background: {BG_ACTIVE}; }}
QPushButton#Ghost {{ background: transparent; border: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QPushButton#Ghost:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
QPushButton#Ghost:pressed {{ background: {BG_ACTIVE}; color: {TEXT}; }}
QPushButton#Primary {{ background: {PRIMARY_FILL}; border: 1px solid {PRIMARY_FILL};
                       color: {PRIMARY_INK}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {PRIMARY_FILL_HOVER}; border-color: {PRIMARY_FILL_HOVER}; }}
QPushButton#Primary:pressed {{ background: {PRIMARY_FILL_DOWN}; border-color: {PRIMARY_FILL_DOWN}; }}
/* 主按钮的禁用档必须点名 id：`QPushButton:disabled` 的特异度低于 `#Primary`。 */
QPushButton#Primary:disabled {{ background: {BG_HOVER}; border-color: {BORDER}; color: {TEXT_FAINT}; }}
/* 页脚/页头的小命令：比正文按钮矮一档，仍保留描边，和"读数胶囊"不是同一种东西。 */
QPushButton#PageAction {{
    background: transparent; border: 1px solid rgba(242, 239, 230, 26); border-radius: 8px;
    padding: 5px 12px; color: {TEXT_DIM}; font-size: 12px; font-weight: 500;
}}
QPushButton#PageAction:hover {{ background: {BG_HOVER}; border-color: rgba(242, 239, 230, 48); color: {TEXT}; }}
QPushButton#PageAction:pressed {{ background: {BG_ACTIVE}; }}
QPushButton#PageAction:disabled {{ background: transparent; border-color: rgba(242, 239, 230, 12); color: {TEXT_FAINT}; }}
QCheckBox, QRadioButton {{ spacing: 8px; color: {TEXT}; }}
/* 布尔项是一颗开关，不是打勾的方框。两态各用一张 PNG（把手在左/在右）。 */
QCheckBox::indicator {{ width: {SWITCH_W}px; height: {SWITCH_H}px;
                       border-radius: {SWITCH_H // 2}px; border: {SWITCH_BORDER}px solid {BORDER};
                       background: {BG_HOVER}; {sw_off} }}
QCheckBox::indicator:checked {{ background: {ACCENT_FILL}; border: {SWITCH_BORDER}px solid {ACCENT_TEXT};
                               {sw_on} }}
QCheckBox::indicator:hover {{ border-color: {TEXT_FAINT}; }}
QCheckBox::indicator:checked:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled, QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_FAINT}; }}
/* 禁用态不该是一块更亮的实心片：那会让「改键」「取消」这些不能点的按钮
   比能点的描边按钮更抢眼。留一道极淡的边、底色全透，退到背景里。 */
QPushButton:disabled {{ background: transparent; border-color: rgba(242, 239, 230, 10); }}
QCheckBox::indicator:disabled {{ background: {BG_RAISE}; border: {SWITCH_BORDER}px solid {BORDER_SOFT}; }}
QSpinBox, QDoubleSpinBox {{
    background: {BG_HOVER}; border: 1px solid rgba(242, 239, 230, 16); border-radius: 8px;
    padding: 4px 24px 4px 10px; color: {TEXT}; font-family: {FONT_MONO}; font-size: 12px;
    selection-background-color: {ACCENT_FILL};
}}
QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: rgba(242, 239, 230, 40); }}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; width: 20px; background: transparent; border: none;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ {chevron_up} width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ {chevron_down} width: 10px; height: 10px; }}
/* 导出进度条：只要一根 4px 的细轨，不画边框、不画百分比文字。 */
QProgressBar {{ background: {BG_HOVER}; border: none; border-radius: 2px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
/* 横向把手的实际高度 = groove 高 + 2×|上下 margin|，`height` 对横向把手不生效。
   margin 由 (SLIDER_HANDLE − GROOVE_H)/2 反推，radius 取直径一半。 */
QSlider::groove:horizontal {{ height: {GROOVE_H}px; background: {BORDER}; border-radius: {GROOVE_H // 2}px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: {GROOVE_H // 2}px; }}
QSlider::handle:horizontal {{
    width: {SLIDER_HANDLE}px; height: {SLIDER_HANDLE}px;
    margin: -{(SLIDER_HANDLE - GROOVE_H) // 2}px 0; border-radius: {SLIDER_HANDLE // 2}px;
    background: {TEXT};
}}
QSlider::handle:horizontal:hover {{ background: {PRIMARY_FILL_HOVER}; }}
QSlider::handle:horizontal:pressed {{ background: {PRIMARY_FILL_DOWN}; }}
/* 双极滑轨：零点在中间，两侧不铺主色——值为 0 时半根彩色轨道会被读成"已经加了一半"。 */
QSlider#BipSlider::sub-page:horizontal, QSlider#BipSlider::add-page:horizontal {{ background: transparent; }}
QSlider#BipSlider::groove:horizontal {{ height: {GROOVE_H}px; background: {BORDER}; border-radius: {GROOVE_H // 2}px; }}
QMenu {{ background: {BG_RAISE}; border: 1px solid {BORDER}; padding: 7px;
         border-radius: {RADIUS['panel']}px; min-width: 164px; }}
/* 左内边距不能按"离菜单边"来读：Qt 还会先让出 14px 的勾选列，34px 叠上去
   会让字从 56px 处才开始，短菜单看着像被推到了右边。18px 落在勾选列之后 ~7px。 */
QMenu::item {{ padding: 8px 30px 8px 18px; border-radius: 7px; color: {TEXT}; font-size: 13px; }}
QMenu::item:selected {{ background: {BG_ACTIVE}; }}
QMenu::item:checked {{ color: {ACCENT_TEXT}; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER_SOFT}; margin: 6px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 11px; }}
QMenu::indicator:checked {{ {check} }}
QMenu::right-arrow {{ {chevron_right} width: 12px; height: 12px; right: 11px; }}
QGroupBox {{ border: 1px solid {BORDER_SOFT}; border-radius: {RADIUS['panel']}px;
            margin-top: 14px; padding-top: 8px; color: {TEXT_DIM}; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
QTabBar::tab {{ background: transparent; padding: 8px 16px; color: {TEXT_DIM};
               border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QPlainTextEdit, QTextEdit {{ background: {BG}; border: 1px solid {BORDER_SOFT};
                            border-radius: {RADIUS['card']}px; color: {TEXT_DIM};
                            selection-background-color: {ACCENT_FILL}; selection-color: {WHITE}; }}
/* 快捷键表：没有这几条时表头和行底仍是 Fusion 的浅色零件，在深色窗口里像贴进来一张别的截图。 */
QTableView, QTableWidget {{ background: {BG_RAISE}; alternate-background-color: {BG_HOVER};
                          border: 1px solid {BORDER_SOFT}; border-radius: {RADIUS['card']}px;
                          gridline-color: {BORDER_SOFT}; color: {TEXT};
                          selection-background-color: {BG_ACTIVE}; selection-color: {TEXT}; }}
QTableView::item, QTableWidget::item {{ padding: 5px 9px; border: none; }}
QTableView::item:selected, QTableWidget::item:selected {{ background: {BG_ACTIVE}; color: {TEXT}; }}
QHeaderView::section {{ background: {BG_DEEP}; color: {TEXT_DIM}; border: none;
                        border-bottom: 1px solid {BORDER}; padding: 8px 10px;
                        font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QTableCornerButton::section {{ background: {BG_DEEP}; border: none; }}
QAbstractScrollArea::corner {{ background: transparent; }}
"""
