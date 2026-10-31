"""离屏平台不会枚举 Windows 字体；需要时读取系统自带字体。"""
from pathlib import Path
import os
from PySide6.QtGui import QFontDatabase


def ensure_fonts() -> None:
    if QFontDatabase.families():
        return
    directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
        path = directory / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
