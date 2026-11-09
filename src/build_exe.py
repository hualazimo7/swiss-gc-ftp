#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 SwissFTP 打成 Windows 单文件 EXE。

    python tools/build_exe.py                 # 用系统 Python 3.14 打包
    python tools/build_exe.py --no-clean     # 复用 PyInstaller 缓存，增量重建

产物：dist/SwissFTP.exe（onefile + windowed，双击无控制台黑窗）
依赖：pyinstaller 6.x、pillow（仅生成图标时）、系统 Python 自带 tkinter。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENTRY = HERE / "swiss_ftp.py"
ICON = HERE / "assets" / "swissftp.ico"
VERSION_FILE = HERE / "version_info.txt"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "swissftp"

VERSION_TXT = """\
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0),
    prodvers=(1, 0, 0, 0),
    mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'Swiss-GC 简中汉化项目'),
        StringStruct('FileDescription', 'SwissFTP - GameCube SD 卡文件传输工具'),
        StringStruct('FileVersion', '1.0.0.0'),
        StringStruct('InternalName', 'SwissFTP'),
        StringStruct('LegalCopyright', '基于 GPLv2 的 Swiss 生态工具'),
        StringStruct('OriginalFilename', 'SwissFTP.exe'),
        StringStruct('ProductName', 'SwissFTP'),
        StringStruct('ProductVersion', '1.0.0')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def preflight() -> None:
    if sys.platform != "win32":
        raise SystemExit("本脚本只能在 Windows 上打包 EXE")
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        raise SystemExit("当前 Python 没有 tkinter，请用 C:\\Python314\\python.exe：%s" % exc)
    import PyInstaller  # noqa: F401
    if sys.version_info < (3, 10):
        raise SystemExit("需要 Python 3.10+")
    if not ICON.exists():
        subprocess.run([sys.executable, str(HERE / "make_icon.py")], check=True)
    VERSION_FILE.write_text(VERSION_TXT, encoding="utf-8")


def build(clean: bool, out_name: str) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",                 # GUI 子系统：双击不弹黑窗
        "--name", out_name,
        "--icon", str(ICON),
        "--version-file", str(VERSION_FILE),
        "--specpath", str(HERE),
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--noupx",
        # 函数内 import 的本地模块，显式声明确保进包
        "--hidden-import", "mock_swiss",
        "--hidden-import", "swiss_client",
        "--hidden-import", "swiss_ftp_gateway",
        "--hidden-import", "ftplib",
        # 这些包体积大且运行时完全不用
        "--exclude-module", "PIL",
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
        str(ENTRY),
    ]
    if clean:
        cmd.insert(3, "--clean")   # 紧跟 "-m PyInstaller"
    print("构建命令：\n  " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(HERE), check=True)
    exe = DIST / (out_name + ".exe")
    if not exe.exists():
        raise SystemExit("构建失败：找不到 %s" % exe)
    return exe


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 SwissFTP 单文件 EXE")
    ap.add_argument("--no-clean", action="store_true", help="不清 PyInstaller 缓存")
    ap.add_argument("--name", default="SwissFTP", help="输出文件名（不含 .exe）")
    args = ap.parse_args()
    preflight()
    exe = build(clean=not args.no_clean, out_name=args.name)
    print("\n构建完成：%s  (%.2f MiB)" % (exe, exe.stat().st_size / 1048576), flush=True)
    print("验证：\"%s\" --version" % exe, flush=True)
    print("自检：\"%s\" --selftest --log selftest.log" % exe, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
