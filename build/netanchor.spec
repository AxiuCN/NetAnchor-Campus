# -*- mode: python ; coding: utf-8 -*-
"""NetAnchor-Campus 打包：一个 onedir，四个入口共享 _internal。"""
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
ENTRY = os.path.join(ROOT, "build")

PYWIN_HIDDEN = [
    "win32timezone",
    "servicemanager",
    "win32service",
    "win32event",
    "win32api",
    "win32security",
    "win32process",
    "win32pipe",
    "win32file",
]

# OCR 依赖只在工作进程里需要；模型与 opencv 的 ffmpeg 解码器按需裁剪
ocr_datas, ocr_binaries, ocr_hidden = collect_all("ddddocr")
DROP_SUFFIX = ("common.onnx", "common_det.onnx", "opencv_videoio_ffmpeg500_64.dll")
ocr_datas = [item for item in ocr_datas if not item[0].endswith(DROP_SUFFIX)]
ocr_binaries = [item for item in ocr_binaries if "opencv_videoio_ffmpeg" not in item[0].lower()]

EXCLUDES = ["pytest", "tkinter.test"]


def make_analysis(script, datas=None, binaries=None, hidden=None):
    return Analysis(
        [script],
        pathex=[SRC],
        binaries=binaries or [],
        datas=datas or [],
        hiddenimports=(hidden or []) + PYWIN_HIDDEN,
        excludes=EXCLUDES,
        noarchive=False,
    )


a_service = make_analysis(os.path.join(ENTRY, "entry_service.py"))
a_tray = make_analysis(os.path.join(ENTRY, "entry_tray.py"))
a_ocr = make_analysis(
    os.path.join(ENTRY, "entry_ocr.py"),
    datas=ocr_datas,
    binaries=ocr_binaries,
    hidden=ocr_hidden,
)
a_cli = make_analysis(os.path.join(ENTRY, "entry_cli.py"))

# cv2 的 hook 会自行加入 ffmpeg 解码器；本项目只解码 gif/png
a_ocr.binaries = [item for item in a_ocr.binaries if "opencv_videoio_ffmpeg" not in item[0].lower()]
a_ocr.datas = [item for item in a_ocr.datas if "opencv_videoio_ffmpeg" not in item[0].lower()]

pyz_service = PYZ(a_service.pure)
pyz_tray = PYZ(a_tray.pure)
pyz_ocr = PYZ(a_ocr.pure)
pyz_cli = PYZ(a_cli.pure)

exe_service = EXE(
    pyz_service, a_service.scripts, [],
    exclude_binaries=True, name="NetAnchor.Service",
    console=True, disable_windowed_traceback=False,
)
exe_tray = EXE(
    pyz_tray, a_tray.scripts, [],
    exclude_binaries=True, name="NetAnchor.Tray",
    console=False, disable_windowed_traceback=False,
)
exe_ocr = EXE(
    pyz_ocr, a_ocr.scripts, [],
    exclude_binaries=True, name="NetAnchor.Ocr",
    console=True, disable_windowed_traceback=False,
)
exe_cli = EXE(
    pyz_cli, a_cli.scripts, [],
    exclude_binaries=True, name="NetAnchor.Cli",
    console=True, disable_windowed_traceback=False,
)

coll = COLLECT(
    exe_service, a_service.binaries, a_service.datas,
    exe_tray, a_tray.binaries, a_tray.datas,
    exe_ocr, a_ocr.binaries, a_ocr.datas,
    exe_cli, a_cli.binaries, a_cli.datas,
    strip=False, upx=False, name="NetAnchor-Campus",
)
