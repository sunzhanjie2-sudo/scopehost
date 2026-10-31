"""厂商适配器包。

注意导入顺序：RigolDs1000eAdapter 必须先于 RigolAdapter 导入，
使 *IDN? 自动识别时 DS1000E 专用适配器优先于通用 Rigol 匹配。
"""
from .base import ScopeAdapter
from .registry import (
    adapters_registry,
    create_adapter,
    detect_adapter,
    list_adapters,
)
from .rigol_ds1000e import RigolDs1000eAdapter  # noqa: F401 —— 先注册
from .rigol import RigolAdapter
from .tektronix import TektronixAdapter
from .yokogawa_dlm3000 import YokogawaDlm3000Adapter

__all__ = [
    "ScopeAdapter",
    "RigolAdapter",
    "RigolDs1000eAdapter",
    "TektronixAdapter",
    "YokogawaDlm3000Adapter",
    "adapters_registry",
    "create_adapter",
    "detect_adapter",
    "list_adapters",
]
