"""厂商适配器抽象基类。

适配器把「不同厂商示波器的 SCPI 方言」收敛成统一的上位机 API：
身份识别、通道设置、时基、触发、波形读取、运行控制。
新增示波器厂商 = 继承本类 + 注册到 registry。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

from ..model import (
    ChannelSettings,
    ScopeIdentity,
    TimebaseSettings,
    TriggerSettings,
    Waveform,
)
from ..scpi import ScpiEngine


class ScopeAdapter(ABC):
    """示波器厂商适配器基类。"""

    #: 适配器显示名
    name: str = "generic"
    #: 用于 *IDN? 自动识别的匹配模式（大小写不敏感），None 表示需手动选择
    idn_pattern: Optional[re.Pattern] = None
    #: 支持的模拟通道数
    channel_count: int = 4

    def __init__(self, engine: ScpiEngine):
        self.engine = engine
        #: 通道启用状态缓存（避免每轮查询，由 update_enabled_cache 刷新）
        self._enabled_cache: dict[int, bool] = {n: True for n in range(1, self.channel_count + 1)}
        #: 是否支持 :SYST:ERR? 错误查询（DS1000E 等老机型不支持，查询会挂起超时）
        self.error_query_supported = True

    # ---------- 身份 ----------
    @property
    def identity(self) -> Optional[ScopeIdentity]:
        return self.engine.identity

    def connect(self) -> ScopeIdentity:
        return self.engine.connect()

    def disconnect(self) -> None:
        self.engine.disconnect()

    @classmethod
    def matches(cls, idn: str) -> bool:
        return bool(cls.idn_pattern and cls.idn_pattern.search(idn))

    # ---------- 状态 ----------
    @abstractmethod
    def run(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_running(self) -> bool: ...

    def single(self) -> None:
        raise NotImplementedError("该适配器尚未实现 Single 触发")

    def force_trigger(self) -> None:
        raise NotImplementedError("该适配器尚未实现强制触发")

    def autoset(self) -> None:
        raise NotImplementedError("该适配器尚未实现自动设置")

    def set_trigger_mode(self, mode: str) -> None:
        raise NotImplementedError("该适配器尚未实现触发模式设置")

    def set_trigger_slope(self, slope: str) -> None:
        raise NotImplementedError("该适配器尚未实现触发边沿设置")

    def set_channel_coupling(self, number: int, coupling: str) -> None:
        raise NotImplementedError("该适配器尚未实现耦合设置")

    # ---------- 设置读取 ----------
    @abstractmethod
    def read_timebase(self) -> TimebaseSettings: ...

    @abstractmethod
    def read_channel(self, number: int) -> ChannelSettings: ...

    @abstractmethod
    def read_trigger(self) -> TriggerSettings: ...

    # ---------- 设置写入 ----------
    @abstractmethod
    def set_timebase(self, scale: float) -> None: ...

    @abstractmethod
    def set_channel_scale(self, number: int, scale: float) -> None: ...

    @abstractmethod
    def set_channel_offset(self, number: int, offset: float) -> None: ...

    @abstractmethod
    def set_channel_enabled(self, number: int, enabled: bool) -> bool:
        """切换通道显示，返回是否生效（True=生效，False=固件限制未生效）。"""

    @abstractmethod
    def set_trigger_level(self, level: float) -> None: ...

    @abstractmethod
    def set_trigger_source(self, source: str) -> None: ...

    # ---------- 波形 ----------
    @abstractmethod
    def fetch_waveform(self, channel: int) -> Waveform: ...

    def capture_screen(self) -> bytes:
        """抓取示波器**真实屏幕**图像（PNG/BMP 字节）。

        各厂商实现 SCPI 抓屏（SAVE:IMAGE / :DISPlay:DATA? 等）；
        默认不支持，抛 NotImplementedError，由 UI 回退到本地截图。
        """
        raise NotImplementedError("该设备不支持远程抓屏")

    def supports_capture(self) -> bool:
        return type(self).capture_screen is not ScopeAdapter.capture_screen

    def render_screen_snapshot(self) -> bytes:
        """用设备真实波形+设置重建一张"设备风格屏幕"PNG（所有设备可用）。

        对不支持远程回传屏幕图像的设备（如 DS1000E），这是能拿到的最忠实
        的"设备界面截图"——画面数据全部来自设备真实读数。
        """
        from ..screen_render import png_bytes, render_annotated_screen

        waveforms = self.fetch_all()
        channels = {
            f"CH{n}": self.read_channel(n) for n in range(1, self.channel_count + 1)
        }
        tb = self.read_timebase()
        img = render_annotated_screen(waveforms, channels, tb)
        return png_bytes(img)

    def fetch_all(self) -> dict[str, Waveform]:
        """读取所有已启用通道的波形（基于启用状态缓存，单通道失败自动跳过）。"""
        out: dict[str, Waveform] = {}
        for n in range(1, self.channel_count + 1):
            if not self._enabled_cache.get(n, True):
                continue
            try:
                out[f"CH{n}"] = self.fetch_waveform(n)
            except Exception:  # noqa: BLE001
                if self.error_query_supported:
                    self.engine.check_error()
        return out

    def update_enabled_cache(self, settings: dict[str, ChannelSettings]) -> None:
        """由设置慢刷新循环调用，同步通道启用状态。"""
        for name, ch in settings.items():
            m = re.match(r"CH(\d)", name)
            if m:
                self._enabled_cache[int(m.group(1))] = ch.enabled
