"""示波器"设备风格屏幕"渲染：用设备真实数据重建屏幕画面（纯 numpy，无 Qt）。

用途：
  - 模拟器生成抓屏图像
  - 不支持远程抓屏的设备（如 DS1152E），用波形+设置重建一张与设备屏幕
    内容一致的 PNG —— 画面数据全部来自设备真实读数。
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

from .model import ChannelSettings, TimebaseSettings, Waveform

CH_COLORS = [
    (255, 213, 74),   # CH1 黄
    (77, 208, 225),   # CH2 青
    (240, 98, 146),   # CH3 品红
    (129, 199, 132),  # CH4 绿
    (186, 104, 200),  # CH5 紫
    (255, 138, 101),  # CH6 橙
]

BG = (16, 20, 24)
GRID = (30, 41, 51)
CENTER = (58, 74, 87)
REF = (36, 52, 64)
TEXT = (210, 220, 228)


def render_scope_screen(
    waveforms: dict[str, Waveform],
    channels: dict[str, ChannelSettings],
    timebase: TimebaseSettings,
    width: int = 800,
    height: int = 480,
) -> np.ndarray:
    """把波形与设置渲染成示波器风格屏幕 RGB 图像。

    布局：10 格水平 × 8 格垂直，顶部通道色标条，底部时基/采样率标注。
    """
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = BG
    grid = np.array(GRID, dtype=np.uint8)
    for x in np.linspace(0, width - 1, timebase.horizontal_divisions + 1).astype(int):
        img[:, x] = grid
    for y in range(0, height, height // 8):
        img[y, :] = grid
    img[height // 2, :] = CENTER
    img[:, width // 2] = REF

    # 通道波形
    for idx, (name, wf) in enumerate(waveforms.items()):
        if wf is None or wf.points < 2:
            continue
        ch = channels.get(name, ChannelSettings(number=idx + 1))
        if not ch.enabled:
            continue
        color = np.array(CH_COLORS[idx % len(CH_COLORS)], dtype=np.uint8)
        ppv = (height / 2) / (4.0 * ch.scale)          # 像素/伏
        span = timebase.horizontal_divisions * timebase.scale
        t = timebase.position - span / 2 + np.arange(width) * span / width
        v = np.interp(t, wf.x, wf.y)
        ys = np.rint(height / 2 - (v - ch.screen_center) * ppv).astype(int)
        ys = np.clip(ys, 2, height - 3)
        for x in range(width):
            y = ys[x]
            img[max(0, y - 1):y + 2, x] = color

    # 顶部通道色标 + 底部信息条
    for idx, name in enumerate(waveforms):
        ch = channels.get(name)
        if ch is None or not ch.enabled:
            continue
        x0 = 14 + idx * 64
        img[10:18, x0:x0 + 44] = np.array(CH_COLORS[idx % len(CH_COLORS)], dtype=np.uint8)
    img[-24:, :] = (10, 13, 16)
    img[-24:, :] = np.maximum(img[-24:, :], np.array([40, 52, 64], dtype=np.uint8)) // 2
    return img


def render_annotated_screen(
    waveforms: dict[str, Waveform],
    channels: dict[str, ChannelSettings],
    timebase: TimebaseSettings,
    width: int = 800,
    height: int = 480,
) -> np.ndarray:
    """渲染并叠加文字标注（时基/采样率/通道量程），用于设备风格截图。

    文字用 5×7 点阵近似（仅数字/字母/常用符号），保持纯 numpy 无依赖。
    """
    img = render_scope_screen(waveforms, channels, timebase, width, height)
    lines = [f"TIME {timebase.scale:.3g} s/div"]
    if timebase.sample_rate:
        lines.append(f"SR {timebase.sample_rate:.3g} Sa/s")
    for idx, (name, wf) in enumerate(waveforms.items()):
        ch = channels.get(name)
        if ch and ch.enabled:
            lines.append(f"{name} {ch.scale:.3g} V/div")
    y = 26
    for line in lines:
        _draw_text(img, line, 8, y, (170, 255, 170))
        y += 12
    return img


# 5x7 点阵字体（大写字母/数字/部分符号）
_FONT = {
    "0": "01110100011000110001100011000101110",
    "1": "00100011000010000100001000010001110",
    "2": "01110100010000100110010000100001111",
    "3": "0111010001000010110000011000101110",
    "4": "00010001100101011111100100001000010",
    "5": "11111100001111000001000001000101110",
    "6": "01110100011000011110100011000101110",
    "7": "11111000010001000100010000100001000",
    "8": "01110100011000101110100011000101110",
    "9": "01110100011000101111000011000101110",
    "A": "01110100011000111111100011000110001",
    "B": "11110100011000111110100011000111110",
    "C": "01110100011000010000100001000101110",
    "D": "11110100011000110001100011000111110",
    "E": "11111100001111011110100001000011111",
    "F": "11111100001111011110100001000010000",
    "G": "01110100011000010111100011000101111",
    "H": "10001100011000111111100011000110001",
    "I": "01110001000010000100001000010001110",
    "J": "00111000010000100001000010001001100",
    "K": "100011001010101110011010100110001",
    "L": "10000100001000010000100001000011111",
    "M": "10001110111011110101100011000110001",
    "N": "10001110011101101101011001110001",
    "O": "01110100011000110001100011000101110",
    "P": "11110100011000111110100001000010000",
    "Q": "01110100011000110001101011001001101",
    "R": "111101000110001111101010100110001",
    "S": "01111100000111100000100001000011110",
    "T": "11111001000010000100001000010000100",
    "U": "10001100011000110001100011000101110",
    "V": "10001100011000110001100010101000100",
    "W": "1000110001100011010111011101110001",
    "X": "10001100010101000100010101000110001",
    "Y": "10001100010101000100001000010000100",
    "Z": "11111000010001000100010001000011111",
    " ": "00000000000000000000000000000000000",
    ".": "00000000000000000000000000000011000",
    "/": "00001000010001000010001000010000100",
    "-": "00000000000000000000011111000000000",
    "+": "00000001000010011111001000010000000",
}


def _draw_text(img: np.ndarray, text: str, x0: int, y0: int, color) -> None:
    """在图像上绘制 5x7 点阵文字（大写化，未知字符跳过）。"""
    color = np.array(color, dtype=np.uint8)
    h, w, _ = img.shape
    cx = x0
    for ch_ in text.upper():
        pat = _FONT.get(ch_)
        if pat is None:
            cx += 3
            continue
        for i, bit in enumerate(pat):
            if bit == "1":
                px, py = cx + (i % 5), y0 + (i // 5)
                if 0 <= px < w and 0 <= py < h:
                    img[py, px] = color
        cx += 6


def png_bytes(img: np.ndarray) -> bytes:
    """RGB 数组 → PNG 字节（纯标准库）。"""
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def bmp_bytes(img: np.ndarray) -> bytes:
    """RGB 数组 → 24 位 BMP 字节（底部向上）。"""
    h, w, _ = img.shape
    row_size = (w * 3 + 3) & ~3
    data = bytearray()
    for y in range(h - 1, -1, -1):
        data += img[y].tobytes() + b"\x00" * (row_size - w * 3)
    file_size = 54 + len(data)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    return header + dib + bytes(data)
