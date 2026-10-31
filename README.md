# ScopeHost — 通用示波器 PC 上位机

本仓库保存源码、构建脚本、使用说明和测试。安装包、实机波形/截图、测试输出、运行依赖目录及仪器手册原文件/提取文本保留在本地，不随源码上传；文中 `release/`、`artifacts/` 和仪器手册路径指本地文件。

## v0.6.4 · 采集状态由用户控制

连接、扫描、刷新、保存、断开及异常处理都不自动执行 RUN / STOP。运行、停止和 Single 只由用户在软件中点击，或在示波器面板上操作。修正 v0.6.3 每轮读取自动停止再恢复运行的行为，保护用户选定的采集记录。

现代 Tek 在 RUN 时通过单次 `WAVFrm?` 读回每通道的完整原始记录，不将不同触发的数据块拼接；各通道依次读取，不能保证来自同一次触发。手动 STOP 或 Single 完成后，按连续数据块读取完整停止记录并核验真实点数。读取结束、取消或断开都保持用户的采集状态。

保存完整 NPZ 前，请手动 STOP 或完成 Single，等待上位机收到完整停止记录。保存只使用已读回的原始数组，不自动停止、重新采集或恢复运行。每通道点数必须等于设备深度，采样间隔必须匹配设备采样率；ZOOM 继续使用真实原始样本。

本轮 MSO44 实测 CH1 为 500 万点、250 MSa/s：STOP 读取约 2.3 秒且保存回读逐点一致，用户手动 RUN 后读取约 1.5 秒并保持运行，两次日志均无采集状态写入。报告见 `artifacts/mso44_v0.6.4/manual_stop.json`、`readonly_run.json`；当前只读验收脚本为 `tests/validate_manual_acquisition.py`。Windows 分发版为 `release/ScopeHost-v0.6.4-Windows.zip`，详见 [MSO44 读取与通信说明](docs/mso44.md)。

## v0.6.3 · 完整设备记录与真实采样 ZOOM（历史版本）

以下记录描述历史版本；其中自动停止并恢复运行的行为已在 v0.6.4 移除。旧版实机验收脚本会控制采集状态，不应用于当前只读验收。

现代 Tektronix 4/5/6 系列默认读取完整记录。每通道使用 `DATA:RESAMPLE 1`，按最多 500,000 点的连续数据块读取，逐块核验点数、换算参数和时间轴；不抽点、不插值、不补点。读取时临时冻结采集，各启用通道来自同一份记录，结束后恢复原先连续运行状态；已经停止或完成 Single 的设备保持停止。界面显示已读取点数及总进度。尚未采集到记录的通道会明确提示先运行或执行 Single。

NPZ 保存严格核验每通道实际数组长度等于设备存储深度、采样间隔等于设备采样率倒数，以及全部开启通道均已读取。部分记录、抽点预览或缺少必要设备设置时拒绝保存完整归档，既有文件不会被失败保存覆盖。大型 NPZ 在后台压缩写入，保存期间可以继续操作界面；保存的是点击时保留的那一帧。

ZOOM 直接使用完整数组的局部真实采样索引。总览仅按像素保留极值绘图，原始数组不变；放大后重新从选区取得采样点，最小范围为两个采样间隔，倍率上限随实际点数增加。旧版 10k 预览 NPZ 中已经遗漏的采样点无法通过放大或升级恢复，需要重新采集。

本轮 MSO44 实机验收通过：500 万点、2 ms/div、250 MSa/s，四通道各完整读取 500 万点，NPZ 保存回读的全部原始码值和电压逐点一致；ZOOM 到 10 ns/div 显示 25 个真实样点，与独立读取的设备区间一致。Single、后台保存、传输中断开与重连均通过，设置已恢复且事件队列为空。四通道完整读取实测约 11 秒，帧后延迟另计。验证脚本为 `tests/validate_full_mso44.py`，报告为 `artifacts/mso44_v0.6.3/full_record_validation.json`。Windows 分发版为 `release/ScopeHost-v0.6.3-Windows.zip`；详情见 [MSO44 完整读取与通信说明](docs/mso44.md)。

## v0.6.2 · MSO44 实机调试（历史预览版本）

使用实际 USB 连接的 MSO44（C049800，固件 1.40.3.236）修正水平位置换算、原生截图临时文件流程和传输期间的正常断开。正常断开会完成当前单次请求、停止后续请求，保留驱动挂起时的取消保护；界面继续处理事件。时基和 V/div 的非预设值按设备准确读回显示，解决实际 4 ms/div 却显示成 5 ms/div 的问题。命令行读取先同步真实通道启用状态。

历史 v0.6.2 中，本台 MSO44 在 Manual、4 ms/div、250 MSa/s、1000 万点记录、四通道开启时，10k～100k 预览约 14 秒/帧；这是抽点预览的耗时，不代表 v0.6.3 完整记录的传输速度。验证脚本 `tests/validate_mso44.py` 会恢复测试前设置。`StartMSO44.cmd` 为这台设备的 USB 快捷入口。

当时实机通过扫描连接、四通道预览、Single、偏移输入、ZOOM/堆叠/游标/FFT、预览归档与原生截图；真实波形传输中正常断开约 2.86 秒，重连成功。测试前通道/时基/触发/传输设置已恢复，事件队列为空。历史验收记录见 `artifacts/mso44_v0.6.2/validation.json` 和 `final_gui_validation.json`；140 项软件回归通过。旧分发版保留在 `release/ScopeHost-v0.6.2-Windows.zip`。

## v0.6.1 · VISA 扫描回归与换电脑连接诊断

修复 v0.6.0 扫描中对 PyVISA `ResourceManager` 错误使用上下文管理器、随后吞掉异常而返回空资源列表的问题。改用显式关闭，扫描失败与真正无设备分别显示。连接窗口增加实际 VISA 库、USB/串口数量、自动/系统 VISA/pyvisa-py 后端选择和诊断 JSON 导出；串口默认隐藏，仅作诊断显示，不再自动将 ASRL 当成 USB 示波器。

复制软件目录不包含系统 USB 驱动。NI-VISA 不是唯一选择，已有兼容的 TekVISA/Keysight VISA 等也可使用；只有 ASRL 资源时，应先确认 USB 仪器接口、驱动及厂商工具中的识别结果。详见 [换电脑后的 VISA / USB 连接说明](docs/visa_usb.md)。新增 `tests/test_visa_discovery.py` 针对真实 API 合约回归；`tests/validate_visa_connection.py` 验证 DS1152E 实际 USB 扫描、GUI 连接/采集和扫描对现有会话的隔离。

## v0.6.0 · MSO44 连接修正与刷新延迟

VISA 扫描和通信隔离到有超时保护的进程，连接可取消并显示核验进度；身份超时不再返回“Unknown 已连接”。修正 MSO44 的通道显示、按通道触发电平及波形前导处理，缺少换算参数时拒绝显示。采集页增加 100～10000 ms 帧后延迟（真实设备默认 1000 ms）和 Tek 10k～100k 预览点数。设备抽取覆盖整段记录，GUI 最多等待一帧，避免绘图/FFT 较慢时积压。

预览及导出的点数可能少于设备深存储，抽取可能遗漏窄脉冲；界面分别显示设备记录、本帧点数和通信耗时。此次为协议夹具与软件回归，尚无 MSO44 真机验证。详见 [MSO44 连接与通信负载](docs/mso44.md)。

## v0.5.0 · 横河 DLM3000 系列

新增 DLM3022/3032/3052（2 通道）、DLM3024/3034/3054（4 通道）的独立适配器：基本控制、Single、边沿触发、实际带宽、同一采集的多通道波形和设备原生截图。连接窗口选择横河后网口端口切换为 10002；采集页增加设备记录长度，通道页提供 AC/DC/DC50，支持双边沿与 Auto Level。

连续采集使用官方 `WAVeform:ALL:SEND?` 路径，要求记录长度 ≤12.5k；停止或 Single 后分块读取完整 WORD 记录，本版上限为 200 万点/通道。超限或参数不完整会明确报错，不猜测换算参数或生成替代波形。逻辑输入、电流探头及增强触发暂未适配。详见 [DLM3000 连接与支持范围](docs/dlm3000.md)，依据 [横河官方通信手册](https://cdn.tmi.yokogawa.com/1/7456/files/IMDLM3054-17EN.pdf)。已做协议与 GUI 验证，尚无横河真机验证。

## v0.4.0 · 偏移输入修复、双向光标和整窗截图

数字控件使用明确的左减、右加按钮，偏移步长随时基或通道电压刻度变化；实时轮询保留正在输入及等待设备执行的数值，命令完成后以实际读回值更新。数字输入按 Enter 或移开焦点提交。

波形下方显示各通道实际硬件带宽限制。DS1000E 的 `:CHANn:BWL?` 对应 20 MHz 或全带宽；旧归档缺少该信息时显示未知。垂直光标 A/B 显示当前或全部启用通道的采样电压，水平光标 C/D 测量所选通道的电压差；支持 ZOOM、堆叠和归档回放。工具栏及 Ctrl+Shift+S 保存整个软件窗口截图，包含设置和测量读数。

验证：`python tests/test_controls_cursors.py`（8 项交互回归）；`python tests/validate_ui_updates.py`（DS1152E 真机，测试后恢复偏移、带宽和原运行状态）。结果见 `artifacts/ds1152e_ui_v0.4_validation.json`。

![DS1152E 光标和带宽显示](artifacts/ds1152e_ui_v0.4.png)

## v0.3.0 · ZOOM 和通道堆叠

工具栏「ZOOM 放大」显示总览和局部视图：拖动蓝色选区/边框调节范围，局部窗口滚轮放大、拖动平移、双击复位；支持 1～100 倍横向放大与本地电压放大。游标/参考波形在两视图同步，可导出 ZOOM 区间 CSV，NPZ 归档保留视图选项。

「通道堆叠」给每个启用通道独立显示区域，共用时间轴，保留各通道 V/div 和偏移；可与 ZOOM、FFT 同时开启。功能仅改变本地显示，原始采样和设备设置保持不变。

已通过 7 项 GUI 回归及 DS1152E 真机验证；日志确认显示操作没有增加硬件写命令。验证脚本：`tests/test_zoom_stacked.py`、`tests/validate_zoom_stacked.py`。真机结果：`artifacts/ds1152e_zoom_validation.json`。

![DS1152E ZOOM 和通道堆叠](artifacts/ds1152e_zoom_stacked.png)

## v0.2.1 · DS1152E 真机修正

Windows 版本位于 `release/ScopeHost/ScopeHost.exe`。保留整个目录，双击 `StartDS1152E.cmd` 即可连接当前 USB 示波器（序列号 DS1EI182307208）；也可从 exe 的「连接设备」扫描 VISA/USB。默认连接真实设备，失败不会回退到模拟器。

![DS1152E 真实 USB 波形](artifacts/ds1152e_workbench.png)

已修复 DS1000E 采样码电压方向、偏移显示与时间轴：`V=(125-code)*scale/25-offset`，600 点覆盖 12 格。GUI、触发、数学和分析选择器按真实通道数显示，DS1152E 只有 CH1/CH2。模拟器须明确启动，并标注 SIMULATION；断开或采集异常时标注保留帧。

实测 CH1 校准方波 3.00 Vpp、约 999.8 Hz，与仪器内置 3.00 Vpp/1.00 kHz 一致；CH2 悬空约 8 mV 底噪，不报告稳定周期。真实数据、设置、SCPI 日志和 38 项验收记录见 `artifacts/ds1152e_validation.json`、`ds1152e_real_capture.npz`、`ds1152e_real_capture.csv`。真机测试命令：`python tests/validate_ds1152e.py`；需 CH1 接校准端，CH2 悬空且两通道已开启。

| 功能 | 操作与说明 |
|---|---|
| 多通道实时显示 | 已启用通道共同显示、固定通道颜色、峰值保留绘图；现代 Tek 在 RUN 时依次读完整记录，手动 STOP 后读取同一停止记录 |
| 运行 / 停止 / Single | 下发单次触发，等待设备完成，读取所有启用通道的冻结记录；等待时可强制触发或停止 |
| 时基与电压控制 | 标准 1-2-5 档位、每通道偏移和 DC/AC/GND 耦合；水平拖动为本地视图平移 |
| 边沿触发 | Auto / Normal、通道源、电平、上升沿 / 下降沿、Force、Auto Set |
| 本地分析 | 跨帧平均 4/16/64、移动平均滤波、A+B / A−B 数学通道，保留独立原始数据 |
| FFT | 时域与频域同时显示；源通道、四种窗函数、显示频宽可选；单边峰值 dBV，校正窗增益、去直流 |
| 测量 | Vpp、Vrms、平均/最大/最小电压、AC RMS、估计频率/周期、完整周期占空比；点击通道行选择分析源 |
| 游标 / 参考 | 垂直 A/B 显示时间及当前/全部通道电压；水平 C/D 显示所选通道电压和 ΔV；参考波形以虚线叠加 |
| 保存 / 回放 | NPZ 保留每个原始采样码、电压及 JSON 设置；保存时严格核验完整设备深度和原生采样间隔，回放不会增加采样点 |
| 导出与图片 | 原始/分析多通道 CSV、单通道 CSV、FFT/测量 CSV、LTSpice PWL、电路、整个软件窗口 PNG、设备截图 |
| 后台通信 | 连接及设备命令在工作线程执行；控制、波形传输与截图串行处理 |

快捷键：空格运行/停止，`Ctrl+S` 保存原始归档，`Ctrl+O` 打开归档，`Ctrl+Shift+S` 保存整个软件窗口截图。

DS1152E 固件 00.04.00.00.01 的基础控制和历史屏幕点数归档已通过真实 USB 验证。MSO44 已有基础控制、完整记录和 v0.6.4 只读采集真机验收；MSO44B 和 DS1000Z 仍需各自真机验证。其他适配器必须按自身协议确认完整性，不能把屏幕预览当成完整深存储。未实现脉宽/逻辑/总线触发、串行协议解码和 XY 显示。

平均仅适用于重复且对齐的波形；Single 绕过跨帧平均。归档保存原始采样及处理选项，不保存平均历史。测量受设备传输点数、量化精度及可见周期数影响；多通道 CSV 以首通道时间轴对齐，超出其他通道记录的区间填 NaN。

验证命令：`python tests/test_core.py`（45 项）和 `python tests/test_workbench.py`（10 项，包含 GUI 交互、触发等待/强制/取消、保存和回放）。后者生成 `artifacts/workbench.png`。打包：`powershell -ExecutionPolicy Bypass -File build_exe.ps1`；也可传入 `-Python "绝对路径/python.exe"`。

DS1000Z 协议修正依据：[RIGOL 官方编程手册](https://int.rigol.com/Images/DS1000ZProgrammingGuideENV1_tcm7-3028_tcm7-3028.pdf)。标准 BYTE 采样块不包含私有 headless 头，十字段 `WAV:PRE?` 用于时间和电压换算。

一套以 **SCPI 标准协议**为核心的跨厂商示波器上位机软件，目标是"一套引擎覆盖市面上绝大多数带程控接口的台式示波器"。内置 **MSO44B 行为模拟器**，没有真机也能完整体验从采集到显示的整条链路。

## 快速开始

```bash
pip install -r requirements.txt

python run.py --demo        # 启动 GUI 并自动连接内置模拟器（无真机演示）
python run.py               # 启动 GUI，手动选择连接方式
python run.py --headless    # 离屏自检：采集一帧并渲染 PNG（CI/自测用）
python tests/test_core.py   # 核心层单元测试 + 模拟器端到端
```

演示模式下应能看到 4 通道波形（正弦/方波/扫频），支持滚轮缩放时基、拖动平移、FFT 频谱、自动测量、CSV 导出及 NPZ 归档回放。

**屏幕截图（真实设备屏幕）**：菜单「文件 → 设备屏幕截图」 会抓取**示波器设备自己的屏幕画面**——泰克机型走 `:SAVe:IMAGe` 存仪器内文件 → `:FILESystem:READFile` 读回 PNG；Rigol DS1000Z 及以上机型走 `:DISPlay:DATA?` 读回 BMP。

⚠️ **老机型（Rigol DS1000E/D，如 DS1152E）限制**：该系列没有 `:DISPlay:DATA?`，也不支持文件读回（只有 `:HARDcopy` 把 BMP 存到 U 盘），**无法远程回传屏幕像素**。此时软件自动改用**数据重建**：用设备真实波形+量程+时基按示波器屏幕样式渲染一张 PNG（内容与设备屏幕一致，来源在状态栏标注"设备屏幕·数据重建"）。若要设备原生截图，可插 U 盘后在设备上执行 `:HARDcopy`。

菜单「文件 → 保存上位机窗口截图」始终保存本机软件窗口。

## 连接真实示波器

点击工具栏「连接」，三种方式：

| 方式 | 适用 | 示例 |
|---|---|---|
| TCP / LAN | 带网口的台式机（推荐 MSO44B） | `192.168.1.50:5025` |
| VISA 资源 | USB-TMC / 串口 / LXI | `USB0::0x0699::0x0408::C000001::INSTR` |
| 内置模拟器 | 开发 / 演示 | 自动 |

连接后软件发送 `*IDN?` **自动识别厂商适配器**（也可手动指定）。VISA 使用本机可用后端；不限定 NI-VISA，其他兼容 VISA 库或有匹配 USB 驱动的 pyvisa-py 也可使用。复制软件不会安装系统 USB 驱动，详见 [VISA / USB 连接说明](docs/visa_usb.md)。

### 泰克 MSO44B 真机要点

- 网口：设置 → I/O → Ethernet 开启，端口 `5025`（原始 socket），本软件用 TCP 直连即可。
- USB：用 `USB-TMC` 模式，VISA 资源形如 `USB0::0x0699::...::INSTR`。
- 波形路径（与手册 Appendix D 一致）：`:DATa:SOUrce` → `:WFMOutpre:ENCdg BINARY` → `:WFMOutpre?`（返回 `KEY value;` 键值对）→ `:CURVe?`（`#N<len>` 原始二进制块，无内嵌头）。
- 换算：`y = (raw − YOFF) × YMULT + YZERO`，`x = (i − PT_OFF) × XINCR + XZERO`（25 个 digitizing level / div）。
- `:WAVFrm?` = `:WFMOutpre?` 文本 + `:CURVe?` 块；现代 Tek 在 RUN 时使用此路径一次读回每通道完整记录，手动 STOP 后使用连续 `CURVe?` 数据块。

## 支持范围

| 厂商/机型 | 方式 | 状态 |
|---|---|---|
| Tektronix MSO44 / MSO44B / 4-5-6 系列 | RUN: WAVFrm? 整包；STOP: 连续 CURVe? 数据块，RESAMPLE 1 | 已实现完整记录；MSO44 只读验收见 v0.6.4 报告，其他型号待真机 |
| Tektronix 屏幕抓取 | SAVe:IMAGe → FILESystem:READFile（PNG） | ✅ 已实现 |
| Tektronix 其他 DPO/MSO 家族 | 部分 WFMOutpre?/CURVe? 命令适配 | 待逐型号核验；不保证完整深记录 |
| **Rigol DS1000E/D（DS1152E 等）** | :WAV:DATA? <src> 直接读 + BYTE 换算公式 | ✅ **真机验证（本文档所用机型）** |
| Rigol DS1000Z 等（:WAV:DATA?） | BYTE + WAV:PRE? 十字段 | ✅ 已实现（解码器单测） |
| Rigol 屏幕抓取（DS1000Z+） | :DISPlay:DATA?（BMP） | ✅ 已实现 |
| Yokogawa DLM3000 六种型号 | ALL:SEND? 同步连续波形 / SEND? 完整 WORD 记录；IMAGE:SEND? 原生截图 | 已实现（协议、Socket、GUI 验证，待真机） |
| 其他 SCPI 台式机（Keysight/Siglent/其他 Yokogawa/Owon…） | Generic SCPI 有限命令尝试 | 尚未加入专用适配，波形读取和换算须逐型号实现并验证 |
| 私有协议白牌（Hantek/Fnirsi…） | 无统一标准 | ❌ 需逐款逆向，架构已留插件位 |

「通用」的边界：**SCPI 生态覆盖绝大多数可程控台式示波器**（一套引擎 + 厂商差异表）；私有协议便携机只能逐个适配（见 `adapters/` 插件模式）。

## 架构

```
ui/             PySide6 界面（波形视图/控制面板/测量表/采集线程）
session.py      会话层：传输 + SCPI 引擎 + 适配器 的统一入口
adapters/       厂商适配器（tektronix / rigol / registry 自动识别）
waveform.py     波形解码：INT8/16/32、IEEE754、ASCII、TDS 头、Rigol 头
scpi.py         IEEE 488.2 二进制块解析 + 查询封装
transport.py    传输层：TCP（按块长读取，兼容块内 0x0A）/ VISA(USBTMC/串口/LXI)
simulator.py    内置 SCPI 模拟示波器（模拟 MSO44B 行为，TCP 5025）
measurements.py Vpp/Vavg/Vrms/频率/周期 + FFT 频谱
```

新增厂商 = 继承 `ScopeAdapter` + 注册到 `adapters_registry`（按 `*IDN?` 自动匹配），约 100 行：
见 `scopeapp/adapters/rigol.py` 作为最小参考实现。

## 性能设计（真机流畅的关键）

完整记录读取时间包括设备准备、SCPI 查询、数据传输和本地分析。设备硬件采样率与上位机刷新率分别显示；不通过丢弃原始样本提高完整记录的刷新率。

| 优化 | 效果 |
|---|---|
| **同轮设置**（`worker.py`） | 现代 Tek 冻结记录后读取本帧通道/时基/触发设置；DS1000E 每次波形读取同轮设置，避免旧参数换算新数据 |
| **分块与复合命令** | 现代 Tek 最多 500k 点一块，逐块确认长度和轴连续性；源/编码/位宽准备合并，禁止截断、抽点或补点 |
| **帧背压与后台任务** | GUI 处理完当前帧后才继续下一轮；基础测量、通信、大型 NPZ 压缩在后台进行，正常断开完成当前数据块并恢复临时暂停的采集 |
| **局部原始采样绘图** | 按选区索引取得实际样本，点数超出像素时保留极值；ZOOM 放大后重取局部样本，保留窄脉冲细节 |

## 技术要点（踩坑记录）

- **二进制块传输**：`CURVe?` 等返回 `#N<len><data>`，数据内可能含 `\n`，TCP 传输层按块长度读取而非按行（`transport.py:_complete_block_len`）。
- **WFMOutpre? 键值对**：4/5/6 系列无固定字段顺序，解析为 `KEY value;` 字典，兼容 `HEADER ON/OFF`。
- **TDS 内嵌头**：老 TDS 家族 `CURVe?` 带 25 字节头（1 字节 Y offset + 6 个 float32 大端缩放参数），解码器保留该路径。
- **Rigol 头**：9 字节小端头（headless + int16 origin/ref + float32 incr），换算 `y=(raw−origin−ref)×incr`。
- **浮点精度**：头内缩放参数是 float32，断言比较用容差。

## 导出（含 LTSpice 支持）

工具栏「保存 / 导出」提供三种格式：

| 格式 | 文件 | 用途 |
|---|---|---|
| **LTSpice 四通道** | 每通道一个 `scope_chN.csv`（两列无表头：时间,电压）+ 自动生成的 `scope_ltspice.asc` 电路 | LTSpice 直接打开 `.asc` 运行，看 V(ch1)~V(ch4) 四路波形 |
| Excel/Origin 多列 | 单文件 `Time,CH1_V,CH2_V,...`（带表头，自动对齐时间轴） | Excel / Origin / MATLAB |
| 单通道 | `Time,CH1_V`（带表头） | 单通道快速导出 |

**LTSpice 需要几个 CSV？** —— **四路 = 四个文件**。LTSpice 观察外部波形数据的机制是 PWL 电压源（`PWL FILE=xxx.csv`），它只认**两列（时间,电压）且无表头**，一个源只能加载一路；四路同时观察就是 4 个 PWL 源（软件自动生成的 `.asc` 已把 4 个源接到 CH1~CH4 节点，打开即用）。这与 Tektronix 手册一致：MSO44B 的 `SAVe:WAVEform ALL,"xxx.csv"` 也是"每个波形存到独立文件"（`docs/mso456_prog.txt` 行 32786-32806）。

**LTSpice 兼容性关键点**（软件已处理）：
- 时间轴自动平移到从 0 开始单调递增（示波器波形以触发点为中心含负时间，LTSpice 仿真从 t=0 起）
- 无表头、纯 ASCII、`%.15g` 双精度数值，深存储大点数不丢精度
- 生成的 `.asc` 含 `.tran` 仿真时长 = 波形时长，运行即得四路

**手工在 LTSpice 里加载**：放置 Voltage 源 → 右键 Value 填 `PWL FILE=C:/路径/scope_ch1.csv` → 负极接地，运行后查看 `V(ch1)`。

## 部署到其他电脑（笔记本等）

**识别不到示波器的原因**：exe 自带的 VISA 支持有两条路——① 系统装了 NI-VISA（visa32.dll）时走 NI-VISA（最省心）；② 没有 NI-VISA 时走内置的 pyvisa-py（纯 Python USBTMC），它需要 **libusb-1.0.dll（exe 已打包）+ 设备驱动是 WinUSB**。而 Windows 10/11 对示波器 USB 默认装内置 `usbtmc.sys` 驱动，libusb 无法访问它 → 枚举不到设备。

**笔记本上用以下任一方案**：

- **方案 A（推荐，一次安装永久解决）**：安装 [NI-VISA Runtime](https://www.ni.com/zh-cn/support/downloads/software-products/download.ni-visa.html)（免费）。装完 exe 自动走 NI-VISA，无需任何额外配置。
- **方案 B（免安装）**：用 [Zadig](https://zadig.akeo.ie/) 把示波器 USB 设备的驱动从 `usbtmc.sys` 换成 **WinUSB**（Zadig 选择设备 → Replace Driver → WinUSB）。exe 已内置 libusb-1.0.dll，换完驱动直接可用。
- **方案 C（已装过 exe 版但驱动没动过）**：如果设备在设备管理器里是 "USB Test and Measurement Device (IVI)"（usbtmc.sys），参考方案 B 换 WinUSB，或直接装 NI-VISA（方案 A）。

**自检命令**（无需显示器，输出环境诊断）：

```
ScopeHost.exe --diag
```

输出会明确告诉你是「未检测到设备」「libusb 后端不可用（缺 dll）」还是「被 usbtmc.sys 占用需换 WinUSB 或装 NI-VISA」。GUI 连接对话框在资源为空时也会显示同样的诊断。

## 打包为 exe（Windows）

```powershell
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

产物 `release\ScopeHost\ScopeHost.exe`（onedir，含 PySide6/pyvisa/pyvisa-py，体积依赖于所用 Python / Qt 版本）。
构建后脚本自动做冒烟验证（内置模拟器离屏渲染）。

exe 支持命令行自检（无需显示器）：

```powershell
ScopeHost.exe --headless                              # 模拟器自检
ScopeHost.exe --headless --visa "USB0::0x1AB1::0x0588::...::INSTR"  # 真机自检（波形+截图）
```

## 目录说明

- `docs/` — 下载的 Tektronix 官方编程手册（TDS 家族、4/5/6 系列 MSO），协议依据。
- `tests/` — 解码器单元测试 + 模拟器端到端（`python tests/test_core.py`，45 项）；新增数字控件、双向光标和整窗截图交互回归见 `tests/test_controls_cursors.py`。
  真机相关脚本（需硬件）：
  - `tests/real_ds1152e_smoke.py` — DS1152E 真机端到端（适配器识别/波形/抓屏重建）
  - `tests/probe_ds1152e.py`、`tests/probe_wave_format.py`、`tests/verify_ds1152e.py` — 真机命令探测与公式验证

## 已知限制 / 后续

- 现代 Tek 已支持完整深存储连续分块读取；其他厂商依各适配器能力处理，未经确认的部分记录不能保存为完整 NPZ。
- 触发类型（边沿/脉宽/逻辑）仅实现了边沿。
- 波形显示为保留峰值的降采样绘制；已实现数学通道，未实现 XY 模式。
- 模拟器仅覆盖 MSO44B 常用子集（`SYSTem:ERRor?` 队列、`HEADER` 等已支持）。
- DS1000E 系列未知查询会一直等待到超时；已验证的命令见 `tests/validate_ds1152e.py`。不要直接运行包含未知查询的旧探测脚本。老固件需要逐条发送控制命令并留出处理/采集更新时间。
- **DS1152E 固件 00.04 已知限制**：通道显示一旦关闭（DISP=0），SCPI 无法远程重新打开（`:CHAN1:DISP ON`、`:AUT` 均被接受但无效果）——软件会检测到写入未生效并提示，需在示波器前面板按 CH 键开启。
