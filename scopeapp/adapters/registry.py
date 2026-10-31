"""适配器注册表：按 *IDN? 自动识别厂商，也支持手动指定。"""
from __future__ import annotations

from typing import Optional, Type

from ..model import ScopeIdentity
from ..scpi import ScpiEngine
from .base import ScopeAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: list[Type[ScopeAdapter]] = []

    def register(self, cls: Type[ScopeAdapter]) -> Type[ScopeAdapter]:
        self._adapters.append(cls)
        return cls

    def all(self) -> list[Type[ScopeAdapter]]:
        return list(self._adapters)

    def names(self) -> list[str]:
        return [a.name for a in self._adapters]

    def by_name(self, name: str) -> Optional[Type[ScopeAdapter]]:
        for a in self._adapters:
            if a.name.lower() == name.lower():
                return a
        return None

    def detect(self, idn: str) -> Optional[Type[ScopeAdapter]]:
        """按 *IDN? 字符串匹配适配器。"""
        for a in self._adapters:
            if a.idn_pattern and a.idn_pattern.search(idn):
                return a
        return None


adapters_registry = AdapterRegistry()


def create_adapter(engine: ScpiEngine, name: Optional[str] = None) -> ScopeAdapter:
    """创建适配器：优先按名字，其次按 *IDN? 自动识别，最后回退 Generic。"""
    idn = engine.identity.full if engine.identity else ""
    cls: Optional[Type[ScopeAdapter]] = None
    if name:
        cls = adapters_registry.by_name(name)
    if cls is None and idn:
        cls = adapters_registry.detect(idn)
    if cls is None:
        cls = GenericAdapter
    return cls(engine)


def detect_adapter(idn: str) -> Optional[Type[ScopeAdapter]]:
    return adapters_registry.detect(idn)


def list_adapters() -> list[str]:
    return adapters_registry.names()


class GenericAdapter(ScopeAdapter):
    """无厂商专属行为时的兜底：按 SCPI 通用命令集工作。"""

    name = "Generic SCPI"

    def run(self) -> None:
        self.engine.write("RUN")

    def stop(self) -> None:
        self.engine.write("STOP")

    def is_running(self) -> bool:
        return self.engine.ask_int("ACQuire:STATE?", 1) == 1

    def read_timebase(self):
        from ..model import TimebaseSettings

        return TimebaseSettings(
            scale=self.engine.ask_float("HORizontal:SCAle?", 1e-3),
            sample_rate=self.engine.ask_float("HORizontal:SAMPLERate?", 0.0),
            record_length=self.engine.ask_int("HORizontal:RECOrdlength?", 0),
        )

    def read_channel(self, number: int):
        from ..model import ChannelSettings, Coupling

        pre = f"CH{number}:"
        coup = self.engine.ask(f"{pre}COUPling?").strip().upper()
        return ChannelSettings(
            number=number,
            enabled=self.engine.ask_int(f"{pre}DISPlay?", 1) == 1,
            scale=self.engine.ask_float(f"{pre}SCAle?", 1.0),
            offset=self.engine.ask_float(f"{pre}OFFSet?", 0.0),
            coupling=Coupling(coup) if coup in {c.value for c in Coupling} else Coupling.DC,
        )

    def read_trigger(self):
        from ..model import TriggerSettings

        return TriggerSettings(
            source=self.engine.ask("TRIGger:A:EDGE:SOUrce?").strip() or "CH1",
            level=self.engine.ask_float("TRIGger:A:LEVel?", 0.0),
        )

    def set_timebase(self, scale: float) -> None:
        self.engine.write(f"HORizontal:SCAle {scale:g}")

    def set_channel_scale(self, number: int, scale: float) -> None:
        self.engine.write(f"CH{number}:SCAle {scale:g}")

    def set_channel_offset(self, number: int, offset: float) -> None:
        self.engine.write(f"CH{number}:OFFSet {offset:g}")

    def set_channel_enabled(self, number: int, enabled: bool) -> bool:
        self.engine.write(f"CH{number}:DISPlay {'ON' if enabled else 'OFF'}")
        self._enabled_cache[number] = enabled
        return True

    def set_trigger_level(self, level: float) -> None:
        self.engine.write(f"TRIGger:A:LEVel {level:g}")

    def set_trigger_source(self, source: str) -> None:
        self.engine.write(f"TRIGger:A:EDGE:SOUrce {source}")

    def fetch_waveform(self, channel: int):
        from ..model import Waveform
        from ..waveform import decode_ascii_response

        self.engine.write(f"DATA:SOUrce CH{channel}")
        text = self.engine.ask("CURVE?")
        tb = self.read_timebase()
        return decode_ascii_response(
            text, channel=f"CH{channel}", x_incr=tb.scale * 10 / 1000 or 1e-9
        )
