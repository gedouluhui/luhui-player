# 第三方组件与许可

LuHui播放器 以 **GNU General Public License v3.0** 发布（见 `LICENSE`）。
下列组件由本项目使用或随便携版分发，其许可如下。

## 运行时 / 库

| 组件 | 版本（开发机） | 许可 | 说明 |
|---|---|---|---|
| PyQt5 | 5.15.10 | GPL v3（Riverbank Computing） | 外壳 UI。GPL 版本的 PyQt5 要求衍生作品同样以 GPL 发布，本项目因此选择 GPLv3。 |
| Qt 5 | 5.15.2 | LGPL v3 / GPL v3（The Qt Company） | PyQt5 依赖的 Qt 运行库。 |
| Python | 3.9+（开发机 3.12.3） | PSF License | 解释器。 |

## 播放内核（不随本仓库提交）

| 组件 | 版本（开发机） | 许可 | 获取方式 |
|---|---|---|---|
| mpv | v0.41.0-1101 | GPL v2+（官方构建） | https://mpv.io/installation/ |
| FFmpeg | N-127213 | LGPL v2.1+ / GPL v2+（取决于构建） | https://ffmpeg.org/download.html |

把 `mpv.exe` 放进 `runtime/mpv/`、`ffmpeg.exe` 放进 `runtime/ffmpeg/` 即可运行；
便携版由 `packaging/portable.spec` 打包，这两个 exe 不进入源码仓库。

## 字体（随仓库与便携版分发）

| 组件 | 许可 | 许可全文 |
|---|---|---|
| Noto Sans SC（Regular / Medium / Bold） | SIL Open Font License 1.1 | `packaging/assets/fonts/OFL.txt` |
| Noto Serif SC（Regular / Medium） | SIL Open Font License 1.1 | `packaging/assets/fonts/OFL.txt` |

## 商标

mpv、FFmpeg、Qt、Python、Noto 均为各自权利人的商标或注册商标。
本项目与上述项目没有隶属或背书关系。
