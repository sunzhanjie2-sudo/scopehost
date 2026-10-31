"""scopeapp — 通用示波器 PC 上位机核心包。

架构分层：
    transport  ->  传输层（VISA / TCP / 模拟器）
    scpi       ->  SCPI 引擎（查询/写入/二进制块解析）
    waveform   ->  波形数据解码（INT8/16/32、IEEE754、ASCII、TDS 头、WAVFrm）
    adapters   ->  厂商适配器（Tektronix / Rigol / Yokogawa / 自动识别注册表）
    simulator  ->  内置 TCP SCPI 模拟示波器（开发与自测用，无真机也能跑）
"""

__version__ = "0.6.4"
