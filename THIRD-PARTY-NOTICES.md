# 第三方组件与许可

NetAnchor-Campus 本身以 **GPL-3.0-only** 发布，完整条款见同目录的 `LICENSE`。

安装目录里同时包含以下第三方组件。它们各自保留原有许可证，本项目的 GPL-3.0 不改变这些条款。

## 运行时组件

| 组件 | 版本 | 许可证 | 说明 |
|---|---|---|---|
| Python 运行时 | 3.14 | PSF-2.0 | 随包分发 `python314.dll`、标准库与扩展模块 |
| Tcl/Tk | 9.0 | Tcl/Tk License（BSD 风格） | 托盘设置窗口使用的 GUI 库 |
| PyInstaller bootloader | 6.22 | GPL-2.0-or-later，附分发特例 | 特例明确允许分发打包产物，不影响本项目自身的 GPL 授权 |
| pystray | 0.19.5 | **LGPL-3.0** | 托盘图标与菜单；见下方说明 |
| Pillow | 13.0 | MIT-CMU | 托盘图标绘制与 ICO 读写 |
| pywin32 | 312 | PSF | Windows 服务、注册表、命名管道等 API |
| ddddocr | 1.6.1 | MIT | 验证码识别；随包附带其模型文件 `common_old.onnx`，模型的使用条款以其项目为准 |
| ONNX Runtime | 1.31 | MIT | 推理引擎 |
| OpenCV（opencv-python） | 5.0 | Apache-2.0 | 图像处理依赖；已剔除 videoio 的 FFmpeg 解码器 |
| NumPy | 2.5 | BSD-3-Clause（含 0BSD / MIT / Zlib / CC0 片段） | 数值运算 |
| OpenSSL | 3.x | Apache-2.0 | Python `ssl` 模块依赖 |
| libffi | 8 | MIT | `ctypes` 依赖 |
| zlib / Expat / libtommath / bzip2 / liblzma | — | 各自的开源许可（zlib、MIT、Public Domain 等） | Python 内置模块依赖 |
| Microsoft VC++ 运行库 / UCRT | — | Microsoft 可再分发组件条款 | 随包分发，用于免装 VC 运行库 |

## 关于 pystray（LGPL-3.0）

LGPL-3.0 要求使用方能够替换该库。本项目按以下方式满足：

- pystray 以**独立的 Python 模块**形式存放在 `_internal/` 下，未被静态链接进 exe
- 使用者可以用自己构建的版本替换该目录下的文件
- 源码：<https://github.com/moses-palmer/pystray>

## 图标与美术素材（不在 GPL-3.0 范围内）

`_internal/netanchor/ui/assets/` 下的应用图标是《原神》角色（胡桃）的同人二创，由 AI 图像生成工具产出，仅用于本项目标识。

角色形象版权归米哈游（miHoYo）所有，本项目与之无从属或授权关系。**该图标不受 GPL-3.0 授权**，请勿单独提取使用。

## 源码与反馈

- 项目地址与完整源码：<https://github.com/AxiuCN/NetAnchor-Campus>
- 问题反馈：<https://github.com/AxiuCN/NetAnchor-Campus/issues>

GPL-3.0 要求分发目标代码时提供对应源码。上述公开仓库即为对应的源码获取位置。