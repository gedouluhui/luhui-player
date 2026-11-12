# LuHui播放器（LuHui Player，原名 格斗芦荟播放器 / Gelu Player）设计规格

日期：2026-10-06 ｜ 更名：2026-11-11 起对外名称改为 LuHui播放器（LuHui Player），早于该日的实测记录保留当时的名字 ｜ 状态：已与用户确认（视觉=Fluent 深色+亚克力；节奏=三轮增量；系统集成=仅快捷方式，不写注册表）

## 1. 目标

一个 Windows 本地视频播放器，功能面覆盖并超过迅雷影音 / PotPlayer / Windows Media Player，
外观为现代 Fluent 深色 + 亚克力，性能上走 GPU 零拷贝硬解。只放本地文件与文件夹，不做流媒体抓取。

## 2. 选型依据（先审计环境后定案）

| 事实 | 影响 |
|---|---|
| 机器无 Node/npm，无 Electron，无 VS 里可用的 WebView2 SDK 线索 | 排除 Electron/Tauri 路线（还要额外装运行时） |
| Python 3.12（anaconda）+ PyQt5 5.15.2 已装且可用（QtWidgets/QtGui/QtOpenGL/QtSvg） | 外壳用 Qt 自绘，零新增依赖 |
| PyAV 17.0.1、numpy、Pillow 已装 | 可自合成测试样片，用于真机验收 |
| 下载源校验：mpv v0.41.0-1101-g5d85ba5fb3 与 FFmpeg N-127213 官方 CI 包，sha256 与 release 清单一致 | 内核可信、可复现 |
| `mpv --hwdec=help` 显示 d3d11va / nvdec / vulkan / dxva2 全列；`--gpu-api` 支持 d3d11 | RTX 3070 Ti Laptop 可走 d3d11va + D3D11 输出的硬解通路 |

**内核**：mpv.exe 子进程，`--wid=<Qt 视频区 HWND>` 嵌入（mpv 自己拥有渲染窗口 → 零拷贝、HDR/色调映射、
ASS 字幕、全格式解码都是 mpv 原生能力，不经过任何 Python 搬运像素）。
**控制面**：命名管道 JSON IPC（`\\.\pipe\gelu-<pid>`），属性订阅 + 异步命令。
**辅助进程**：ffmpeg.exe 用于进度条缩略图与媒体探测；ffprobe 不单独依赖。
**外壳**：PyQt5，无边框窗口（保留原生阴影/圆角/贴边快照）+ 自绘标题栏/控制条/面板。

## 3. 关键机制约束（已确认，非猜测）

1. **airspace 约束**：mpv 的窗口是视频区容器 HWND 的原生子窗口，原生子窗口永远盖在 Qt 绘制的父表面之上。
   因此视频区内不能直接放 Qt 控件。浮在视频上的东西（浮动控制条、OSD 提示、右键菜单、音量气泡）
   一律用 **owner 顶层窗口**（Qt.FramelessWindowHint + WA_TranslucentBackground + WindowDoesNotAcceptFocus），
   由 Win32 所有主规则保证它浮在 mpv 之上。
2. **输入路由**（选项名按本机 mpv v0.41.0 `--list-options` 实测）：
   - 键盘：`--input-vo-keyboard=no`（**没有** `--input-keyboard` 这个选项，写错会被静默忽略），
     再加 `--input-default-bindings=no --input-builtin-bindings=no`，全部按键由 Qt 快捷键表处理
     （避免 mpv 子窗口抢走焦点后按键丢失）。
   - 滚轮：交给 mpv（`input.conf` 绑定 WHEEL_UP/DOWN → volume），因为滚轮消息落在 mpv 子窗口上，Qt 收不到。
   - 左/右键、悬停：Qt 侧以 30 ms 轮询 `GetCursorPos` + `GetAsyncKeyState` 检测按下边沿（不装全局钩子，
     只读光标与按键状态）。mpv 侧不需要额外绑 no-op：上面两条已经把它的默认/内建绑定全关了，
     实测它不再响应鼠标（`no-op` 这个命令名在本机 mpv 里也不存在）。
   - 焦点：检测到视频区点击后 `SetFocus(顶层HWND)`，保证快捷键立即可用；文本输入框聚焦时不抢。
3. **空状态**：播放无文件时隐藏视频容器（原生父窗口隐藏 ⇒ mpv 子窗口随之消失），显示 Qt 占位页。
4. **DPI**：Qt 侧 High-DPI，mpv 子窗口按物理像素填充客户区，几何换算集中在 win32 辅助层。
5. **可调项以内核回报为准**（本机 mpv v0.41.0-1101 实测，三条都推翻过第一版的假设）：
   - 越界值是被**拒绝**而不是被夹取：`set brightness 130` 之后读回来还是旧值（不是 100）。
     所以 `app/engine/tweaks.py` 里那套 lo/hi 夹取是**承重**的，不是保险丝；界面若照"我发出去的值"
     画，用户看到的就是"设了没生效"。
   - 枚举按编译期能力收：`tone-mapping=butterlight` 被拒（读回仍是 `auto`），`spline/hable/…` 可用。
     面板照回报重画，才能把"这一版内核不认"显形出来。
   - **`sub-border-size` 不推 property-change**（`sub-scale`/`sub-shadow-offset`/`sub-bold` 都推）：
     值改成功、`get` 读得到、事件永远不来。因此 `set_tweak(confirm=True)` 在确认通过后自己广播一次
     `sig_info`——订阅缓存是界面唯一的真值来源，缺这一发就会出现"外壳改成功了、界面要等下次开页"。
6. **浮层的窗口归属**：带 parent 的 `Qt.Tool` 浮层在 Win32 侧是 owned window，`GetAncestor(GA_ROOT)`
   给出的是**owner**而不是浮层自己的 HWND。判"这块像素是不是我们的窗口"必须沿 `GA_PARENT` 把命中链
   摊开比对（tests/test_ui.py 的 `_hwnd_chain_at`），只比顶层句柄会把自家浮层判成"被别人盖住"。
7. **解码档位是启动期选项，不是热开关**（本机 mpv v0.41.0-1101 播放中实测，第二版性能档位方案据此改写）：
   - `hw-decode` / `hwdec-active` / `current-decoder` 这三个"教科书上常见"的属性在本机 **根本不存在**
     （IPC 回 `property not found`）。第一版把它们当可读真值来设计统计页，会整页空掉。
   - 能读的只有 `hwdec-current`（实测回报 `'d3d11va'`）与 `options/hwdec`（回报**列表** `["auto-safe"]`）；
     两者形状不同，判"生效值"只能取前者。
   - 改不动：`set_property hw-decode X` 报 property not found，`set_option hwdec d3d11va` 报 invalid parameter，
     且 `loadfile … replace` 之后 `options/hwdec` 仍是启动那次的值 —— 换片不会重新解析一次。
     所以"性能档位"必须放设置页并标**重启后生效**，界面拿 `hwdec-current` 显示当前真值。
   - 对比之下这些是**真热切**（设完立刻读回新值）：`vo`、`gpu-api`、`opengl-es`、`target-colorspace-hint`、
     `deband`、`dscale`、`tscale`、`correct-downscaling`；而 `profile`、`opengl-fbo-format` 设完读回空 → 归到启动期。
     注意 `gpu-api` 实测会把 `current-gpu-context` 从 `d3d11` 换成 `angle`：内核确实接受了，但这轮测试是
     **独立 mpv 进程**跑的，`--wid` 嵌入 + 硬解通路下换 GPU API 有可能黑屏，因此不列为热开关，
     同样放设置页标"重启后生效"。
8. **统计页只列本机读得到的字段**（同上实测，缺字段就整行不画，不写"0"糊弄）：
   可用 —— `hwdec-current`、`current-vo`、`current-gpu-context`、`gpu-api`、`video-codec`、`audio-codec`、
   `width`/`height`、`video-out-params/aspect`、`container-fps`、`estimated-vf-fps`、`frame-drop-count`、
   `decoder-frame-drop-count`、`avsync`、`audio-bitrate`、`demuxer-cache-duration`、`demuxer-cache-state`、
   `track-list/count`、`volume`/`volume-max`、`mpv-version`（走 IPC 命令）。
   本机不存在 —— `fps`、`estimated-display-fps`、`vo-delayed-frame-drop-count`、`video-bitrate`、
   `container-format`、`aspect-ratio`、`pixel-aspect-ratio`、`video-target-size`、`sub-latency`、`dithering`、
   `demuxer-cache-length`、`chapter`。
   另：`audio-bitrate` 在 H.264/AAC 样片上回报 128297（bit/s），而 `video-bitrate` 恒缺 ⇒ 码率一栏只能标"音频"，
   不能拼一条"总码率"冒充 ffprobe 的结果。
9. **Windows 文本产物有三条字节规矩，全部由真跑/真导入侧验证**（第三轮生成 `.bat`/`.reg` 时实测）：
   - `.bat` 必须**整份 CRLF**。LF-only 的批处理里凡是带中文的行会被 cmd 从中间劈开当命令执行：
     实测现场是 `故障自检.bat` 只丢下两行"'遍故障自检并停住给人看。' 不是内部或外部命令"
     与"系统找不到指定的路径"，`python -m app.doctor` 那一行根本没跑到，而退出码仍是 0——
     文本层面的 lint 全绿，是 `cmd /c` 真跑一遍才抓住的。
   - `.reg` 必须 UTF-16LE + BOM + CRLF，值里的反斜杠双写、引号写成 `\"`；键名前面挂 `-` 才是删键。
   - 注册表 `command` 取参用 `%1`，**不是**批处理的 `%~1`（shell 展开不认，右键点了会去开一个叫
     `%~1` 的文件）。范围只落在 `HKCU\Software\Classes\SystemFileAssociations\video\shell\LuHuiPlayer`，
     不碰 `HKLM`，也不碰有哈希保护的 `UserChoice`。
   还有一条是给判据自身的：回读文本要 `newline=""`——通用换行会把盘上的 CRLF 读成 LF，
   于是"行尾"这条判据既罚好产物又抓不到真坏了行尾的产物（这一版就是这么自缚过一次）。
10. **录制这一族：内核有 `stream-record`，但它在本外壳的装配下做不出可信的产物**（本机 mpv
   v0.41.0-1101-g5d85ba5fb 实测，`tests/probe_record.py` → `tests/out/record-probe-r13b.log`、
   `tests/probe_record2.py` → `tests/out/record-probe2-r13.log`，样片 `tests/samples/gelu-sample-1.mp4`
   10.00 s / 632107 B / 505 kb/s，出厂参数 `--cache=yes --demuxer-max-bytes=200MiB`）：
   - **入口只有一个名字**：`record-file`、`recording`、`recording-progress`、`recording-tag-segment`、
     `recording-initial-mode` 五个属性 IPC 全回 `property not found`；出厂手册 `runtime/mpv/doc/manual.pdf`
     全文里 record 词形只出现在 `--stream-record`（p162）和倒放章节那句"Stream recording is broken"（p45）。
     两路对齐才算数——只靠 IPC 那一路，"属性不存在"与"值恰好是空串"在这台内核上分不开
     （`stream-record` 未设时回报 `''`）。⇒ 内核**不提供**"是否在录／录了多久"的可读真值，
     界面那颗红点和那个计时只能由外壳自己记账，不能写成"内核回报"。
   - **运行中开录是静默空操作**：播放到 1.47 s 发 `set_property stream-record <路径>`，命令回 ok、
     `get_property` 把路径原样回报，连测 4 次盘上都是 `-1`（文件根本没被创建），log 里只有
     `Set property: stream-record="…" -> 1` 这一行，**没有** `[recorder]` 那行。
     对照组：选项不动、只再 `loadfile` 一次，`[recorder] This is an experimental feature…` 立刻出现，
     文件也出来了。⇒ "按钮亮了、盘上没文件"不是路径写错，是这条命令在已开片的进程里不挂录制器。
   - 把这件事归到"没有新数据可读"而不是"没这个功能"的第三条腿：`--demuxer-max-bytes=65536
     --demuxer-max-back-bytes=0` 下运行中开录，13 ms 后就挂了 recorder 并写出文件（曲线 0 B→262144 B）。
     出厂那 200 MiB 缓存把 632 KB 的本地片在开片瞬间读完，之后 demuxer 不再读流，
     而手册原话是"只写缓存尾部新增的数据"——于是"从现在这一刻开始录"无数据可写。
   - **产物什么时候"能用"，两种形状给出两种答案**（`probe_record2.py` 的 F/G 两腿，停录后每 2 秒量一次）：
     出厂缓存档（F：录制器压根没挂上）里，`set stream-record ""` 把读回收成 `''`、log 只留
     `Set property: stream-record="" -> 1`，之后 12 秒、换片、`quit()` 都量到"文件不存在"
     ——运行中开录不是"产物晚点才出现"，是**永远没有产物**。
     小缓存档（G：`[w][recorder]` 那行在 13 ms 内出现，录制器确实挂着）里，停录让 log 补出
     `[w][lavf] Stopping recording.`，文件 2 秒内从 262144 补到 298069 后连测 6 次不动，
     `ffmpeg -v error` 无报错、`Duration=00:00:05.10` ⇒ 停录**会**收尾，但要等 1~2 秒，不是立刻。
     同一趟还量到更要命的一条：**用刚录完的那台内核**回放自己的产物是 `time-pos 0.00→0.00（没走）`，
     而**新起的内核**放同一个文件是 `1.34→2.54（在走）`。⇒ "录完立刻播"这条路不能在同一进程里做。
   - **录着 seek 会把时间轴写坏**：log `[recorder] Discontinuity at timestamp 7.833333.`，
     产物 `Duration=00:00:13.89`（源 10.00 s）、674308 B > 整片 remux 的 626598 B。
   - **`dump-cache` 不按请求的区间走**：两次不同的窗口（2.00→4.50 与 2.00→7.00）产物都是
     `Duration=00:00:08.35`，log 跟着 `Discontinuity at timestamp 0.000000`；连续档（`end=no`）能出
     完整的 626598 B/10.04 s 有效件，但停它的 `dump-cache 0 0 ""` 在 log 里是"ended→started→stopped"三条，
     语义可疑，不当"可用功能"报。
   - 这条路本来的用武之地在本外壳里不存在：`--stream-record` 是把**解封装器读到的数据**原样 remux，
     面向的是网络流；§1 写死"只放本地文件与文件夹"，代码里没有任何 URL 入口（拖拽只取
     `toLocalFile()`）。**未测**：真网络流下的开录时机与产物质量（本机无可依赖的在线片源，
     不为验证去抓）——将来若加网络流入口，必须按上面那条缓存机制重量一次，
     要点是"开录时机只能是'内核还在读流'的时候"＋"停录后 1~2 秒才收尾、且别用同一台内核立刻回放产物"。
   - 顺手量到的对照（不走内核）：出厂 `runtime/ffmpeg/ffmpeg.exe` 一条 `-ss 2 -i 样片 -t 4 -c copy`
     出 381051 B、`Duration=00:00:04.07`、`ffmpeg -v error` 无报错、我们内核放着时间在走。
     但那是"切一段"的转码 UI 范畴（§7），且与截图工坊的 GIF 区间导出重叠，本轮不落地，只记候选。

11. **声音均衡这一族（`af` 链）的形状与"坏值的下场"是本机实测的**（mpv v0.41.0-1101-g5d85ba5fb，
   FFmpeg N-127213。量法：粉噪经 `--ao=pcm` 落成 WAV，同一把 FFT 带尺逐带比 dB；带视频轨 +
   `--force-window=yes` 那一档才把管线拖到 ≈1.00× 实时、供"播放中段发命令"用。
   证据：`tests/out/eq-probe-r15b.log`（腿 ⓪①②③③b④a④b⑤⑥⑦，下面按腿号引）与
   `tests/out/eq-vol-r12.log`（`tests/probe_eq_volume.py`，只问 `volume=` 那几串写法）。
   逐条按"已测／借来／未测"归档，见本节末尾那三行）：
   - **这一族坏值的下场不是"这一档没生效"，是整条链起不来、一声不出**：越界增益（`bass=g=9999`）、
     不存在的滤镜名（`lavfi=[definitely_not_a_filter=g=1]`）、空图（`lavfi=[]`）三档产物字节数都是
     `-1`，内核原话 `Creating filter 'lavfi' failed.` → `Audio filter initialized failed!` →
     `No video or audio streams selected.`（③b/⑥）。所以表里的夹紧比画面那一族更是承重件。
   - **`af` 的六种写法里五种出声，一种致命**（①，同一条 bass+12@110）：`lavfi=[图]`、`lavfi=graph="图"`、
     裸 `bass=…`、`lavfi-bass=…`、`lavfi="图"` 都出真声音（逐带 +11.7/+7.1，与 ffmpeg 侧一致）；
     `lavfi=name=k:v` 是**启动致命**（`Option af: Error while parsing lavfi parameter bass (g)`，进程起不来）。
     本表只发 `lavfi=[…]`：②④⑤⑥四腿里唯一被反复自证、且读回形状最规整的一种。
   - **逗号同时是 mpv 切 `af` 列表的分隔符**（③/③b/⑥）：十段链两种拼法实测频谱逐带相同
     （RMS −9.4、九带 +6.8…+11.8），但 `change-list af append` 带逗号**实测拒收**
     （`error running command`，链表纹丝不动）⇒ 本表选"每条一个滤镜、每条都不带逗号"，
     代价量过：两种拼法在 ao=pcm 上吞吐都是 ≈13.8×（③b），十一条目不比一条图贵。
   - **换档只有整串 `set` 这一条路**（⑥）：`set` 多条目／单条带逗号、`set_property af <串>` 都 ok；
     `set_property af "[]"` 被拒（`unsupported format for accessing property`），而 `lavfi=[]` 内核真去建
     空图滤镜、结果一声不出 ⇒ "关到零"发的是**空串**（读回 `[]`）。`remove`/`toggle` 要精确匹配整串，
     改一根滑轨就变成"删一条＋append 一条"，而 append 带逗号又拒收 ⇒ 一律整条替换。
   - **`af` 是列表属性、换片不丢**（⑤，按真实节奏放那条管线）：低频 47.9 →（挂 bass+12）59.8 →
     （`loadfile` 换第二片）59.1 ⇒ 不需要在 file-loaded 时补发。
   - **前置增益只能是 `…dB` 后缀写法**（`eq-vol-r12.log`）：`volume=-9dB` 实测 −9.0 dB、
     `volume=0.3548`（线性写法）同样 −9.0 dB——两把尺对得上；`volume=dB(-9)` 与 `dB(9)` 那种函数形式
     本机 **rc=4294967274 直接失败**；而 `volume=-12`（**没**后缀）是线性 −12＝幅度乘 12，
     实测反而 **+17.9 dB 并削顶**（`max_volume=0.0`）。符号相同、量纲不同、下场是爆音，
     所以拼串处无条件补 `dB`，反解处遇到不带后缀的条目算"读不懂"、把原话报出来，绝不按 dB 解释。
   - **削顶是量的不是猜的**（⑦）：十段全 +12 不带前置时 `peak=1.000 clip=657`；同一串后面加
     `volume=-9dB` 变 `peak=0.524 clip=0`、逐带落回 +0.7…+2.8。外壳据此定了一条口味线
     `CLIP_NET_MAX = 3.0`（"最大抬升＋前置 ≤ +3 dB"），预设按它自动配负前置，
     而**用户自己拖的前置不替他改**（替他改就是替他做决定），只在状态行提醒净抬升。
   - **热切这件事差点被量具自己骗过去**（④a/④b）：`--ao=pcm` 落盘**不按实时跑**（8 s 媒体 0.58 s 抽干
     ＝13.8×；90 s 媒体四档 0.55～0.91 s 抽干＝147.5～164.5× 与 99.2～102.9×，四个数逐行在 ④b 里），
     所以"第 1.2 s 发命令"发在一个早就 EOF 的进程上，④b 四档全判"没有证据"：两条命令应答 ok 却落在
     已 EOF 的进程上、两条干脆没发出去，而应答 ok 那两条的切点量到产物末尾 90.0 s——后半要取的
     [91.5, 96.5] 在文件外面。换成带视频轨、≈1.00× 实时那条管线之后：对照 anull 腿逐带 ≤0.8 dB
     （这就是"两档的差不是量具造的"那条前提），挂 bass+12 的切点是低频 48.4→59.2（**+10.8**），
     十段全 +12 的切点后逐带 +8.6…+11.7（`clip=538`）。⇒ "拖动松手就下发"真的有效，
     不需要写"重开片子才生效"那种话。
   - 参数名与范围照本机内核/`ffmpeg -h filter=` 回报：`equalizer` 是 `f`（Hz）、`t`（`h`＝按 Hz）、
     `w`、`g`（dB，内核范围 −900..900）；`superequalizer` 是 `1b..18b` 十八档**线性 0..20**（不是 dB），
     本表不用它——把线性 0..20 翻成用户的 dB 会引入一层没人验证过的映射；`anequalizer` 本机**不可用**
     （`Could not set non-existent option 'f'` + 整条起不来，②）。十段带宽取
     `w = max(int(f*0.45), 8)`（≈0.45 倍中心频）。
   - **已测**：上面每一条都有腿号与留档日志；真机那 14 条（`--af` 落地、播放中热切、坏值原话、
     恢复、面板读真值、拖一根落到内核、复位成 `[]`）在 `tests/out/eq-acc-r12-full2.log`，
     87/87、0 判废。**借来**：只有一处——`lavfi=[…]` 那种括号写法出处是出厂手册 p194，
     但它在本机被内核自证过（①），不是照抄；其余数字没有一个来自外部文档。**未测**：
     ①别的抬升幅度下的削顶临界前置（−9 dB 只是 +12 那**一个**测点，口味线在别的高度是外推）；
     ②多声道与直通档（SPDIF/TrueHD 一路 `--audio-device`）下 af 是否仍逐档生效；
     ③主观听感（频谱尺不是耳朵；十段各自的听感差别、那个 0.45 倍频程带宽的"手感"都没人听过）；
     ④换 mpv 版本之后的全套形状——`tests/test_equalizer.py` 里有一条专门钉"表依据 == 本机版本"，
     版本一变它就红，红的时候上面这整串结论要重量。
12. **「打开方式」那一族的机制约束是 2026-10-16 在本机量出来的**（写代码前都得知道，不然会写出
    看着对、菜单里没我们／判据恒绿的产物）：
    - **列表里那行字不是 ProgID 那串英文**：能确定的是——拿 `pythonw.exe` 当落点时列表里出现的字样就是
      "pythonw"，用户既认不出也没法选（本机 `FileExts\.mkv\OpenWithList` 里确实躺着一条 `pythonw.exe`，
      是用户当年手动浏览挑的；那样点了也不会播，pythonw 会把 `.mkv` 当脚本执行）。所以这一族落成
      真启动器 `luhui-player.exe`（`launcher.cs` 现场 csc 编、中文全走 `\u` 转义），并把**两处**候选来源
      都写成同一个显示名：exe 的文件描述 `FileDescription`＋`Classes\Applications\luhui-player.exe` 的
      `FriendlyAppName`。**到底是哪一处供给的这个名字，本机没分离出来测**（两处同值，测到的只是
      `GetMenuName` 回来那行＝〈LuHui播放器〉，见 §6 第 36 条 ②），所以别把"就靠文件描述"当结论写；
      要紧的是两处都不许落成解释不了的名字，`lint_openwith` 与 `--selftest` 里各有一条盯着
      `FriendlyAppName` 不等于播放器名的那种坏法。
    - **读版本信息的单位坑**：`VS_VERSIONINFO` 里文本节点的 `wValueLength` 数的是**宽字符个数（含结尾
      NUL）**，不是字节数，而 `VerQueryValueW` 把这个数原样回给你。本机实测 `FileDescription`＝
      "格斗芦荟播放器"（7 字）时它回 8，按 8 **字节**读只剩"格斗芦荟"——像资源被截，其实是量具自己
      咬掉半截。两份实现（我们的 ctypes 腿与 .NET 的 `VersionInfo`）逐字对上才敢判"资源没问题"。
      按 `len` 个宽字符读、再在第一个 NUL 处切，两种口径都不怕。
    - **注册哪四处**（全在 `HKCU`，`HKLM` 一个字不碰）：`Software\Classes\LuHuiPlayer.Media`
      （ProgID＋`shell\open\command`）、`Software\Classes\Applications\luhui-player.exe`（＋`FriendlyAppName`）、
      `Software\Classes\<每种扩展名>\OpenWithProgids` 里那条**值名为 PROGID、值为空**的条目、
      `Software\Microsoft\Windows\CurrentVersion\App Paths\luhui-player.exe`。扩展名名单用 AST 从
      `app/engine/player.py` 的 `MEDIA_EXTS` 现取（本机 48 条），手抄的名单会在加格式那天悄悄落后，
      而注册表里的落后是看不见的。
    - **`reg import` 建 `Classes\<.ext>\OpenWithProgids` 会连带新建那根父键**，它的默认值读出来是
      `''`（不是"没有这个值"）。这条**不是**"改了默认程序"：`None → ''` 是导入必然产生的变化，
      对账里要豁免；反过来 `'mpegvideo' → ''`（把用户原有值清空）与任何两个非空值之间的变化都必须翻红。
      豁免不能只靠判据自觉：`None → ''` 这一支由三条反向哨兵（`check_openwith.py --selftest` 的 ⑪⑫⑬）
      夹住，"有人往裸扩展名节写 `@=`"这一支由字节闸夹住（生成器的 `lint_openwith` 与本文件的模板检查
      都不允许模板里出现那种行）——所以这条豁免只放行"没写过"，不放过"写了空的"。
    - **外壳侧的数据源形状**：`shell32!SHAssocEnumHandlers` 是"哪些处理程序能打开这个类型"的官方入口，
      但 `ASSOC_FILTER_DEFAULT` 与 `ASSOC_FILTER_EXE` 的鉴别力不同（前者连假扩展名都列出 19 条含我们，
      因为 App Paths 让这个 exe 成为通用候选；后者对同一个假扩展名列出 0 条）——判据必须带假扩展名哨兵
      才算有牙。COM vtable 三坑：槽地址要先解引用；`GetName`（第 7 槽）hr=0 却写 NULL；
      `GetUIName`（第 8 槽）一调就硬 AV。给菜单用的那行字在 `GetMenuName`（第 4 槽）。
      这一族的完整账与留档号在 §6 第 36 条。

13. **外部打开这一条链的三个形状：转交必须有回执、参数不许静默吃、留痕要能自证**
    （2026-10-16～17 本机实测；用户那句原话是「设为默认后双击确实打开了播放器，但**视频根本没加载
    进来**，界面停在『打开文件夹』那一页」）：
    - **两条各自都能造出这个症状，合起来就是"文件没了但没人报错"**：① `collect_paths` 早先是
      `os.path.exists` 一筛就丢、只往 stderr 印一句——而双击/右键那条路走的是 `pythonw`，
      **连控制台都没有**，那句警告永远没人看见；② 转交早先是"写完字节就宣布成功"，接收方那句
      `if win is not None` 在窗口还在装配时把载荷直接丢弃。两个各自都能过"看起来能用"的目测。
    - **现在的契约四条**：回执必须带这一次的 `nonce`（空 nonce、别人的 nonce 都不算数）；接收方
      装配期先排队、`attach` 之后立刻补交，**回执等真落地才发**；拿不到回执就自己开新窗口
      （宁可多开一扇，也不许把文件吞了）；四类参数形状各有一条点名——多套一层引号的同一路径、
      外壳没展开的 `%1`/`%~1`、被空格劈成几段的同一路径、相对路径要补成绝对（选项类不算丢文件）。
    - **留痕是现场证据，不是日志装饰**：`data/logs/launch.log` 一行一次启动
      （`kind=cold/forward｜argv 数｜kept｜dropped｜ignored`）。doctor 第 17 项「上次外部打开的留痕」
      读的就是这本，而它那条判据的坏法恰恰是"一行都不带参数"——**旧写法在那儿只会报"日志正常"**
      （§6 第 37 条记它翻红的那趟）。
    - 判据：`tests/test_instance.py`（正跑＋`--falsify`，条数由脚本 `[FINAL]` 自打）管协议与参数形状，
      `tests/e2e_openwith.py` 管"按注册表里那条命令原样执行"，`tests/e2e_instance_handoff.py`
      管"已经开着实例再双击"那条只有真两进程才踩得到的路。

14. **GPU 这一族的真值：硬解默认就在跑、缩放本来就落在 GPU 上、而这台装配里没有 NGU 那条路**
    （本机 `luhui-player.exe` → `pythonw open_file.py` → 外壳自起 mpv 的**产品装配**下实测，
    不是裸起 mpv；三档装配各跑一趟，留档 `tests/out/e2e-gpu-1017-014247.log`＋反向对照
    `e2e-gpu-1017-014416.log`）：
    - **"默认档有没有在硬解"读的是内核回报，不是配置里那个愿望**：`hwdec-current='d3d11va'`、
      `current-gpu-context='d3d11'`、`current-vo='gpu'`；`hwdec`／`options/hwdec` 是**命令行回显**
      （`bogus-value` 也照原样回显），所以它注定不能当证据。
    - **尺子先量"有没有鉴别力"，再量产品**：强制软解那趟 `hwdec-current='no'`、NVDEC 占用从
      ①档的峰值 **18.0** 掉到 **0.0**、内核 CPU 从 **12.5%** 涨到 **191.7%**（同一支 4K、同一种量法）
      ——这两条就是①那条绿的牙，否则"硬解在跑"是恒真废尺。4K60 三档（HEVC/H.264/AV1）推进率
      **0.999**（`time-pos` 增量 ÷ 墙钟增量）、画面掉帧增量 **0**。
    - **"跑在哪块卡上"有实测答案**：内核自己日志里的适配器名与 `nvidia-smi` 首卡同名
      （`NVIDIA GeForce RTX 3070 Ti Laptop GPU`）。这条不是顺带——同一段文本在两处独立取到才算数。
    - **白名单是承重件**：`--hwdec=bogus-value` 内核照样启动、只是静默退软解（用户只会发现"变卡了"），
      所以非法值必须在递给内核之前被 `app/engine/perf.py` 拦下并回退默认档，且回退这件事要报出来。
      实测拦下之后硬解仍在跑（`options/hwdec=['auto-safe']`）。
    - **超分为什么没做**（问的是迅雷影音那类"GPU 超分"）：本机内核 `--list-options` 共 1274 行，
      `--scale`/`--dscale`/`--cscale` 的可选值里有 `ewa_lanczossharp`、`lanczos`、`mitchell`、
      `catmull_rom`、`jinc`、`haasnsoft` 这一族（**这就是已经在跑的 GPU 侧高质量缩放**，
      设置页那三档 `quality_preset` 拼的就是它们，`vo=gpu` ⇒ 算在显卡上），
      但 **`--ngu-*` 与 `ancedx4` 一条都搜不到**——mpv 自带的那个神经超分没编进这台构建。
      所以"AI 超分"在这里不是改一个设置，是**换内核构建**或**外挂 `--glsl-shaders` 的第三方 shader 包**：
      前者会让 §3 第 5、7 条与 §6 那几本靠这台内核量出来的账全部作废重测，后者要逐档实测帧预算
      而**掉帧必须让窗口真出帧才量得到**（锁屏期间量不出来）。两者都属于"能给用户一个没人验证过的档位"，
      所以没做；量具已经就位（`tests/e2e_gpu.py` 那把推进率＋掉帧增量的尺），屏亮的窗口里补一趟探针
      就能把它变成有数字的决定。
    - **属性名清单是实测来的**：`hw-decode`／`current-decoder`／`current-hwdec`／`decoder-desc`／
      `d3d11-adapters` 在本机这版内核**不存在**（property not found）。一次问一批时，
      一条不存在的属性会把**整批**读数作废，症状是"全部读数 None＋一条也没取到"而内核活得好好的
      （这趟真翻过车；所以逐条问的 `ask_soft` 是量具形状，见 §6 第 37 条）。

15. **测试起的实例不许碰用户真正的 `data\`：边界是一条判据，不是一句自觉**
    （本轮实发事故换来的，事故与清理账在 §6 第 37 条）：
    - 凡按"双击"形状起实例的趟，一律 `tests/player_harness.make_env(临时目录, {"mute": True,
      "volume": 0})` 把 `LUHUI_DATA` 指出去。写临时目录那份 config 用 `newline="\n"`——Windows 上
      `io.open(..., "w")` 默认会把整份 LF 换成 CRLF，回读看不出、只有按字节数才看得见。
    - **静音不许信"我写进配置了"**：必须从**被测实例自己的内核**读回 `mute`/`volume`
      （`ph.silent_now`：`mute is True` 且 `float(volume) == 0.0`，两道都关实了才算静音；
      读不到就判"这趟不作数"，不许赌它没声）。写进去的值只是进程内垫片，实例一重启就作废。
      判据自己带 8 档已知好坏对照（`SILENT_CASES`），其中两档就是历史事故：`volume or 1`
      把 `0.0` 读成"没读到"（于是已经静音好的一趟被判 ENV、整趟没踩到要测的东西），
      以及 `mute` 读成 `1`/`None` 时不许算静音。
    - 收尾对账：用户真目录那四样指纹——`config.json` 与 `watch-state.json` 的 md5、`launch.log`
      行数、`thumbs` 格数——**开跑前后逐字相同**才算这趟过。对不上时先分流"是谁写的"：
      差异文本里认得出本趟样本名才记 FAIL，认不出就记 ENV 并把原行摊出来（环境抖动不许冒充产品红，
      也不许把自己的脏账盖成"环境不给"）。
    - 操作把手只在 `tests/player_harness.py` 一份。本轮把 `tests/e2e_instance_handoff.py` 里那 8 个
      重复实现（`interpreter`/`SILENT_CASES`/`pipe_listening`/`player_pids`/`spawn`/`ask`/`tell`/
      `wait_pid`/`kill_tree`）收拢过去，只留"判据与记账"在各脚本里——**抄第二遍的时候坑会跟着抄，
      判据却会各改各的**，而这一族分叉过一次。

## 4. 模块划分（每个单元一个职责，可独立理解）

```
app/config.py            读写 JSON 设置（含默认值与迁移），不认识 UI；另存品牌三常量 APP_TITLE/APP_NAME/BRAND_ID，管道名·截图前缀·输入配置名·测试标记都从 BRAND_ID 派生，改名只动这里。
                         观看进度（watch-state）另有一层**读盘缓存**：缓存本体永远等于"上一次落盘成功的那份"，
                         靠文件记号（mtime_ns+size）判断要不要重读，写盘顺手写穿；文件不存在（记号 None）同样算
                         "没变过"——否则首次运行每张卡片都要 open 一次必然失败。失效半径见 §6 第 32 条
app/win32.py             Win32 辅助：DWM 圆角/亚克力、光标与按键状态、SetFocus、
                         显示器几何、休眠抑制、WM_NCCALCSIZE/HITTEST 常量
                         （亚克力那三条 set_acrylic / extend_glass / _blur_behind **写了没接**：
                         分层浮层与 DWM backdrop 能否共存没实测过，实测通过才配有自己的配置键）
app/engine/ipc.py        MPV IPC 客户端：连接/重发、request_id 匹配、observe_property 分发
app/engine/player.py     mpv 子进程生命周期 + 面向应用的播放 API（load/seek/track/snapshot…）
                         并把 mpv 属性映射成 Qt 信号
app/engine/thumbs.py     ffmpeg 抽帧缩略图（带缓存 + 并发节流），供时间轴悬停预览；回收**按批**（每 20 份
                         成功才扫一次全目录），责任在 store 自己的 `_note_done`，不挂在界面接线上。
                         内存里两本账都封顶：整图 sheet 只留 `SHEET_CAP=3` 张（约 17MB 上限，
                         **LRU**——命中也挪到队尾，正在悬停那支不会被新抽完的挤走），
                         失败负缓存 `FAILED_CAP=4096`（FIFO，挤出数记在 `failed_evictions`）；
                         不封顶时悬停过多少支片就攥多少张 5.8MB 整图，直到退出（§6 第 41(d) 条）
app/engine/tweaks.py     可调项单一真源：Num/Flag/Choice 表驱动 UI 量程、夹取、持久化键、
                         启动参数与测试期望（一处加一档，界面/引擎/自测三边同时跟上）
app/engine/perf.py       启动期四项单一真源（硬解档/GPU API/缩放预设/音量上限）：
                         界面下拉、启动参数、校验与回退说明都出自这一张表。
                         非这里不可的原因：内核面对非法 --hwdec 是**静默降级成软解**，
                         不报错不退出，只有拼参数前拦住才看得出来
app/engine/transform.py  画面族单一事实表：12 项的量程/步长/默认值、整条滤镜链的拼法与
                         顺序（去带→降噪→锐化→裁剪→左右镜像→上下镜像）、`startup_args`、
                         `needs_sw` 与 `format=yuv420p` 前缀规则、`filter_failures()` 的日志标记表、
                         `reset_values()`、`same_value()`。与 `tweaks.py` 分表的原因是**下发形状不同**：
                         几何一次一条 `set_property` 且内核会推回报，增强必须整条 `change-list vf set`
                         替换且内核**不回报**——混进一张表就会有两处代码各自决定"这项怎么发"，
                         那正是"界面动了、画面没动"的产地。文件头九条结论全部标了实测出处
                         （越界值是整条拒收不是夹紧、列表挂着名字≠在跑、`deband` 的参数是 1thr/range
                         不是 iterations、内核 GPU 侧去带不能热切、`append` 一次只收一档……）
app/engine/equalizer.py  声音均衡族单一事实表：十段（31 Hz…16 kHz，ISO 十倍频程标准档）＋前置增益的
                         量程、`equalizer=f=…:t=h:w=…:g=…` 的拼法与"低频在前、前置最后"的固定顺序、
                         `build_chain`/`startup_args`/`preamp_graph`/`normalize`/`parse_entries`/
                         `parse_matches`/`PRESETS`（平坦·人声·低音·明亮·影院·夜间）。
                         与 `transform.py` 分表的理由是**坏值的下场不同**：那一族坏了是"画面不动、
                         列表照显示"，这一族坏了是"整条 `af` 起不来、一声不出"（§3 第 11 条 ③b/⑥ 实测
                         三档产物字节数都是 `-1`），所以这里的夹紧、非有限值退回 0、"表外档位一条命令
                         都不发"三件事都是承重件而不是卫生条款。
                         另有一条反解纪律：`af` 是列表属性，内核推回来的是
                         `[{'name':'lavfi','params':{'graph':'equalizer=f=31:…'}}]` 这种串，面板那十个数
                         只能由 `parse_entries()` 从**读回**反解，不能拿"我发出去的那串"充数；
                         认不出的形状（尤其 `volume=` 后面不带 `dB` 的那一种，那是线性不是 dB）
                         一律算"读不懂"并把原话报出来，绝不猜成 0 dB 也不猜成 dB——
                         符号相同、量纲不同，猜错的那一档是爆音。
                         文件头七条结论逐条标了腿号（`tests/out/eq-probe-r15b.log`），口味线
                         `CLIP_NET_MAX=3.0` 只有 +12/−9 那**一个**实测点撑着，别处是外推（§3 第 11 条末）
app/engine/library.py    媒体库扫描：摊平目录 → 只对"变了/没见过"的文件探测时长 → JSON 索引复用
app/engine/posters.py    媒体库封面：一条 ffmpeg 命令取一张代表帧，落盘缓存 + LRU + prune（回收按批：每 50 份
                         成功才扫一次全目录，计数与触发都在 store 自己身上——挂在界面接线上是"每份一次"，
                         一次拖窗滚动就能把回收做成第二个卡顿源）
app/engine/usage.py      资源占用实测（不认 Qt）：psutil 按 pid 量 CPU/内存 + nvidia-smi 取 GPU 整机读数。
                         三条口径写死在这里：①取不到的一律 None 并带原因，格式化只剩"没测到"这一条路，
                         免得页面和复制出去的报表一个写 0 一个写 —；②GPU 那列永远是**整机**瞬时值，
                         报表抬头就写着"不是本播放器独占"；③`clocks_throttle_reasons.active` 是位掩码，
                         0x1/0x2/0x10 这类"空闲/启动期"状态位要和真限流（热降频、功率刹车）分成两行——
                         把"刚没活干"报成"被降频了"是这类面板最常见的谎。
                         `sample_window()` 会真睡那么久，所以只许在工作线程里调
app/engine/gif.py        GIF 导出：一条 ffmpeg 命令出成品（`split` → `palettegen=stats_mode=diff`
                         → `paletteuse` 双通道，调色板为这一段单独生成；暗场片段用默认单遍编码会整片糊成灰块）。
                         进度只认 `-progress pipe:1` 的 `out_time_us=`，不是"我以为编到第几帧"；
                         stdout 逐行读、stderr 单独落文件（两个管道都留在内存里读，报错刷满就会卡死子进程）；
                         字幕只烧**外挂文件**（`subtitles` 滤镜要的是磁盘上的 .srt）；
                         `clamp_range()` 是界面与命令共用的那一把夹子，两边不可能对不上
app/ui/icons.py          QPainterPath 矢量图标工厂（不依赖图标字体，避免缺字形）
app/ui/theme.py          设计令牌：颜色/圆角/字号/间距 + 全局 QSS
app/ui/chrome.py         无边框窗口外壳（原生阴影+圆角+贴边快照+最大化边界账）
app/ui/main_window.py    主窗口：装配引擎与界面，拥有全部快捷键与菜单动作；侧栏六页是一个
                         QStackedWidget + 六个 TabBtn（2026-10-12 加「声音均衡」那一页时它是索引 4，
                         统计让到 5；页名与键名同源在 `tab_defs`/`tab_actions` 两张一一对应的表里，
                         均衡那一格是**空表**——这一页本轮没有新增快捷键，键盘入口留在面板的 `bump()`）。
                         形状纪律：`tab_defs[i]` 只能是「(标签, 说法)」两位，因为好几把尺子按下标读
                         `tab_defs[i][0]` 认页签，加一位会让它们全部错位——键名因此挂在控件上，
                         不挂在这张表里。文件头那条纪律：界面**从不**用同步
                         `get_property` 刷新自己，状态一律来自 player 的订阅缓存/信号
                         （读线程里发同步请求会自我死锁，UI 线程里发则把卡顿从"可能"变成"必然"）。
                         画面族那 8 把键（`R`/`Shift+R`/`O`/`P`/`Shift+P`/`H`/`Shift+H`/`Ctrl+G`）
                         在这里接到 transform_panel。
                         页签宽度由 `_layout_tabs()` 自己钉、不交给布局：Qt 给 QToolButton 的 sizeHint
                         在文字与 QSS padding 之外还自带 11px 松弛，五页签时代五个页签就是 55px，正好把 300px
                         的侧栏顶到放不下、布局把每格压回 54px 切掉半个「播放列表」。现在每格钉在
                         「文字+2×留白」与 sizeHint 的**大值**上，留白两档（10/4）、按钮缝三档
                         （4/2/0），设置页允许的 240..520 全区间逐档实测排得开（240 那档要收到缝 2）。
                         排不开时是「换行」不是「压缩」（2026-10-10，`_place_tab_rows`）：页签条是
                         一条纵向盒、每排一个独立的横向盒，**不是一张共享列的网格**——共享列时
                         「最宽那排要多少」并不是布局真正要的数（列宽按各排同一列取最大、跨排互相
                         牵连），`_layout_tabs` 报 416 而网格实际要 543，侧栏上限 520 ⇒ 列被压窄、
                         固定宽的按钮又不肯缩，改造前在 4.0 档字号下两格真的压在一起 23×72px
                         （logs/sweep-font4.0-*.log）。拆成每排自己的盒之后，函数返回的数就是布局
                         要的数；量具也才可能按「第几排」分组——盒内按钮的 `y` 恒为 0，按 y 数排数
                         会恒报 1 排（§6 第 26 条）。
                         宽度进来之前先过 `_clamped_sidebar_width()`：设置页那根 spinbox 只夹得住
                         "人按出来的值"，而 `config.json` 是用户能手改的（README 与 doctor 都教人改它），
                         一个 100 会让最后两个页签变成点不到的死键——量程只有一份出处
                         （`settings_dialog.INT_RANGE`），盘上那份不改，下次开设置页自然对齐。
                         四档像素留档在 `tests/out/sweep/sidebar-tabs-{240,260,300,520}.png`
                         （`tests/probe_tab_pixels.py`，离屏出图不看屏幕）：240/260 走窄留白＋缝 2/4、
                         300 起走宽留白，逐张目检过——选中那格的圆角底不压字、一排格不挤成一坨
                         （那批 PNG 是五页签时代抓的，六页之后的逐档留档由 `tests/watch_ui_round.py`
                         的 sweep 腿重抓；页签颗数在尺子那边一律按 `tab_btns`/`tab_defs` 现取、不抄名单，
                         这一条是 2026-10-12 按 §6 第 34 条改的，改之前有五把尺抄着旧名单）。

app/ui/title_bar.py      标题栏：应用名 + 当前片名 + 最小化/最大化/关闭。拖动由**本控件**处理：
                         chrome 的 `WM_NCHITTEST` 把标题栏判成 HTCLIENT（客户区鼠标事件才派发得
                         下来），按住走过 `startDragDistance()` 后调 `win32.begin_system_move()`
                         进系统移动循环，双击调 `_toggle_max`；窗钮自己吃点击不触发拖动。为什么
                         不给 HTCAPTION 且不拦 WM_NCLBUTTONDOWN，见 §6 第 42 条 R24。
                         关闭钮悬停换**像素**（灰 X→白 X）：QSS 的 `color` 改不到 QIcon 里的像素，
                         判据直接读图标最亮值（见 §6 第 41(g) 条）
app/ui/video_surface.py  视频容器（原生 HWND 供 mpv 嵌入）+ 输入轮询 + 全屏
app/ui/win_geom.py       窗口几何记忆的**纯规划器**：存档 → 交给它（多屏矩形、最大化位）→
                         返回可用的 (x, y, w, h, max)。三条规则：左屏负坐标是合法值；
                         整块跑出屏幕就回主屏居中；位置整窗夹进可用区（屏幕比下限小时
                         尺寸也跟着收，保证一开窗全可见）。没有存档时 default_rect()
                         按主屏可用区居中。它不碰 QWidget，坏存档（缺键/错类型/NaN）
                         在纯函数里就能逐条喂；
                         接线在 main_window（拖动停下 1.2s 才写盘，不刷配置）
app/ui/scrubber.py       时间轴：缓冲范围、章节标记、悬停时间气泡与缩略图、拖拽
app/ui/controls_bar.py   播放/停止/上下曲/帧步进/倍速/音量/音轨字幕菜单/循环模式
app/ui/playlist_panel.py 播放列表（增删、拖放、排序、乱序循环、当前项高亮）；内核每次推 playlist 不直接
                         整表重建，60ms 拖尾窗口合并（throttle+trailing：窗外各画一遍、永远合到**最后一份**，
                         不会停在中间态）——否则拖进 200 个文件是 200 遍清造＝O(N²)
app/ui/library_panel.py  媒体库：文件夹扫描、最近文件、封面缩略图、续播状态。搜索框走 220ms 防抖
                         （只挂在击键那一入口，扫描回包/排序当场就画），整表重建那趟**不同步解码封面**：
                         先铺文字＋按 (扩展名, 逻辑尺寸, DPR) 缓存的占位块，封面按 12 张一片排在
                         事件循环空隙里补。卡片尺寸不是固定 192：`card_metrics(视口宽)` 按"每格至少
                         150、单格封顶 260"算列数与格宽（300 侧栏一列 260、520 三列 160），
                         60ms 防抖后再套用，**视口宽变也要重排**（滚动条来去只改视口、不改面板宽）。
                         库里 0 部片与"搜索没命中"是两个不同的空状态页（各说各的下一步），
                         底注「双击卡片播放」在无卡片时收起
app/ui/tweak_panel.py    调节页（侧栏第三页）：字幕样式/画面均衡/HDR/声音设备与 A-B
app/ui/form_rows.py      侧栏共用的「标题 + 当前值 + 滑轨」一行（`NumRow`/`add_check`/`add_choice`）。
                         单独开一个文件而不是让两页各抄一份：QSlider 只有整数而表里是 float+step，
                         `round` 与 `int` 的差别直接就是用户手上的"拖两下只动了 0.1"，两份代码迟早
                         漂成"同一个滑轨在两个页面手感不一样"。`fmt` 只是显示层换算（panscan 的
                         0..1 给人看成百分比），不参与取值——夹紧、下发、比对仍然归表。
                         它还管悬停提示的登记（`bind_key_tip`/`bind_key_tip_parts`/`paint_key_tip`/
                         `refresh_key_tips`，登记存在控件自己的 `__dict__[TIP_ATTR]` 里）：
                         「说法（键名）」两句里**键名那半句永远由 `keymap` 按生效表现算**，
                         说法那半句可以是字符串或无参 callable。**callable 的说法一律走动作口径**
                         ——按下去会干什么就说什么（暂停时说「播放」、静音时说「取消静音」），
                         和图标、右键菜单、`_mute_saying` 同族；2026-10-13 之前 `_play_saying` 写成
                         了状态口径（暂停时说「暂停」），而登记里存的是构造时取到的**绑定方法对象**，
                         想验方向得换 `__dict__` 里那一份、改类属性是空操作（§6 第 35 条 ④）
app/ui/transform_panel.py画面页（侧栏第四页）：6 项几何 + 6 档增强。两半的**回读来源不同**，
                         所以界面措辞也不同：几何滑轨画内核订阅推下来的值，增强滑轨画配置值
                         （这一族内核不回报），而"到底有没有真在跑"另走一次下发时读
                         `player.filter_failures()` 的日志原话——列表挂着名字、`enabled` 照是 True、
                         像素零变化的"假开启"在链状态行里红字点名。裁剪后内核不回报新尺寸
                         （`dwidth` 仍写原片大小），所以这里只说"按 2.35:1 裁了"而不编一个裁后分辨率。
                         内核没起来时照样能调（值全进 `startup_args`），措辞是"已记入设置，播放后生效"
app/ui/audio_eq_panel.py 声音均衡页（侧栏第五页）：31 Hz–16 kHz 十段（每段 −12…+12 dB）＋前置增益
                         （−18…+6 dB）＋六条预设＋归零＋链状态行。它和第三、四页长得像而**下发的形状
                         与坏值的下场都不同**，所以另开第三份事实表与第三块面板：这一页是一条 `af`
                         滤镜链，坏值的下场是整个进程一声不出（本机实测越界增益／不存在的滤镜名／空图
                         三档的产物字节都是 −1，内核原话 `Audio filter initialized failed!`），
                         于是量程由 `engine/equalizer.py` 那张表夹死，界面上出现不了 999。
                         面板上那十个数一律来自**读回反解**（`af` 是列表属性，内核推回来的是一串
                         `lavfi` 条目而不是逐档数字），不是"我发出去的那串"；读不懂的条目原话进状态行，
                         不猜。归零＝不挂滤镜（全 0 时发空串，内核读回 `[]`），所以不会出现
                         "写着 0 dB 却偷偷挂十一条"。预设自带负前置（十段全 +12 不带前置实测
                         `clip=657`，同一串加 `volume=-9dB` 变 `clip=0`），而用户自己拖的前置只在
                         状态行提醒净抬升、不替他改。两条纪律与第三、四页同：拖动途中不 confirm
                         （否则每个 valueChanged 补一次同步往返，手感一格一顿）、控件一律 NoFocus。
                         这一页本轮**没有新增快捷键**（键位表那格是空表，键盘入口在面板里留着 `bump()`）
app/ui/stats_panel.py    统计页（侧栏第六页，2026-10-12 加了「声音均衡」之后从第五位让到末位）：解码通路、丢帧、音画偏差、缓存的实时读数；
                         一秒一轮按需轮询（那些属性是每帧级推送，挂订阅会把主线程刷满），
                         页面不可见就停表；内核没回报的项整行不画。
                         下半页另有资源占用四行 + "采样 3 秒/复制报表"两个按钮（数据源是 engine/usage.py
                         而不是内核）：三秒的活在 QThreadPool 里睡，UI 线程一步不停；没点过采样之前四行
                         整行藏着——`_Row` 建出来是可见的、右侧挂着占位符"—"，光在注释里写"不画"不算数
app/ui/floaters.py       OSD 气泡 + 时间轴悬停预览：带 parent 的顶层窗口（拥有关系让它压在 mpv 子窗口之上）
app/ui/sleep_timer.py    睡眠定时：一个绝对截止时刻 + "到点做什么"，对话框按需现造
app/ui/settings_dialog.py设置页：解码档位与路径/主题/续播；每项标注生效时机（立即/下次载入/重启）
app/ui/menus.py          右键菜单、轨道菜单
app/ui/keymap.py         快捷键事实表：默认绑定（出厂 72 条）、diff 存储模型、合法性判据、冲突检测、
                         导入导出、`mirrorable()`（给工具窗镜像筛键）。必须存在的原因：
                         `QKeySequence("PgDn")` 解析成 `Qt::Key_UNKNOWN`——装上去不报错、永不响、
                         看起来完全合法，而 `isEmpty()` 与 `toString()` 都拦不住（实测见
                         tests/probe_keyseq*.py）
app/ui/keymap_dialog.py  改键页三条决定都是被这类坑逼出来的：**按键捕获**而不是手打键名；
                         同键双绑／按不出／表外动作名三种毛病**不许保存**（存进去的那一刻起
                         症状就变成"用户的电脑坏了"）；落盘只写 **diff** 不写整表，
                         否则将来版本新增的默认键会被用户那份旧快照静默吃掉
app/ui/help_page.py      快捷键一览页（F1）：把生效表按**动作名**分 8 组排成两列表格（65 项动作／
                         72 条绑定），纯函数 `render()` 产出 HTML、对话框只负责显示与搜索。
                         为什么不用 `QPlainTextEdit` + `.ljust()`：比例字体下按字符数补空格，
                         说明列会逐行漂移，看上去像草稿；表格由排版引擎按列宽摆，搜索按"键名或
                         说明"筛、表头写"筛出 N / 总数"，一条都丢会由测试对账抓出来
app/ui/gif_dialog.py     截图工坊（非模态）：只管"把界面里的数收成一个 dict 发出去"。不碰引擎
                         （位置/时长/A-B/外挂字幕由外部 `state_of()` 回调进来），所以测试拿一个假字典
                         就能跑完整个对话框；夹住在 `spec()` 里走 `gif.clamp_range()`；
                         进度与结果两个槽先兜类型再动控件——PyQt5 槽里抛异常不是 traceback，
                         是进程当场没了（rc=127、stderr 全空）
app/main.py              装配：QApplication、命令行参数、单实例、启动自检、崩溃落盘（crash.log）
app/doctor.py            故障自检入口（`python -m app.doctor`）：17 项各写"查了什么/看到什么/下一步做什么"，
                         有 FAIL 退 1；不开窗口、不看像素、锁屏也能跑（最常见的两个故障恰恰发生在界面开不起来时）。
                         第 14 项「资源占用实测」的三档是照着**缺席的后果**分的：psutil 缺＝提醒（那几行整页写
                         "没装 psutil"），nvidia-smi 缺＝**正常**（非 N 卡就该报 OK——对它报警会让人学会忽略整份
                         报告），找得到却跑不通/没读数＝提醒并带原文。第 15–17 项是「外部打开链路」「默认程序
                         现状」「上次外部打开的留痕」，本轮 17 项现跑 **0 项阻断、1 项提醒、16 项正常**
                         （`tests/out/doctor-r22b.log`）。
                         「崩溃记录」那条按**年龄**分流：crash.log 早于 `app/` 下最新改过的 .py ⇒ 那次栈来自
                         旧代码，报 OK 并写明"这之后没有新的崩溃记录"；不早于或判不了年龄才报 WARN——
                         旧版把 10-19 那桩历史栈永久报成 WARN，等于训练用户忽略整份报告（§6 第 41(b) 条）
                         --keys 打印**生效**键位表（README 里不抄表，避免与用户改过的键位不同步）；
                         --falsify 逐条喂已知好/坏事实，坏的不翻红就是假绿光（本轮 **20/20 CAUGHT**，
                         `tests/out/doctor-falsify-r22b.log`）；
                         --neg 把 app/ 搬到没有 runtime/ 的临时目录再跑一遍——整份报告若变成"永远 0 阻断"，这条当场抓住它
README.md                用户入口文档：四种启动方式（含双击 .bat）与右键菜单的导入边界、
                         症状→先看哪一项的速查表、功能一览（只列实现到的）、
                         文件位置与 LUHUI_DATA、runtime/ 里两个内核 exe、开发者验收命令
packaging/make_assets.py 生成 assets/app.ico（复用 ui/icons.logo 的几何，七档各自画、手写 .ico 容器）
packaging/make_shortcut.py 桌面快捷方式（pythonw -m app.main + 工作目录 + 图标），建完回读核对
packaging/make_launchers.py 启动脚本、**启动器 exe** 与**可选**注册表模板的生成器：启动播放器.bat /
                         故障自检.bat（GBK+CRLF）、packaging/open_file.py（.reg 那条命令的落点，自己把根塞 sys.path）、
                         packaging/launcher.cs（启动器源码，纯 ASCII、中文全走 \u 转义，csc 现场编）＋
                         luhui-player.exe（注册表里写的就是它：列表里那行字不会是 ProgID 那串英文，
                         拿 pythonw.exe 当落点就会出现"pythonw"且点了不播；显示名由 exe 的
                         **FileDescription** 与 Applications 那根的 **FriendlyAppName** 两处同值供着）＋
                         packaging/reg/*.reg（UTF-16LE+BOM+CRLF，两套各带撤销件：右键菜单一根键，
                         「打开方式」四根键＝ProgID＋Classes\Applications\<exe>＋每个扩展名的
                         OpenWithProgids 里我们那条值＋App Paths\<exe>；**只生成不导入**，
                         本机「打开方式」那套是 2026-10-16 按用户明确授权另行 reg import 的，见 §7）。
                         注册表里取参用 %1 而不是批处理的 %~1；绝对路径来自当前解释器，换机器重跑一次即可。
                         每份产物写完立刻复查**盘上那一份**的字节；--check 只复查，
                         --selftest 是本机现数的已知好坏对照（2026-10-16 收尾行：共 68 条，
                         CAUGHT 55／CLEAN 13／MISSED 0／NOP 0）
tests/test_launchers.py  那七份文本产物加启动器的验收（2026-10-16 现数：共 75 项，失败 0 项，不看像素）
                         + 证伪（同日的收尾行：18 条盘上变异，CAUGHT 18／MISSED 0／NOP 0）：
                         判据来自**真执行**——`cmd /c 故障自检.bat` 必须真打出**每一项**报告（项数取
                         `len(app.doctor.CHECKS)`，不写死数字；只数 `[ OK ]` 也会把"这台机器绿不绿"和
                         "报告齐不齐"混成一格）且 stderr 干净
                         （上一版 .bat 是 LF 行尾，lint 全绿而 cmd 把带中文的行劈开当命令执行、doctor 根本没跑到），
                         open_file.py 要在**陌生 cwd + 干净解释器**里跑到 `import app.main` 才算数；
                         证伪跑两道闸（生成器 --check 与本文件自己的判据都要翻红，且后者必须在坏字节落盘**之后**再算），
                         还原后逐份 md5 对账 + 对照组（全部还原后 --check 重新变绿）
tests/check_openwith.py  「打开方式」那套注册的**只读**对账（不写一个字节）：--check 把模板里的
                         55 节／58 条值逐条与注册表现数比，外加两条"默认程序没被动过"的独立通道
                         （每根扩展名键的默认值＋HKCR 侧读到的同一处），UserChoice 一个字节都不许变；
                         --snapshot/--diff 用两份 JSON 快照对账，只允许三种变化（我们的键新出现、
                         OpenWithProgids 里多出我们那条值、`None → ''` 这种建键必然留下的空默认值），
                         其余一律翻红；--selftest 是本机现数的 16 条已知好坏对照
                         （CAUGHT 11／CLEAN 3／MISSED 1／NOP 1——后两条是**故意**做的瞎判据与空注入，
                         这本账要求它们被记成 MISSED／NOP，见 §6 第 36 条）
tests/check_openwith_shell.py  问**外壳自己的数据源**（shell32!SHAssocEnumHandlers）而不是问 UI：
                         逐个扩展名枚举"能打开它的处理程序"，按显示名**全等**比〈LuHui播放器〉在不在；
                         跑两个 filter（DEFAULT 与 EXE），并自带一个假扩展名哨兵证明 EXE 那条有鉴别力
                         （详见 §6 第 36 条那笔"DEFAULT 没牙"的账）。取显示名只用 vtable 第 4 槽
                         GetMenuName——第 8 槽 GetUIName 在本机一调就 access violation、第 7 槽 GetName
                         返回 hr=0 却把 NULL 写进出参，都是测出来的（`--probe-dead-slots` 可复现）。
                         不建窗口、不写注册表、屏幕睡着也跑
tests/falsify_openwith_shell.py 给上面那把尺注入 4 处静默 bug（走错槽／哨兵把真扩展名当假扩展名／
                         对照组阈值改成不可能满足／命中判定摘掉），逐条要求当场翻红**且点名**；
                         只往沙箱副本 `_mut_openwith_shell.py` 写，副本存在即拒跑，跑完立刻删
tests/e2e_openwith.py    端到端：从 HKCU 现读我们那条 `shell\open\command`，**原样执行**（不是另起一条
                         自认为等价的命令），再断言窗口标题、mpv 子进程、`path` 属性、时间推进与画面像素；
                         `--expect-red <坏路径>` 是反向对照，那本账要求必须红
tests/probe_openwith_*.py / .ps1 一次性探针（不算判据）：API 形状、exe 落点、UIA 树、窗口遮挡状态
                         各探一趟，留档在 tests/out/probe-openwith-*.txt——它们的存在是为了说明
                         "为什么最终这本账不问 UI 而要问数据源"
tests/test_usage.py      资源占用链验收（56 项，不看像素）+ 证伪（9 条盘上变异）：
                         这一份特有的一条判据是**"测到了"必须由一次真测证明**——起一个真在烧 CPU 的子进程，
                         要求 1 秒窗口里量到 ≥50%（把 `time.sleep(window)` 摘掉的写法照样跑出一堆 0.0%、
                         照样"看起来正常"），整机占比必须由同一份单核读数除以核数得出；
                         fixture 只喂"读数"，**不预先写答案**：那一行没量到时该显示什么，得由面板自己的分支产生
                         （先前版本把答案写进 fixture 再断言界面把它显示出来，等于自证，补状态文本那段一行都没走）
tests/test_transform.py  画面族验收（59 项）+ 证伪（32 条坏样本，0 漏网 0 空判）：判据按**证据来源**
                         分三家——①离线表的形状（量程/链顺序/前缀规则/复位覆盖 12 项，不起内核就能判），
                         ②`screenshot-to-file video`（滤镜链改的是解码输出，这条路看得到锐化/降噪/去带/镜像），
                         ③**固定 1280×720 宿主 QWidget** 里的 `screenshot-to-file window`
                         （比例/充满/平移那几项改的是"往窗里怎么摆"，只有这条路看得见黑边和位移）。
                         这一族里"生效"的口径是**日志干净 + 像素真的动了**两条一起；只看 `vf` 列表的判据
                         恒真，所以一条都没写。--falsify 除了喂坏观测值，还当场把产品承重件摘掉重跑离线半
                         （夹紧、取模、`format` 前缀、链顺序、复位漏项、日志标记表……14 处），
                         最后一条判据是"全部还原后离线半重新全绿"——还原不许是嘴上说的
tests/probe_transform*.py 内核实测脚本（0..12 外加接线级 probe_transform_engine.py），留档在
                         tests/out/probe-transform*.log：transform.py 头部九条结论每一条都指得到某一份 log。
                         换 mpv 版本后 test_transform 有一条判据专门钉"表依据 == 本机版本"，
                         就是把"别人机器的数"挡在"实测"这个词外面的那道闸
tests/test_equalizer.py  声音均衡族验收（87 项：表 33／引擎 10／面板 30／真机 14，计数由脚本自己现数，
                         不是文档抄一份）+ 证伪（注入 22 处、抓到 22 处、漏网 0 处）。四家的**证据来源**
                         不同，所以不许互相顶：①表——夹紧、拼串、括号里不许有逗号、前置的 `dB` 后缀、
                         空链＝空串、反解器认不认；②引擎（真 `MpvPlayer` 但不起内核）——内核没在家时
                         `set_audio_eq` 不去问 `af`、纯读不把"没人应答"发成"链坏了"、`build_args` 里的
                         `--af=` 与表同步；③面板（假内核）——拖动途中零次同步往返、松手发且只发一次、
                         表外档位一条不发、滑轨画的是**读回**的那个数；④真机（本机 mpv + 真实输出通路）——
                         启动参数落地、播放中热切读得回来、归零干净清空、坏值**绕过外壳**直喂内核时日志里
                         真有原话。假内核不是"预写答案的 fixture"：它按上一次发出去的那条命令回一片 `af`
                         （形状照实测 ⑥ 那一腿），拼串歪了它照原样回报，红由反解那头发现；而"假内核与真引擎
                         逐方法同构"由一条 AST 判据钉着（签名＋`if` 条件集合），它哪天悄悄宽松一点就红。
                         这一族"生效"的口径同样是**读回的形状 + 日志里没有失败原话**两条一起——只看 `af`
                         列表的判据恒真（坏值时列表照样挂着名字，而已经一声不出了）。`--no-kernel` 那半
                         把真机腿记成 ENV 判废（退 3），不写成产品失败
tests/probe_eq.py         af 族内核实测（腿号 ⓪①②③③b④a④b⑤⑥⑦，留档 tests/out/eq-probe-r15b.log）：
                         `engine/equalizer.py` 头部每条结论都指得到某一份 log 的某一条腿；热切只在
                         **④a** 那条腿量得到（带视频轨 + `--force-window=yes` 把管线拖到≈实时：
                         标定腿实测"墙钟 68.03 s 里 time-pos 走到 60.00 s"＝≈0.88×）——
                         **④b** 那档"纯 `ao=pcm` 全速抽干 90 s 粉噪"实测 0.88 s 抽完（≈103×，
                         该腿标题里那句"≈150×"是另一趟的同量级读数），发命令时 `time-pos` 已经报 90.0，
                         也就是切点落在产物之外 ⇒ **这一腿没有证据**（所以"不开窗口能不能量热切"
                         的答案是不能；⓪ 那条标定写的是同一件事的短媒体端：8 秒音频 0.58 秒抽干 ≈13.8×）
tests/probe_eq_volume.py  只问 `volume=` 那几串写法的一次性量具（留档 tests/out/eq-vol-r12.log）：
                         `volume=-12`（不带 `dB`）是**线性乘 12**，实测 +17.9 dB 并削顶——这一条是
                         前置必须带后缀的那笔账，`equalizer.py` 第 6 条与 §3 第 11 条引的都是它
tests/probe_surface_cover.py
                         「加一页，逐屏体检会不会静默漏掉它」的结构性尺子（§6 第 34 条的后续，
                         留档 tests/out/surface-cover-r12.log 7/7、tests/out/surface-cover-falsify-r12.log
                         注入 7 处 / 抓到 7 处 / 漏网 0 处）：`capture_sweep.py` 里加一页要动**四处**
                         手抄名单（`SURFACES` 一行、`s_sidebar_*` 捕法、捕法里 `self._sidebar(N)` 的
                         下标、`widget_of` 的 table 一条），少抄任何一处都不报错——那一页就是"永远
                         没有档"，整趟体检照旧全绿。这一把只把两张表从源码里**解析**出来对账
                         （AST，不建窗口、不抓屏、一个字节不写 `tests/out`），所以它既不等屏幕，
                         也不与看门狗那条腿抢 `tests/out/sweep/`（§6 第 33 条 (a) 那条串行纪律在这儿
                         根本不构成约束）。第 0 条判据先问量具自己瞎不瞎（三处名单解析出空表不许
                         冒充全绿），最后一条兜"名单齐了但配错页"。第一版就在这两条之间红过一次：
                         它拿 `sidebar-audio-eq` 的键尾段去比方法名 `s_sidebar_audio_eq`——Python
                         标识符不能带连字符，红的是判据不是产品（按"红灯先问判据"改的是判据，
                         归一化写在 `slug_of()` 的注释里）。NOP 分支带对照组验过：把其中一条注入
                         换成"什么都没改"的假注入，台账必须记 NOP 并按没抓到算（rc=1），
                         不让一条空操作冒充"抓到"。
                         **2026-10-13 起它不再只是"记得跑才算数"**（上一版这里写的是"不在任何一条
                         常跑的链上，接之前不许写成已经防住了"）：`test_ui.py` 的结构段挂了一条
                         Qt-free 对账腿（`PSC.load()` → `PSC.judges()`，判据条数下限也钉住——空表
                         不许冒充全绿；读盘／解析／import 三种失败各记 ENV 不记产品红，也不许把
                         整趟顶成 NameError），证伪台账另加一条 `drop_surface` 注入（带 NOP 判别：
                         三张表逐张比过，没改动任何一张就不算抓到；基线红也顶掉"抓到"）。
                         r14 那趟两条都 CAUGHT，读数在 §6 第 35 条。
tests/probe_playtip_teeth.py 「播放键说法翻转」那把尺（#73）在**真内核 + 真窗口**通路上的牙。
                         为什么纯函数台账（U6h 八条形状）不够：那八条不经过内核、也不经过 Qt 的
                         跨线程投递，而 #73 恰好坏在投递时序上（`player._apply()` 先写缓存再发
                         `sig_pause`），所以"这把尺在真跑的链上红不红"必须用真链量一次
                         （留档 `tests/out/probe-playtip-teeth-r15.log`：两趟独立跑各 **14/14 rc=0**，
                         14 条判决行把"耗时=Nms"归一后**逐字节相同**（第一趟留底
                         `probe-playtip-teeth-r15-firsttry-0152.log`，脚本 md5 56acb4f8…；第二趟 md5
                         d9034209…，只多了"跑法"那一行抬头 ⇒ 两趟不是同一份字节，说"独立"是指
                         两次真执行，不是说同一份脚本跑了两遍）。两种坏法——
                         **M1 摘回旧映射**（暂停时说「暂停」）⇒ 新尺 2 秒不放行，且读数证明拦住它的
                         是说法那半（内核 `pause=True` 已经到了），旧写法在这一刻同样不放行；
                         **M2 把说法冻成常量「播放」**⇒ 新尺不放行（tip 与按之前**一字不差**），
                         而旧的 `startswith("播放")` **放行** —— r13/r8 那六趟以上"偶然绿"的形状
                         是被**造出来**的，不是事后讲的道理。三道不许松的断言：① `mut != base`
                         （第一版探针改的是 `type(bar)._play_saying`，那是**空操作**——登记里存的是
                         构造期取到的**绑定方法对象**，换类属性换不动它；不验"变异真的改了字面"就
                         差点把"尺没牙"写进台账）；② 每摘一次还原后跑一条控制腿**重新变绿**
                         （证明"没放行"不是探针自己立不起来），且开跑前当场断言 `test_ui.APP` 已绑定
                         ——上一版没绑，`pump()` 在 `APP is None` 时直接 return，于是"重画永远不来"
                         被读成"尺拦得住"；③ 退出前 `controls_bar.py`／`form_rows.py` 的 md5 与进入时
                         一致（本趟只动内存里的登记项，产品文件一个字节不写）。
                         要真桌面会话（同 `test_ui.py`：offscreen 下起不了 mpv 的 wid 嵌入）；
                         复跑命令打在这份日志的第一行。改 `_play_saying`／`_on_pause`／
                         `player._apply` 那三处的送达顺序时复跑它。
tests/probe_tally_teeth.py 「证伪台账自打收口」（#74 U59）自己的牙。为什么 U59 不够：它是一条
                         "已知答案＋接线形状"的判据，形状那条只证明**没被人改回去**，不证明某个坏
                         行为现场发生过——那话必须明写，不许混进"全被抓到"那批里充数。所以这份探针
                         把五种坏法真摆一遍（只读源码、只跑纯函数，**不碰产品文件**，因此能在
                         `--falsify` 那种原地改文件的趟之外单独跑）：M1 把台账数成验收那本账、
                         M2 判决行的标签另算一套、M3 自打排在分流之后、M4 干脆不印、M5 退回就地重数。
                         每条先断言 `mutated != base`（空操作记 NOP 不算抓到），再要求翻红**且点名**
                         它坏在哪一家（①数法／②标签／③位置／④接线）。另一条腿是**真执行**：往
                         `falsified` 里摆六行已知答案（含元组／整数 1／空串三种非布尔），当场调
                         `report()` 抓输出，要求回执自打的数＝按桶重算＝同一份输出里逐行数出来的
                         标签，且这一趟 rc=1（有 MISSED 时"量具不可信"必须压过"环境没给"）。
                         第三档 `--selftest` 把判据换成恒真的桩，要求五种坏法**全部**暴露，否则
                         这条探针自己就是句空话。留档 `tests/out/probe-tally-teeth-r16.log`
                         **01:48 回盘上数**：md5 `dbd192cbfb7b6c4803984e42f0d8a445`、1435 字节、
                         23:37:28 落盘（这条引用挪过一次：23:10:09 那份是 1694aca3…，是同一条命令第一次
                         写的留档；之后为保住 `test_ui.py` 已引用的字节而撤销一处纯排版改动，同一命令
                         顺手重跑了一次 ⇒ 日志被重新生成。**教训：重新生成留档会让文档里已抄走的
                         md5／时刻作废**，这里以盘上这份为准）。01:49 原地复跑过一遍作**可复现性**核对，
                         输出重定向到临时文件、这份留档一个字节没动（mtime 仍 23:37:28）：
                         复跑的 stdout 与留档第 2–7 行**逐字节相同**（两份 md5 都是 `a49977e8e9dd…`），
                         `RC_PROBE=0`、`RC_SELFTEST=0`，且换调用形状（重定向到文件／接终端／喂 `/dev/null`）
                         rc 全是 0 ⇒ 这条量具的输出确定、rc 不随形状变，文档里的数不是手抄的孤本。
                         复跑命令打在这份日志第一行；改 `report()` 收尾那几行、或改分桶口径时复跑它。
                         挂法与 `probe_playtip_teeth.py` 同：**U59 那条在 `--falsify` 台账里**，
                         随看门狗八趟链每轮都跑（形状漂了当场红）；这份五注入探针是手动档，
                         因为它不改产品文件、也不需要真屏幕，塞进那条链只会把"八趟都要真平台通路"
                         这句前提弄混。
tests/test_engine.py     内核层验收（静态 51 个 `check(` 调用点、本机 grep `^\s*check(`；2026-10-20
                         实跑 **50/50**——差的 1 个是 `--falsify` 才走的那条，静态点≠实跑条数，
                         汇总行由脚本自己现算，别把 grep 数抄成"实跑"。上一轮 48/48 见
                         tests/logs/test-engine-relpath-004829.log）+ 证伪（静态 32 个
                         `falsified.append` 点、实跑 31/31 CAUGHT / 0 MISSED，收口行由脚本自己打；
                         旧的 30/30 见 tests/logs/test-engine-falsify-004837.log）：
                         IPC 往返、属性订阅、seek/倍速/音轨/截图、崩溃重启、量程与枚举的内核真值、
                         perf 白名单（非法档必须拼不进参数且要留回退说明）、
                         静音三态（起点跟随 config.mute → 翻面 → 翻回，配 _fire 摘桩注入）
tests/test_ui.py         外壳验收 + 证伪（--falsify）。**计数分两种，别混着抄**：
                         静态调用点（本机 grep，行首口径 `^\s+check\(`；**数于 2026-10-12 加完「声音均衡」
                         那一页之后——下面这串 151／10／72／79／6／182 已被下一段的 2026-10-13 现数
                         订正、只当历史**；再上一版记的 140／66／8／171 是 R8 时代的数、同样过期）：
                         **151 条 `check(` + 10 条 `pixel_check(`**、
                         **72 条 `falsified.append(`**（其中第 136 行那条在 `f_case` 自己肚子里，
                         所以证伪台账的静态注入点是 71 直接 append + 8 `f_case` = **79 行**）、
                         **6 条 `env_check(`**（那一句"旧记的 8 与现数 6 差两条"在下一段已经解开：
                         差的不是口径，是这一轮真的新添了 4 条）；宽口径的 `check(` 会把三个定义、
                         注释、`tabs_fit` 里的调用一起数进来，这次量到 **182** 条，多 31 条。
                         静态数**不是**实跑数：下一轮改判据就照这四条 grep 再数一遍，别抄上面这串。
                         **数于 2026-10-14 01:47:03**（对着盘上现那份 `tests/test_ui.py` 重数，六个数与
                         23:07–23:10 那趟逐位相同：152／11／78／8／10／188。中间 `test_ui.py` 只被撤销过
                         一处纯排版改动，md5 回到 `d7a2cde6151a…`，也就是 23:04 与 23:07 两份留档背后的
                         那一份字节；它的 mtime 23:37:12 比这两份日志晚，不是"出日志之后又动了产品"，
                         是那次撤销的落盘时刻。上一串 152／11／76⇒83／10／188 数的是 #73 之前、
                         再上一串 151／10／71⇒79 数的是均衡器那页之前，保留只当链条、别再抄）：
                         **152 条 `check(` + 11 条 `pixel_check(`**（#73/#74 都没改验收台账的条数）、
                         **78 条 `falsified.append(`**（第 136 行那条仍在 `f_case` 肚子里 ⇒
                         注入点是 77 直接 append + 8 `f_case` = **85 个登记点**；登记点 ≠ 台账行数，
                         实跑 84 行，差额见下面那段对账）、
                         **10 条 `env_check(`**（#70/#71/#72 那轮真的新添了 4 条——侧栏覆盖那三条失败出口
                         （import／读盘／解析）各一条、悬停预览的指针争用一条；#73/#74 没加失败出口）；
                         宽口径 `check(`（含定义、注释、`tabs_fit` 里的调用）量到 **188** 行，多 36 条。
                         **回执自己打收口**（#74，`tests/out/falsify-ui-r16.log` md5 4e08c7f9…、
                         23:04:21 落盘、rc=0）：台账末尾多一行
                         `---- 证伪台账自打：共 84 行 —— CAUGHT 84／MISSED 0／ENV 0／非布尔 0 ----`。
                         这四个数由 `falsify_tally()` 从 `falsified` 当场算，**出处不再是"我逐行数行首"**；
                         我仍然独立数了一遍作对照（同一份日志 `^\[CAUGHT\]`=84、`^\[MISSED\]`=0、
                         `^\[ENV \]`=0、`^\[JUNK\]`=0），两边逐位相同才敢把这句自打当证据——
                         自打与外数只有一边对，就是又造了一处没人核过的话。
                         顺带新增一家 `[JUNK]`：记回来既不是布尔也不是 None 的行（典型＝忘了取元组的
                         `[0]`，恒真的元组会冒充"抓到了"）。旧口径把这一类打成 `[ENV ]`，于是"量具记错
                         了类型"被读成"今天屏幕不给"；分桶一律走 `is`，不用 `in (True, False, None)`
                         ——后者把 1 当 True、0 当 False，"串回来一个整数"能混进 CAUGHT。
                         **静态与实跑当场对过账**：01:30 屏黑那趟（`falsify-ui-r15-local.log`，自打
                         还没上线）79 CAUGHT + 4 条"要靠真屏幕、本轮不作数" = **83 行**；屏亮那趟
                         （`falsify-ui-r15.log`）83 条 CAUGHT、同样 83 行。#74 加了 U59 一条 ⇒ 应到 84 行，
                         r16 实测正是 84。静态 85 与实跑 84 差的那一处**不是漏网**，是
                         `except BaseException as _p6f_e` 那条腿（现 `tests/test_ui.py:4742`——同一个引用
                         在 #74 之前记的是 4662，上面插进 80 行它就漂了，**认锚点文本、别认行号**）：
                         它只在"侧栏覆盖对账"那把量具自己读盘／解析崩掉时才登记（一登记就是 MISSED、
                         rc 掉 1），健康跑必然不出现；对照组 U57-A／U57-B 不在证伪台账里（走 `check()`／
                         `pixel_check()` 在台账抬头之前打印）。MISSED=0。
                         上一版这里写的是"79+4+1 对照=84，与静态 84 逐行对上"——那是**凑数凑对了总数**：
                         拿一条不在台账里的东西补差，恰好把"有一条腿平时不响"这件事盖掉了。
                         这条自打**自己有没有牙**不在这里自证，看 `tests/probe_tally_teeth.py`
                         （U59 是"已知答案＋接线形状"，弱于真注入，所以另摆五种坏法逐条要求翻红）。
                         这一轮新增的四条判据各归其位：结构段那条 Qt-free 侧栏覆盖对账进 `check(`，
                         它三条失败出口进 `env_check(`；U6e/U6f/U6g 三条进证伪台账；
                         悬停预览那条按"真光标在不在轨道里"分流，ENV 走 `env_check(`、产品侧走
                         `pixel_check(`（同一判据两条出口，所以 `pixel_check(` 只 +1 而 `env_check(` +4）。
                         实跑数要看屏幕给不给像素。**R7（看门狗等到的那一轮，2026-10-11 01:59–02:02）**：
                         证伪 **72 CAUGHT / 0 MISSED**、rc=0（tests/out/falsify-ui-r7.log）；
                         验收 **153/154**、rc=1（tests/out/accept-ui-r7.log——那唯一一条红是外来窗口
                         压住视频区，属"环境红冒充产品红"，修法与修后的数见 §6 第 31 条）；
                         逐屏体检 `--screen` 标准档 / 1.3 / 1.8 三档各 **50 条全绿**、rc=0
                         （tests/out/sweep-ui-r7*.log，`ui-round-done.flag` 五趟哨兵齐全）。
                         **R8（①–⑤／U52–U57 全数进去之后，同一台机、同一套屏，02:11/02:12）**：
                         证伪 **73 CAUGHT / 0 MISSED / 0 ENV**、验收 **154/154 / 无失败**、两趟都 rc=0
                         （tests/out/falsify-ui-r8.log、tests/out/accept-ui-r8.log）。
                         更早的两份只当历史：亮屏整跑 149/149（tests/logs/test-ui-rel-20261011-005120.log，
                         那是 §6 第 30 条五条形状验收**加进来之前**的数）；屏已暗那一趟
                         69 CAUGHT / 0 MISSED / 3 ENV、rc=3（tests/logs/test-ui-falsify-20261011-014829.log，
                         三条要靠真屏摆出来的坏样本本轮没证据——见 §6 第 22 条，rc=3 是环境不是产品）。
                         **判决条数 ≠ 调用点条数**：有判据在
                         循环里逐条记账（`tabs_fit` 240..520 每档一条），所以下一轮要盯的是「零 MISSED」，
                         不是拿调用点数减出来的数。历史那组 60 CAUGHT + 1 MISSED + 2 ENV
                         （tests/out/falsify-ui-r4.log）已经过期——那条 MISSED 是遮挡闸门没走环境闸门，
                         修掉之后就是 §6 第 22 条，别再抄进新一轮。
                         页签判据（`tabs_fit` 走 240..520 全量程＋`_clamped_sidebar_width` 的量程收口）
                         在换行改造后按「第几排」分组，量具与实现共用同一对
                         `_tab_rows_of`／`_in_strip` 口径（§6 第 26 条）。
                         形状闸那五处的验收与证伪（①–⑤／U52–U56）全部写成**关系式**：
                         两套装配（有没有跑 `theme.install`）量到的绝对像素不同，写死像素的判据
                         换一路装配就红得没有道理（理由与实测数字见 §6 第 30 条）。
                         八处吃真抓屏的判据走 `pixel_check`（§6 第 31 条）：挡路的窗口全属别的过程 ⇒
                         记 ENV 不进台账，自家浮层没抬起来 ⇒ 照常记红。
                         真窗口、真内核、真截屏；抓屏前有两道闸门——遮挡闸门点名"挡在最上面的是谁"，
                         环境闸门判"屏幕此刻给不给像素"（显示器休眠时整屏连任务栏都是 0，
                         那算环境不允许，退出码 3，不许写成产品坏了 5 件事）
tests/test_shutdown_quiet.py 退出收口守卫（#109）：A 结构闸门（AST：池子只许 poolkit 造／
                         工作线程里不许裸发射／closeEvent+aboutToQuit 两条路都得接上收口）、
                         B 锁可重入、C 发射兜底 + pin 的**配对**对照（不 pin 必须死）、
                         D/E 真退进程（在途收口、拆 store、杀孩子）。计数与判决行全部由脚本
                         自己打印：2026-10-20 05:13 实测汇总 `13/13` 通过、rc=0
                         （`tests/out/test-shutdown-quiet-051304.log`），
                         `--falsify` 39/39 抓到／漏网 0／变异失效(NOP) 0、rc=0
                         （`tests/out/test-shutdown-quiet-falsify-051325.log`）。
                         **它为什么不拿"在途退出 8/15 崩 → 0/8 不崩"当判据**：04:48 复跑五组
                         （`tests/out/probe-exit-crash-postfix-0448.log`）时连"把 pin＋emit 整项
                         摘回修前形状"的 G 组都 0/6 干净 ⇒ 这一台今天复现不了那个 race，
                         race 当不了阳性对照。改押两条确定性的：同一场面（工作线程里
                         `sip.delete(壳)` 之后发射）裸写每次 `0xC0000409`、走 `poolkit.emit`
                         每次 rc=0 且真吞到 2 条；收口值不值看 E-vs-D 的退出耗时
                         （6.44/6.64/6.64s → 0.81s，且 `killed=1` 不留还在写盘的 ffmpeg）。
                         缺 ffmpeg 或缺 4K 样片时 D 族五条记 ENV 并点名，不记产品红、不静默跳。
tests/_probe_exit_crash.py 一次性分布探针（**不是判据**，跑完留日志即可）：五组 ×N 趟量
                         "在途退出／排空再退／收口再退／摘掉收口／摘掉 pin"的 failfast 分布，
                         每行即时 flush、每趟带 faulthandler 周期栈。判决块写死四分支：
                         D 组前提不成立 ⇒ 量具失灵；G 组干净 ⇒ 有效性未证；D 干净且 G 翻红 ⇒
                         才准写"修好了"。04:48 那趟走的是中间那一支（"这一台复现不了"）。
tests/test_thumbs.py     缩略图链验收（21 项）+ 证伪（11 项）：裁格几何用独立单帧对照，坏网格必须判错位；
                         新增 sheet 记忆的 LRU 上限（命中续命、最久没碰先走）与两条打桩生效对照；
                         2026-10-26 读数 `tests/out/test_thumbs.log` 21/21、
                         `test_thumbs-falsify.log` 11/11
tests/test_library.py    媒体库链验收（64 项）+ 证伪（14 项）：时长解析、索引复用、封面取帧、坏容器降级，
                         以及扫描回包的**墓碑/现根过滤**与在途 refresh 排队补扫（§6 第 41(i) 条）；
                         2026-10-26 读数 `tests/out/library-r22i3.log` 64/64、
                         `library-falsify-r22i3.log` 14/14
tests/test_responsiveness.py 界面响应性验收（24 项）+ 证伪（32 项）：**只数次数、不掐表**，offscreen 起跑，
                         不起内核、不起 ffmpeg、不抓屏。账目——逐条问进度→真读盘次数、整表重建→stat 次数、
                         击键→整表重建遍数、playlist 推送→清造行数、缓存成功→全目录扫描次数、封面→同步解码
                         张数/分片数、侧栏宽→卡片列数（按**当前视口**再算一遍当参照）、空态三态翻面，
                         以及 B 系列六笔（封面命中→摸卡数、同几何 refit→铺窗数、图标→命中/封顶、
                         最小化→轮询拍数、目录展开→交回条数、失败负缓存→条目数）；每条判据都配一条
                         "拆回原样"的注入，且判据函数被两趟复用。2026-10-26 读数
                         `tests/out/resp-r22i6.log` 24/24、`resp-falsify-r22i6.log` 32/32
tests/test_win_geom.py   窗口几何记忆验收（23 项）+ 证伪（6 项）：纯规划器的坏存档/拔屏/负坐标/
                         全窗夹进可用区/无存档居中，以及主窗构造期恢复、拖动停手才写盘、
                         保存失败不穿出 closeEvent；
                         2026-11-09 读数 23/23、证伪 6/6
tests/test_help_page.py  快捷键一览页验收（14 项）+ 证伪（11 项）：65 项动作不重不漏分进 8 组、
                         两列表格逐行同 x（真 QTextBrowser 的 cursorRect，不 grep 源码）、
                         多键并一行、搜索/清空、尖括号转义；`help-page-r22h.log` 14/14、
                         `help-page-falsify-r22h.log` 11/11
tests/test_chapter.py    章节跳转验收（13 项）+ 证伪（21 条）：`chapter_target()` 的边界、`chapter_step`
                         发绝对下标且连按不丢步（1.5s 期望窗）、`set_chapter` 越界不发命令、菜单形状、
                         主窗两条接线与 OSD 话术、"只读缓存不发同步请求"；2026-10-26 读数
                         `tests/out/test_chapter.log` 13/13、`test_chapter-falsify.log` 21 CAUGHT／0 MISSED
tests/test_session_state.py 会话五键闭环验收（4 项）+ 证伪（13 条）：内核真值→cfg 内存→盘上 json→
                         新进程读回→`build_args` 的 argv，两端都用真数据；音量真值 0、字符串
                         "no/false/0"、越界值收口；`session-state-r22c.log` 4/4、
                         `session-state-falsify-r22c.log` 13/13
tests/test_engine_async_real.py 异步内核握手验收（7 项）+ 证伪（5 条，真起 runtime 内核）：
                         `begin_start` 毫秒级返回、正常握手 0.42s、内核早退/管道不出现/超时都
                         收干净并点名 code；`engine-async-real-r22b.log` 7/7、
                         `engine-async-real-falsify-r22b.log` 5/5
tests/test_ui_polish.py  界面收口验收（8 项）+ 证伪（11 条）：标准档 1033px→880 紧凑档（18 颗全在、
                         30px／音量 72／行距 0、无出界）、回差 1050/1075/1085、关闭钮悬停换像素、
                         主按钮三态、统计页空态；`ui-polish-r22g.log` 8/8、
                         `ui-polish-falsify-r22g.log` 11/11
tests/probe_placeholder_cost.py 一次性量具（复现入口）：3000 张卡的整表重建与占位块补画耗时
                         （offscreen，不弹窗）；2026-10-26 读数 `tests/out/probe-placeholder-cost-r22i.log`：
                         `rebuild=27.8ms sync_decodes=0 placeholders_made=2`、
                         `refresh=8.7ms／缓存命中 8.4ms`（一帧预算 16.7ms）
tests/probe_rebuild_cost.py 一次性量具：整表重建那一趟里"哪一步贵"的本机读数（N=300，offscreen），
                         跑完即可丢弃，留档 tests/out/probe-rebuild-cost.log
tests/test_keymap.py     键位事实表验收（25 项）+ 证伪（38 组，0 漏网 0 空判）：死写法/移位标点逐类夹住、
                         diff 往返、别名撞键折叠、工具窗镜像的筛法（按键值不按字面串）、导出导入；
                         已知答案对照 17 死／26 活／12 按不出，出厂表自证零撞键是其余条的前提
tests/test_keymap_dialog.py 改键对话框验收（17 项）+ 证伪（21 组）：五态捕获、改/删/增落盘成 diff、
                         撞键禁存、行标红等于 bad_pairs、连点保存幂等、导出写不进路径要说清原因
tests/test_gif_dialog.py 工坊对话框验收（16 项）+ 证伪（21 组）：区间夹取、上界跟着剩余时间、A-B、
                         字幕勾与配置双向、进度槽兜类型、键位镜像装两次只响一次、八种状态×十一个入口不许抛
tests/test_gif_wiring.py 主窗口接线验收（19 项）+ 证伪（33 组）：替身与真对话框同一张接口表、
                         gif_state 只读缓存不发同步请求、连拍参数夹量程与脏值收口、打开工坊时递镜像
tests/smoke_launch.py    用户入口端到端：真起 python/pythonw 进程，看屏幕、转交、关窗清理
tests/audit_qt_attrs.py  静态排查 app/ 里每个 Qt 成员写法在本机 PyQt5 上是否存在，四遍：
                         `Qt类.成员`／局部变量．成员／`self.属性.成员`／`self.方法(…)` 的参数表；
                         --selftest 带已知答案对照（**14 条**，2026-10-12 数 `[PASS]` 行现得，
                         2026-10-13 02:14 交账时又现跑一遍仍是 14 条 PASS／0 FAIL、rc=0，
                         `tests/out/audit-selftest-finalcheck-20261013.log`——这里原先
                         记的 13 是更早一版对照表的数，加一条对照没人回来改文档，就一直漂着），
                         并逐条钉住"它看不见什么"（盲区不许假装看得见）。
                         解析那一步（find_bad）也归它自己管：`from PyQt5 import QtCore` 之后写
                         `PyQt5.QtCore.PYQT_VERSION_STR`，只 `hasattr(PyQt5,"QtCore")` 会判成
                         "PyQt5 里没有 QtCore"——那是审计器**自己没导**，不是产品没有（app/doctor.py
                         那两行就这么连报两条假红，而 --selftest 当时照样全绿：这一步原先没对照）。
                         补了五条对照，并且前两条跑之前先把子模块从父包上摘下来——不然前面几遍
                         早已连带导进 QtCore，判据在坏实现上也判绿（实测如此），摘不到就自己判不作数
tests/test_gif_real.py   截图工坊真机验收（23 项，2026-10-13 r15 起为**实测口径**，不再是静态点数）：
                         按 G 真开窗口、真 ffmpeg 导出、产物逐帧解出来判"会动"、
                         同区间带字幕/不带字幕各一趟比"字真烧进去了"、**工坊是活动窗时**按 B 真截出 N 张
                         （活动窗与镜像键当场验，立不住就回 rc=3 而不是硬判）、导出中途关窗真不留 ffmpeg；
                         --falsify 12 组已知坏样本必须逐条翻红（含"一张没截也报没再涨"这类恒真的绿），
                         --residue 只判关窗残留（4 项，一个像素都不看，显示器休眠时也能跑）。
                         三档 r15 都真跑过：`tests/out/gif-real-r15.log` rc=0、24 条 PASS（其中 1 条是
                         前提，判据 23）；`tests/out/gif-falsify-r15.log` rc=0、12 条 [CAUGHT]、
                         收尾"证伪 12 组，漏网 0 组"；`tests/out/gif-residue-r15.log` rc=0、4 条 PASS。
                         静态数 `check(` 字面 **28** 处 = 1 处 `def check(`（第 67 行）+ **27** 处调用，
                         那 27 与两档实跑逐函数对上：`run_real` 23 + `run_residue` 4 = 27 ⇒
                         台账里"23 项／12 组／4 项"从此有实跑背书，而 27 这个数本身只是"判据条数"，
                         不含前提那一条，也不含定义那一处（混了就会误以为差一条漏跑）。
tests/watch_gif_real.py  看门狗：轮询"屏幕给不给像素"，条件一翻转就把 test_gif_real 的三种模式整链跑完，
                         产物落 tests/out/FINAL-gif-real-<utc秒>.md + DONE-<同一串>.ok；
                         自带 8 小时超时（到点也留"跑到哪一步"的说明，绝不静默消失），每步各自 try/except
                         免得一个 probe 卡住吞掉后面的截图，且按命令行自身 pid 排除做幂等
tests/capture_sweep.py   逐屏留档（与 test_ui 分开的理由：那边问"功能对不对"，这边问"有没有画坏"）。
                         每张表面一份 PNG + 五把机械尺（有没有墨、字有没有被截、裁了却没留线索、
                         两块文字有没有叠、整块掉在横向裁剪框外）＋一张跨主题对比表。第四把"压扁尺"是 2026-10-10 补的：
                         前三把量的是"格子放不放得下这行字"，而侧栏页签的坏是**布局把格子压小了**
                         ——`contentsRect()` 不含 QSS 的 padding（实测文字 48px、格 54px、留给字的
                         只有 34px），挂着 tooltip 又被第二把按设计放过，于是一字不切地报了三周绿。
                         宽的一档比 `sizeHint`（样式自己报的需求），高的一档比 `heightForWidth(实得宽)`
                         ——照抄 sizeHint 的高会把每一页的说明文字全判成压扁（wordWrap 的 sizeHint
                         不知道最终宽度；本机实测实得 944 时它猜 180×60）。已知宽报半径写在函数
                         文档里：sizeHint 带 11~17px 的通用松弛，所以它管"布局压破了控件的最小需求"，
                         "字此刻少没少"仍由前两把管。
                         第五把「掉出视口尺」（`beyond_viewport`，2026-10-10）治的是另一类结构性盲区：
                         侧栏每一页是 QScrollArea 里摊开的一张子控件，页比视口宽时整页被横向裁掉，
                         而前四把都拿控件自己的 `contentsRect()` 当裁剪框——每个子控件自己看自己都
                         「放得下」，坏在页的右边，四把尺全绿（§6 第 25 条）。它把控件映射到 viewport
                         坐标后再比右沿/下沿，两条 Qt 陷阱钉在函数文档里：`QRect.right()` 是 `x+w-1`
                         （容差得写成 `r.right() > host.width()` 才与旧算式逐字节等价），以及换行后
                         按钮在行盒里的 `y` 恒为 0。离屏那半走 `render_to()`，屏幕休眠也照样画得出
                         真东西；mpv 画面像素、DWM 阴影、遮挡闸门那半要亮屏，走 `--screen`，
                         不给像素就整段 rc=3。本机 2026-10-11 实跑（默认字号档）：`--screen` 验收 50 条全过
                         （tests/logs/capture-sweep-screen-005824.log）、`--falsify` 52 条已知坏样本
                         全翻红、0 漏网（tests/logs/capture-sweep-falsify-005813.log）
                         **每张需要亮屏的表面自带状态指纹**（2026-10-13 r15 补的，`sw.notes[name]`）：
                         起因是 r14→r15 截图工坊那一面同形状墨迹从 6.62% 涨到 6.96%，五把尺全绿却"和
                         上一轮不一样"——逐像素一比，5304 个改动点**全落在 y=335..413 那一条提示行上**
                         （gif 工坊的「还剩 X 秒可导」随播放进度变字）。这不是产品坏了，也不是尺坏了，
                         是**跨轮比绝对墨迹时没有把"这一面的此刻状态"记下来**。修法既不是放宽阈值也
                         不是把这一面摘掉，而是给回执补上原文（`s_gif` 现在写「起点=… 提示行='…」），
                         下一个人再看到墨迹漂就先比指纹；判据本身一个字没动。r15 之后同一趟
                         `tests/out/sweep-ui-r15b.log` 回到 6.64%（与 r13 的 6.63%、r14 的 6.62% 同档）
                         并带出指纹行，那趟 rc=3、49 条 PASS／0 条 FAIL／0 条 ENV（比 r15 的 51 少两条，
                         因为显示器在真抓屏的最后两条之前就睡了），末尾那行按 rc=3 的规矩点名
                         "抓屏那几条不作数"，记成环境红而不是产品红。PASS 条数（50→51）随表面数长，
                         只当读数看，不进任何判据。
tests/watch_ui_round.py  另一条看门狗，**借 test_ui 自己的量具**（`capture_health()`）判屏，
                         条件一翻转就串跑 UI 证伪 → UI 验收 → capture_sweep 逐屏档，三份 log 各自的
                         rc 写进 tests/out/ui-round-done.flag；第二轮起跑前重看一次屏，屏幕又暗了
                         就把逐屏档记成"跳过"而不是判产品坏；同样 8 小时超时、到点留"跑到哪一步"。
                         写它的目的是"停着的尾巴不许丢回给人"——锁屏时不该要求谁去解锁跑三条命令
tests/check_lnk.py       点桌面 .lnk 这条真实双击链路：ShellExecuteW→窗口出现→进程路径核对→关窗无内核残留
```

## 5. 三轮交付范围

**第一轮（内核可用 + 外壳成型）**：IPC 层与进程管理、mpv 嵌入、无边框 Fluent 外壳与主题、
控制条（时间轴拖拽/缓冲显示/章节点、播放暂停停止、上下曲、帧步进、倍速、音量、静音、循环模式）、
播放列表、右键菜单雏形、快捷键、全屏、截图存帧、真机验收。（桌面快捷方式与应用图标原列在第三轮，
第一轮就做了——不然用户没法双击进来试。）

**第二轮（功能对齐成熟播放器）**：媒体库（文件夹扫描+缩略图+最近+断点续播）、时间轴悬停缩略图、
音轨/字幕轨切换与音频延迟、字幕样式（字号/颜色/描边/位置/双语 sub2）与外部字幕加载、
画面（宽高比/裁剪/缩放平移/旋转/去带/锐化）、HDR 色调映射与色彩管理、性能档位（hwdec/gpu-api/解码质量预设）、
音量增强到 200%、音频设备切换、睡眠定时器、A-B 循环、书签跳转点、统计面板。

**第三轮（超越项 + 收尾）**：GPU 超分/高质量缩放预设、逐帧画质增强滤镜开关、快捷键自定义与导入导出、
多窗口/多实例、外观（主题色/透明度/字体）、GIF/连拍截图、性能与内存实测（帧率/掉帧/CPU/GPU 占用报表）、
启动脚本与可选的右键菜单 .reg 模板（只生成、由用户自己导入）、README 与故障自检入口。

**交付实况（2026-10-10 核对代码后修订，README 与这里保持一致）**：上面是**计划**，不是已完成清单。
本机代码里**仍然没有**的：书签跳转点（`app/` 里 grep 不到 bookmark）、GPU 超分（没有 esrnn/Anime4K 这类
预设，"高质量缩放"只有 `perf.py` 里的 lanczos／mitchell 档，别把它说成超分）、
多窗口多实例（单实例是设计决定，见 README「怎么启动」）、界面字体自定义与界面透明度
（**主题色有**：设置页 `theme_accent`；`floaters.py` 里那点 opacity 是气泡自己的画法，不是给用户调的开关）。
上一版这里写着"宽高比/画面裁剪/缩放平移/旋转/锐化没有"、"deband 只在性能档位里、不是独立开关"——
**两句都已过期**：2026-10-10 画面页（侧栏第四页）落地上面那五项加平移/镜像/降噪/去带共 12 项，
去带现在是两处、不是同一个开关（这一页的 ffmpeg 滤镜 `deband` 可热切；设置页性能档位那档是
mpv 自带的 GPU 侧去带，实测热切不生效，见 `transform.py` 第 4 条）。
DWM 亚克力那三条函数仍然写了没接（§4 的 win32 条目）；
样片字幕是同名外挂 .srt，GIF 烧字幕也只认外挂文件。
计划中已落地的：媒体库/缩略图/续播、时间轴悬停缩略图、字幕样式与双语（secondary-sid）与外部字幕（sub-add）、
音轨与音频延迟、HDR 色调映射、性能档位、音量上限、音频设备、睡眠定时、A-B 循环、统计面板、
键位自定义与导入导出、GIF/连拍、启动脚本与 .reg 右键模板、README、app.doctor、
**声音均衡器**（2026-10-12 加的，**原三轮计划里没有这一项**：十段 + 前置 + 六条预设，侧栏第五页；
它是"对标 PotPlayer/迅雷影音"时对出来的缺口，不是一个成熟播放器该有而这里没有的东西。
口径见 §3 第 11 条与 §6 第 34 条），
**画面变换与增强**（原列第二轮的"画面"一项 + 第三轮的"逐帧画质增强滤镜开关"：见 §4 的
transform.py / transform_panel.py 条目与 §6 第 19、20 条的实测口径）、
**资源占用实测**（原列第三轮的"CPU/GPU 占用报表"；统计页点按钮量 3 秒：内核与外壳各自的 CPU/内存按 pid 走
psutil，GPU 利用率/显存/温度/功耗/SM 时钟/限流原因走 nvidia-smi 且**标明是整机**，见 §4 的 usage.py 条目——
它和"内核没回报的项整行不画"（§3 第 8 条）是同一条纪律的两半：那边管内核不给的别编，这边管自己量的别说成独占）。

**修订（2026-10-09）**：截图工坊开着时会把主窗整张快捷键表吞掉——这是真机验收那一轮报出来的缺陷，
现在按 §6 第 16 条的镜像修法修好，用户开着工坊也能直接按空格/`B`/`←→`。同一轮里的三条判据本身也有毛病，
一并改掉并写进 §6 第 11、17 条：内核是懒启动的（"随窗口启动成功"问错了问题）、6 秒/640 宽那一趟
0.45 秒就跑完（"关窗后没有残留"是运气好）、"再按一次 B 是取消"旧写法 0 张→0 张照样绿。

**修订（2026-10-10）**：画面这一族从"计划里有、代码里没有"变成落地，过程中翻掉三条**判据自己的**说法
（详见 §6 第 18–20 条）：内核不夹越界值而是整条拒收（旧结论"发 -1.0 也夹到 1.0"把上一档的旧值当成了夹紧结果）、
`change-list vf append` 带逗号是整条拒收而不是"留残留"（旧判据断言了一个内核根本不接受的形状，
真跑起来 FAIL 的是判据不是产品）、`test_ui` 里"遮挡闸门"那条既没走屏幕闸门也没走环境闸门，
在显示器休眠那一轮被读成"检测器抓到了 bug"。三条都是**先测机制、再改判据**，没有一条是靠把期望改松做绿的。

**逐屏体检翻出来的一批（2026-10-10，§5 那条"全面看一遍视觉"的落实）**：把 33 张表面逐张画出来目检，
第一张就抓到「播放列表」这个页签左右各少半个字——而它是在**四把尺全绿**的情况下发生的（前三把量
"格子放不放得下字"，`contentsRect` 不含 QSS padding、tooltip 又按设计豁免，没有一把看"布局给不给得起
样式自己报的宽"）。补了第四把尺、把页签宽度改成产品自己钉（留白两档＋按钮缝三档，240..520 逐档实测），
并在证伪台账里给新尺配了五条已知答案：正样本必须**只有**新尺报（否则测不到盲区）、把宽还给 sizeHint
之后不许再报（否则恒红）、带省略号不报、wordWrap 说明文字不许被高度档误报（sizeHint 猜行数是虚报）、
真的"三行只给一行高"必须报。这一轮另记两条量具纪律在 §6 第 23–24 条。

**修订（2026-10-16）：入口这一族补上「打开方式」**。原列第三轮的只有"右键菜单 .reg 模板（只生成）"
一项，用户实测之后发现那不够——右键 → 打开方式 的列表里没有播放器，就只能"打开播放器再浏览路径找文件"。
于是这一轮做了三件事：① 生成真启动器 `luhui-player.exe`（列表里不能出现"pythonw"那种没人认得的名字，
详见 §3 第 12 条）；
② 生成「打开方式」那套 HKCU 模板（四根键、48 种扩展名，扩展名名单 AST 现取；不碰 `UserChoice`
也不写扩展名默认值），并在用户明确授权后导入；③ 补三级证据——注册表现数（`check_openwith.py --check`）、
**外壳自己的候选名单**（`check_openwith_shell.py`，走 `SHAssocEnumHandlers`）、照注册表那条命令
原样执行能不能真播（`e2e_openwith.py`），每级都带反向对照与注入证伪。机制约束记在 §3 第 12 条，
这一轮的账（含"DEFAULT 那条判据本来没牙"与"本机 UIA 摸不到资源管理器，所以'菜单里看得见'这一条
没能用程序证明"）记在 §6 第 36 条。

**修订（2026-10-26）：R22 补的是"不报错的难用"，不是新页面**。九件事：窗口几何记忆（含多屏收口）、
doctor 的崩溃记录按年龄分流、章节跳转（键位与菜单）、缩略图内存两本账封顶、会话五键（音量/静音/
倍速/循环/乱序）重启闭环、内核异步握手不冻界面、880 宽紧凑控制条＋关闭钮悬停换像素＋主按钮三态＋
统计页空态、快捷键一览页改分组表格＋搜索、媒体库一次 stat＋扫描回包竞态＋响应式卡片与占位块。
逐条的机制、读数与证伪记在 §6 第 41 条；三档划分里"真桌面/真窗口"那几格仍未签字（用户在用电脑，
`tests/test_ui.py` 这轮没跑），不许拿旧账冒充。README 的功能一览只加已实现且已验的项，
没有"最近播放"页、没有 AI 超分、没有录制（理由仍在 §7）。

## 6. 验收口径（每轮都做，不靠"应该能跑"）

1. 合成样片：PyAV 编码 H.264 + AAC，字幕是**同名外挂 .srt**（tests/gen_sample.py 只写 sidecar，
   不往容器里塞软字幕轨——GIF 那边"字幕只能烧外挂文件"的口径正好与它一致；不依赖网络素材）。
2. 真机启动播放器并加载样片，截图逐项目检：有画面、有时间推进、控件状态与 mpv 属性一致。
3. 机械校验：mpv 日志无 error；IPC 往返延迟采样；属性订阅回调计数>0。
4. 交互回归脚本：模拟快捷键/倍速/ seek/ 全屏 / 截图，断言 mpv 属性变化与产物文件存在。
5. 音频是否真出声无法用截图证明 → 不做假验证：只读 mpv 回报的 `audio-out`/ao 驱动与音量事件，
   报告里明确写出"这一条需用户耳朵确认"。
6. 必须走**用户入口**（`python -m app.main`、`pythonw -m app.main`、桌面 .lnk）而不是只测 MainWindow：
   装配层自己就是 bug 高发区——实测过 `QLocalServer.NoError` 在本机 PyQt5 上根本不存在，
   测试全绿而用户双击永远打不开播放器。为此 tests/audit_qt_attrs.py 把这类"枚举成员没被重导出"
   的写法静态扫一遍，smoke_launch.py 按真启动路径逐像素判画面与转交。
7. 未捕获异常一律落盘 data/logs/crash.log（写完 fsync）：PyQt5 里从 C++ 回调进 Python 抛异常
   是直接 abort 进程（实测 rc=127、stdout/stderr 全空），不落盘就只剩"播放器突然没了"。
8. **截屏判据必须先证明"拍到的是我们自己"**：`grabWindow(0)` 抓的是屏幕那块区域，别的窗口盖上来
   就拍到别人的像素（实测：一张整屏是别人验证码弹窗的留档，"颜色数 ≥6"照样判 PASS）。
   所以每条屏幕留档都过 `capture()` 的命中链闸门（见 §3 第 6 条），被盖住时判 FAIL 而不是拿脏图判绿；
   纯 Qt 画的面板改用 `QWidget.grab()`，不受遮挡影响。
9. **像素判据要用两组已知答案夹住**：`has_picture` 的三个门槛（颜色桶、亮度标准差、彩度命中数）
   各自否掉一类冒牌货——纯色/全黑/全透明（标准差≈0）、深底白字的空浮层与卡片占位块（有反差、
   有颜色桶，却没有彩度）。证伪模式拿"彩色帧 vs 纯色空框""真封面 vs 占位块"两头对照，
   门槛不是拍出来的：彩色帧彩度命中 96、空框 0；真封面 46、占位块 1。
10. **关掉窗口＝取消这一趟导出**：ffmpeg 跑在线程池里，窗口的生死管不着它。实测过（写这条之前）：
    导出中途关窗后 pid 仍在，产物从 0 字节一路写到 5 MB——用户以为"取消了"，其实是"关了个还在
    后台写盘的导出"。`closeEvent` 现在先停连拍定时器、再 `gif.cancel()`（杀进程＋半截产物由 worker
    的 `_finish` 收掉），判据在 `tests/test_gif_real.py --residue`，并且带一条闸门：**关窗那一下
    必须真在跑**——跑完了才关就没有残留可判，那条绿是运气，不作数。
11. **环境闸门排在建窗之前，卡住必须留下栈**：显示器休眠/锁屏时整屏抠出来是全黑（本机实测：
    亮度 0、颜色 1，连任务栏都是 0），而这一半判据要看像素——先建窗再判屏，日志里就会留下一串
    "看着像产品同时坏了几件事"的红，那是环境不给屏幕，写成产品失败就是伪造故障。反过来，一轮跑不
    完又没退出码就叫卡死：验收脚本统一开 `faulthandler.dump_traceback_later`
    （实测正是它钓出了 `pump(400)`＝等 400 秒这个把毫秒当单位的旧参数名，见 tests/test_ui.py）。
    同一位子上还埋着一个问错了的问题：内核是**懒启动**的（第一次播放请求才 `ensure_engine()`），
    所以"内核随窗口启动成功"这条判据在建窗后立刻问，必然红——上一轮把它当成了产品故障。锚点该挪到
    递上播放请求之后，并且"真播起来了"要把 `duration > 0` 放进等待条件里（旧版 `duration=None`
    也报过绿，而工坊那五个数全取自内核此刻的状态，时长没落定时的绿是抢跑）。

12. **文本产物要按"消费它的那个程序"来判，不是按 Python 读起来顺不顺**：`.bat`/`.reg` 这类文件的
    验收必须跑在**真正落盘的那串字节**上（回读一律 `newline=""`，否则通用换行会把 CRLF 读成 LF，
    行尾判据既罚好产物又抓不到坏产物），并且至少有一条判据来自**执行**（`cmd /c` 跑一次 .bat、
    在陌生 cwd 用干净解释器跑到 `import app.main`）。本轮的 LF 事故就是教训：36 条 lint 全绿，
    而 cmd 把中文行劈开当命令执行、退出码照样是 0——文本匹配的绿灯不等于用户能双击。
13. **判据自己也得能被消费**：跨进程取回执时，编码口径要显式对齐——那个探针子进程是裸
    `python -c`，它的 print 跟着本机 ANSI（GBK）走，而项目根里有中文；上一版按 UTF-8 硬解，
    把「赛博鸡蛋」解成一堆替换符，于是**在好产物上翻红**。改法是让子进程自己写 UTF-8 字节、
    父进程严格解码，解不开就单独点名"解码口径漂了"，绝不冒充"产品算错了根"（误报和漏报一样是判据坏了）。
    另一头是**判据太松**：`lint_open_py` 原先只 grep `"__file__" in text`，于是把 ROOT 写成
    "只往上跳一层"（算出 `packaging\`，`import app` 必炸）也照样放行——现在 lint 数 `dirname(` 的次数，
    陌生 cwd 的真跑探针再独立验一次算出来的根，两条变异各有归属。
14. **"测到了"必须由一次真测证明，"补好的文案"必须由代码补出来**：资源占用这条链上有两处
    静默失败。（一）CPU 百分比是**两个时刻之差**，把 `time.sleep(window)` 摘掉照样跑出一堆 0.0%、
    报表照样成文——所以验收起一个真在烧 CPU 的子进程，要求 1 秒窗口量到 ≥50%，这条不通其余数字都不作数。
    （二）先前那版把"这一路没量到时该显示什么"直接写进 fixture，再断言界面把这句话显示回来：
    看着像判据，其实是自证，面板里补状态文本的分支一条也没走。现在 fixture 只喂读数，
    文案由面板按真实原因分岔（pid 空 / 采样任务自己崩了是两回事，都写成"用户没放片子"就是把程序故障
    说成用户没干活），并各配一条变异。
    另两条本轮踩到的坑记在这里，因为它们都会把判据变成"永远绿"或"永远红"：offscreen 平台上父窗没
    `show()` 时子控件 `isVisible()` 一律 False，于是"采样前整行藏着"对填了数的行也判 True（恒真，
    产品把行画出来也抓不到），而"投回界面后四行有数"永远判红（看着像产品坏，其实是判据没条件）。
    两头都要 `panel.show()` 之后才算数，"藏"这条才逼得下去（建行时不真藏就翻红）；
    而 parse_smi 的表头有**两道**兜底（`startswith("name,")` 与
    `cols[0]=="name"`），只摘其中一道不翻红**不是判据失灵**、是产品还有另一道——变异要摘就得把整项能力摘干净。
15. **判据会不会被"进程里已经算过的东西"喂绿，也要有对照**：audit_qt_attrs 在 app/doctor.py 上连报两条
    "PyQt5 里没有 QtCore"（假红），而它的 `--selftest` 当时全绿——因为**解析这一步压根没有对照样本**。
    补上之后第一条仍然不算数：`hasattr(PyQt5,"QtCore")` 取决于本进程之前有没有导入过它，而 --selftest 前几遍
    早就连带导入了，于是把修法摘掉它照样判绿。现在的写法是**跑这条之前先把子模块从父包上 delattr 摘掉**，
    摘不开就自己判 FAIL 并写明"前提不成立，这条不作数"。两条坏写法（不试子模块／成员一律放行）都要求
    selftest 翻红，跑法与结果留在 `tests/out/r3-4e-resolve-falsify.py`（它自己按字节还原并核 md5）。

16. **工具窗会把整张快捷键表吞掉**：`QShortcut(context=WindowShortcut)` 只认**当前活动窗口**。
    截图工坊是不模态面板，它一成为活动窗，主窗那 60 多条键就整体收不到——用户开着工坊想按空格暂停、
    按 B 连拍，得先用鼠标点一下播放器。真机实测的形状（tests/probe_gif3.py B1–B4）：工坊开着时把按键
    递给主窗，命中的动作是 `[]`；把主窗 `activateWindow()` 抢回来立刻又是 `['burst_shot']`。
    修法是**镜像**而不是另建派发表：`keymap.mirrorable()` 把生效表里"该照样响"的那部分（按**键值**摘掉
    Esc/Return/Enter 三把留给面板自己的键，别名与带修饰的都不误伤）递给 `GifDialog.install_owner_keys`，
    QShortcut 的父对象必须是那个窗口，命中仍交回 `MainWindow.dispatch`；每次打开都重递一遍，
    旧的一组先 `setEnabled(False)`+摘父+`deleteLater()` 卸干净（不卸就一次按键响两次）。
    三层各自独立判：键表语义（test_keymap 25 项／证伪 38 组）、对话框行为（test_gif_dialog 16 项／21 组）、
    主窗接线（test_gif_wiring 19 项／33 组），真机那一条在 test_gif_real 的 B 段。
17. **"绿"有三种假法，都得靠判据自己防**：①**问错问题**（见第 11 条末尾的懒启动）；
    ②**运气好的绿**——导出中途关窗那条，先前用的是 6 秒/640 宽/15 帧，本机实测 0.45 秒就跑完，
    于是"关窗后没有残留"是压根没东西可残留；现在改成整片 10 秒/960 宽/20 帧，并且单独钉一条
    "关窗那一下**确实还在跑**"，不成立就判这条不作数；③**恒真的绿**——"再按一次 B 是取消"旧版只比
    `取消后张数 == 取消前张数`，0 张→0 张照样绿，现在要求取消前 ≥1 张且 < 总张数（跑满了再按停
    就没有"中途取消"这回事）；同理"工坊是活动窗时按 B"要当场验活动窗＋镜像里真挂着那一把键，
    否则"主窗自己响了"也会报绿，而那正是修之前就在响的行为。对账两条事实时**别拿同一个函数算两遍**
    再跟自己点头：要比的是键表里那把键 vs 窗口上装出来的 QShortcut 键值（两个来源）。
    环境给不了前提就直说（回 rc=3），不写成产品失败。
18. **判据断言"内核会怎么做"之前，先把内核怎么做测清楚**：画面这一族原来写着"越界值内核会夹到上限"
    （所以外壳的夹紧是冗余保险）。`probe_transform11.py` 从**已知旧值**逐项重测才看清：本机这版内核
    一次都没夹过，越界值**整条拒收**、`get` 回来的永远是发之前的旧值。结论碰巧没错（外壳确实得夹），
    机制说反了——"夹到最大"与"根本不动"在界面上症状完全不同（前者画面糊到最大，后者界面动了画面没动），
    而出错时该往哪查正是由机制决定的。旧写法错在哪也写进了文档：它把上一档量到的旧值 1.0 当成了夹紧结果。
    配套那条判据不许省：`transform.MPV_MEASURED_ON` 与本机 `p.version` 对不上就红，
    因为"实测"两个字一旦能跨版本、跨机器地继承，它就又是别人机器上的数了。
19. **列表≠事实（滤镜链这一族的"生效"要两条一起证）**：硬解给的是 d3d11 的 nv12 帧，内核把吃不下的
    滤镜扔掉之后 `vf` 列表里名字照挂、`enabled` 照是 True、`change-list` 的应答照是成功，而同一帧
    逐像素差 **0.00**。所以"这一档在跑"的判据只有 `filter_failures()`（日志里的
    `Disabling filter …` / `Impossible to convert …` 等标记）**干净** + **像素真的动了**这两条一起成立；
    只看列表的判据在这一族里恒真，验收里一条都没写。界面同理：链状态行必须把日志原话带出来，
    红字点名"列表挂着名字而内核没跑起来"，不能只说"已开启"。
    另一头是同一条纪律的第二半：像素动了也只证"这一档在动"，**不证**"观感变好"——无参考图量不出后者，
    判据文案里就这么写。
20. **下发命令的"形状"也是实测对象**：`change-list vf append` 一次只收**一个**滤镜，带逗号的整条链
    会被原话拒了（`Option vf-append: -append takes only 1 item (no ',')`）且**链表纹丝不动**；
    单档 append 两次就是链上挂两条（这才是"残留"的真形状），`remove` 一次只删一条同名；
    整条替换只有 `set`，干净清空只有 `set ""`（实测回报 `[]`）。验收里这三条形状各钉一条，
    并给 `DISABLE_MARKS` 配了那句真机原话——日志扫描器不认它，界面就会把"根本没挂上去"
    显示成"链是空的、正常"。证伪那边对应一条摘件：把这两句从标记表里删掉，离线半必须点名。
21. **给判据用的诊断注解也要按 API 逐字节解码**：抓屏失败时那行"环境自述"里印过
    `供电标志=0x5b（0x8=显示器断电/休眠）`。`SYSTEM_POWER_STATUS` 的第 3 个字节是
    `BatteryLifePercent`（0x5b＝91%），BatteryFlag 的 0x08 是"交流在线"，而这个 API 从头到尾
    **不知道显示器状态**——一句编出来的注解会把整轮判废读成"用户设置的锅"。修法是结构体字段顺序
    照官方重写、位含义交给 `power_words()` 逐位解码，并加一条已知答案对照（0x09⇒"电量高／交流在线"、
    0xC0⇒"无电池／电源状态未知"、0⇒"无位"）。屏在不在**只**由整屏亮度采样那把尺判（`capture_health()`），
    诊断文字只许解释它能归因的部分。
22. **证伪条目也得走环境闸门**：`--falsify` 里依赖真抓屏的坏样本，屏幕暗时会被判据正确地判"没有像素"，
    在证伪台账里就记成 **MISSED＝检测器不可信**；真原因是环境，两者混成一格会把一次好端端的量具判废。
    所以这类条目统一走 `f_case(..., needs_screen=True)`，屏幕不给像素时记 ENV 而不是 MISSED，
    退出码交给第 11 条的 rc=3。反过来也一样：环境闸门自己要有已知答案（全黑帧必须判不可用、
    有色差的一帧必须放行），否则恒绿的闸门会把真瞎的抓屏通路读成"一切正常"，
    而留档那几张全黑 PNG 就冒充成了验收证据。
23. **钉几何之前先 polish，钉"需要多宽"要钉在量的那个轴上**：这一轮修侧栏页签切字时，对照件连着假红
    三次，每次都查出不同的坑——① 没 `ensurePolished()` 就取 `fontMetrics()`/`heightForWidth()`，
    全局 QSS 的字号还没落到字体上，量到的是另一个字号的度量（这里量到行高 12，真排版要 17），
    于是"钉子"自己短了一行；② 对照件的宽度**不能**用 `setFixedWidth` 钉：wordWrap 标签的虚报恰恰来自
    "宽度由布局给、sizeHint 只能按窄宽猜行数"，一钉宽度 sizeHint 就跟着变准，那条对照退化成恒真；
    ③ 高度要钉成 `heightForWidth(布局会给的那个宽)`，钉成 `fontMetrics().height()` 就是在拿算式
    代替被测的那把尺。三条共同的形状是：**对照件里"我以为的形状"必须当场断言**（实得宽 > 文字宽、
    且 < sizeHint 宽，两条都写进断言），否则测的是运气。
24. **量具要标出"宽报半径"，产品要钉在半径之外**：`sizeHint` 在"文字+2×QSS padding"之外还带一份
    Qt 的通用松弛（本机实测 `border:none` 的页签 +11px、默认边框的对照件 +17px），所以"实得宽恰好
    等于文字+padding"也会被它报一句。这不算错——它管的是"布局压破了控件自己报的最小需求"这一类，
    但必须把这条半径写进函数文档，否则下一个人在 240px 侧栏上看到五句红会以为产品坏了。
    产品侧的解法不是把尺调松，而是把每格钉在「文字+2×留白」与 sizeHint 的**大值**上、
    排不开时再收按钮缝（4/2/0 三档），让 240..520 每一档都落在半径之外。

25. **前四把尺共用一个裁剪框假设，滚动区把它整块绕过**：`clipped_labels`／`clipped_without_hint`／
    `colliding_text`／`squeezed_widgets` 都拿控件自己的 `contentsRect()` 当"能被裁到哪"，而侧栏每一页是
    QScrollArea 里摊开的一张子控件——页比视口宽时右边整块被裁掉，控件自己那儿"字放得下"，四把尺全绿。
    补的第五把（`beyond_viewport`）把控件映射到 viewport 坐标再比右沿/下沿，两条 Qt 陷阱当场踩过：
    ① `QRect.right()/bottom()` 是 `x+w-1` 而不是 `x+w`，容差必须写成 `r.right() > host.width()`
    才与旧算式逐字节等价（写成 `>=` 就把 1px 的越界吞了）；② 判据要跑在**落盘的那一轮**上——
    先在页面上把宽度调宽、再回头拿旧的几何算，会把已经修好的形状量成红。
26. **换行改造把"第几排"这个概念换掉了，量具跟着换才算得对**：页签从共享列的网格改成
    "每排一个独立横向盒"之后，盒内按钮的 `y()` **恒为 0**（相对盒原点）——按 y 分组等于所有按钮
    都在同一排，`排数=1` 而产品真的摆了两排（这条 MISSED 是最初那一版留下的）。现在排身份只从
    `parentWidget()` 取（`_tab_rows_of`），容器内坐标只从 `mapTo(tab_strip, …)` 取（`_in_strip`），
    并且把"按钮分组数出来的排数"和"`win._tab_rows` 的盒数"两个来源对齐——两边不一致就是布局与
    判据各看一套，也是一条红。教训：**改了布局的层级，等于改了所有几何判据的坐标系**，
    只改产品不改量具就会得到"量具说没事、眼睛看见重叠"。
27. **交给子进程的路径一律先收成绝对**：mpv 是用 `cwd=runtime/mpv` 起的，于是任何**相对**路径
    （`--log-file`、`--input-conf`、`--screenshot-directory`、`loadfile`）都会在那台子进程的目录下
    重新解释。本机 2026-10-10 实测的形状：`LUHUI_DATA` 写成相对路径后，三种截图模式全在 0.05s 内
    回 `error running command`、盘上一张 PNG 都没有，而 mpv 自己那份解释性日志落进了
    `runtime/mpv/tests/…` 一棵空目录——**失败得毫无诊断**。修法在产品侧收口一处
    （`app/engine/player.py:mpv_path()`：`os.path.abspath(os.path.expanduser(p))`，命名管道
    `\\.\pipe` 原样放行），配置侧把 `DATA_DIR`/`LOG_DIR` 直接算成绝对；验收钉住"三条启动参数都绝对、
    管道不许被改坏、`DATA_DIR` 本身是绝对"，证伪把 `mpv_path` 调成恒等函数后必须翻红
    （logs/test-engine-falsify-004837.log 30 CAUGHT/0 MISSED）。顺带一条 raw-string 坑：
    `r"\\.\pipe\\"` 结尾是两个反斜杠、永远匹配不上真管道名，前缀取到 `pipe` 就停。
    2026-10-11 按这条口径把**所有子进程**扫了一遍（`tests/probe_child_paths.py`：从仓库根目录
    喂相对路径 `tests/samples/gelu-sample-1.mp4`，再看哪些字符串会进子进程命令行）：六个候选
    （`prop("path")`、`gif_state()["video"]`、`FFMPEG_EXE`、`MPV_EXE`、`DATA_DIR`、`screenshot_dir`）
    实测**全部 `isabs=True`**——因为都从 `mpv_path()` 那一处过，收口一处就够。自己换 `cwd` 的子进程
    只有 `app/engine/gif.py`（`Popen(cmd, cwd=os.path.dirname(self.out))`），而它的视频路径继承内核
    已回报的那份，同样绝对。ffmpeg 那一头（缩略图／海报／媒体库扫描）跑在**我们自己的** cwd 里，
    所以相对路径仍解得开：探针里一份全新的 `ThumbStore` 拿相对路径照样产出 `meta.json` +
    `sheet-01.jpg`（tests/out/probe-child-paths/）。**因此这一趟不改产品代码**——没有实测到的坏就
    不预先加 `abspath`（第 6 节这条台账只钉"已经咬到的形状"），而是把它记成一条触发器：
    **哪天给 thumbs/posters 也加 `cwd=`，必须先收绝对**，否则就会复刻 2026-10-10 那种
    "三张 PNG 都没有、mpv 的解释性日志落在子进程目录下成一棵空树"的无诊断失败。
28. **量具自己也要有前提守卫和收尾，否则它的脏会判成产品的坏**：`capture_sweep --screen` 的
    抓屏两条连着两轮红，根因都在量具而不是产品——① 先 `win.close()` 再抓屏，遮挡闸门只能说
    "这块区域没露在最上层"，rc=1 把顺序错报成产品坏；② 体检跑到 `_open_keymap`/`_open_gif`/设置页时
    把三张自家常驻对话框留在主窗上面（`_open_*` 只 `show()` 不负责关），抓屏时遮挡闸门点出两个
    自家句柄（logs/capture-sweep-screen-005220.log:50-51；对应 KeymapDialog 768×686、
    GifDialog 665×476，逐张点名见 logs/probe-leftover-windows-*.log）。现在抓屏前
    `dismiss_dialogs()` 只关 QDialog、非 QDialog 的浮窗只点名不清场（否则"产品真画了张不该开的窗"
    会被量具抹掉），`grab_precondition()` 守"主窗与目标都可见"，塌了就进 `unmeasured` 走 rc=2。
    两条都进了 `--falsify`，并配一条"什么都不收拾"的对照证明上一条不是恒真
    （logs/capture-sweep-falsify-005813.log：52 CAUGHT/0 MISSED）。
29. **逐档字号体检给出的是"适用半径"，不是"任何字号都不裁字"**：`capture_sweep --font-scale` 一档
    一个进程（换档必须换存图目录，PNG 写回原名就是把标准档的证据覆盖掉——仓里没有 VCS）。
    2026-10-11 在**页签换行改造之后**的代码上逐档重跑（离屏、五把尺全开）：
    1.5 / 3.0 / 3.5 各 **48 条全绿**（标准档再加 `--screen` 是 50 条），页签条在高字号档自动排成
    两排，所以改造前那笔 23×72px 的重叠**在当前代码上不再出现**（旧账记在第 26 条，别当现值抄）。
    往上是探边档，红必须能点名是谁越界：4.0 两条红＝侧栏·媒体库「双击卡片播放…」实得高 78px <
    需要 122px、侧栏·统计「上半页 1 秒一轮」172px < 183px，两笔都是**压扁尺**（宽排得开，那一列
    给不起要的行高）；5.0 两条红＝媒体库压扁＋统计页两个按钮截字（「采样 3 秒」需 272px > 可用
    246px）；6.0 五条红＝侧栏五页全红（**这是侧栏五页时代的读数**：2026-10-12 加了第六页
    「声音均衡」之后这一档的条数与点名都要重跑，跑法在看门狗那条腿上，别把"五条"当现值抄）。读数分别在各档的日志里（1.5/3.0：tests/logs/capture_sweep-fontscale*-20261011-0102*.log；3.5–6.0：tests/logs/capture_sweep-tier*-0105*.log、capture_sweep-tier*-0106*.log、capture_sweep-tier*-0107*.log）。
    所以 §5 那条"界面字号自定义"现在能承诺的区间是 **1.0–3.5**（契约档保守取到 3.0）；4.0 以上
    要动的是"每列给几行高"而不只是"字号设置"，别把设置加上就当这一档过了。
    另一条量具账：`_scale_qss()` 是对 QSS 字符串做替换，**在同一进程里跑第二遍会在已放大的值上
    再放大一次**（1.0→1.5→2.25），所以换档必须换进程——省这一次进程，量到的就不是任何一档的真实形状。
    同一天 R7 那趟（看门狗等屏亮）把**带 `--screen` 的三档**补齐了：标准 / 1.3 / 1.8 各 **50 条全绿**、
    三趟 rc=0（tests/out/sweep-ui-r7.log、sweep-ui-r7-font1.3.log、sweep-ui-r7-font1.8.log）。
    这一份补的是第 30 条那笔账的"出厂口径"：侧栏需求量修好之后，那条 24px 的「1.00x」裁字
    最初只在**探针进程**（没跑 `theme.install`）里量到，现在在出厂装配 + 真抓屏这两层都清场了
    （三档里 `侧栏·调节` 那条逐屏判据全 PASS，墨迹占比 21.97%／15.26%／16.53%）。

30. **"这一格要多宽"交给程序算之后，程序自己就是最需要验收的那一件**：侧栏与设置页现在有五处
    形状是算出来的——`form_rows.content_width`（`_pages_need` 的唯一算法出处）、`mk_hint` 的换行出路、
    `TitleBar._fit_height`、`add_choice` 的 12 字档、`form_rows.fit_to_content`（设置窗加宽）。
    五处各钉一条验收（`test_ui` 的①–⑤）与一条证伪（`--falsify` 的 U52–U56），三条产品侧的坑与
    两条判据侧的坑都在这一轮里翻出来：
    **坑①（量具报了个假数）**：没排过版的 `QScrollArea` 自报 `width()==100`／视口 98，于是
    "滚动条占多少 = 条子宽 − 视口宽"这一把量到的是 **2px**，兜底那一支（`width()>0` 才走）永远轮不到，
    侧栏因此钉窄一整根滚动条——构造期读 378、排过版之后同一页读 402，差的正是行尾胶囊「1.00x」
    （裁而未告尺点名：需 45px、只露 40px）。修法是取两者的大值
    （`bar = max(实时差值, 滚动条自己的 sizeHint)`），多算 10px 的代价换来"少算就是切字"不再成立。
    **坑②（差点把 16px 凭空加进产品）**：`QStyle.pixelMetric(PM_ScrollBarExtent)` 也能量出滚动条厚，
    但它**不认样式表**。出厂装配（`theme.install` 之后）实测三条来源 10／10／26，把 26 放进 max
    就是给每一档侧栏白涨 16px；探针进程（只 `setStyleSheet`）那一路才是 2／26／26。
    这条以"实测否决 + 为什么不用它"写进 `form_rows.content_width` 的注释，代码里没留那把尺。
    **坑③（需求在开页之后才见真值）**：设备清单等 IPC、库卡片等扫描回填，所以
    `_sidebar_page()` 开页后补一次 `QTimer.singleShot(0, self._fit_sidebar)`（等布局跑完再补）。
    **判据坑 A（把"钉窄"当"会红"）**：摘掉 `content_width` 之后侧栏停在 240、真实需求 270，
    数值上确实窄了，两把尺却 **0 条**——缺口被带省略号的标签先吸收掉，实测约 40px 以内不点名
    （同一档两套装配对照：内容块 238／缺口 32 ⇒ 0 条；内容块 274／缺口 66 ⇒ 越界尺 11 条，
    `tests/probe_u52_teeth.py`、`tests/probe_shape_guards.py`）。所以这一条分两腿：
    量程下沿那一档**只断数值**（摘闸报出来的需求 195 连内容块自身最小宽 238 都不到，
    这个宽度天生装不下它自己那一页），尺那条另造一个越过省略号吸收段的形状（把该页字号 ×2、
    仍钉量程下沿），并且**同一档装着闸必须清场且没顶到 520 上限**——没有这半边对照，
    "摘闸红"就退化成"钉窄就一定红"。
    **判据坑 B（Qt 缓存把"复原"吞了）**：证伪的复原腿原来是拿同一只 `QComboBox`
    `setMinimumContentsLength(12)` 调回去——sizeHint 已 polish 过、缓存不回涨（实测停在 80），
    于是这条被误记成 MISSED。现在复原腿用工厂**重新造一只**，它既是产品路径也是对照组
    （排除"第一只天生宽、第二只天生窄"）。
    一条贯穿的纪律：两套装配（跑没跑 `theme.install`）量到的绝对像素**不同**
    （调节页需求 306／274／270 都出现过），所以①–⑤与 U52–U56 里**没有一个写死的像素数**，
    全部是"需求 ≥ 内容块 + 滚动条"、"侧栏 ≥ 需求"这类关系式。
    顺带把设置页那一项的标题跟着行为改成「侧栏宽度下限」（它一直是 `min(max(配置, 需求), 520)`，
    即用户设的是下限而不是定值）。

31. **"眼睛被挡住了"和"我们没画出来"必须分两本账记**：R7 那趟验收（tests/out/accept-ui-r7.log:148）
    汇总 153/154、rc=1，唯一那条红是**外来窗口**（pid 18248 的一个 Qt 窗）压在视频区上、
    `raise_`/topmost 都抬不动——`capture()` 已经把这块登进判废台账（`env_notes`），而同一处判据写作
    `... and not e_area` 又必然为假，于是同一条**既"不作数"又"真实失败"**：环境状态冒充了产品结论，
    rc 从 3 掉到 1，下一位读者会照着那条红去改本来没坏的渲染代码。这正是 §6 第 22 条、第 28 条
    已经立过的规矩在**记账这一头**漏了接线：闸门只做了"点名是谁挡的"，没做"这条判据归哪本账"。
    现在 `capture()` 判废时把块名挂进 `CAPTURE_ENV`（**进门先 `discard`**：命中的永远是这一趟的结论，
    否则一次早到的遮挡会把后面所有同名判据免检掉），main() 里八处吃真抓屏的判据（OSD 气泡／悬停预览／
    媒体库·调节·统计·画面四张侧栏留档／视频区／整窗）改走 `pixel_check()`：块名在册 ⇒ 打印 `[ENV]`
    且不往台账记红；不在册 ⇒ 照常 PASS/FAIL。U23c 那条既有接线桩顺势加了一条断言（自家句柄挡路时
    块名**不许**进册），另加 U57 三条腿（打了标记必须不进台账、没打标记必须照常记红、判废过一次后
    再抓到干净帧必须摘净），第 2 腿就是防"这条分流变成恒真金牌"的对照。
    修后同环境重跑：`--falsify` **73 CAUGHT / 0 MISSED / 0 ENV**、验收 **154/154**、rc=0
    （tests/out/falsify-ui-r8.log、tests/out/accept-ui-r8.log，2026-10-11 02:11/02:12）。
    **这条修法的验证分两半，别拿前一半冒充后一半**：U57 那三条腿是**接线桩**（把
    `_hwnd_chain_at`／`_exempt_blocker` 调出来），它证的是"分流写对了、没接反"；而"真有一扇别人的窗
    压上来时收尾码真的是 3、汇总里真的没有失败项"得另跑一趟真窗口（`tests/probe_foreign_cover.py`
    起一扇**另一个进程**的置顶灰窗，再跑验收）。02:14 试过一趟，赶上屏又暗了、整轮在
    `capture_health()` 上早退（rc=3，但**根本没走到视频区那一条**，所以那一趟不算验证，
    只算"环境又不给"）；这一半挂在 `tests/watch_cover_experiment.py`（等屏亮、最多 12 趟、
    哨兵 tests/out/cover-exp-done.flag）。它的判据函数拿七份已知答案对照过，其中两份正是
    "看起来像、其实不算"的形状：早退那趟判 SKIP，R8 那种"根本没挡住"判不通过——
    只看 rc=3 就把"没发生"当成"验过了"，是这一类看门狗最容易给自己发的免检金牌。
    **后一半已于 2026-10-11 08:38 真窗口验到**（`tests/out/cover-exp-done.flag`：
    `generated_at=2026-10-11 08:38:20 / try01: rc=3 ENV=True FAIL=False rc=3 汇总=无失败（147/147）`）：
    看门狗从 02:20 等到 08:37，屏幕读数由连续六小时的 `整屏 3840×2160 亮度0（最低0/最高0）颜色1`
    翻成 `亮度240 颜色56` 才开跑；遮挡窗是**另一个进程**的 Qt 窗（pid 72676，`0x27f145a/Qt5152QWindowIcon`），
    `raise_`/topmost 抬不动。那一趟（`tests/out/accept-ui-r9-covered-try01.log`）三条判据全中：
    ① 出现了 `[ENV] 视频区在屏幕上真的有画面…` 且原文点名"这块区域被**别的进程**的窗口盖住"；
    ② 同一行没有以 `[FAIL]` 出现；③ 汇总"无失败"、rc=3。
    台账里的数对得上：R8 那趟 154 条 − 本轮判废 7 处（悬停预览／媒体库·调节·统计·画面四张侧栏／
    视频区／整窗）= 147，所以"少 7 条"是**同一批判据转记 ENV**，不是有判据被删或被放宽——
    这正是这条修法要的的形状：眼睛被挡住时既不发金牌也不发产品红。
    另一条只记不改的形状：那一轮还留了一句
    `QWindowsWindow::setGeometry: Unable to set geometry 296x96… on "\\.\DISPLAY16"`——
    `\\.\DISPLAY16` 不是主屏、`Unable to set geometry` 是 Qt 在**多显示器 + 150% 缩放**下的常态抱怨，
    它既不进判据（样式相关 0 条）也不改变任何一屏的形状结论，所以没给它加闸门；
    真要管，先问"哪一屏、谁在那儿"再动手，别拿一条噪声日志当产品缺陷。

32. **卡顿没有"报错"那一步，所以验收只能把它翻译成"同一件事被做了几遍"**：这轮收尾体检的性能那一半，
    四个卡顿源全在媒体库与播放列表上，而它们**不会让任何一条像素判据翻红**（画面确实画出来了）、也不会崩，
    最坏的形状只是"点下去没反应半秒"。耗时本机不承诺（同一份改动在别的机器上能差三倍：杀软、磁盘冷热、
    封面缓存新鲜度都在里面），所以**耗时不进契约、次数进**——这四个数是机器无关的：
    ①一次击键把整库重扫几遍；②整表重建那趟同步解码几张封面；③往里拖一个文件夹把列表清造几遍；
    ④一张新缓存回来把全目录扫几遍。
    **先测机制、再动手**：`tests/probe_rebuild_cost.py`（一次性量具，N=300、offscreen，读数落
    `tests/out/probe-rebuild-cost.log`，2026-10-11 02:57）逐项拆开这一趟的账——
    `key_for` 22.8 µs/项、`request` 77.5 µs/项、`image()` 全冷 **1000.9 µs/项**（缓存封顶 60 份，超出就逐张
    真解码）、`image()` 命中 0.3 µs/项、`watch_state()` 旧口径 **544.8 µs/条**（整份表 53400 字节，每张卡片
    重读一遍）⇒ 合计 ≈1646 µs/项、3000 项一趟 ≈4.94 s。这条读数直接决定了修法：**光加防抖不够**，
    因为最贵的两笔（逐张解码、逐条重读整份表）在防抖之外，于是"防抖＋封面分批补"是一起做的。
    四笔修复与落地读数（`tests/out/resp-4.log` **13/13、rc=0**，`tests/out/resp-falsify-3.log`
    **15 CAUGHT / 0 MISSED、rc=0**，同机同装配 02:54）：watch-state 只在文件真变过时重读
    （240 张卡片逐条问 ⇒ 真读盘 **1** 次，靠 mtime_ns+size 记号；写穿 ⇒ 自己写完立刻可见、不为此多读一遍盘）、
    搜索 220ms 防抖＋封面 12 张一片（240 项重建 1 趟 ⇒ 同步解码 **0** 张、分 20 片补完，图标齐 240/240）、
    playlist 60ms 拖尾合并（200 次推送 ⇒ 总共画 **1** 遍，上限 20 遍；拆回原样＝推送直连重建是 200 遍＝
    20100 次行清造）、缓存回收按批（120 张封面成功 ⇒ 全目录扫 **2** 次，海报每 50 份、缩略图每 20 份；
    拆回原样是每张一次＝120 次，而"每张一次"这个形状原先就挂在主窗口的接线上）。
    **这一条真正值钱的是量具自己翻出来的三笔**，不是那四处改动：
    (a) **场景对这个 bug 是空操作 ⇒ 那条绿是假的**。"补图按整库行号取项"这条注入首轮 **MISSED**：旧场景写的是
    "长表换成 6 项短表"，而 `self._items[0..5]` 和 `self._shown[0..5]` 本来就是同 6 个文件——6 项内部怎么排
    都还在表内，判据看的是"有没有跑到表外"，所以注坏了照样 0 张外来。**放宽的是场景、不是判据**：改成用
    搜索过滤造出这个 bug 的真形状（网格 10 行、整库 240 项，`_shown` 是 `_items` 的真子集），同一条注入
    立刻 230 张外来。写判据时"它现在绿"和"它能变红"是两件事，后者要单独跑一遍才知道。
    (b) **红灯先问前提在不在**：写穿那条首轮读到"多读盘 1 次"，不是产品坏，是"文件还不存在"这一档
    被 `stamp is not None` 挡在缓存门外——而首次运行恰恰天天走这条路（每张卡片 open 一次必然失败）。
    修法在产品（`None == None` 也算没变过），不是把场景改成"先让文件存在"糊过去。已声明的失效半径：
    外部程序在**同一个 mtime_ns 且同样大小**上改写内容认不出；这份文件除了本程序只有自己写的路径
    （单实例转交把第二个实例收进同一进程），所以这一档不单独设防，真出现"进度不刷新"按这条查。
    (c) **场景没收尾会污染下一趟，而且是以"进程静默消失"的形式**：过滤那一步若不等防抖落地就 `deleteLater`，
    `pump` 会在"还没过滤"的 240 行上提前返回——同一件事先让判据读到错状态（报 240 行、要 10 行），
    再让后面的证伪腿在**进程级**abort（rc=127、stderr 全空，就是本条 §6 早就记过的 PyQt 槽异常形状）。
    现在 `fired=`（防抖窗真的落地、`_shown` 换成过滤那份）本身就是判据的一项，绿它才允许往下读。
    (d) **给一整批离线判据统一套环境变量，会把形状漏给真跑的子进程**：这一轮的批量跑法是
    `LUHUI_DATA=tests/.selftest-data QT_QPA_PLATFORM=offscreen` 挨个跑，`test_launchers.py` 里
    "故障自检.bat 真跑完、14 项报告都在"那条就红了（报告行数=12、CHECKS=14、rc=0、stderr 干净）。
    红的原因不在产品：那份判据是**用 cmd 真起一个 python 子进程**的，我导出的隔离变量跟着进了子进程，
    于是自检在隔离目录里少写了两项。摘掉这两个变量单跑就是 43/43、rc=0（tests/out/rr2-test_launchers.log）。
    记下来的规矩：批量套离线环境时，**吃子进程继承环境的量具要单独按自己的形状跑**；反过来，
    这类判据本身也该把子进程的环境钉死，而不是信调用方——本轮只登记跑法红线，没动它的判据。
    (e) **绝对"墨迹占比"下限是装配相关的，换个平台这份量具就跑不起来**：同一批套了
    `QT_QPA_PLATFORM=offscreen` 的批量跑，`capture_sweep.py` 报**七张表面贴地板**（标题条／快捷键一览／
    四张菜单／右键菜单）、rc=1。对照跑法（同机、摘掉那个变量重跑）给回的是标题条 **2.39%**、右键菜单
    **8.43%**、整窗 **24.59%**——与 02:26 那份留档（tests/out/sweep-20261011-0226.log）**逐条同值**
    （tests/out/rr2-capture_sweep.log）；而离屏那趟同一张标题条读到 **0.35%**、采样点数从 209616 掉到
    51920（tests/out/rr-capture_sweep.log）。差的既是 dpr 也是抗锯齿，不是"画没画出来"。
    所以这七条红**不是产品结论**，是那些百分比地板按产品装配（Windows 平台 + 150% 缩放）标定、
    离屏装配根本不该拿来判它。修法不是把地板放宽：`main()` 开头加一条**跑法闸门**，检测到 offscreen
    就打印 `[ENV]` 并**退 2 拒跑**（实测：套变量 rc=2、摘变量 rc=0，`--only title` 仍 2.39%，
    标准档 PNG 另存 tests/out/sweep-r10-20261011-0305/ 免得被下一趟覆盖）。`falsify()` 没动——
    那五把尺判的是相对形状，不吃这条绝对地板。
    回归口径：`app.doctor` 0 项阻断、`test_library.py` 55/55、`test_thumbs.py` 20/20、
    `test_keymap.py` 25 条 0 失败、`test_keymap_dialog.py` 17 条 0 失败、`test_gif_dialog.py` 16 条 0 失败、
    `test_gif_wiring.py` rc=0（线程池残留 0）、`test_usage.py` 56 项 0 失败、
    `test_transform.py` 34/34 另有 2 处判废（要真窗口，rc=3）、`test_launchers.py` 43/43（按它自己的形状跑，见 (d)），
    读数都在 tests/out/rr-*.log 与 rr2-*.log；
    `test_ui.py` 这一趟是**离屏**跑的，25 PASS 之外有 4 条红（原生框架位 0×0／原生事件计数空／
    mpv 子窗口两条），那四条要的是真 HWND，offscreen 平台上结构上不成立，**不能算本轮改动的结论**，
    也不能算产品红。
    **这一段的两个数后来都在真窗口装配下重测过**（见 §6 第 33 条，2026-10-11 22:57–23:03）：
    `test_transform.py` 那边两条判废变成 PASS、整趟 59 PASS／0 ENV／rc=0；`test_ui.py` 那边四条红全部
    变成 PASS、整趟 **154/154、0 ENV、0 FAIL、rc=0**。所以"34/34"与"25 PASS＋4 红"这两串数是
    **离屏装配的读数**，只能用来对照装配差，不能再拿来当产品结论——两套装配的绝对像素本来就不同（见 (e)），
    而"结构上不成立"的那几条更是只在其中一套里成立。

    **2026-10-21 00:07 补（R21-d，B 系列六笔离线性能项）**：这一轮没停在上面四处，把全仓审查里已核实的
    六条 B 项一次收掉，并各自变成判据（同一份 `tests/test_responsiveness.py` 长到 **20 项／25 条证伪**，
    读数 `resp-r21d-3.log` 20/20、`resp-r21d-falsify-3.log` 25/25、用时 3.0s/5.4s）：
    B5 `_on_poster` 命中即停（24 行里第一行的封面回来 ⇒ 摸 **1** 张卡；旧写法 **24** 张）；
    B6 `refit` 带几何指纹（同几何 5 次 ⇒ 真铺 **0** 次、底层 Win32 **0** 次；尺寸变一次铺 **1** 次；
    空状态 **0** 次；替身只模拟"容器里有一个子窗口"这一真机条件，不是给量具开后门）；
    B8 图标工厂 128 项 FIFO 缓存（同一枚只画 **1** 枚、灌 168 个不同 key 后仍 **128** 项、最新一枚仍命中）；
    B1 30ms 轮询跟着窗口态开关（构造期 **0** 拍、最小化 **0** 拍、恢复后重新起表；接线用 AST 判读：
    `self.video.sync_poll()` 必须在 `changeEvent` 的 WindowStateChange 分支里、且不许塞进 `if self._fs`）；
    B3 目录展开封顶 3000（12 个文件的目录按 5 展开 ⇒ 交回 **5** 个、截断标记真；没到顶的目录一个不少；
    默认参＝类常量）；
    B11 两个 store 的失败负缓存都封顶 4096（灌 4113 条 ⇒ 留 **4096**、挤出 **17**、最新那条仍记得）。
    量具自己又翻出一笔：`PosterStore._failed` 原来是 `set`，`set.pop()` 挤掉的是**任意**一条——第一版
    判据读到"最新那条也被挤掉、最旧那条还在"就是它；改成 dict 的插入序 FIFO 后才配得上"到顶挤最旧"这句话
    （`posters.py` 里写明了）。而 `ipc._LEAKED` **故意不封顶**：那几条是在途 OVERLAPPED 缓冲的强引用，
    提前放手等于让内核往已释放内存写（UAF），它的条数只跟退出瞬间的在途数走、不与库规模相关——
    漏列进"已核实的性能项"会被好心改坏，所以在这里点名它**不进** B11 那一族。
    回归：`test_thumbs.py` 20/20＋证伪 7/7、`test_library.py` 60/60（offscreen；真窗口那两条留给 R21-e）。

33. **"等屏幕"那一半不能靠推测交账——屏一亮就得自己跑完**：这一轮把只欠"屏幕给像素"的四条尾巴一次跑完，
    读数都在 `tests/out/*-r11*.log`（2026-10-11 22:57–23:03，同一台机、出厂装配、150% 缩放）。
    交账顺序是：先由 §6 第 31 条那个 `watch_cover_experiment.py` 在 08:37 等到屏、08:38 把"真外来窗口 ⇒ 记 ENV"
    那一半交回（见第 31 条末段）；22:56 再量一次屏（`整屏 3840×2160 亮度52 颜色182`）才开第二轮——
    **开跑前重量屏，不拿上一轮的读数当依据**，屏幕这件事一小时内能翻面两次。
    ① UI：`test_ui.py --falsify` **73 CAUGHT／0 MISSED／rc=0**、`test_ui.py` **154/154、0 ENV、0 FAIL、rc=0**——
    离屏那 4 条结构红（原生框架位 0×0／原生事件计数空／mpv 子窗口两条）在真 HWND 下全过，真抓屏两条给了数
    （视频区 2370×1244 亮度34 标准差58 颜色49；整窗 49 色 亮度34）。**`0 ENV` 这一项要单独看**：它才说明这一趟
    不是"被挡住了还算过"，判废台账是空的，154 条全都有测量对象。
    同一份证伪回执里那一条 `[FAIL] U57-A 没判废时必须照常记红 —— 对照组：这一条该进台账`
    （tests/out/falsify-ui-r11.log:11）**不是产品红**，是 U57 那条腿故意造的形状——它要证明"没打判废标记时台账
    真的会记红"，所以自己往台账写一条红，同一条腿的收口在同文件 line 88 的 `[CAUGHT] 像素判据的记账分家…`。
    教训：**对照组故意留下的红必须顺着回执找得回"它属于哪条腿"**，否则下一位读者（或下一轮的我）会照着这条红
    去改本来没坏的代码——这正是第 31 条要避免的那类误读，只不过这次发生在量具自己的回执里。
    ② 逐屏抓档：`capture_sweep.py --screen` 三档（1.0／1.3／1.8 各一进程）**每档 50 条 PASS、0 FAIL、0 ENV、
    "掉出视口"点名 0 个、rc=0**。最浅三张：1.0 档 empty-page 1.00%／title 2.39%／gif 6.79%；
    1.3 档 1.41%／3.78%／7.38%；1.8 档 2.20%／6.46%／7.48%——字号放大时最浅的表面反而更浓（字变多了），
    这条走向本身就是"档没跑错、也没被上一档污染"的旁证（`_scale_qss` 那个"拿上一档产物再乘一遍"的旧错，
    第 30 条记过）。标题条 1.0 档 **2.39%** 与 02:26、03:05 两份留档逐条同值 ⇒ 第 32 条 (e) 那条
    "绝对墨迹地板是装配相关的"标定在第三次独立跑上仍复现。#45 那把"掉出视口尺"从此有了**产品装配下的对照**：
    出厂三档 0 越界（侧栏靠 `_fit_sidebar` 300→520 一路兜住），它管的是未来那档"界面字号"与
    "窗口拖窄＋字号调大"的组合，不是当前出厂缺陷。
    ③ 截图工坊真机：`test_gif_real.py` 24 行 PASS（其中 1 行是"抓屏通路可用"这条前提，判据 23 条与 §5 的计数对得上）、
    `--falsify` **12 CAUGHT／0 MISSED／0 NOP／rc=0**。真 mpv＋真 ffmpeg：两趟导出、产物 16 帧**逐帧比首帧**
    （15 帧与首帧不同＝真会动）、同区间带字幕／不带字幕各一趟比出"字真烧进去了"、**工坊是活动窗时**按 B
    真截出配置里那几张（#32 那条"热键被面板吞掉"的真机腿）、再按 B 取消后不再多截、导出中途关窗 ⇒
    残留 ffmpeg=[]／crash.log 新增 0 字节。跑完按镜像名查 `mpv.exe`／`ffmpeg.exe`：两条都是"没有运行的任务"。
    ④ 画面族真机：`test_transform.py` **59 PASS／0 ENV／rc=0**、`--falsify` **32 CAUGHT／0 MISSED／0 空判／rc=0**——
    离屏那趟两条判废（基线帧／噪声片段基线）这趟变 PASS，基线 1280×720、边缘能量 1.400，
    §5 里"59 项／32 条"那串数第一次是在**真窗口**下复现出来的，不是照抄离线装配。
    三条只属于"批量真机趟"的纪律：**(a) 串行、不并跑**——这四类趟都会建真窗口、抢前台、往 `tests/out` 写同名 PNG，
    并行必然把对方读成"被外来窗口挡住"（第 31 条那 7 处判废就是这么来的），所以一条链里四趟串着跑、
    同一时刻盘上只放一条链；**(b) 覆盖前留底**——出厂档 PNG 固定写在 `tests/out/sweep/`，开跑前先 `cp -r` 成
    `sweep-pre-r11-20261011-2259/`（仓里没有 VCS，覆盖等于丢证据）；**(c) 开跑前重量屏、跑完再看 ENV 计数**——
    "屏幕亮着"不等于"这一趟没被挡"，这两件事分别由 `capture_health()` 和每趟日志里 `[ENV]` 的行数裁决，
    只看其中一个都不算交账。

34. **加一页界面会牵动多少把尺**：2026-10-12 给侧栏加「声音均衡」这一页（第六页、页签排第五），
    产品侧只有三处（`tab_defs`／`tab_actions`／`sidebar_stack`），**尺子侧的账全在"名单从哪儿取"**：
    抄一份五页名单的地方，加页那天不会报错，只会让那一行打印的数字指向别的一页——
    "漏了一页"在输出里没有任何症状。所以这一轮把剩下五把还抄名单的尺改成现取
    （`probe_font_clip.py`、`probe_min_width.py` 走 `enumerate(str(d[0]) for d in win.tab_defs)`，
    `probe_scrollbar_allow.py`、`probe_shape_guards.py` 各加一个 `page_name(win, i)`，
    `probe_tab_fit.py` 那份没人用的 `LABELS` 直接删掉——留着只会被下一把尺抄走），
    `test_ui.py` 里那两处（页签点击名单、各页换行说明行数）上一轮已经改成按 `tab_btns`／
    `sidebar_stack` 现取，配套的形状判据是 `sidebar_stack.count() == len(tab_defs)`，缺哪一头都当场红。
    改完逐条实测（离屏装配，`tests/out/probe-lists-r12-offscreen*.log`）：六页各自的页名与需求都出来了
    （声音均衡页内容宽 282／290，取决于字号档），原来被读成"统计"的那一页现在点名点对了。
    同一批日志里还能看到"六格排不下就换行"这条机制在加页之后照旧工作（1.5 档、离屏装配下页签条
    需要 558px 而侧栏钉在 520，第六格「统计」落到了第二排 x=0）——**这条只当结构旁证**：
    离屏装配的绝对像素与出厂装配不同（§6 第 30 条那笔账），换行在两排下排得开与否的真值仍以
    `capture_sweep.py --font-scale` 那几档为准。
    **这一条只证"名单取对了"，不证像素**——六页下的逐屏留档、`test_ui` 真机半、逐档字号那三档
    仍在 `tests/watch_ui_round.py` 那条腿上等屏幕给像素（屏幕睡着时 `capture_health()` 判"本轮不作数"、
    退 3，那是环境没给东西，不是产品坏了，也不是做完了）。看门狗自己不做留底这件纪律，
    所以 06:10 由外部把 `tests/out/sweep/` 整目录抄成 `sweep-pre-r12-20261012-0610/`（56 个文件），
    六趟落盘会覆标准档那批 PNG；r13 六个日志名当场查过与盘上现有文件不撞车。
    顺带清掉三笔"数字抄在散文里"的账：`stats_panel`/README/design 里那句"侧栏第五页"跟着页序改，
    而 `theme.py` 与 `main_window.py` 里"五个页签就是 55px"那两处**保留原数并标成五页签时代的读数**
    （松弛按格数累加这条算式没变，六格下的绝对宽没测，所以不写 66px 冒充实测）。
    同一类病在**留档侧**也有：`capture_sweep.py` 加一页要动四处手抄名单（`SURFACES`／`s_sidebar_*`／
    捕法里的 `_sidebar(N)` 下标／`widget_of`），少抄一处不报错，只是那一页永远没有档——这一轮
    四处都齐（靠人记得），于是补了一把不建窗口、不写文件的对账尺 `tests/probe_surface_cover.py`
    （验收 7/7、证伪 7/7，见 §4 那条；**它当时还没接进任何常跑的链**，接之前它只是"记得跑才算数"——
    这一句记的是 2026-10-12 的状态，2026-10-13 由 #70 接进 `test_ui.py` 结构段，见下面第 35 条 ④）。
    最后清的是"文档里的手抄数会漂"这一笔：本轮照 grep 现数，`test_ui` 的静态调用点从旧记录的
    140／66／8／171 变成 **151／72／6／182**（加页那一轮真往 `test_ui` 里添了判据，文档没跟着动），
    `audit_qt_attrs --selftest` 的对照从 README 与 §4 记的 **13** 变成现数 **14**（同样没人回来改）。
    两处都改成"数于 2026-10-12、下轮照那四条 grep 再数一遍"的写法；`test_gif_real` 那句"23 项／
    12 组／4 项"**本轮没验**（它的静态 `check(` 是 27 处，但静态≠实跑，这条要等能真跑那趟的收尾行，
    记在这儿而不是顺手改成 27——那等于拿另一种口径冒充实测口径）。
    **2026-10-13 01:17 补了这两笔能补的真跑数**（串行跑、一次一趟；被证伪改坏的七份文件
    ——两份 .bat、两份 .reg、`packaging/open_file.py`、`app/engine/usage.py`、`app/ui/stats_panel.py`
    ——开跑前整份抄进 `tests/out/pre-falsify2-20261013-0125/` 并当场记 md5，跑完逐份核回**全 SAME**）：
    `test_launchers.py --falsify` 实测 **8 条注入 / 0 漏网**、rc=0（`tests/out/launchers-falsify-r14.log`），
    `test_usage.py --falsify` 实测 **9 条注入 / 0 漏网**、rc=0（`tests/out/usage-falsify-r14.log`）——
    README 里"改坏 8 条／改坏 9 条"那两句从此是实测口径而不是手抄。
    （这一句记的是 2026-10-13 的状态：`test_launchers` 的注入条数后来随产物一起长——
    「打开方式」那两份 .reg 与启动器 exe 进来之后是 **18 条**，2026-10-16 现数，见第 36 条；
    README 里那句已经跟着改成 18，"改坏 9 条"那条 `test_usage` 没动过。）
    `test_gif_real` 那笔**已付清（01:46 真跑＋02:00 补两档）**：工坊那趟吃真抓屏，屏黑时它自己会退 3
    （不作数），所以挂在 看门狗 r15 的第八趟上。三档实跑数各归各的口径——
    真机模式 **24 行 PASS**（其中 1 行是"抓屏通路可用"这条前提 ⇒ 判据 **23 条**，
    `tests/out/gif-real-r15.log`，rc=0）；`--falsify` 收尾行原文
    **"证伪 12 组，漏网 0 组"**、rc=0（`tests/out/gif-falsify-r15.log`）；
    `--residue` **4 条 PASS**、rc=0（`tests/out/gif-residue-r15.log`，那一档一个像素都不看，
    屏黑也跑得）。静态 `check(` 字面 **28** 处 = 1 处定义 + **27** 处调用，那 27 与两档实跑**逐函数对上**：
    `run_real` 23 + `run_residue` 4 = 27 ⇒ "23 项／12 组／4 项"那句从此是**实测口径**，
    而 12 组走的是 `FALSIFY` 表＋临时目录现造现删那一路，不在这 27 个调用点里（三种口径不互相顶）。

35. **r13 那五条红的账（判据侧三处、留档侧一处、环境侧一处）与 r14 的收口＋ r14 那条新红（这条是产品坏）**：
    r13 那一轮跑在
    2026-10-12 08:43–08:46（`accept-ui-r13.log` mtime 08:44:33，留底在 `tests/out/pre-r14-20261013/`——
    目录名记的是抄进去的那天，不是跑的那天），证伪 **77 CAUGHT / 2 MISSED**、rc=1，验收 **162/165**、rc=1，
    逐屏三档与读数腿 rc=0。五条红里**没有一条是产品坏**——四条是量具自己抄了旧世界，一条是屏幕不给像素。
    按「红灯先问判据再修判据」那三问过一遍，逐条记账：
    ① **手抄页序**（U36 与「侧栏四页的按键提示」那把尺）：加「声音均衡」成第六页之后，统计页的下标
        从 4 变 5，而这两处把页号写死在判据里——于是它们量的是别的一页。改成 `page_index(win, "统计")`
        ／`page_of(...)` 现取，名单不再是判据里的字面量。
    ② **QStackedWidget 的非当前页**（同一条提示尺的第二种坏法）：不在当前 index 的那几页 `isVisible()`
        恒 False，所以那句「画面页页脚：该有字却没有可见行」是量具拿错了页，不是页脚没字。
        `_hint_on_screen` 因此带"切到那一页量、量完还原"的前置，r14 的 PASS 读数里逐条写着
        「（切到 TransformPanel 量，已还原）」——这句话就是这条腿真的动过页的证据。
    ③ **页签行成宽度瓶颈**（U52）：这条问的是侧栏**总宽**，而 `_fit_sidebar` 的
        `w = min(max(配置宽, max(页签行需求, 页内容需求)), 520)` 里页签行那份也贡献进 max——
        摘掉内容通道的闸之后总宽照样够，判据于是量不到它要量的那件事。改成只问
        `sidebar_page_need` 那一根通道（`_pn_real >= _cw_real and _pn_naive < _cw_real`），
        ×2 字号那半把尺（三处裁字的像素对照）原样保留。
        r14 证伪读数：U36 CAUGHT（`统计页下标=5 侧栏可见=True`）、U52 CAUGHT
        （`内容通道 装闸=272→摘闸=195／页签行=288`）。
    ④ **留档侧的名单**（第 34 条末尾那把 `probe_surface_cover.py`）本轮上了常跑链（#70）：
        `test_ui.py` 结构段挂一条 Qt-free 对账腿（`PSC.load()`→`PSC.judges()`，判据条数有下限、
        import／读盘／解析三种失败各记 ENV 不记产品红），证伪台账另加一条 `drop_surface` 注入
        （带 NOP 判别：三张表逐张比过，没改动任何一张就不算抓到；基线有红也顶掉"抓到"）。
        r14 两头都拿到证据：验收 `[PASS] …页序=播放列表/媒体库/调节/画面/声音均衡/统计｜判据 7 条，红=无`
        （`accept-ui-r14.log:116`），证伪 `[CAUGHT] …基线红=无 空操作=False 点名=['每一页都有一个侧栏表面档…']`
        （`falsify-ui-r14.log:38`）。§4 的静态计数那串跟着现数订正（152／11／76⇒83／10／188，
        上一版 151／10／72／79／6／182 标成"数于 2026-10-12、已被订正"）。
    ⑤ **悬停预览那两条**（#72）：r13 的「浮层里真的是抽出来的一帧」（读回 0×0、原生窗口报隐藏）
        和「光标换一格 → 浮层跟着走」（`736 → 736`）是**同一条机制的两个症状**，不是两个 bug：
        合成 hover 喂完之后真指针还停在滑轨附近，它自己的 `mouseMoveEvent`/`leaveEvent` 把浮层
        推到右夹紧位（`_place` 的 `hi_x = max(lo_x, 952-212-4) = 735`，dpr=2 下读回 1472 那个原点），
        紧跟着的 leave 又把浮层 hide 掉——所以位置腿读到"钉死"，像素腿读到"隐藏/0×0"。
        机制假设不当根因：这条是靠"736 恰好等于夹紧值、而合成事件从没往那儿指过"算出来的，
        再用 r14 实测收口。修在量具侧两处：开跑前先把真光标请到视频区左上角（`win.video.mapToGlobal(6,6)`），
        再喂合成 hover；并新增纯函数 `preview_contention` 做分流——事件里最后那格／x／可见性任一变了
        才算"浮层被动过"，被动过时看真光标在不在轨道矩形里：**压在轨道上 ⇒ ENV**（环境争用，不作数），
        **没压轨道而它自己动了 ⇒ FAIL**（产品侧真坏了）。同一判据两条出口，所以 `pixel_check(` 只 +1
        而 `env_check(` +4。r14 三条腿全 PASS（`(433,585,212×140)` 出现 → `412×268 亮度32 颜色173` 有画面
        → `433 → 736` 跟着走 → 离开收起）。这把尺自己也有牙：证伪 U6g 喂六条已知好坏对照，
        钉住"恒 ENV 金牌"和"恒 FAIL"两种坏法。
    **r14 整轮**（`tests/out/ui-round-done.flag`，01:06:22）：证伪 **82 CAUGHT / 0 MISSED** rc=0、
    验收 **165/166** rc=1、逐屏标准档／1.3／1.8 三趟 rc=0、读数腿 rc=0。
    唯一那条红**不在上面五条里**，而它最后落在"产品坏"这一边——本条这一段是对它早先那段写法的**订正**：
    01:06 刚看到它时我把它记成"两问未定"（判据读到送达之前的旧话／内核真丢通知），并写下
    "r15 两趟读数出来之前不写已修或偶发"。01:15–01:35 用离屏装配把方向量死了，两问里那一半都不用等 r15：
    ① **它是产品坏**。`_play_saying` 那行写的是**状态口径**（暂停时说「暂停」），而同一条控制条上
        另外三处全是**动作口径**：图标 `_set_icon(btn_play, "play" if paused else "pause")`（暂停时给▶，
        即"点我开播"）、`_mute_saying`（静音时给「取消静音」）、右键菜单
        `("暂停" if not paused else "播放", "toggle_play")`。离屏真装配实测（`QT_QPA_PLATFORM=offscreen`，
        直接喂 `_on_pause`）：修复前 `pause=True → tip='暂停（Space/K）'`，修复后
        `pause=True → '播放（Space/K）'`、`pause=False → '暂停（Space/K）'`，与图标同步。
    ② **r13 及更早那六趟的绿是旧话，不是产品对**。旧映射下「播放」这半句**只可能**来自播放态
        （`_paused=False`）；而日志里那行的读数恰是 `pause=True tip=播放（空格）`——缓存已经 True、
        悬停还是按之前的那一句。机制在 `player._apply()`：**先写 `_props` 缓存再发 `sig_pause`**
        （`app/engine/player.py:422`／`:429`），发信号那一下跑在 `mpv-ipc-read` 读事件线程上
        （`app/engine/ipc.py:271` 起的 `_read_loop`），重画要走 Qt 排队送达。这条时间线不是新假说：
        同一个文件里 2816–2818 行的注释早就记过它的另一副面孔（duration 与滑轨 `_dur` 之间隔着一次投递，
        "同一份代码两轮里一轮绿一轮红，红的正是这条"）。r14 只是这一次送达抢在了读数前面，
        于是把反义映射照了出来。
    ③ **判据侧的洞同批补**（红灯先问判据：它为何存在=让悬停说法跟着内核；前提还在=动作口径由上面三处
        同族供着；接班的=下面这把带八条好坏对照的纯函数尺）。旧写法两处失明：`startswith("播放")`
        会被中性的「播放/暂停（Space/K）」顶过去；而两句都只比"现在对不对"、不比"这句是不是刚翻过来的"，
        于是旧话能冒充同步。新增纯函数 `play_tip_flip(pause_now, tip_before, tip_now)`——
        比**括号之前那半句全文**（不是前缀）且要求**与按之前那句不同**，两条闸互为盲区补位；
        验收里那两句改成走它，`再按一次恢复播放` 也从"只读缓存"升成同一把尺倒着量一次。
        证伪台账 U6h 喂八条已知答案的形状（正解双向／状态口径／一字不差的旧话／中性蒙前缀／
        反向说错／说法半句空／整句空），并**要求理由点名是哪一半不成立**——只装一条闸的尺会在某两条上
        给出另一个理由。01:30 屏黑那趟实测 `[CAUGHT]`（`tests/out/falsify-ui-r15-local.log:33`）。
    ④ **这把尺自己的牙在真链路上验过**（01:53–01:55，收在 §4 那条 `tests/probe_playtip_teeth.py`）：
        把产品侧摘回旧映射（改控件 `__dict__[TIP_ATTR]` 里那份 callable，喂真 IPC 走一次真 toggle），
        新尺 2 秒不放行＝抓到，而读数证明拦下来的是**说法**那一半（`内核pause=True` 已经到了）：
        `放行=False 耗时=2000ms 内核pause=True tip='暂停（Space/K）' 按之前='播放（Space/K）'
        说法那半=pause=True 该说='播放' 实说='暂停'`。第二趟变异（M2）把说法冻成常量「播放」，
        造出的正是 r13/r8 那种"送达前后一字不差"的可观察形状：
        `放行=False … 说法那半=说法对了但和按之前那句**一字不差**（'播放（Space/K）'）`，
        而同那一刻旧的 `startswith("播放")` **放行**（`startswith('播放')=True`）——
        "偶然绿"是被**跑出来**的，不是事后讲的道理。四趟控制腿（不加变异／每次还原后）都在
        31–32 ms 放行，证明探针不是只会说"不放行"。连跑两趟各 **14/14 rc=0**，判决行逐字节相同
        （只有"耗时"栏变），留底 `tests/out/probe-playtip-teeth-r15-firsttry-0152.log`，
        复跑命令就打在这份日志的第一行。盘上另有证伪台账 U6h 那八条已知好坏对照
        （`falsify-ui-r15.log:40`、屏黑那趟 `falsify-ui-r15-local.log:33` 同一条都 CAUGHT）——
        它们量的是纯函数那一头，这一趟量的是投递之后那一头，两把不许互相顶。
        顺带记一笔量具教训：第一版探针改的是 `type(bar)._play_saying`，那是**空操作**——登记里存的是
        构造时取到的**绑定方法对象**，换类属性换不动它；探针当场没断言"变异后 ≠ 基线"，就差点把
        "尺没牙"当成结论写进台账。从这里起，摘映射必须改 `__dict__` 里那一份且必带 `mut != base` 断言，
        而探针自己开跑前必须断言 `test_ui.APP` 已绑定（上一版没绑，`pump()` 空转，"重画永远不来"
        被读成"尺拦得住"）。判决一律读 `flip()` 返回的字段，**不去 grep 自己的 detail 文案**——
        文案一改判决就漂，那正是 #73 这一轮学到的那类坑。
    ⑤ **真机两趟已经对上（01:43–01:44，看门狗 r15 的第 2、3 趟）**：`accept-ui-r15.log` 与
        `accept-ui-r15b.log`（同机同屏隔 45 秒）各 **166/166 通过、无失败**，那两条读数的字符串都一样：
        `pause=True '暂停（Space/K）' → '播放（Space/K）'`、`pause=False '播放（Space/K）' → '暂停（Space/K）'`。
        这条不再是"偶发读数"也不是"要等下一趟"——判据现在读的是**送达之后的那一句**，两趟独立跑到同一读数。
        同趟第 1 趟证伪 **83 CAUGHT / 0 MISSED** rc=0（`falsify-ui-r15.log:40` 就是 U6h 那条）。
        与静态登记点对得上，但**别按"83+1 对照=84"那样凑**（先前这里就是这么凑的，是错的）：
        r15 那两趟实跑的**证伪台账都是 83 行**——屏亮 83 CAUGHT，
        屏黑 79 CAUGHT + 4 条 `[ENV ]` 不作数（`~~` 那行逐条点了名）；而当时 AST 数 `falsify()`
        里的登记点是 **84** 处（76 处 `falsified.append(` + 8 处 `f_case(`，没有一处在循环里）。
        差的正是 `except BaseException as _p6f_e` 那条腿（当时在 `tests/test_ui.py:4662`）：
        它只在"侧栏覆盖对账"那把量具自己读盘／解析崩掉时才登记，且一登记就是 MISSED（rc 掉到 1），
        健康跑必然不出现 ⇒ 83 对 84 不是洞，是那条腿**等着失灵才响**。
        **#74（2026-10-13 23:04）把这件事变成回执自己说的**：`report()` 收尾多一行自打，r16 实测
        `---- 证伪台账自打：共 84 行 —— CAUGHT 84／MISSED 0／ENV 0／非布尔 0 ----`、rc=0
        （`tests/out/falsify-ui-r16.log`）。登记点随之涨到 **85** 处（77 直接 append + 8 `f_case`），
        实跑 84 行——差额仍是那一条腿，如今它在 4742 行：同一个引用因为上面插进 80 行就漂了 ⇒
        **认锚点文本、别认行号**。自打的四个数与外部逐行数行首（`^\[CAUGHT\]`=84／`^\[MISSED\]`=0／
        `^\[ENV \]`=0／`^\[JUNK\]`=0）在同一份日志上逐位相同，两边都记进 §4；紧接着的验收趟
        **166/166、rc=0**（`accept-ui-r16.log`）＝原地改文件那种证伪趟还原得干净。
        这条自打自己的牙不在这里自证，看 `tests/probe_tally_teeth.py`（五种坏法逐条要求翻红＋
        真执行那条三方对账＋恒真桩自检；`probe-tally-teeth-r16.log` 现 md5 `dbd192cb…`、两档 rc 全 0，
        01:49 又原地复跑过一趟：stdout 与那份留档第 2–7 行逐字节相同（`a49977e8…`），所以文档里这几个
        数是量具当场打印的、不是手抄的孤本）。
        而 U57-A／U57-B 那两条对照组根本不在证伪台账里
        （它们走 `check()`／`pixel_check()` 在台账抬头之前打印，U57-A 那条桩红还被
        `del results[...]` 从验收台账里摘掉，免得证伪跑次把红留给汇总）。
        01:30 屏黑那趟的账另算：79 CAUGHT + 4 条"要靠真屏幕、本轮不作数" = 83 行
        （`tests/out/falsify-ui-r15-local.log`，同一份量具在屏黑时只把那四条换成 ENV，
        台账行数不变——这正是"不作数"与"漏网"分家的意思）。
    ⑥ **r15 八趟整链的账（01:40–01:46 看门狗那一轮，哨兵 `tests/out/ui-round-done.flag`
        `generated_at=2026-10-13 01:46:45`，八行全 `rc=0`）**：证伪 **83 CAUGHT / 0 MISSED**、
        验收两趟各 **166/166**（⑤ 已记）；逐屏三档（1.0／1.3／1.8）各 **51 条 PASS、0 FAIL、0 ENV**、
        rc=0，最浅三面在 1.0 档是 empty-page **1.00%**／title **2.39%**／gif 6.96%，
        1.3 档 1.41%／3.78%／8.26%，1.8 档 2.20%／6.46%／9.04%——标题条 2.39% 与 02:26、03:05、
        r13、r14 那几份留档**逐条同值**（第 32 条 (e) 那条"绝对墨迹地板是装配相关的"标定第五次独立复现），
        而且这次连 PNG 都逐像素相同（r14↔r15 的 `title.png`、`settings.png` 各 **0 个**改动点）；
        读数腿 `probe_type.py` rc=0（`[result] 全部读到了像素`，调节页 10 行／画面页 6 行
        值档宽全 58、右缘分布单一 ⇒ 药丸齐平那条仍成立）。
        **同形状下唯一动了的是工坊那一面**：6.63%（r13）→6.62%（r14）→**6.96%（r15）**，
        而采样点数（722400）与底色占比（75%）三档一模一样 ⇒ 几何没变，变的是**内容**。
        按"跨轮互比先要页面态指纹"的规矩这里本来该停下来——回执没给指纹，于是把两张 PNG 切开定位：
        5304 个改动点全落在 y=335..413 那一条提示行上，r15 那句是
        「起点离片尾只剩 0.0 秒，导不出这一段」而 r14 是「还剩 1.7 秒可导」（裁图留档
        `tests/out/_gifband3-r14.png`／`_gifband3-r15.png`，r15 原图在
        `sweep-pre-r15b-20261013-0200/gif.png`）。**结论：不是产品坏，是抓档链跑到工坊时样片已经播到片尾**，
        而那句提示按「片长 − 起点」现算。修法不是放宽阈值也不是把这一面摘掉，是**给这一面补状态指纹**
        （`capture_sweep.py` 的 `s_gif` 现在把「起点／提示行原文」写进回执），02:01 补跑一趟标准档
        （`tests/out/sweep-ui-r15b.log`）：指纹行出来了（`起点=4.38 提示行='还剩 1.6 秒可导'`），
        墨迹回到 **6.64%**，与 r13/r14 同档同值——**这一趟 rc=3**：收尾两条真抓屏（整窗／控制条抓屏）
        期间屏幕睡了（`整屏 3840×2160 亮度0（最低0/最高0）颜色1`），台账上是 49 PASS／0 FAIL／0 ENV，
        那两条没进台账、只在收尾那行按 rc=3 点名"抓屏那几条不作数"，按第 31 条的规矩记环境不记产品。教训两条：① **同形状（采样点数＋底色占比一致）
        下的墨迹漂移，第一解释是"内容随状态变"而不是"产品改了"**，先定位像素再谈判据；
        ② 留档面**自带状态指纹**才谈得上跨轮比数，否则下一次还得重做我这次的切图。
    留底四处（覆盖前抄，不用脚本自己的记忆）：`pre-r14-20261013/`（r13 六份日志 + 7 张 PNG + 一份
    离屏证伪）、`sweep-pre-r14-20261013-0055/`（`tests/out/sweep/` 整目录 32MB）、
    `pre-r15-20261013-0115/`（r14 六份日志 + 七趟名单 + 哨兵 + 8 张屏图）、
    `sweep-pre-r15-20261013-0140/`（r15 之前的 `tests/out/sweep/` 整目录 57 个文件）＋
    `sweep-pre-r15b-20261013-0200/`（补指纹腿之前那一份，**r15 的 6.96% 工坊 PNG 就存这儿**）＋
    `watch-ui-round-pre-r15-20261013-0141.log`（看门狗自己的旧版）＋
    `probe-playtip-teeth-r15-firsttry-0152.log`（探针第一趟，与第二趟独立同读数）。

36. **「打开方式」这一族的账：三级证据、一条本来没牙的判据、和一段问不到 UI 的路**：
    用户那句原话是「我现在想右键视频资源然后把打开方式选成这个播放器都不行啊，**根本都没有这个播放器的
    选项**」，而当时我这边的账是"58 条注册表值全对、端到端点了能播"——两句都真，缺的那一环是
    **没有人替用户问过外壳**。这一轮补的就是那一环，三级证据全是 2026-10-16 02:36–02:44 现跑现留档
    （串行走、一次一趟，会改文件的两个证伪趟各跑前后都确认过盘上没有别人的副本）：
    ① **数据在不在**：`check_openwith.py --check` → 8 项判定 0 失败、模板 55 节／58 条值逐条与注册表现数
        对上（`tests/out/check-openwith-live-1016-023615.log`）；量具自己 `--selftest` 16 条
        CAUGHT 11／CLEAN 3／MISSED 1／NOP 1（`-023610.log`，后两桶是**故意**做的瞎判据与空注入，
        这本账要求它们被记成 MISSED／NOP 而不是"抓到"）。
    ② **外壳认不认**：`check_openwith_shell.py` → PASS 3／FAIL 0／ENV 0／NOTE 3、rc=0、2.5s
        （`-023653.log`：48 个扩展名 DEFAULT 候选总条数 1044、单扩展名第三方最少 17 条，
        EXE 候选最少 1 条最多 10 条）；反向对照 `--expect-red` → FAIL 2、rc=0，那条判据的原文把
        五个数一起印出来，缺一个都不许绿：「DEFAULT 缺 48／EXE 缺 48／哨兵有牙=True／
        失明扩展名=0／候选总数=1044」（`-023655.log`）。
    ③ **点了能不能播**：`e2e_openwith.py` 从 HKCU 现读我们那条 `shell\open\command`、**原样执行**
        （不是另起一条自认为等价的命令），断言窗口标题、mpv 子进程、`path`、时间推进与像素 →
        PASS 12／FAIL 0／ENV 0、9.6s（`e2e-openwith-1016-023744.log`）。
    这一族的绿都是**拆得动的绿**：`falsify_openwith_shell.py` CAUGHT 4／MISSED 0／NOP 0
    （`-024105.log`）、`test_launchers.py --falsify` 18 条全 CAUGHT（`-024409.log`）、
    `make_launchers.py --selftest` 共 68 条／MISSED 0 NOP 0（`-024138.log`）。

    三笔 measured 的账，都不是一开始想到的：

    (a) **DEFAULT filter 这条判据本身没牙**：假扩展名 `.qqqzzznotatype` 在 DEFAULT 下照样列出 19 条、
        **含我们**——`App Paths` 让这个 exe 成为任何文件类型的通用候选。要是只测 DEFAULT，这本账在
        "我们一条都没注册"的机器上也会绿。有牙的是 `ASSOC_FILTER_EXE`：同一个假扩展名列出 0 条。
        这一对比当场可复跑（`python tests/check_openwith_shell.py --only .qqqzzznotatype`，
        留档 `tests/out/check-openwith-shell-1016-024729.log`：那行 NOTE 自己印
        `.qqqzzznotatype:19/17/0`，同一趟里 EXE 那条判据正确地翻红）。
        修法不是把判据放宽，是**把哨兵写进这本账**：假扩展名哪天也列出我们，EXE 那条自己降级成 ENV、
        正文写「不许拿它当证据」（`falsify` 第二条变异就是把它喂成这种样子，它落在 ENV 支、rc=2）。

    (b) **本机问不到那个菜单**（Win11 24H2／26300，四条门全是实测关着的、不是推测）：`FolderItem.Verbs()`
        里根本没有 openas；`InvokeVerb('openas')`、`rundll32 shell32.dll,OpenAs_RunDLL`、
        `SHOpenWithDialog`、`OpenWith.exe <path>` 四条各试过一趟都没把面板叫出来。改走"开真窗口自己找
        文件项"，`CabinetWClass` 的 UIA 子树**只有 1 个元素**（`TITLE_BAR_SCAFFOLDING_WINDOW_CLASS`），
        用户自己开的三个窗口也一样（`tests/out/probe-openwith-cloak-1016-021550.txt` 逐窗口印着
        `UIA子元素=1`；顺带量到最小化的 `CabinetWClass` 矩形是 Empty、还原后才有点，早先那版探针
        筛类别名写成 `CabinetWindow`，一根都没匹配上）。所以「眼睛在菜单里看到它」这一条**本机没法用
        程序证明**，README 与本条都照实这么写；能证明的是外壳那份"能打开这个类型的处理程序"名单——
        `SHAssocEnumHandlers` 是它的官方入口，而"菜单画的就是这一份"是**合理推断、不是本机实测**，
        这句话到此为止。探针留档 `tests/out/probe-openwith-*.txt`，都是一次性的、不进判据。

    (c) **COM vtable 的三个坑**（都写进 `probe_openwith_assoc.py` 与 `check_openwith_shell.py` 的头里）：
        槽地址要**先解引用**再交给 CFUNCTYPE——第一版直接拿 `tbl + 8*i` 当入口点，当场
        access violation writing 0x7ffe…4818，那正好是第 3 个槽自己的地址；第 7 槽 `GetName` 返回 hr=0
        却把 NULL 写进出参；第 8 槽 `GetUIName` 对每一个 handler 一调就硬 AV（`--probe-dead-slots` 可复现，
        默认不碰它）。能用的是第 4 槽 `GetMenuName`，它给的正好是菜单上那一行字。

    **红灯先问判据**在这轮用了一次：`check_openwith_shell.py` 第一趟报 FAIL，理由是 48 个扩展名里各有
    一条候选取不到名字。先查"量的那条腿对不对"再决定动不动产品：真名取到 996 条，占位那 48 条在每个
    扩展名上都恰好一条、返回码同为 0x80004005 ⇒ 是**某一个外来处理程序自己不吐名字**，不是取名字的腿断了。
    于是那条判据拆成四支（全空→ENV／零占位→PASS／每扩展名恰好一条且同码→NOTE／其余→FAIL），牙还在；
    而 `falsify` 里"走错槽"那条变异第一趟被记成 MISSED——查下来是**我的期望**写错（期望它落 FAIL，
    而"整扩展名全空"本来就是设计成"不作数"的 ENV 支），改期望、闸门一条没放宽。
    同一趟还顺手把散文里手抄的「48 个扩展名」换成脚本自己数的 `len(rows)`。

    **量完再决定、于是没做的一处改动**：Win11 那个面板有时要靠开始菜单里的 .lnk 才认得出这是个应用。
    本机 `find` 过 per-user 与 common 两个 Start Menu，没有任何播放器 .lnk，而外壳枚举在 DEFAULT 与
    EXE 两个 filter 下都已经逐扩展名列出我们（48/48）⇒ 候选名单不缺这一条，多写一处就多一处要撤销的
    东西，所以没生成。这是量完之后的决定，不是省掉这一步：若哪天真机发现菜单里没有，按 (b) 那份留档回头加。

37. **R17 这一轮的四笔账：双击到底加载没有、GPU 到底在不在跑、一把数报告的尺怎么会瞎、
    以及一本测试账把脏水泼到用户机器上**（2026-10-16～17 本机现跑现留档；机制约束在 §3 第 13～15 条）：

    (a) **用户报的那条是真的，而且现在有一条能复跑的账**。症状原话「双击视频确实打开了播放器，
        但视频根本没加载进来，只是单纯的播放器界面，显示打开文件夹那些」。修完之后按注册表里那条
        命令**原样执行**的那趟（`tests/e2e_openwith.py`，留档 `tests/out/e2e-openwith-r17fix.log`，
        PASS 14／FAIL 0／ENV 1、9.4s）交回来的读数：
        「顶层窗口标题带上那个文件名 —— hwnd=0x12c0cb2 标题=`gelu-sample-3.mkv - 格斗芦荟播放器`」
        「内核报的播放位置在往前走、pause 为假、vid 轨道已选中 —— playback-time 1.0 → 3.52（差 2.52）
        pause=False vid=1」——**这两条都不看界面文字，问的是外壳窗口与内核自己**。
        那条 ENV 是像素腿（整屏 3840×2160 亮度 0，锁屏中），按规矩记环境不记产品。
        「已经开着实例再双击第二个文件」那条只有真两进程才踩得到，`tests/e2e_instance_handoff.py`
        四趟独立复跑同读数（02:13／02:36／02:44／02:48，各 PASS 16／FAIL 0／ENV 0／NOTE 4，
        反向对照各 FAIL 1 条成立；留档 `e2e-handoff-1017-02{1358,3658,4421,4802}.log`）。

    (b) **GPU：不是"设了没生效"，是有四把尺各自咬合**（`tests/e2e_gpu.py`，
        留档 `e2e-gpu-1017-014247.log`＝PASS 21／FAIL 0／ENV 0、55.8s，
        `e2e-gpu-1017-014416.log`＝反向对照成立）。三档装配串行的意义全在那对牙上：
        ①默认档 `hwdec-current='d3d11va'`、`current-gpu-context='d3d11'`、`current-vo='gpu'`，
        4K60 推进率 **0.999**、掉帧增量 **0**，NVDEC 峰值 **18.0**；
        ②`hwdec=no` ⇒ `hwdec-current='no'`、NVDEC **0.0**、内核 CPU **191.7%**（①同法是 **12.5%**）；
        ③`hwdec=bogus-value` ⇒ 内核收到的是 `options/hwdec=['auto-safe']`（白名单拦下并回退）。
        ②③就是①的牙——判据在②也报"硬解在跑"的话，①的绿一钱不值。
        "跑在哪块卡"两条独立来源同名：内核日志 `Device Name` 与 `nvidia-smi` 首卡
        都是 `NVIDIA GeForce RTX 3070 Ti Laptop GPU`。这两趟本轮**接进常跑链**（链 19→21→23 趟），
        02:50:27 那一趟全链的账：`绿 22／真失败 0／环境不给 1／本轮不作数 0 —— 趟数由脚本自己数`
        （那一趟的趟数与最新一趟不同，所以这里**不带"共 N 趟"的尾巴**——文档里凡是带这个尾巴的
        句子，对账尺都拿它比最新哨兵；旧轮读数只许以"绿／真失败／环境／不作数"四个数出现）
        （`tests/out/chain-r17-done.keep-1017-025027.flag`，`generated_at=2026-10-17 02:50:27`，
        md5 `cc0d0b37129ea935a01bc8b9505e8daa`；这份是**带 `.keep-` 的不可覆写留底**，因为链下次
        跑就会把同名哨兵另抄一份——引用必须钉在不会变的那本上）。唯一那格 `rc=2` 还是
        `端到端：按注册表里那条命令原样执行（正跑）` 的像素腿（屏黑）。链里 GPU 那两趟在
        02:48:33／02:49:30 各跑一趟，**判据计数**与前面那趟逐字相同（`e2e-gpu-1017-024833.log`
        ＝PASS 21／FAIL 0／ENV 0、55.4s，`e2e-gpu-1017-024930.log`＝反向对照成立），
        而窗口内的 CPU／推进率两码本来就随负载漂：那一趟读到的是硬解内核 **10.9%**、软解 **195.1%**、
        推进率 **1.001**（①那条判据要的是"软解显著高于硬解"，不是某个固定值——README 的 GPU 表钉的就是
        `e2e-gpu-1017-024833.log` 这一份留档，换一趟这两个数会漂，所以**不挪引用**）。
        **引擎缓存表动过之后整链复跑过一趟**（09:42:22～09:50:43，见 (i)）：
        `绿 23／真失败 0／环境不给 0／本轮不作数 0 —— 趟数由脚本自己数`——
        哨兵 `tests/out/chain-r18-done.keep-1017-095043.flag`（md5 `acf4c4779b21a1d88eaf0f921a57dcbb`）、
        逐腿原文 `tests/out/chain-r18.keep-1017-095043.log`（md5 `5ff874b84cf084e44e661515ed497eaa`）。
        02:50 那趟唯一空着的像素腿这次屏亮着，真跑了（第 575 行 `端到端：全绿 —— PASS 16／FAIL 0／ENV 0`）；
        GPU 两腿是第 694 行 PASS 21／FAIL 0／ENV 0（55.7s）与第 732 行反向对照成立；
        播放列表那本是第 163／183 行（判据表 23 条全绿／变异表 12 种全被抓）；
        「没往用户真目录写东西」那条在第 572 行，读数 `config=951f3e3e watch=98737e5a 留痕 2 行 缓存 0 格，
        与开跑前逐字相同`（复跑后又核了一遍，与 (e) 抄的那串逐字相同）。
        顺带把 §3 第 14 条那个"没做的档位"钉死：本机内核 1274 行选项里 `--ngu-*`／`ancedx4`
        **0 命中**，所以"AI 超分"要么换构建要么外挂 shader，两者都要真出帧才量得掉帧——
        这条现在只有"内核里没有"是实测，"能不能实时"没有读数，因此**不做**（§7）。

    (c) **一把数报告的尺怎么瞎的（红灯先问判据）**：上一趟链把「启动器验收（正跑）」报成真失败，
        理由是该判据数出来"报告缺两行"。查下来产品没坏、`故障自检.bat` 17 项一条不少——
        是数行的那条正则只长得像 `[ OK ]`（内侧有空格），对 `[WARN]`/`[FAIL]` 失明，
        于是把"本机有两条提醒"读成"报告缺两行"。修法不是放宽阈值也不是摘掉那格：
        标记改成**从产品取**（`app.doctor.LEVEL_MARK` 全表＋新增 `UNKNOWN_MARK="[????]"`），
        另立一条"`[????]` 不许出现在报告里"的哨兵（doctor 将来加档忘配标记就会落到这），
        再给这把尺配 **7 格已知答案自检**（`tests/test_launchers.py count_rows_selftest`）——
        基线 17／少印一行→16／空输出→0／**改一行成 `[WARN]` 仍 17**／改成 `[FAIL]` 仍 17／
        每条挂续行仍 17／标记写成未登记档→17 行含 1 格未登记，外加一条 NOP 闸门（注入必须与基线不同）。
        本轮重跑：数行尺自检 7 格全对、证伪 18 条 CAUGHT 18／MISSED 0／NOP 0、
        启动器验收正跑「共 76 项，失败 0 项」，那一趟 rc=0（留底 `tests/out/chain-r17.keep-1017-025027.log`
        第 419／447／455／467 行——`chain-r17.log` 这个同名文件每次开链都会被另抄再重写，
        所以引用一律钉在 `.keep-` 那份上，md5 `ef31d33b93bb84ea6c756ad8506b8d1f`）。
        同一趟还抓到一处手抄值：`app/main.py` 的头里停在"25 条／10 条"，实际 30／13——
        已改成"条数由脚本 `[FINAL]` 自打"。

    (d) **修掉一条数据层的真缺陷，并且把它当初为什么说错**：同一部片会因为写法不同在 `recent`
        里并排占两格（`F:/美国队长3…` 与 `F:\美国队长3…`），Windows 下大小写也不同（`gelu-sample-1` /
        `GELU-SAMPLE-1`）。`Config.recent_key()` 用 `normcase(normpath(...))` 只给**列表去重**做键，
        **不动 watch-state 的键**——改那边的键形状等于把用户已经存下的进度读不到。
        5 条判据（两种写法塌一条／大小写塌一条／落盘再读回仍一条／另一部片不许塌／空串不许清表）
        ＋1 条变异 L7「去重键改回原样比就会留下两条（旧写法 2／现写法 1，两个数都得对上）」，
        本轮起这两趟也在链里。
        **本轮收尾时（03:15～03:20）自查出一句当初说过头的话**：这条被我写成"用户能看见的真缺陷"，
        但全仓 grep 的实测是——`recent` **只有一处写**（`app/engine/player.py:616` 调 `push_recent`），
        **UI 里没有任何一处读它**（普查留档 `tests/out/recent-consumers-1017-0322.txt`：
        `grep -rn recent app/ui/` 现数 **0 行**）。侧栏那页用的是 `cfg.watch_state(path)` 逐条取进度
        （同一份留档第 19 行：`app/ui/library_panel.py:329`），与 `recent` 不是同一本账；
        界面上的「最近加入」排序原话是第 21 行那句 `SORTS = (("mtime", "最近加入"), ...)`——那是
        **文件修改时间**。
        所以那两条重复**此刻在屏幕上看不见**，它是数据层的脏＋一本没人读的账；去重判据照旧要（那页
        做出来的第一天就不会并排两格），但"用户能看见"这半句撤回。顺带把这条**功能缺口**记实在 §7
        旁边：对标 PotPlayer/迅雷影音的"最近播放"列表页**本机没有**——数据攒了、界面没接，
        这一格不许因为"config 里有条 recent"就冒充已有功能。
        2026-10-17 03:20 盘上现数仍是两条并排（`F:\美国队长3…` 与 `F:/美国队长3…`），
        因为自愈要等那部片再被打开一次（`push_recent` 在写的时候塌）——本轮**不动用户数据**，
        留底同 (e)：`tests/out/user-config.json.pre-1017-022241`。

    (e) **最难看的一笔：测试自己的账**。**本轮发现 `tests/e2e_openwith.py` 没隔离 `LUHUI_DATA`**：
        它按双击形状起真实例，于是把 `gelu-sample-1.mp4`／`gelu-sample-3.mkv`／
        `Temp\gelu-arg-probe\…Test.mkv` 写进了用户真正的 `recent`、`watch-state.json`、
        缩略图缓存与 `launch.log`，并且在用户真配置（`volume=200`、`mute=False`）上放了带 aac
        音轨的样本——凌晨放出声。处置：先带时间戳留底（`tests/out/user-config.json.pre-1017-022241`、
        `user-watch-state.json.pre-1017-022241`）再清盘上污染（recent 5→2、watch 3→1、删 3 格缓存、
        日志保留）。修法不是一句"以后注意"，是三条机器判据（§3 第 15 条）：隔离数据目录＋
        从**被测实例自己**读回 `mute`/`volume`（实测行「mute=True volume=0.0；隔离目录里预置的是
        mute=true＋volume=0，**真配置那边是 volume=200**——读回来的必须是前者」）＋
        用户真目录四样指纹开跑前后逐字相同（实测行「config=951f3e3e watch=98737e5a 留痕 2 行 缓存 0 格，
        与开跑前逐字相同」），另加一条"启动留痕必须写到隔离目录那一本"。反向对照那趟
        （`e2e-openwith-red-r17fix.log`，FAIL 2 条／ENV 1 条）证明这三条不是恒绿。
        同一条纪律回头核了另外两趟起实例的账：`e2e_instance_handoff.py` 与 `e2e_gpu.py`
        本来就隔离＋静音回读（把手都取自 `player_harness`），事故只在这一本上。
        尾巴收口：把 handoff 那趟里 8 个重复实现的把手收拢到 `player_harness` 一份，
        重构前后三趟读数逐条相同（见 (a)）——**判据一条没动，才敢说是重构而不是改写**。

    (f) **屏幕不给像素时这一轮的账长什么样（不许拿它冒充做完，也不许拿它冒充产品坏）**：
        本轮新加的 UI 判据（播放列表「添加」＝追加／拖到窗口＝整表替换／空列表添加必须播起来＋
        还原前提）全在 `test_ui.py` 的 `main()` 里，而它第 4.5 节有一道 `capture_health()` 闸门：
        整屏抠不出像素时当场 `return 3`，**根本走不到那四条**。此刻本机实测就是那个形状
        （`tests/out/screen-probe-r17.txt`：整屏 3840×2160、亮度采样只有一种、最亮 0）。
        所以尾巴挂的是看门狗 `tests/watch_ui_r17.py`：它要求"屏出像素 **且** 链锁已收"两件事同时成立
        才起趟（02:29～02:30 那两行实测就是这把锁生效的形状——链在跑，它继续等），三趟串行
        （`--falsify`→验收→再验收，日志 `tests/out/{falsify,accept}-ui-r17.log` 与
        `accept-ui-r17b.log`），哨兵 `tests/out/watch-ui-r17-done.flag`，8 小时上限、
        到点仍不给像素就记"跳过"而不是判产品坏。链于 02:50:27 拆锁后它照设计只剩屏这一条闸门：
        02:52:03／02:53:03／02:54:03／02:55:03 四行读数逐字相同
        （`整屏 3840×2160 亮度0（最低0/最高0）颜色1`，pid 36164 从 02:15:01 起一直在轮询）。
        **这两格现在都有读数了**（08:41～09:41 七趟，全部在盘上）：证伪台账自打
        `共 85 行 —— CAUGHT 85／MISSED 0／ENV 0／非布尔 0`，四趟同形状（`falsify-ui-r17.log`、
        `falsify-ui-r17-0912.log`、`falsify-ui-r17b-0925.log`，加上摘掉 `current-vo` **之后**重跑的
        `falsify-ui-r18-0941.log`）；真窗口验收 `accept-ui-r17-0904.log`／`accept-ui-r17b-0910.log`／
        `accept-ui-r17c-0925.log`／`accept-ui-r17d-0925.log` 四趟各 **170/170 通过、无失败、零判废**。
        08:41／08:44 那两趟是**修根因之前**的现场，各红 4 条（汇总行点名：「播放列表点『添加』＝加长」
        「对照：拖到窗口＝整表替换」「还原到本段开始时的状态」「真扫描把样片目录收进表」）——
        前三条就是 (i) 那条缓存缺陷的真红，第四条是判据自己手抄了样片个数（已改成按目录现数，
        `tests/test_ui.py:3308` 那句标题写着"期望按目录现数，不抄标题"）。
        为 `current-vo` 新增那条真机判据之后台账多了一行（总条数由验收那趟的汇总行自打，这里不抄）
        （`tests/test_ui.py` 的 6.10：换过文件之后
        媒体信息那句「输出 …」还在不在，三段式：现状／`_props.pop` 注入／`_apply` 交回）。
        09:35 与 09:39 两趟**零真红**（`accept-ui-r18-0935.log` 汇总 `157/157`＋14 条判废、
        `accept-ui-r18b-0939.log` 汇总 `163/163`＋9 条判废），6.10 那两条都是绿的，读数
        `现状 vo='gpu' 信息句含输出=True；注入后 vo=None 信息句='软件解码'；交回后 vo='gpu' 含输出=True`；
        判废的是按键与像素那几段——外来窗口（`Kimi Code.exe`／`Kimi.exe`／`wpspdf.exe`／隔壁项目的
        `salt_player.py`，四个 pid 都在 ENV 行里点了名）压在最上面，按 (f) 开头的口径记 ENV 不记 FAIL。
        **那一格在 10-17 上午就补上了，本轮又复跑了一趟**：看门狗自己起的那两趟
        （`accept-ui-r18-watch-1-0954.log`／`accept-ui-r18-watch-2-1114.log`）各自汇总
        `171/171 通过；无失败`、`[ENV]` 0 行、6.10 那条绿；2026-10-19 改完回执层之后再复跑一趟
        （`accept-ui-r19-2212.log`，同样 **171/171、零判废**，配套的证伪本是
        `falsify-ui-r19-2213.log`）。起趟的三道闸门还挂在 `tests/watch_ui_r18.py`（pid 见
        `tests/out/watch-ui-r18.pid`，哨兵 `tests/out/watch-ui-r18-done.flag`）：三道闸门缺一不开跑——
        链锁已收、`test_ui.capture_health` 那把尺说屏在出像素、`GetLastInputInfo` 说这台机器至少空闲
        180 秒（**这一条是为用户加的**：一趟验收要把自己窗口抬到前台四五分钟，人在打字时不该被打断；
        它不参与任何判决）。判废（rc=3）就歇 15 分钟再试、每趟最多 8 试；真红（rc=1）立刻停并把
        点名写进哨兵，**绝不重跑磨成绿**。哨兵里的汇总与判废数由脚本从各趟日志**现读**，文档不抄它。
        **8 小时这一维本轮另加了一位接力看门狗**（`tests/watch_ui_r17_relay.py`，pid 38072，
        03:14:27 起）：上一位 10:07 到点时若屏幕仍不给，它按设计写"三趟全跳过"并退 3——
        尾巴就落回用户手里了，而"解锁后请你跑这三条命令"这句话不许再出现。接力件只做三件事：
        等上一位收工→读它的哨兵→只要哨兵里还有"跳过"（或哨兵根本不存在＝没起来/跑崩）且盘上
        确实没有活的看门狗，就原样再起一位（同 8 小时上限、同三趟、同判据），最多 3 跳、本位总上限 40 小时。
        它**不起真窗口、不碰 `test_ui`、不碰链锁**——那些还是上一位的活；哨兵 `watch-ui-r17-relay.flag`
        与日志 `watch-ui-r17-relay.log` 都是它自己的，不覆盖上一位的现场（追加）。
        判决函数带 **6 格已知答案＋3 条变异**（留档 `tests/out/relay-selftest.txt`：6 格全对、
        CAUGHT 3／MISSED 0；调用形状回执 `tests/out/relay-selftest-shapes.txt`——03:36～03:37 又现跑了一遍，
        三形状仍是同一颗 md5 `1ba62e8f…`、rc 全 0，所以这句不是当初那一次的快照），其中一格已知答案
        就是这条纪律本身：**哨兵 rc=1（真失败）→ 收**，不许用"再跑一遍"把产品红洗白。
        写它的过程中踩到两桩，都当场修了：① 自己的 `except BaseException` 把 `sys.exit(0)` 兜住，
        于是绿色 selftest 报出 rc=2（**正常退出不是"自身异常"**，加 `except SystemExit: raise`）；
        ② 第一版按命令行子串筛进程，把三个 bash 包装器（我的命令行里带着这个文件名）数成了
        "3 位上游看门狗"，等于永远不敢接力——现在要求镜像名是 python(w).exe 且命令行含
        `watch_ui_r17.py` 本体（接力件自己叫 `…_relay.py`，天然不含那个串），
        并按真执行验了一遍防叠加闸门：上游活着时直接调 `spawn_upstream()` 返回
        `None` 并留一行"盘上已经有 1 位上游了，本位不叠加"（留档 `tests/out/relay-guard.txt`）。
        **03:36 现数进程表时还看到本机另一位不相干的看门狗**：隔壁项目 salt-player 的
        `tools/mini_unlock_watcher.py`（pid 18696，`--budget 43200 --chain-with-device --groups window,material`）
        与它自己的 `salt_player.py`（pid 12632）。按规矩**不动它**（不按镜像名杀进程，也不是我们家的东西）。
        这里记它只有一个理由：屏幕一亮，两位"等解锁"的会同时开工，那一趟的真窗口与抓屏可能互相挡。
        `test_ui` 本来就把这两种情况分开——"挡路的窗口全属别的过程"记 **ENV** 不记 FAIL
        （`tests/test_ui.py:79`、`:109-121`，U57 两条腿钉着：外来挡路记 ENV、自家挡路照常 FAIL）。
        所以补跑若真记了 ENV，出处就是这一行，不用事后猜。

    (g) **本轮收尾时现数注册表，发现"双击归谁"这一格又不在我们这儿了**（02:56～02:58 三条只读查询，
        不改任何东西）：`FileExts\.mkv\UserChoice` 与 `.mp4/.avi/.mov` 一样**根本不存在**
        （`reg query` 回"系统找不到指定的注册表项或值"，把整个 `FileExts` 扫一遍也没有任何一个
        `.m*`/`.a*` 带着 `ProgId`）；`HKCU\Software\Classes\.mkv` 的**类默认值**写着 `xmp.mkv`，
        同一层还留着 `stellarplayer_bak`、`FormatPlayer.bak=xmp.mkv`、`xmpbak=FormatPlayer.mkv`
        三串"上一个占有者"的备份值——这就是各家播放器互相抢扩展名的现场；而 `HKCR\xmp.mkv\shell\open\command`
        的原话是 `"C:\Program Files\Xmp\Xmp.exe" "%1" /sopenfrom WndExplr ...`（迅雷影音）。
        我们那条 `LuHuiPlayer.Media` **仍在** `.mkv\OpenWithProgids` 名单里（与链里 48/48 那本账一致），
        `shell\open\command` 也仍是带 `%1` 的真启动器那一行。doctor 现数就报这个形状：
        `[WARN] 默认程序现状 双击现在进的不是本播放器：.mkv → 媒体文件(.mkv)〔xmp.mkv〕、
        .mp4 → 媒体文件(.mp4)〔xmp.mp4〕`（`tests/out/chain-r17.keep-1017-025027.log` 第 94 行）。
        **这条对本轮交账的影响要说尽**：(a)～(e) 修的是"文件进了我们这一侧以后会不会丢"，那一半有实测；
        而"双击到底进不进我们"取决于这一格，它**不在我们手里**——按 §7 的规矩不写类默认值、
        也不硬写有哈希保护的 `UserChoice`，所以只能说"请在打开方式面板里勾『始终』"
        （Windows 的解析顺序里 `UserChoice` 排在类默认值之前，这条本机没法实测：这台机器此刻
        根本没有那一格）。同理，`launch.log` 里 02:12 那两行是本测试的样本与反向对照哨兵
        （`argv=1 kept=1` 与 `kept=0 dropped=1` 那条丢的正是 `C:\no-such-dir\no-such-video.mp4`），
        **不是用户自己的双击记录**——用户那一次双击发生在 `launch.log` 这本账建立之前，没留下现场。

    (h) **交账前把三份文档回盘上数一遍**（一次性量具 `_r17_doc_check.py`，不进常跑链；留档
        `tests/out/r17-doc-consistency-run.txt` 与 `r17-doc-selftest-run.txt`，面向用户那份是
        `docs/交账-2026-10-17.md`——它自己也在被核的三份里）：判据是"本轮写进
        README/§6/交账的每个数字都必须能在盘上产物里找到"——doctor 项数与**项名逐名对账**（拿
        `app.doctor.CHECKS` 的名字列表比 README 那张表的第一列，不只数行）、链趟数拿哨兵自打的
        `[FINAL]` 比 `chain_r17.py` 里 `LEGS` 的源条数、GPU 段每个小数比
        `tests/out/e2e-gpu-1017-024833.log`、文档引用的每一颗留档 md5 比文件真实 md5（r17、r18
        **两对** keep 留档各算一条——留档被后来那趟覆写过就核不过）。
        **这一轮又添了三族尺**：①屏幕腿那几本日志——文档点名的每本 `accept-ui-*`／`falsify-ui-*`
        都必须在盘上，且文档抄的那串汇总读数必须与**它自己那一本**的汇总行逐字相同，证伪那句
        `共 85 行 —— CAUGHT 85／MISSED 0／ENV 0／非布尔 0` 必须与那本自打的收口行逐字相同
        （不拿 grep 数标签冒充：falsify 那几本里本来就有 1 行 `[FAIL]` 与 2 行 `[ENV]`，那是 U57
        对照组**要求**的形状，数标签会把对照组成产品红）；②6.10 那句三段式读数必须能在某本 accept
        日志里逐字找到；③**两边都会红的口径闸**——"含 6.10 且零判废的整趟"盘上有没有，文档必须
        跟着盘上走：盘上还没有 ⇒ design 要写明那一格没补上，交账不许写成已补齐；盘上已经有了 ⇒
        design 不许还写着"还差那一趟"，且两份文档各点名其中一本日志（那几本由量具**现数**，文档抄
        哪本都得在现数名单里）。两种撒谎都被同一族尺按住；`watch_ui_r18` 这个文件名两份文档都
        得留着——那一格补没补上，机制都在账上，否则"记 ENV 不记产品红"就成了把尾巴丢回用户的那句话。
        条数不进正文：`共 N 条判据`／`CAUGHT N` 只由脚本自打，交账抄一次就对一次账（对账条数比
        "本趟自打"、证伪条数比源码里 `len(MUTATIONS)`），design 这份连那两个数都不抄了；台账总条数
        也不抄（上一版这里写过一句"170→**171**"，那是任何一趟都还没印出来的数，已删）。坏样本逐条
        名字见 `_r17_doc_check.py` 的 `MUTATIONS`——本轮新添的几条专打上面这三族尺：把某串汇总读数改成一个
        盘上没有的比值、把某本日志改成不存在的名、把证伪那句的 CAUGHT 改小 1、把两份文档的"还缺"
        口径各自偷偷改成"已补齐"、撤掉接班看门狗的点名。
        量具自己的三种调用形状（接终端／`> 文件`／`2>&1`）
        **rc 全 0 且输出逐字节相同**——这句话的出处是 `tests/out/r17-doc-shapes.txt`
        （`python _r17_doc_check.py --shapes` 自打三行 md5 与两个布尔，回执在盘上就有牙），
        正文里从此**不再手抄那颗 md5**（抄进正文的 md5 每加一条判据就过期一次，是活的手抄值）。
        这条新尺子路上踩到一个**自锁死循环**，值得记：交账抄的"红 0"最初是拿**上一趟**留档比的，
        于是"文档说红 0 → 留档是上一趟的红 N → 又比不中"永不收敛（三形状回执同一形状的病：
        判据读的是自己这一族更早那一代写的文件）。修法不是放宽阈值，是**把自比的那条换成活值**——
        条数比"本趟自打"（先探一趟拿数、再正式判，两条判据自己也在账上，所以探针数＝正式数），
        证伪条数比源码里 `len(MUTATIONS)`，留档只核"在不在盘上＋有没有自打红"。
        三形状回执这一格到 2026-10-26 才真正治好。它有两处自比：①`--shapes` 的三个内层趟都是
        正常官方趟，会去读**上一份**三形状回执，坏一次（第一趟盘上没回执也算）就永远写不出绿的；
        ②语料把三本量具回执也收了进去，而正式趟会把 official 那本重写，后一个子进程读到前一个
        刚写的语料，字符数一变输出就不再逐字节相同。修法：内层趟带 `--shapes-inner`，跳过
        "三形状回执必须两半都为绿"这一格（判据条数不变），这一格由 `shapes()` 自己的三形状逐字节
        比对接班；正常趟照旧严格读盘，而且**两半都核**——只核"md5 全相同"会漏掉"三趟一起失败、
        输出当然一样"的假绿。语料只收产品/验收留档，三本量具回执一律不收。正常官方趟没有这个
        开关——把回执种成任意一半坏，它当场翻红；`--selftest` 里摆了三份临时回执的对照
        （md5 坏→红、rc 坏→红、两半都好→绿），防止这条判据被改瞎后恒红也被当成"抓到"。
        第一趟跑出**两条红，都是尺瞎不是文档错**：表段锚点写成 `### …故障自检`（README 里那段
        根本不是这个标题，于是数出 0 行）、`LEGS` 是 tuple 不是 list（正则只配 `[` 于是数出 −1）。
        按 (c) 那本账同一口径处理（**红灯先问判据**）：**改锚点、不放宽阈值**，并顺手把"只数行"升级成"逐名对账"——
        因为数行那把尺对本轮那次事故（`[WARN]`/`[FAIL]` 看不见）是同一个形状的病。
        **这趟扫描真正抓到的一条是文档错，不是盘上错**：(d) 那句"顺手修掉一条**用户能看见**的真缺陷"
        站不住——`app/ui/` 里读 `recent` 的 0 行（留档 `tests/out/recent-consumers-1017-0322.txt`），
        那两条重复只在数据层，屏幕上看不见；界面上的「最近加入」是文件 mtime。
        (d) 已按实测改写，并把"没有『最近播放』页"这条**功能缺口**如实记进 §7，
        同时给这句新话配了一条活尺（判据「app/ui/*.py 里读 recent 的行数 = 0」＋变异 M7 撤掉留档指向必须翻红）。
        同时补一条**出处**：超分那格原先只有"1274 行／0 命中"这句话，没有可复跑的留档；
        现在有了 `tests/out/mpv-list-options-1017-0306.txt`（70697 字节、md5
        `9951f00d1929e32229057167e1a2667b`；行数与命中数由同一份字节 `splitlines()`＋`count()` 现数），
        §7 那条"不做 AI 超分"的实测半边从此有出处，两份文档都指向它。

    (i) **"点添加反而把正在播那条挤没了"的根因在缓存层，而"其余格子无害"这句话当时是推测**
        ——这一条是本节里唯一一处**先修了一格、再把整张表量了一遍**的记录，两半都要落账。

        **根因**：`app/engine/player.py` 的 `_FILE_SCOPED` 是"换文件（`start-file`）时抹掉的订阅缓存
        名单"，抹掉指望着内核在新文件上再推一发把缓存填回来。`playlist-pos` 列在里面，于是
        `open_files(append=True)` 用来判"当前有没有条目"的那句 `prop("playlist-pos", -1)` 在换文件后
        读到 −1，追加退化成 `loadfile … replace`＝整表替换——用户实测的形状。判"该不该留在表里"
        的标准本轮写成**两件事**（不是一条"它跟着文件变吗"）：**换文件时真值会不会变** ＋
        **内核会不会再推那一发**；值不变＋不推 ⇒ 抹了就永远空着。真内核留档
        `tests/out/probe-globals-across-file.txt`（四格 WATCH：`loop-file`／`ab-loop-a`／`ab-loop-b`／
        `playlist-pos`；③ 换文件后 `get_property` 仍回 `inf`／1.0／2.0，④ 内核只推了 `playlist-pos`
        那一格，其余三格一条 property-change 都没推）。
        修前／修后成对留档：`tests/out/probe-playlist-pos-wipe.txt`（②`True`＝被抹、④剩下 `[]`、
        ⑤发的是 `replace`、⑥列表从 2 条变 1 条）与 `tests/out/probe-playlist-pos-wipe-postfix.txt`
        （②`False`、④剩下 `['playlist-pos']`、⑤`['loadfile', '…gelu-sample-3.mkv', 'append']`、⑥`False`）。
        判据：`tests/test_append_semantics.py` 的 **E18**（playlist-pos）＋**E19**（那三格）＋各自变异，
        真机侧 `tests/test_ui.py` 6.6～6.9。

        **量整张表**：E18／E19 那四格当初是**手挑的**——只证明了挑出来的四格，剩下十七格写"无害"
        是推测。于是补一趟普查 `tests/probe_file_scoped_table.py`：表内 21 格＋表外对照 4 格，
        **两条腿各换一次片**，逐格回三个问题（真值变没变／内核推没推／抹了之后有没有人读它）。
        两条腿的形状是被前三版伪结果教出来的：
        ①腿 A 用 `--no-audio` 的 4K 样片，`aid` 两趟都读不到 ⇒ 被误判"该摘"；②样片没章节，
        `chapter-list` 同样误判；③"两趟都读不到"的格因为占位串不同（`（读不到）` vs `读不到`）
        被比较式读成"值变了"＝**假安全**。修法不是放宽判据，是**加一条腿并换夹具**：腿 B 用现造的
        `tests/out/probe-fixture/chapters-2sub.mkv` → `chapters-2sub-b.mkv`（两支都带 3 章节／2 字幕，
        第二支还要有第二条音轨，否则换过去 `chapter` 变 `property unavailable`，只能记"判不了"），
        并且把"读不到"单列一家；`property not found`（这颗内核根本没这格）与 `property unavailable`
        （夹具造不出）**必须分家**——前者是死条目、后者是量具的边界，都不许写进"无害"。
        腿 B 全程静音，静音从**那个实例自己**回读（`mute` 是 True 且 `volume` 是 0.0 才算立住，
        立不住整腿不作数）。留档 `tests/out/probe-file-scoped-table-v4-092438.txt`（v1～v3 是被
        伪结果坑过的那三版，留在盘上当现场），第 48 行判决逐字：
        `该摘且有人读 1 ['current-vo']；该摘但没人读 0 （无）；查无此格 1 ['container-format']；判不了（夹具）0 （无）；留在表里有实测依据 19`，
        第 49 行的对账逐字：`分桶对账：1+0+1+0+19=21，表内格数=21 ⇒ 逐格都归了桶`。
        （上一版这里把这两行**缩写**成了四格，读起来更像结论，但对不上盘上那本——本轮已按逐字改回，
        并给这条订正配了判据：判决行里每个桶的「标签＋条数」都必须在文档里出现。）

        **量出来的第四格是 `current-vo`**（输出驱动是**进程级**的，两条腿都量到 `gpu-next`→`gpu-next`
        且换文件不推）⇒ 已从 `_FILE_SCOPED` 摘出（表内 21→20 格）。用户看得见的后果不是"统计页少一行"
        这么轻：`_media_info()`（按 `I` 那半句、`app/ui/main_window.py:1417-1438`）与统计页「输出驱动」
        那一行（`app/ui/stats_panel.py:242`、`:301-302`）在换过片子之后**变空**。判据两半各钉各的：
        离线 **E20**＋变异「把 `current-vo` 注回表里必须顶红 E20」（钉的是"表里不许再有它"），
        真机 **6.10**（钉的是"用户看得见的那半句"，中间隔着 `_media_info()` 一层，两边不互相代替）。
        `container-format` 记为**内核查无此格**（与 `app/ui/stats_panel.py:18` 既有实测记录一致）——
        死条目，抹不抹都是空操作，本轮不动产品，也不写进"无害"。

        **两处自己踩的坑，一起记**：①普查那版脚本一度是**就地覆盖** `tests/probe_globals_across_file.py`
        写进去的——那正好是被产品注释引用的 `probe-globals-across-file.txt` 的生成器，于是"已引用留档
        复现不出"（本机没有 git，覆盖即丢失）。处置：先把幸存的普查版 `cp -p` 留底（
        `tests/probe_globals_across_file.py.census-twin-1017-090931.py`，`cp -p` 之后当场核过 md5
        与那份普查版逐字节相同，证明留底没丢东西；它现在与同目录那份四格版**不同**，这是应该的），
        再把四格版**恢复原样**，重跑一份新档（`tests/out/probe-globals-across-file-rerun-091056.txt`）
        与已引用那份逐字比对（①–④除 pid/pipe 外一致），**不动已引用那本**。教训：改探针之前
        先查它是不是某本账的复现入口。②根目录那把一次性草稿 `_probe_pos.py` 已升成正式件
        `tests/probe_playlist_pos_wipe.py`（隔离 `LUHUI_DATA`、`QT_QPA_PLATFORM=offscreen`、
        走产品自己的 `_apply` 入缓存口、不手搓 `_props`），草稿删除。

        **这一段的停止规则**：`_FILE_SCOPED` 的每一格现在有"实测依据／查无此格"两种身份之一，
        没有第三种"没量过"。但**这不等于这张表永远对了**——它量的是这台内核（v0.41.0-1101-g5d85ba5fb）、
        这两个夹具、这两条腿；换内核构建或换样片，`container-format` 之外的格子仍可能翻面，
        复现入口就是 `tests/probe_file_scoped_table.py`（一次性的，不进常跑链；要重跑就重跑它，
        别改判据）。

38. **R19 这一轮的三笔账：内核到底收不收那条 `loadfile`、界面那句话到底谁在撑着、
    以及一把尺如何把"补完了"这一格从"还缺"翻过来**（2026-10-19 本机现跑现留档；
    约束在 §3 第 13～15 条，事故现场在 §6 第 37 条 (a)(c)）：

    (a) **凶形状是在真内核上量出来的，不是推演**。用户那条"双击之后只弹提示、什么都没播"最后
        归到 `loadfile` 的第 3、4 个参数上：mpv 要的是 `loadfile <url> <flags> <index> <options>`，
        `index` 必须是整数、`options` 必须是**裸字符串**。产品曾经发过 `[["start=…"]]` 这种形状，
        内核整条退回，原话固定是 `Command loadfile: argument index has incompatible type.`
        这一句现在是**两条腿钉住的**：①`tests/e2e_loadfile_receipt.py` 的 K5 阳性对照——拿旧凶形状
        直接发一次，读数 `抛出=IpcError: 命令失败(invalid parameter): ['loadfile', …, 'replace',
        ['start=12.800']]；日志里退回行=['[   2.878][e][ipc_0] Command loadfile: argument index
        has incompatible type.']`；②K4 负对照——产品正常路上 `退回行=（0 条）`。
        出厂那一头的形状是 `app/engine/player.py:862` 的 `cmd += [-1, f"start={pos:.3f}"]`，
        承重件指纹 `59f990c7c1870d6f5657950dd3d56909`（对账尺现算比对，改过产品没重抄指纹就红；
        2026-10-21 00:08 R21-d 动过同一份文件里的目录展开封顶，2026-10-26 R22 又加了章节跳转与
        异步握手，行号与指纹一并重抄——`start=` 这一句本身没动）。

    (b) **真内核那一腿的读数**（`tests/out/e2e-loadfile-receipt-1019-220025.log`，链里那趟的收口
        `PASS 11／FAIL 0／ENV 0`）：`K0` 起内核 pid 与耗时、`K1` 这一趟确实静音（读数从**被测实例
        自己**重读，不继承上一趟的"已经静音了"——真设备的静音闸门是进程内的，`app/engine/player.py`
        换实例就作废）、`K2` `duration=32.00s time-pos=1.08s`、`K3` 写入 `12.80s` 读到
        `time-pos=12.83s` 且 `direct 1→2 / queued 2`（两声各钉一头：`DirectConnection` 那声证明
        **内核发了**，默认队列那声证明**界面这条路走通了**）、`K3b` 与"没历史"那一趟差 `11.75s`
        ——这条是**鉴别力对照**：只有 K3 绿、K3b 也绿的话，这把尺是饱和的，读数没有意义；
        `K7` 用户真那份 `data\` 一个字节没动（`变化文件=（没有）`，全部写进临时 `LUHUI_DATA`）。
        量具自己配的牙：`tests/out/e2e-loadfile-drift-1019-215401.txt`——把 636 行注回**当年那个
        凶形状**（锚点在整份文件里唯一、注入形状≠出厂形状两道闸都过），K3／K3b／K4 三句同时翻红
        （装载计数 1→1、time-pos 仍停在基线 1.08s、日志里冒出内核原话），K2 照旧绿——正好是当年
        "不带进度看着正常、一带续播位置整条被退"的现场；按第三方留底（`/Temp` 里带时间戳的 `cp -p`
        副本）还原后复跑，md5 回到出厂那颗，`PASS 11／FAIL 0／ENV 0`。

    (c) **样片 D 与那条"片尾算看完"的规则**：续播那一档最早是红的——写进 `6.0` 读回 `0.0`。
        红灯先问判据：查下来产品没坏，坏在**输入不合法**——`Config.remember_position` 规定
        `pos > duration − 15` 或 `pos/duration > 0.92` 算看完（存 0.0），拿 10 秒的样片量续播，
        任何"看起来在中间"的位置都会被判成看完。处置不是放宽那条规则（它是产品语义），是**换输入**：
        `tests/gen_sample.py` 加样片 D（`gelu-sample-long.mp4`，32s），量具加 `MIN_DUR=25`／`TAIL=15`
        与合法点挑选（`min(0.4D, D−TAIL−0.5)`），挑不出合法点就记 ENV 并**当场点名这道算术**
        （"这一趟不作数"，不冒充通过，也不冒充产品坏）。

    (d) **量具自己踩的 Qt 生命周期坑**：第一趟真内核跑出来是 `[ipc] 回调异常: wrapped C/C++ object
        of type MpvPlayer has been deleted`——函数里建的那个局部 `QApplication` 先被 GC，
        无父的 `MpvPlayer` 就跟着失效。改成模块级 `QAPP` + `ensure_app()`，在建产品对象**之前**
        拿好，并在注释里写明"这条不是产品的红，是量具自己把宿主拆了"。

    (e) **界面那句话改由内核的证据撑着**（`tests/test_open_receipt.py`，链里收口 `PASS 15／FAIL 0／ENV 0`）：
        `MainWindow.open_receipt()` 是纯函数，`plays=True` 那一档的凭据只能是内核的 `file-loaded`，
        追加分支的凭据只能是内核推来的列表真的变长；两者都没到就**沉默**，20 秒（`OPEN_RECEIPT_MS`）
        到点才按"列表里有没有这一条"分诊成两句不同的话（命令被拒／收了但解不开），并且都点名
        原话在 `data\logs\mpv.log`。这一族的负样本是"已知坏的读数"逐档喂＋15 种坏写法证伪
        （其中一条把当年那句谎——`file-loaded` 没来也当场说"已载入"——注回去必须顶红）。
        条数一律由脚本 `[FINAL]` 自打，文档不抄。

    (f) **回执层改版动到了一把旧尺的期待，这一步是**红灯先问判据**走完的**：`tests/test_ui.py`
        的 6.8（空列表上点「添加」必须真播起来）复跑时红了一条——气泡读数是
        `已载入 1 个文件`，而判据要 `追加 1 个文件`。三问的答案：这条判据存在是为了抓"只入列表
        不动"（裸 `append` 在空列表上的形状）；那个前提还在，而且**承重的是 `pos`/`path` 那两行**
        （本次实测 `n=1 pos=0 path='gelu-sample-3.mkv'` 全对，缺陷形状照样红）；接班的是谁——
        文字那一半本来钉的是"回执层改版之前界面自己猜的那句话"，改版后它的真源在 `open_files`：
        列表空着时 `append_first = append and live >= 0` 为假 ⇒ 第一条发的是 `replace` ⇒
        `open_receipt` 只会说「载入」。所以订正判据、不订正产品，并把订正依据写进函数注释。
        订正之后**两条文字判据各配一条负样本**（空列表上说「追加…没打断当前播放」必须红——
        这一档没有当前条目可打断，那句是另一条分支的凭据读串了）。复跑：
        `accept-ui-r19-2212.log` 汇总 `171/171 通过；无失败`、`[ENV]` 0 行；
        `falsify-ui-r19-2213.log` 收口 `共 85 行 —— CAUGHT 85／MISSED 0／ENV 0／非布尔 0`。

    (g) **链 23→26 趟**（新增：打开回执正跑、打开回执·证伪、端到端真内核 loadfile 回执）。
        最新那趟哨兵原文 `绿 26／真失败 0／环境不给 0／本轮不作数 0 —— 趟数由脚本自己数，共 26 趟`
        （`generated_at=2026-10-19 22:02:25`，`tests/out/chain-r19-done.keep-1019-220225.flag`
        md5 `2321a638b03e3c1d97efbdcab27bef9e`，逐腿原文
        `tests/out/chain-r19.keep-1019-220225.log` md5 `6895079743322862e5755b5a8bfbadea`）。

    (h) **对账尺自己也被这一轮改了四处**（每处都配注入，不然"新判据"只是新写的句子）：
        ①`ROUNDS` 加 r19 那对 `.keep-` 留档；②旧口径那条从**手抄枚举**（`共 (19|21) 趟`）换成
        "文档里每一串 `共 N 趟` 都必须等于最新哨兵自打的那个 N"——枚举每加一轮就得回来添一个数，
        忘了添就是"旧读数没人核"，而历史句子改成不带这个尾巴（见 37(b)）；③链里那条像素腿的读数
        从常数改成**从最新链留档现取**（`PASS 16／FAIL 0／ENV 0` 这一串是现取的，判据要求两份文档
        写的就是它）；④新增两道闸：文档抄的每一串 `PASS x／FAIL y／ENV z` 必须能在最新链留档或
        文档自己点名的 `tests/out/…` 里逐字找到（语料由文档点名清单现取），以及 636 行那颗 md5
        回盘上现算。口径闸也从单态改成**两态**：那一格补上之前要求文档"写明没补上＋点名看门狗"，
        补上之后要求"不许还写着差那一趟＋两份文档各点名其中一本日志"（现数名单）。
        变异表同步：`共 N 趟`／汇总比值／`PASS/FAIL/ENV`／留档文件名／md5 这五类注入改成
        `@占位符`——写死字面量的那一版，文档一改口径就落在不存在的文本上、`replace` 空转，
        记成 NOP 而不是判据漏网（红的是变异表自己）；新增 M21～M24 四条各打这四族新尺。
        三形状（接管道／`> 文件`／`> 文件 2>&1`）复跑：`tests/out/r17-doc-shapes.txt` 自打
        `md5 全相同=True`。

    (i) **这一轮的三档与停止规则**。**已测**：上面 (a)～(h) 每一条都有本趟留档，凶形状与说句谎
        两种坏法都被注入抓过。**借来的**：无（本轮没引用别人机器上的数）。**没测、因此不做**：
        用户真机双击那一路的**最终归因**还差一个外部前提——`.mkv`／`.mp4` 的"默认程序"那一格
        当前指向迅雷影音（`xmp.mkv`／`xmp.mp4`，doctor 的「默认程序现状」项现数），按 §3 第 9、12 条
        的约束我们不代改 `UserChoice`，所以"双击到底走到我们这一头没有"只能由用户改默认之后
        再量；内核侧那条已经量到了。**停止规则**：只要 (a) 那颗 md5 与 (b) 那两腿没同时翻面，
        这一族尺就不再新增判据——要动的是重跑 `tests/e2e_loadfile_receipt.py`，不是改判据。

    (j) **收口 #96 时，量具自己翻出四处账（这三处是它查自己查出来的，不是别人指出的）**：

        1. **`dest` 写的名字与文档点名的名字不是一个源**。`main()` 把留档写成
           `r17-doc-consistency.txt`（少 `-run`），而判据与 README/design/交账读的是
           `r17-doc-consistency-run.txt`。后果是每一趟正式跑都在给一本 **10-17 的旧档**作保：
           旧档自打"共 40 条、红 0 条"，本趟实际 75 条，两本从没被比过——因为那条判据只搜
           子串"红 0 条"，命中就算绿。10-19 复跑时 `stat` 才发现那两本文件的 mtime 还停在
           10-17（新输出全落在另一个名字下）。修法：`dest` 直接取 `RECEIPT`，`RECEIPT` 是唯一
           真源（写、读、文档点名三处同一个字典）；判据也从"搜子串"改成**按数字读**
           对方那本自打的收口行（`CAUGHT/共 N 条变异` 与本趟 `len(MUTATIONS)` 逐个数对）。
        2. **`receipt_verdicts()` 是死代码**——它写着"每趟只许读对方那本"的三条逻辑，但全仓
           没有一处调用（`grep receipt_verdicts` 只剩定义）。10-19 把它的数核对并进 `judge()`
           之后删掉，定义处留了这段病因。口径：判据里有牙＝必须真的跑在链上，光写函数不算。
        3. **"绿要等上一趟绿"的死循环**。旧那条"两份量具留档在盘上且自打没红"里，正式趟会去
           读**自己正要写的那本**并要求它"红 0 条"：10-19 我改判据条数（74→75）时那趟只错一条，
           却把自己那本写成了"红 1 条"，于是下一趟永远起不来。修法：**自己那本只查存在**
           （本趟绿不绿由本趟 `[FINAL]` 行与 rc 担保），**对方那本才查内容**。
           对方趟的基线行（`[PASS]/[FAIL] 基线`）改成**只报不闸**——它判的就是本趟自己也在判的
           那些行，拿去当闸就是循环印证。
        4. **M24 那条 `[MISSED]` 的根因不在变异，在判据**。上一轮"样片名/内核原话"那两条是把
           `readme + design` 拼成一整坨比"命中 2 次"，于是往 design 里注入时 README 那份还在，
           总数照旧＝2，判据静默放行。10-19 把这两条拆成**逐文档**各判一份（`in readme` /
           `in design` 分别算），再补一条"样片 D 得真在盘上"（`tests/samples/gelu-sample-long.mp4`，
           续播那一档的输入）——文档说"量的是这支 32 秒的片子"就得有这支。
           因此判据总数 74→**75**（两条并三条），交账抄的那句话同步改了。

        复跑顺序与读数（都是**不弹窗口**的纯文件量具）：`python _r17_doc_check.py` →
        `共 75 条判据，红 0 条`、rc=0；`--selftest` → `CAUGHT 24／MISSED 0／NOP 0（共 24 条变异）`、
        rc=0 且基线行 `[PASS]`；`--shapes` → `tests/out/r17-doc-shapes.txt` 自打
        `md5 全相同=True／rc 全 0=True`。新加的那条"按数字读对方那本"**当场就红过一次**：
        当时盘上的证伪留档自打 `CAUGHT 20`（10-17 那一代，变异表已扩到 24），
        点名"对方＝r17-doc-selftest-run.txt：对不上（那本自打 CAUGHT 20…／本趟现算＝变异 24）"——
        这是**盘上真样本**给的负对照，比注入更硬；补跑 `--selftest` 换掉那本旧档后才转绿。
        两本旧名留档（少 `-run` 那两本）改名为 `*.WRONG-NAME.keep-1019-*.txt` 存档，
        不删（它们是这台机器上"名字不同源"的现场），也确认过三份文档没有一处指向这两个名字。

39. **退出收口这一族的四条不变式、一把把自己冻住的尺、以及"race 不能当阳性对照"**
    （2026-10-20 本机现跑现留档；守卫是 `tests/test_shutdown_quiet.py`，机制全文在
    `app/engine/poolkit.py` 开头）：

    (a) **四条件**（每家 store 用同一套，别再各写一份、各漏一处）：发射只该"送到就没"，
        绝不该把宿主带走（`emit` 宽到 `BaseException`、吞掉的计数并留原文）；在途壳由模块级
        名册钉住（`pin/unpin`，`run()` 结束才解寄）；**池子不归 store 所有**（`new_pool` 挂
        `QCoreApplication`，强引用在名册里——`QThreadPool(self)` 那一版是实测主线程死锁）；
        退出前收干净（`shutdown`＝立旗→清排队→杀登记过的 ffmpeg 孩子→**Python 侧 sleep 轮询**
        等收；这里绝不写长的 `waitForDone(ms)`，那是"攥着 GIL 等一个需要 GIL 的线程"，
        正是死锁的机制本体）。

    (b) **这把锁必须可重入，而且是被自己的量具量出来的**。04:42 给探针加一句 `stats()` 打印
        读数，D 组四趟各挂 120s；六份 faulthandler 栈同一条路径：`poolkit.py:304 in stats`
        → `poolkit.py:246 in pools`。`_LOCK` 原本是 `threading.Lock`，`stats()` 持着它调
        `pools()`（也要锁）⇒ 同线程二次 acquire 永久阻塞，而且是**持着锁**阻塞，
        `shutdown()` 的立旗/清排队/杀孩子全跟着排队。修法不是" caller 别嵌套"而是
        `_LOCK = threading.RLock()`（登记表里的函数互相调用是常态）。这一条的教训写进了量具：
        **窗口关不掉比崩更难查**——崩有 rc 与 crash.log，冻住只有用户干等。

    (c) **"在途退出 15 趟崩 8 趟"这条分布今天复现不了，所以它当不了判据的对照**。
        04:48 五组复跑（`tests/out/probe-exit-crash-postfix-0448.log`）：D（收口再退）8/8 干净、
        收口 0.03–0.05s、`killed=1`、吞 0；E（同形状摘掉收口）3/3 **也**干净，只退出要
        6.44/6.64/6.64s；F（只摘 pin）0/3；G（pin＋emit 整项摘回修前形状）0/6。
        ⇒ 判决只能落在"有效性未证"那一支，**不许写成 8/15 → 0/8**（那会把"这台机器此刻跑赢了
        race"说成"修好了"）。守卫改押两条确定性的：同一场面（工作线程里 `sip.delete(壳)` 之后
        发射）裸写每次 `rc=0xC0000409`（D2）、走 `poolkit.emit` 每次 rc=0 且真吞到 2 条（D3），
        两条只差那一层包裹；收口值不值看 E-vs-D 的退出耗时。历史分布只作为**现象记录**保留
        （crash.log 2026-10-19 22:56:34 那一栏的 `RuntimeError: wrapped C/C++ object of type
        _JobSignals has been deleted` 是真的，兜底分支二次抛也是真的）。

    (d) **量具自己的一条采样错**：D1/E3 第一趟红在 `killed=0／tracked=0`，看着像"产品没登记孩子"。
        实测是采样太早——工作线程从 submit 到 `poolkit.spawn` 之间有几毫秒，在那几毫秒里读
        `child_pids()` 就是空。改成"等到名册里有 pid **且那个 pid 真的活着**才动手"，
        两腿各自现跑 `tracked=1 alive=1`、`killed=1`、rc=0。**判据的前提必须由量具自己确认，
        否则环境/时序会把产品告红**（同 §6 第 30、31 条那两族的教训）。
        收口后的 pid 只查名册里那一个（`GetExitCodeProcess`/psutil），绝不按镜像名批处理进程。

    (e) 现数（由脚本自己打印，不手抄）：2026-10-20 05:13 `tests/test_shutdown_quiet.py`
        → 汇总行 `13/13` 通过、无失败、rc=0（`tests/out/test-shutdown-quiet-051304.log`）；
        `--falsify` → `39/39 抓到；漏网 0；变异失效(NOP) 0`、rc=0
        （`tests/out/test-shutdown-quiet-falsify-051325.log`）。缺 ffmpeg 或 4K 样片时
        D 族五条记 ENV 并点名，不记产品红、不静默跳过。

40. **全屏"四周黑边"这一族：三处让位、一把漏读右缘的尺、以及一条必须等前台才能签字的尾巴**
    （2026-10-20/21；用户原话「全屏之后周围显示不全」；产品侧 R20/R21-a/b/c，量具侧 R20 换尺）

    (a) **症状不是画面坏了，是舞台被三样东西各让走一块**。全屏时控制条仍占根布局的一行
        （实测浮层矩形 `(0, 1004, 1920, 76)`，即条高 76px），侧栏仍排在 body 的左列
        （收起前压掉 300px 宽），悬停唤醒又会把标题栏唤回来（标题栏在场时再压掉一条）。
        16:9 的片子放在被压矮/压窄的盒子里只能按另一条边适配，四周就空出纯黑——用户看到的
        "画面只在屏幕中间一块"是这一步的算术结果，不是解码或渲染坏了。这三笔在
        `tests/out/probe-fullscreen-fit-0335.log` 的同一条 [FINAL] 里各有对应项
        （`标题栏在场=False`／`条住浮层=True`／`侧栏=False(300px)`）。

    (b) **产品侧的做法**（同一个控制条实例搬来搬去，不造第二条）：
        - **R20 浮层化**：全屏时把 ControlsBar 搬进 `floaters.ChromeHost`——一个无边框、
          贴主人窗口下沿的顶层窗（`Qt.Tool | FramelessWindowHint | WindowDoesNotAcceptFocus`，
          `WA_ShowWithoutActivating`）。mpv 的画面是原生子窗口，永远画在 Qt 表面之上，
          留在布局里的条只能把舞台压矮；能盖在原生子窗口上面的只有另一个顶层窗口。退出全屏
          再把同一个实例搬回根布局（`take`/`release` 幂等，摘旧布局走显式 `removeWidget`，
          防止"根布局还留着这一行"）。
        - **R21-a 唤醒只唤条**：`_wake_ui()` 只做 `_set_bar_shown(True)`＋续上自动隐藏，
          **不碰标题栏**。鼠标晃一下把标题栏也带回来，等于每唤一次就再压矮一次舞台；
          验收里 U61 把"唤醒后标题栏仍在场"钉成红。
        - **R21-b 三条状态错位**：被系统强行退出全屏（`changeEvent` 里已不是 FullScreen）、
          最小化、以及全屏里用户显式开合侧栏——三种状态各自收口：前者把 `_fs` 归零并走一次
          `_apply_mode()`；最小化时 `host.veil()` 并停掉 30ms 轮询；侧栏用"进全屏前的快照"
          决定退出时还不还，全屏里显式开合则销掉快照（显式操作优先）。
        - **R21-c 几何尺补两把**：`chrome_of` 现在把"条占行（by_bar）"与"侧栏挤宽（by_sidebar）"
          都算进判决链，落定后的 B 只在"舞台已是整块屏、子窗口也铺满"时才算产品 B。

    (c) **量具侧：旧内缩尺每 240 列只取一个源像素，右缘那几列暗内容被整格漏掉**。
        2026-10-20 03:35 那趟（`tests/out/probe-fullscreen-fit-0335.log`）自打
        `档位=复现`、`落定后的 B=['二次+8.00s']`，读数是「右内缩 64px ⇒ 不规则留黑」；
        同一张 `tests/out/r20-re2-fs-8.00s.png` 用逐像素尺重读是 `(0, 274, 0, 274)`
        「上下留黑」，旧缩样尺是 `(0, 273, 64, 273)`「不规则留黑」。
        证据在 `tests/out/r20-re2-ruler-artifact-20261021.md`：右缘最后 12 列的过阈像素数
        `[3, 3, 3, 2, 2, 2, 2, 2, 2, 1, 1, 1]`、最大色差
        `[56, 54, 53, 52, 49, 48, 47, 47, 44, 42, 38, 34]`，最右列只有一个像素过阈（y=415、灰 34）；
        对照 re1 每列 152~157 个、最大色差 145~151。几何也对得上：片源 2.400:1，
        3840×2160 里内容高 1600、上下各 280；抓图四边各裁 6px ⇒ 274/274、宽 3828，
        3828/1600＝2.3925 是裁边之后的比值，不是片源变窄。
        两张现场图的留底（下一趟探针会覆写原文件名，判据读留底）：
        `r20-re1-fs-3.00s.keep-1021.png` md5 `68cb5190015c57ce811d96a890012242`、
        `r20-re2-fs-8.00s.keep-1021.png` md5 `7c425834127a0caa7e697d7f4d5545fc`。
        修法：`ink_bbox` 改逐像素（numpy 整图，约 0.05s/张），形状容差仍是 4 格＝64px
        （醒着的浮层图标 26px vs 真·B 几百 px）；旧缩样保留成 `_bbox_sampled`，只给证伪用。

    (d) **校准现数**（由脚本自己打印，不手抄；三本留档都在盘上）：
        - `tests/out/probe-fullscreen-fit-selfcheck-0024.log`：
          `[FINAL] R20 像素尺自检：共 10 张已知答案，错 0 张 ⇒ rc=0`、
          `[FINAL] R20 几何尺自检：共 13 档已知答案，错 0 档 ⇒ rc=0`；
        - `tests/out/probe-fullscreen-fit-mutate-0024.log`：
          `[FINAL] R20 自检证伪：共 12 条，判不到的（MISSED/NOP/CRASH）0 条 ⇒ rc=0`、
          `[FINAL] R20 几何尺证伪：共 9 条，判不到的 0 条 ⇒ rc=0`；
          M12 把旧缩样注回来只翻红第 ⑩ 张（右缘暗而稀），与"新尺在管事"一致。
        - `tests/out/probe-fullscreen-fit-recheck-0024.log`：
          `[FINAL] 复量：12 张，读不出 0 张 ⇒ rc=0`；12 张里 10 张正常相都是「上下留黑」，
          `r20-99-lever` 与 `r20-00-windowed` 是「居中偏小」（前者是故意压小的注入，
          后者窗口本来不是屏）。
        **03:35 那一趟整趟不作数**：它的 `[被测] 短=fb758e9a7f71` ≠ 当前这一版
        （`main_window.py`／`video_surface.py`／`player.py` 三份在那之后改过；当前指纹用
        `python tests/build_stamp.py` 现打，不抄在这里），所以它连"当前版本正常/异常"的旁证
        都当不了。另外它的 B 也不是产品缺陷：它不是 `[FAIL]`（探针自身 rc=0，B 只是它自打的
        一条坏读数），换尺后同一张图改判正常。别引用它的 `档位=复现`，也别把它的
        `条住浮层=True` 当作当前版本的验收读数。

    (e) **离线状态机已经跑过一趟（不弹窗）**：`tests/test_fs_chrome_offline.py` 在
        `QT_QPA_PLATFORM=offscreen` 下用同一份产品代码走完进出全屏、悬停唤醒、到点收起、
        侧栏还原、最小化/恢复、外力拽出全屏、拖动途中不收条、菜单开着不收条与退路档
        （`float_controls_in_fs=False`）。
        `tests/out/fs-chrome-offline-20261021-005307.log`：
        `[FINAL] 全屏 chrome 离线状态机：共 20 条判据，红 0 条 ⇒ rc=0`；
        `tests/out/fs-chrome-offline-falsify-20261021-005307.log`：
        `[FINAL] 状态机证伪：共 9 条坏实现，判不到的（MISSED/NOP）0 条，基线绿 ⇒ rc=0`
        （9 条坏法各打一种回归：唤醒带回标题、收起留空底板、退全屏忘收宿主、宿主半宽、
        退出不还原侧栏、不监听窗口态、唤醒不唤宿主、拖动途中到点照收、菜单开着到点照收；
        只改运行时方法，不碰盘上文件）。最后两条是 10-21 离线审查补上的：
        ①拖轨道拖到条外再按住不动，`bar.underMouse()` 已经为假、晃鼠标那一路又不响，
        2.6 秒到点就会把**带鼠标抓取的条**收走——Qt 不补发 `mouseReleaseEvent`，
        `sig_drag_state(False)` 再也不来，`_drag_scrub` 卡在 True（续播位置不再按间隔落盘、
        只剩关窗那一次，缩略图浮层留在画面上）；②条上那几本菜单是条的**孩子窗**，`exec()` 期间这只 2.6 秒的
        表照样响，到点收条会把菜单一起收掉（全屏里挑字幕/倍速根本不止 2.6 秒）。
        修法是 `_maybe_autohide` 在 `_drag_scrub` 与 `QApplication.activePopupWidget()`
        两种情形下只重挂不收；L16/L17 与 L18~L20 各自钉住"该收"与"不该收"两头，
        X8/X9 分别把两条守卫摘掉当场翻红。
        它**只签状态机**：不签 mpv 子窗口是否铺满、不签像素黑边、不签浮层的 z-order 与
        真窗口里的菜单弹层（只签"菜单开着时到点不收条"这一条），这些仍然只在真窗口里量。
        （真光标探针、全屏段正跑与证伪、`osd-dimensions` 旁证这三格在 (f) 里已经补上；
        状态机这一本仍只签状态机。）

    (f) **真机签字：2026-10-22 02:20–02:23 那一趟（当前这一版自己签字）**
        看门狗 `tests/watch_fullscreen_r20.py` 以分离进程起趟，四腿都第 1 试就绿；
        哨兵 `tests/out/watch-fullscreen-r20-done.flag` 的 `generated_at=2026-10-22 02:23:56`、
        整链 rc=0：
        - 探针两趟：`probe-fullscreen-fit-watch-1-t1-022007.log`（合成样片，34s）与
          `probe-fullscreen-fit-watch-2-t1-022041.log`（用户的 4K 原盘，55s），两趟都
          `档位=没复现 档位自洽=True`、拍次=9；最后一拍 `舞台=(1920, 1080)／屏=(1920, 1080)`、
          `标题栏在场=False`、`条住浮层=True`、`浮层矩形=(0, 1004, 1920, 76)`、`侧栏=False(300px)`、
          `悬停唤醒把标题栏唤回来=False`；落定的 6 拍舞台要么等于整块屏、要么只让出当场醒着的那条条
          （退路模式），`落定后几何仍不干净的拍次=[]`。两趟的起趟与跑完短指纹相同
          （`python tests/build_stamp.py` 现打，这里不抄）。
        - 内核旁证这一格补上了：4K 原盘全屏时
          `osd-dimensions={'w': 3840, 'h': 2160, 'mt': 0, 'mb': 0, 'ml': 0, 'mr': 0}`、
          `aspect=1.777778`，与像素尺的「舞台＝整块屏」互为独立证据；窗口相
          `w=2382 h=1256 ml=mr=75`，退出全屏后边距归零。
        - 真窗口验收 `accept-ui-r20-watch-t1-022137.log`（75s，rc=0）：`195/195 通过；无失败`；
          证伪 `falsify-ui-r20-watch-t1-022252.log`（64s，rc=0）：
          `共 93 行 —— CAUGHT 93／MISSED 0／ENV 0／非布尔 0`。
        - **第一趟签字（02:08）是红的，没有藏**：同一套判据在 93 行里 `MISSED 2`。两条都不是
          "读数不好看"，是两处真缺陷：① `VideoSurface.refit()` 的跳缓存只看容器几何——U60 把
          mpv 原生子窗口从外部缩成一半后容器没变，复原走产品 `refit()` 仍被跳过，子窗口留在
          864×628。修法是缓存 `_fit_child`，跳之前用 `win32.child_fills_client` 核对"子窗口仍
          正好盖住客户区"，对不上就重铺；U60 复原后矩形读到
          `容器 1728×1256，子窗口 1728×1256`、像素 `True/上下留黑`。② 侧栏需求判据的阈值比它
          自己宣称的窄：定义写「内容块最小宽＋滚动条」，断言却用 `_naive_need < _body_tw_min`
          （240<240 为假），把该翻红的那档放行了。改成 `< _body_tw_min + _bar_floor`
          （240+10=250），240 档当场 CAUGHT，×2 像素档仍是成对的那颗牙。
        - **看门狗自己的证据保存也修了一处**：旧版把重试日志名在循环外算、每试都用 `"w"` 重开，
          第 2 试会盖掉第 1 试的回执（01:57 那趟 rc=3、当时 194 条全过但环境没给证据的那本，
          就是被 02:06 的重试盖掉的）。改成 `-t{attempt}-HHMMSS` 每试一名；新链四本都是 `-t1-…`，
          02:08 那本红日志也还在盘上
          （留底 `watch-fullscreen-r20-done-2026-10-22-020850.flag`）。
        - **画面自己烤的黑边与产品缺陷必须分家**：这道 4K 原盘的内容是 2.40:1（ffmpeg 量到
          上下各 13%、左右 0 px），全屏像素尺读到「上下留黑」是**正确形状**，`FS_OK_SHAPES`
          也把单轴留黑算正常；它不是 R20 的 B。用户先前看到的「周围显示不全」是 chrome 让位
          （条占 76px、侧栏 300px、悬停再把标题栏带回来），现在舞台就是整块屏。要不要把烤进
          片子的黑边裁掉（`panscan`/裁切）是另一个选项，默认不裁——裁了会切掉画面上下 13%
          的内容并改变构图，等用户单独决定。
        - 停止规则：这一族现在四本账齐（产品三处让位／离线像素与几何尺校准／离屏状态机
          20 条＋9 条证伪／真机四腿在当前版本上全绿），不再重跑磨绿、也不再宣称更强。
          换 mpv 构建或换样片时直接复跑 `tests/probe_fullscreen_fit.py`，判据不动。

41. **R22 这一轮：把"重启就复位 / 只能干等 / 越用越吃内存"这类不报错的缺陷逐条钉住**
    这一轮没加新页面，做的都是"不崩、不报错，只是慢慢变得难用"的那一类：窗口位置记不住、
    历史崩溃永久报警、章节没有入口（键位表里空着）、悬停过的片把整图一直攥着、关掉再开
    音量回 100、内核握手时窗口冻住、880 宽下控制条右边被裁、帮助页是一张用空格对出来的长表、
    240 张卡每张各 stat 一次。九件各配一条可复跑的判据与"换成常见错误写法必须翻红"的证伪；
    下面每条读数都能在具名回执里找到，总数不在这里手抄（由各脚本自己的 `[FINAL]` 行打）。

    (a) **窗口几何记忆（`app/ui/win_geom.py` + `main_window` 接线）**。存档只存五个整数
        （x/y/w/h/max），规则全在纯函数 `plan()` 里，按下述顺序判：① 按**与存档矩形的交集面积**
        挑屏（主要在哪块屏就留在哪块屏）；完全不在任何屏（显示器被拔掉）→ 主屏正中；
        ② 尺寸先保产品下限 880×560，再夹进所选屏的可用区——屏比下限还小时**屏幕优先**，
        窗宽/最小宽一起收到屏幕宽，否则一开窗就有一截在屏幕外；③ 位置**整窗夹进可用区**
        （不再只要求露出 160×80），横向允许负坐标（多屏常用），纵向不许把标题栏推到屏幕上沿；
        没有存档时 `default_rect()` 按主屏可用区居中（1180×720，屏小则缩到屏），
        不再交给 Windows 的左上角级联。写盘只认"普通态"：
        `normalGeometry()`，全屏时取进全屏前的 `_geom_pre_fs`，最大化位单记一格；拖动/缩放
        **停手 1.2s** 才落盘（`_geom_save_timer` 单发），启动恢复失败只往 stderr 记一行、
        不许拖住启动；保存失败（目标是目录）不许穿出 `closeEvent`。
        读数：`tests/out/win-geom-r22a.log`；本轮规则更新后 **23/23**；证伪 **6/6**
        （X4 屏外不居中、X5 丢掉最大化位、X6 构造期不恢复都会当场翻红）。
        真机多屏只做了"合成屏列表"的逐格喂法，没拔过物理显示器——这一格算"按设计推断"。

    (b) **doctor 的崩溃记录按年龄分流**。旧版只要盘上有 `crash.log` 就永久报 WARN；用户 10-20
        已经实测到这条永久提醒，它会训练人忽略整份报告。现在 `_newest_source()` 只扫 `app/`
        下的 `.py`，`j_crash()` 比较 crash 与最新源码的 mtime：**早于** ⇒ OK，写明"这是历史记录"
        并附两个时刻与最后两行栈；不早于或判不了年龄才 WARN。本机现跑：`crash.log` 是
        2026-10-19 22:56:34，最新源码 2026-10-26 00:20:52（library_panel.py）⇒ 这一行是 `[ OK ]`，
        整份 17 项 **0 项阻断、1 项提醒、16 项正常**（`tests/out/doctor-r22b.log`；那一条提醒是
        「默认程序现状」——`.mkv` 仍归迅雷影音 `xmp.mkv`，按 §3 第 9、12 条不代改）。
        证伪 **20 组 CAUGHT 20／MISSED 0**（`doctor-falsify-r22b.log`），其中两条专打这一族：
        「默认程序现状：别人占着默认位不许报 OK，读不到只算没验过」与「上次外部打开的留痕：
        没有一行带参数时必须翻红」。

    (c) **章节跳转（快捷键 + 菜单 + 章节点提示）**。`chapter_target()` 是纯函数（首/末/无章节/
        无当前章/大步越界），`chapter_step()` 只读缓存里的 `chapter`/`chapter-list`，**不发同步请求**
        （UI 线程不许被内核往返拖住）；跳转用 `set_property chapter <夹住后的下标>`，不用
        mpv 默认键位的 `add chapter 1`——实测后者在末章会一路走到文件尾、`chapter` 随后不可用
        （`tests/out/chapter-cmd.log`）。连按时缓存还没翻面，`_chapter_expect` 用 1.5s 窗口
        从上一拍的目标继续走；菜单跳转是绝对落点，`set_chapter()` 用**原始缓存**重定基线，
        换片子时把期望清掉。`chapter_notice()` 把五种结局都说出来：没有章节、只有 1 章、
        已是第一章/最后一章（带总数）、第 N/总数（非 0 秒才带时间与标题）。菜单在没有内核时
        说的是「还没启动」而不是「没有章节」（M11 钉住这一条，别把上一次会话的残留章节表
        当成这支片子的）。
        读数：`tests/out/test_chapter.log` **13/13**；证伪 `test_chapter-falsify.log`
        **21 CAUGHT／0 MISSED**（含 M10 快速再按用旧缓存回退、M11 无内核话术、两条打桩生效对照）。
        没跑的：拿一支**真带章节**的片子走一遍真窗口——命令形状在真内核上量过，端到端的
        画面跳转这轮没有前台可用。

    (d) **缩略图内存两本账封顶（审计 M3）**。整图 sheet 以前"悬停过多少支片就攥多少张"
        （每张约 5.8MB，一两百 MB 且不会自己退，直到退出）；现在 `SHEET_CAP=3`（约 17MB 上限），
        且是 **LRU**：命中也挪到队尾，正在悬停那支不会被新抽完的挤走。失败负缓存
        `FAILED_CAP=4096`（坏文件再多也不许跟着库长），FIFO 挤出，挤出数记在 `failed_evictions`。
        读数：`tests/out/test_thumbs.log` **21/21**（新判据读 cap 区间 2..8、逐张取图验次序）；
        证伪 `test_thumbs-falsify.log` **11/11**：T8 把缓存改回无界、T9 改成"有上限但 FIFO"，
        各自被同一条 LRU 判据抓住，另两条断言函数对象真的换了（不是空打桩）。

    (e) **会话五键闭环**（音量/静音/倍速/循环/乱序）。关窗那一瞬从**内核真值**回写
        `MainWindow._remember_playback_state()`，`MpvPlayer.build_args()` 下次启动再带回内核，
        两端都用真数据验：内核回报 → cfg 内存 → 盘上 `config.json` → 新 `Config` 读回 →
        真正要递给内核的 argv。两条旧账都成了判据：`int(prop("volume") or 100)` 把**真值 0**
        当"没读到"（拖到底，重启又响）；`bool(prop("mute"))` 遇到字符串 `"no"/"false"` 得 True
        （见 `kernel_flag`）。循环要把 `loop-file`/`loop-playlist` 两格读成一个模式键。
        这段跑在 `closeEvent`（Qt 槽）里，抛一个异常不是 traceback 而是进程当场 abort，
        所以每个值都过类型收口，再拿假内核逐项喂。
        读数：`tests/out/session-state-r22c.log` **4/4**；证伪 `session-state-falsify-r22c.log`
        **13/13**（含"`bool()` 也必须让字符串样本翻红"、以及"记账函数自己恒真"那条自检）。
        真内核那一段由 `tests/test_engine.py` 管，这里只签回写/拼参的映射与类型。

    (f) **内核启动不再卡住 GUI（异步握手）**。旧路径 `connect(timeout=10)` 在 GUI 线程里最长堵
        10 秒：正常 0.42s 也会让窗口不重画（黑屏＋转圈），内核真坏时整 10 秒"未响应"。
        现在 `begin_start()` 只做 Popen（毫秒级）并设 deadline，`poll_start()` 每次做一次
        非阻塞 `CreateFileW` 尝试，回 `'ok'/'pending'/'fail'`；GUI 侧 40ms 一拍
        （`_poll_engine_start`），等待期间窗口照常呼吸。排队项保留 `(files, subs, replace)`，
        内核"启动中"与"坏了"外观一样，不能把这段请求丢掉；失败统一收口：队列作废、占位页回来、
        原因留在屏幕上看得到（`sig_log` 与 `empty.set_notice(..., sticky=True)`），关窗时
        `_cancel_pending_open()` 把队列清掉，别让下一拍去碰一扇没了的窗。
        读数：`tests/out/engine-async-real-r22b.log` **7/7**（**真起 runtime 内核**：正常握手、
        内核早退 0.03s 判 fail、管道不出现 0.70s 判 fail 并收掉进程）；
        证伪 `engine-async-real-falsify-r22b.log` **5/5**（M3 内核已死仍回 pending、M4 管道不来
        也不收尾，都被当场顶红）。

    (g) **窄窗控制条、关闭钮悬停、主按钮三态、统计页空态**。外壳承诺最小宽 880，而标准档
        18 颗按钮的和是 1033：只把 `minimumSizeHint` 改小没用——QLayout 会把原生最小宽写回
        父窗口，实测"请求 880、实际停在 1033"。现在 `minimumSizeHint()` 给 880 的松值，
        `resizeEvent` 在 <1040 进紧凑档（30px 按钮、音量 72、行距 0，18 颗全在，紧凑档实测
        布局最小 865），>1080 才回标准档（1050/1075 仍紧凑，拖动时不来回跳）。关闭钮悬停
        **换像素**：QSS 的 `color` 改不到 QIcon 里的像素，判据读图标最亮值（灰 X→白 X），
        最小化钮不许跟着变。`#Primary` 是 id 选择器，特异度压过 `QPushButton:pressed/:disabled`：
        不点名这两档时按下去和悬停一样、导出中的"开始"仍是亮主色底（`gif_dialog` 真会禁用）；
        现在常态主色、按下更深、禁用退中性底。统计页没片子时收起四段与预设行、只留空态说明，
        资源占用段仍在（它量的不是这支片）。
        读数：`tests/out/ui-polish-r22g.log` **8/8**；证伪 `ui-polish-falsify-r22g.log` **11/11**
        （M4 不让布局松掉 1033、S1 空态整段摘掉、S2 有片子还挂空态说明等）。
        这些像素是离屏平台真渲染出来读的；真桌面的鼠标路径这轮没跑（用户在用电脑）。

    (h) **快捷键一览页（F1）**。旧版是 `QPlainTextEdit` + `.ljust()`：比例字体下按字符数补空格，
        说明列逐行漂移，看上去像草稿。现在 65 项动作/72 条绑定按**动作名**分进 8 组
        （`GROUPS`；表外动作归"其他"），纯函数 `render()` 产出 HTML，两列表格由排版引擎摆列宽、
        每行同 x（用真 `QTextBrowser` 的 `cursorRect` 量，不 grep 源码）；搜索按"键名或说明"筛、
        表头写"筛出 N / 总数"，一条都不许丢；多键并一行；尖括号走唯一转义入口。主窗 `_show_help`
        已接到新页，旧的 `.ljust` 实现下线。
        读数：`tests/out/help-page-r22h.log` **14/14**；证伪 `help-page-falsify-r22h.log` **11/11**
        （F8 搜索被忽略、F9 搜不到不说"没有匹配"、F10 把同一动作的兄弟键筛掉）。
        字形只在离屏排版里验过对齐与不丢项，真窗口目检这轮没有前台。

    (i) **媒体库：一次 stat、扫描回包竞态、响应式卡片与占位块**。三件事各有一条承重判据：
        ① 整表重建共用**一份** `cfg.watch_snapshot()`，不再逐卡 `watch_state`（240 张卡就是 240 次
        stat）；② 扫描回包先按**墓碑**（`_gone`，用户刚移除的文件不许被旧结果带回来）与
        **当前根集合**（扫描途中移除的根，整棵子树都不许复活）过滤，再落盘；扫描途中来的
        refresh 不许丢——记成 pending，回包后补跑一趟（`followups` 记账），否则新加的目录
        要等用户第二次手动刷新；③ 卡片尺寸由 `card_metrics(视口宽)` 算（每格至少 150、单格
        封顶 260，300 侧栏一列 260、520 三列 160），60ms 防抖后套用，**视口宽变也要重排**
        （滚动条来去只改 `grid.viewport()` 的宽、不改面板宽，`resizeEvent` 不会来——
        `installEventFilter` 收 `QEvent.Resize` 再起一次排版定时器）。重建那一趟**不同步解码
        封面**：先铺文字与按 `(扩展名, 逻辑宽高, DPR)` 缓存的占位块，封面按 12 张一片在事件循环
        空隙里补（`sync_decodes` 记账，必须为 0）；两个空状态页（库空／搜空）各说各的下一步，
        无卡片时底注「双击卡片播放」收起。
        读数：`tests/out/library-r22i3.log` **64/64**、`library-falsify-r22i3.log` **14/14**
        （L7 去重键改回原样、回包整表照抄、回包后不补扫在途 refresh 都会翻红）；
        `tests/out/resp-r22i6.log` **24/24**、`resp-falsify-r22i6.log` **32/32**
        （卡片那条按**当前视口**再算一遍当参照；摘掉视口监听时 520 档停在 155、
        `现视口应得=160` 当场红）。
        性能读数（`tests/out/probe-placeholder-cost-r22i.log`，3000 项，offscreen）：
        `rebuild=27.8ms`（扫描回包/排序那一下的一次性成本，约 1.7 帧，不是每帧）、
        `refresh(首趟)=8.7ms`、`refresh(缓存命中)=8.4ms`，一帧预算 16.7ms；`sync_decodes=0`、
        `placeholders_made=2`（两种扩展名各一份缓存）。
        目检留档（`tests/probe_library_layout.py`，`WA_DontShowOnScreen` 不弹前台）：
        `tests/out/probe-library-layout-r22i.log` 读到 `300: cell=260 cols=1 icon=244x137
        viewport=268`、`520: cell=160 cols=3 icon=144x81 viewport=498`；四张 PNG
        `library-layout-300/520.png`、`library-empty-empty/nomatch.png` 已逐张看过
        （520 是三列、占位块带播放记号与扩展名、底注可见；两个空态标题/提示/按钮各不相同）。

    (j) **这一轮的三档与停止规则**。**已测**＝上面九条各自的回执（全部是纯文件/离屏量具，
        不弹前台、锁屏也能跑）；其中 (f) 真起了 runtime 内核，(e)(c) 的 argv/命令形状来自
        真产品代码，其余是离屏真渲染或纯函数逐格。**按设计推断**＝真机多屏拔插（(a) 的
        物理那一半）、真桌面上的悬停/窄窗鼠标路径（(g)）、真窗口里的帮助页字形（(h)）——
        这三格都要前台，用户明说在用电脑，所以**没有签字**。**没做**＝`tests/test_ui.py` 整套真窗口
        验收与 `capture_sweep --screen` 这轮没有跑；它们签的是"键与眼睛此刻归谁"，与本轮
        这些离线判据不重叠，不拿它们的旧账冒充本轮的绿。停止规则：不再重跑磨绿；用户不用
        电脑时把上面九条原样复跑一遍即可，任何一条红先问判据是否坏了（三问：为何存在／前提
        还在吗／谁接班），不许为了变绿放宽阈值。换 mpv 构建或换样片时，复现入口分别是
        `tests/test_engine_async_real.py`、`tests/test_thumbs.py`、`tests/probe_library_layout.py`
        与 `tests/probe_placeholder_cost.py`，判据不动。

42. **R23/R24：拖动两笔账——先修坐标尺，再换拖动机制；首开贴屏是"出厂摆位"那格错**
    用户实测两件事：① 标题栏/边框拖不动，播放画面反而能拖；② 双击打开就贴屏幕上侧，有一截在
    屏幕外。
    **R23（坐标尺）**：前者不是拖动逻辑写反，而是**命中测试的坐标系混了两把尺**：
    `WM_NCHITTEST` 给的是物理屏幕点，旧代码按 `GetDpiForWindow()/96`（本机 144/96=1.5）换成
    Qt 逻辑点；而 Qt 的默认高 DPI 取整把 1.5 收成 **2.0**（实测 `devicePixelRatioF()==2.0`，
    逻辑屏 1920×1080、物理 3840×2160）。于是标题栏右半边经 1.5 换算后落到窗口外、回
    HTCLIENT——而上一轮给"整页拖动"加的轮询路径仍在画面区生效，用户看到的就是"反了"。
    修法：命中测试与 `WM_GETMINMAXINFO` 一律改用 **Qt 的 `devicePixelRatioF()`**，与 Qt 自己
    摆窗口、量控件用同一把尺；同时**删掉**画面区/空状态页的拖窗轮询（`sig_drag_window`、
    `begin_window_drag`、`_EmptyPage` 的按下/移动/抬起），画面区回到纯播放交互（单击播放/暂停、
    双击切换、按住不移动窗口）。
    **R24（拖动机制）**：R23 之后标题栏命中已正确给 HTCAPTION、品牌字也能命中，用户却报
    "哪里都拖不动了"——**DefWindowProc 的隐式 HTCAPTION 拖动在 WS_POPUP 无边框 + WM_NCCALCSIZE
    客户区扩满这套样式下根本不启动**（根因：命中正确却拖不动）。想自己拦 `WM_NCLBUTTONDOWN`
    也走不通：Qt 的应用级原生过滤器收不到输入消息（`qwindowscontext.cpp` 的
    `!isInputMessage(msg)` 把 WM_NCLBUTTONDOWN 挡在应用级过滤器之外），而 PyQt5 没有
    `QWindow.installNativeEventFilter`（只有 eventFilter/installEventFilter），窗口级原生过滤
    装不上。最终修法：标题栏在 `WM_NCHITTEST` 里**回 HTCLIENT**，让 Qt 正常派发客户区鼠标
    事件；TitleBar 自己收 mousePress/Move/Release/DoubleClick——按下只记原点，移动越过
    `QApplication.startDragDistance()` 才调 `win32.begin_system_move()`（ReleaseCapture +
    PostMessage(WM_SYSCOMMAND, SC_DRAGMOVE=0xF012)，与 Qt `QWindow.startSystemMove` 同口径）
    进系统移动循环，贴边吸附/跨屏/最大化拽下还原仍由系统接管；双击调 `_toggle_max`。四边缩放
    照旧由命中码 HTLEFT/… 交 DefWindowProc。视频区是 mpv 的原生子窗口，点击到不了主窗 HWND，
    天然不拖。
    初始摆位：无存档时 `default_rect()` 按主屏可用区居中（1180×720，屏小先缩到屏），不再留
    Windows 的级联默认位；有存档时 `plan()` ①②③ 把尺寸与位置**整窗夹进可用区**（旧规则只保证
    露 160×80），屏比产品下限 880×560 还小时屏幕优先，最小尺寸与 `WM_GETMINMAXINFO` 的
    `ptMinTrackSize` 都读 live 的 `minimumSize()`，避免"摆进去又被下限弹出来"。
    判据（全部离屏/屏外，无前台窗口）：`tests/probe_nchittest_real.py` 在真实 DPI 下把主窗
    摆到 (-4000,160) 屏外（Windows 实际落在 -8011,275）、`WindowDoesNotAcceptFocus` +
    `WA_ShowWithoutActivating`，真发 `WM_NCHITTEST`：标题栏/品牌字/窗钮/视频区=HTCLIENT、
    左/顶边=HTLEFT/HTTOP；再直接喂合成鼠标事件走一遍 TitleBar 的处理器：微动 < 阈值不发起
    移动、拖 60px 恰好调一次 `begin_system_move(hwnd)`、双击调到 `_toggle_max`，**13/13**；
    `tests/probe_nchittest_hidden.py`（不 show、只建 HWND，同一套命中 + 拖动接线）**16/16**，
    其中多一条**投递链路**判据：把按下/拖动事件发给光标下真实子控件 DragRegion，事件上浮到
    TitleBar 并恰好调一次 `begin_system_move`（子控件不认鼠标事件，正式点击走的就是这条路）；
    `tests/test_win_geom.py` **23/23**、证伪 **6/6**；`tests/test_ui_polish.py` **8/8**、
    证伪 **11/11**；重打包后 `tests/probe_portable_smoke.py` 桌面包 **12/12**。按设计推断但
    未签字：真机用鼠标按住标题栏拖动、拖边框缩放的手感（要前台窗口，用户在用电脑，没跑）。

43. **R25：四轴复检——健壮性、日常、外观、性能各补一刀（判据先行，20/20）**
    这一轮先把 20 条判据写进 `tests/test_round25.py`（离屏、不启动 mpv、不占前台），逐条
    **看着它因正确的原因变红**（20/20 红）再改产品代码，最后 20/20 绿。改动按四轴归档：

    **健壮性（不许让一次异常把进程带走）**
    - `config._save_watch_state` 全程兜 `OSError`：只读目录／磁盘满时返回 False、stderr 留一句
      「观看进度保存失败…」，**内存里那条进度仍然可见**（本次会话接着续播），临时文件清掉，
      缓存照样写穿；排序键 `ts` 用 `float(... or 0)` 容错，不再被一条坏记录拖垮整张表。
    - `recall_position` 对非 dict 条目、非数值 `pos` 一律归 0；坏 `watch-state.json` 自愈。
    - `MpvIpc` 新增 `_fail_pending`：`close()`、读线程退出、写线程出错三条路都把在途请求
      立刻用 `IpcError` 唤醒——不再让 UI 干等 3 秒超时；写线程死亡同时置 `_alive=False`。
    - 看门狗：内核**久跑 ≥60s** 后崩溃就把重启额度重置（看了一整晚偶发崩一次，不该从此不再
      自愈）；重启前先 `close()` 旧 IPC。关掉后**保留引用**直到 `start()` 覆盖：若新一轮 Popen
      就失败，后续命令拿到的是立刻 `IpcError`，而不是 `None.fire(...)` 的 AttributeError。
    - `_scan_log_for_fatal` 只 `seek(size-8192)` 读尾部（1MB 日志不必整份进内存）；`expand()`
      给目录遍历加 `MAX_WALK_DIRS=20000` 封顶并记 `last_expand_dirs`/`last_expand_truncated`
      ——文件数上限管不住"目录多、媒体少"的盘根；缩略图/封面的 prune 对坏 meta、文件中途消失
      都不许炸（缩略图 metadata 只在 dict 时采信，getmtime 失败按最旧处理）；`GifJob.run()`
      开头先看 `cancelled`，进程起来之前取消就不再起 ffmpeg。

    **日常使用**
    - 播放列表清空后标题回正为 `APP_TITLE`；外部拖入打不开的路径，即使播放中（空状态页已隐藏）
      也走 OSD 点名「打不开：…」，不再静默。
    - `set_fullscreen` 只在状态真要变时动窗口：已最大化的窗口退出全屏**回到最大化**，不再被
      `showNormal()` 打回普通态；重复点当前页签不再重放淡入（`changed` 才跑 `page_fade`）。
    - 空闲（没载片）只灰掉"对这一条片子动手"的入口：传输/逐帧/截图/倍速/音轨/字幕；音量、
      循环、乱序、视图开关仍可先设好。
    - 无参数第二次启动：一律转交给已有实例并叫回窗口，不再建第二扇窗。
    - 续播按**每条目**生效：只有第一条 `loadfile` 能用 `start=`，其余条目在 `file-loaded`
      回包里按 `watch-state` seek；`_resume_skip` 记住"第一条已经带位置"，不重复 seek 打断起播。

    **外观**
    - `TEXT_FAINT` 提到 `#8A9286`：在 BG/BG_RAISE/BG_HOVER/BG_ACTIVE/BG_DEEP 五档底的对比度
      为 6.07/5.91/5.25/4.61/5.72，全部过 AA；选中行里的次要文字不再踩线。
    - OSD 长文本按可用宽中间省略（封顶 560），宽度由字宽现算，不再画出一条 1430px 的横幅。
    - 换主色热刷新：`theme.on_accent` 钩子表（弱引用，控件销毁自动清账）；标题 logo、控制条
      勾选态图标、倍速胶囊、媒体库范围按钮当场换色，不用重启。
    - 按下档补齐：滑轨把手用 `PRIMARY_FILL_DOWN`（比悬停更实），窗钮/关闭钮/幽灵按钮各有
      `:pressed`；`DANGER_DOWN` 是关闭钮的按下色。
    - 图标按真实屏幕 DPR 渲染（1.0–4.0，两位小数）并**进缓存键**；100%/125%/150% 各拿各的
      密度，不会把一档位图缩到另一档上发虚。

    **性能**
    - 上面两条缓存/IO 直接减负：日志尾部 8KB、目录遍历封顶、prune 不再因一条坏 meta 重扫；
      主色刷新走图标缓存（只有颜色变的那几枚真重画）；页签重复点击不启动动画定时器。

    **证据**：`python tests/test_round25.py` → **20/20 PASS**，末行打印
    `[FINAL] R25 离线判据全绿（不冒充真机：未启动 mpv、未占前台）`。回归（全部离屏）：
    `test_ui_polish` 8/8、`test_responsiveness` 24/24、`test_instance` 30/30、
    `test_equalizer` 87/87、`test_session_state` 4/4、`test_append_semantics` 27/27、
    `test_fs_chrome_offline` 20/20、`test_engine` 50/50、`test_engine_async` 11/11、
    `test_library` 64/64、`test_chapter` 13/13、`test_thumbs` 21/21、`test_shutdown_quiet` 13/13、
    `test_subtitle_drop` 6/6、`test_keymap` 25/25、`test_keymap_dialog` 17/17、
    `test_help_page` 14/14、`test_usage` 56/56、`test_win_geom` 23/23、`test_launchers` 76/76、
    `test_open_receipt` 8/8、`test_gif` 16/16、`test_gif_wiring` 19/19、`test_gif_dialog` 16/16，
    全部 rc=0。`test_ui.py` 在离屏下按既有 ENV 门中止（整屏 0×0、抠不出像素，rc=3）——像素类
    判据不计入本轮，也不拿旧账冒充绿。按设计推断但未签字：真机上换主色的即时观感、125%/100%
    屏上的图标锐度、真实鼠标下的按下反馈（都要前台，用户在用电脑，没有跑）。

## 7. 明确不做（避免范围膨胀）

- 不装全局鼠标/键盘钩子（隐私与稳定性）；**生成器永远不写注册表**——`packaging/make_launchers.py` 只
  产出 `.reg` 模板，导入那一下是用户决定（§3 第 9、12 条）。本机现状可以当场核对，不必信这段话：
  「打开方式」那套四根键已于 **2026-10-16 经用户明确授权**由助手 `reg import` 导入（导前只生成、
  导后第一趟对账 `tests/out/openwith-check-after-r16-0222.log`，58 条值逐条一致），右键菜单那套
  **至今没导入**（`python tests/test_launchers.py` 的 NOTE 行照实报"HKCU 查不到"）。撤销件与注册件
  同目录：`packaging\reg\撤销打开方式注册.reg` 只摘我们自己写进去的那几处；
  **两套都不碰任何扩展名的默认打开方式**——既不动有哈希保护的 `UserChoice`（硬写既不可靠也不该做），
  也不写扩展名键的默认值；`reg import` 连带新建父键留下的 `''` 默认值属于"没写过"，
  这条豁免怎么夹住不放过真改动，见 §3 第 12 条与 §6 第 36 条；不改 hosts；不做在线片源抓取与 DRM 内容；
- 不做视频编辑/转码 UI（ffmpeg 在本外壳里的用途就三条：抽缩略图、探测时长/分辨率、截图工坊的
  GIF 与连拍导出；除此之外的裁剪、拼接、转码档位都不做）；不追求 Win7 兼容（DWM 亚克力需 Win10 1809+/Win11）。
- **不把"最近播放"当成已有功能，本轮也不补那一页**：数据侧一直在攒（`app/engine/player.py:616`
  调 `push_recent`，写入时按 `normcase(normpath(...))` 去重），但 2026-10-17 03:22 的普查留档
  `tests/out/recent-consumers-1017-0322.txt` 的实测是
  **`app/ui/` 里读 `recent` 的 0 行**——侧栏媒体库那页逐条取的是 `cfg.watch_state(path)`
  （`library_panel.py:329`，续播进度），排序里的「最近加入」是**文件修改时间**（同档第 21 行
  `SORTS = (("mtime", "最近加入"), ...)`）。
  所以这一格现在是"有账没页面"。补它得开一整个侧栏页（四处手抄名单 SURFACES／捕法／下标／widget_of
  要跟着动，`tests/probe_surface_cover.py` 那把对账尺要重新齐，UI 判据要屏幕出像素），
  本轮屏幕不给像素、尾巴还挂在两个看门狗上，硬塞进来只会交出一页没人验过的界面——**宁可如实说没做**。
  要做的话范围就三条：按打开时间倒序、同一部片只占一格（去重键已经就位）、点一下直接接着上次的位置播
  （`recall_position` 也已经就位），都不需要新机制。见 §6 第 37(d)。
- **不做"录制"按钮**（对标迅雷影音/PotPlayer 那一颗红点）：理由不是"内核没这功能"，而是本机实测
  出来的四条——运行中开录静默不挂录制器（盘上永远没有那个文件）、产物停录后还要等 1~2 秒才收尾
  且刚写完的内核放不了自己的产物、录着 seek 会写坏时间轴、内核不回报"是否在录/录了多久"
  （§3 第 10 条，数字与探针日志都在）。它真正的用武之地是网络流，而本外壳只放本地文件（§1）。
- **不做"AI 超分"档位**（对标迅雷影音那个 GPU 超分开关）：分两半说清楚，别混成一句"做不了"。
  **实测的那半**——本机内核 `runtime/mpv/mpv.exe --list-options` 共 1274 行，`--ngu-*` 与 `ancedx4`
  **0 命中**（mpv 自带的神经超分没编进这台构建；**这两个数有盘上出处**：整包原始输出留档
  `tests/out/mpv-list-options-1017-0306.txt`，70697 字节、md5 `9951f00d1929e32229057167e1a2667b`，
  行数与命中数是同一份字节 `splitlines()`＋`count()` 现数的，别信这句话，重跑那条命令自己数），而 `--scale`/`--cscale`/`--dscale` 的可选值里
  `ewa_lanczossharp`／`lanczos`／`mitchell`／`catmull_rom`／`jinc`／`haasnsoft` 全在
  （§3 第 14 条：设置页那三档 `quality_preset` 拼的就是它们，跑在 `vo=gpu` 上——**"GPU 侧高质量缩放"
  这一半本来就有，也已经实测在跑**）。**没读数的半**——真上超分要么换内核构建、要么外挂
  `--glsl-shaders` 的第三方 shader 包，而两者好不好用取决于**掉帧**，掉帧必须让窗口真出帧才量得到。
  换构建还会让 §3 第 5、7 条与 §6 那几本靠这台内核量出来的账全部作废重测。所以这里不做的原因是
  "拿不出验证过的档位"，不是"内核不支持这个功能名"；量具（`tests/e2e_gpu.py` 的推进率＋掉帧增量）
  已经就位，屏亮的窗口里补一趟探针就能把它变成有数字的决定（§6 第 37(b)）。

