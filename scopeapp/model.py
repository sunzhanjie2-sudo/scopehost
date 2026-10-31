"""数据模型：波形、通道设置、时基、测量结果、设备身份。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np


class Coupling(str, Enum):
    DC = "DC"
    AC = "AC"
    GND = "GND"
    DC50 = "DC50"


class WaveformEncoding(str, Enum):
    """SCPI 波形编码，对应各厂商 DATA:ENCdg / :WAV:FORM 取值。"""

    INT8 = "INT8"          # 二进制有符号 8 位（RIBinary / BYTE）
    INT16 = "INT16"        # 二进制有符号 16 位（RPBinary / WORD）
    INT32 = "INT32"
    IEEE754 = "IEEE754"    # 浮点（浮点 32/64 位）
    ASCII = "ASCII"


@dataclass
class ScopeIdentity:
    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""

    @property
    def full(self) -> str:
        return f"{self.manufacturer} {self.model} (SN {self.serial}, FW {self.firmware})".strip()

    @classmethod
    def parse_idn(cls, idn: str) -> "ScopeIdentity":
        parts = [p.strip() for p in idn.split(",")]
        while len(parts) < 4:
            parts.append("")
        return cls(*parts[:4])


@dataclass
class ChannelSettings:
    """单通道设置。scale 单位 V/div，offset 单位 V。"""

    number: int = 1
    enabled: bool = True
    scale: float = 1.0          # V/div
    offset: float = 0.0         # V
    coupling: Coupling = Coupling.DC
    bandwidth: Optional[str] = None  # 如 "20MHz"
    offset_polarity: int = -1   # 显示位移的符号；Rigol 的 OFFSet 是 +1
    vertical_position: float = 0.0  # div，横河独立于 offset 的垂直显示位置

    @property
    def screen_center(self) -> float:
        """网格中心所对应的输入电压，保留设备 OFFSet 的读回值。"""
        return -self.offset_polarity * self.offset - self.vertical_position * self.scale


@dataclass
class TimebaseSettings:
    scale: float = 1e-3         # s/div
    position: float = 0.0       # s，触发参考点相对屏幕中心偏移
    sample_rate: float = 0.0    # Sa/s（可由适配器填充）
    record_length: int = 0      # 点数
    horizontal_divisions: int = 10  # DS1000E 屏幕为 12 格


@dataclass
class TriggerSettings:
    source: str = "CH1"
    level: float = 0.0          # V
    slope: str = "RISE"
    mode: str = "AUTO"


@dataclass
class Waveform:
    """解码后的波形：y 为电压（V），x 为时间（s）。"""

    channel: str = "CH1"
    y: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    x_start: float = 0.0        # 第一个采样点时刻（s）
    x_incr: float = 1e-9        # 采样间隔（s）
    points: int = 0
    raw: Optional[np.ndarray] = None          # 解码前的原始整数/浮点数据
    source_encoding: str = ""   # 原始编码（INT8 等）
    decode_note: str = ""       # 适配器/解码器附加说明
    device_record_length: int = 0  # 本次读取对应的设备真实记录长度；0 表示未知
    sample_stride: int = 0         # 1=每个原始采样点；0 表示未知
    full_record: Optional[bool] = None  # None=未知；False=预览/部分记录
    record_stable: Optional[bool] = None  # False=RUN 时读回，未确认各通道属于同次触发

    def __post_init__(self) -> None:
        self.points = int(self.y.size)

    @property
    def x(self) -> np.ndarray:
        return self.x_start + np.arange(self.points) * self.x_incr

    @property
    def duration(self) -> float:
        return self.points * self.x_incr

    @property
    def vpp(self) -> float:
        return float(np.ptp(self.y)) if self.points else 0.0

    @property
    def vmean(self) -> float:
        return float(np.mean(self.y)) if self.points else 0.0

    @property
    def vrms(self) -> float:
        return float(np.sqrt(np.mean(self.y**2))) if self.points else 0.0

    def to_csv(self, path: str, delimiter: str = ",") -> None:
        """导出 CSV：第一列为时间(s)，其余列为通道电压(V)。"""
        data = np.column_stack((self.x, self.y))
        header = f"Time_s,{self.channel}_V"
        np.savetxt(path, data, delimiter=delimiter, header=header, comments="")


@dataclass
class Measurement:
    name: str
    value: float
    unit: str = ""


@dataclass
class ScopeStatus:
    connected: bool = False
    identity: Optional[ScopeIdentity] = None
    message: str = ""
