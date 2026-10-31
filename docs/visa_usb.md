# VISA 与 USB 连接说明（v0.6.1）

## v0.6.0 扫描回归

v0.6.0 的扫描代码误用了 `with pyvisa.ResourceManager()`，而打包的 PyVISA 1.16.2 资源管理器没有 `__enter__` / `__exit__`。异常被旧代码忽略后变成空资源列表，因此可能出现 v0.5.0 有资源、v0.6.0 显示无设备的差异。

v0.6.1 改为显式 `close()`，扫描错误完整显示并可导出诊断。后台进程隔离、超时和取消机制保留。扫描不会尝试打开枚举到的串口，也不会开启示波器通道。

同一电脑上脚本可连接而旧版界面扫描为空时，先使用完整的 v0.6.1 软件目录重新测试。连接窗口标题包含版本号；诊断同时记录程序版本、源码/打包运行方式、实际 VISA 库、PyVISA 版本和位数。脚本能连接说明它所用的通信路径可用，仍应比较它与界面的资源地址和后端是否一致。

## 是否必须安装 NI-VISA

**NI-VISA 不是唯一选择，但电脑需要与设备兼容的 USB 仪器驱动。**

| 方式 | 需要什么 | 软件中的选择 |
|---|---|---|
| NI-VISA | 安装包含 USB 仪器支持的 NI-VISA，并让 Windows 正确识别 USBTMC 设备 | 自动选择，或系统 VISA |
| TekVISA / Keysight VISA / R&S VISA 等 | 已安装的系统 VISA 库和设备兼容的 USB 驱动；库的位数须与软件相容 | 自动选择，或系统 VISA |
| pyvisa-py | 程序已打包 Python 库和 libusb，但系统上仍需要兼容的设备 USB 驱动 | 内置 pyvisa-py |

PyVISA 默认优先使用可用的系统 IVI-VISA，找不到时使用 pyvisa-py；已安装的 USB 驱动可能适用于某一个后端，因此扫描和连接需使用相同后端。依据 [PyVISA 官方后端说明](https://pyvisa.readthedocs.io/en/latest/introduction/configuring.html) 与 [pyvisa-py USB 依赖说明](https://pyvisa.readthedocs.io/projects/pyvisa-py/en/latest/installation.html)。

多厂家 USB 示波器可优先使用 NI-VISA。通过 [NI 官方下载页](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html) 获取适用于目标 Windows 的安装版本；安装后重新插拔仪器，在 NI MAX 中确认 USB 仪器出现，再启动上位机。USBTMC 的配置参见 [NI 官方 USB 仪器设置](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA03q000000x1qzCAA&l=zh-CN)。程序不会替用户安装或更换系统 USB 驱动。

## 资源地址不是设备身份

- `USB0::...::INSTR`：USB 仪器资源。仍要读 `*IDN?`，才能确认是哪台仪器及型号。
- `ASRL3::INSTR`：串口 COM3。并不表示扫描到示波器；截图中的 “Cannot configure port / PermissionError / 设备没有发挥作用” 是串口打开失败。
- `TCPIP...::INSTR`：通常为 VISA LAN 仪器资源；与原始 SCPI TCP Socket 的主机/端口模式不同。

串口可能属于电脑主板、蓝牙、USB 转串口或其他外设。当前 GUI 面向 USB / VISA LAN 及原始 TCP，没有串口波特率/校验位配置，因此 ASRL 只用于诊断显示，不提供串口连接。

## 新电脑只有 ASRL 时

1. 确认示波器已开机，USB 线连接的是用于电脑通信的 USB DEVICE / USBTMC 接口。检查 Windows 设备管理器是否出现该设备及驱动错误。
2. 安装或检查兼容的 VISA / USBTMC 驱动。若已有厂商 VISA，不必机械地再安装 NI-VISA；先用其连接工具确认设备。
3. 在上位机选择对应的 VISA 后端后扫描，检查是否出现 `USB...::INSTR`，而不是仅出现串口。
4. 在列表中选择正确的 USB 仪器，再连接。软件通过 `*IDN?` 确认身份，失败不会使用模拟波形替代。
5. 若仍失败，点击「导出连接诊断」，文件包含实际后端、资源列表和扫描错误。它能区分程序异常、系统 VISA 缺失和当前未找到 USB 仪器，不会把空列表直接判为硬件损坏。

若仪器支持 LAN，也可使用已适配的网口接口，避免依赖 USB 驱动；原始 TCP 模式仍需设备开放对应 Socket 服务。

## 命令行诊断

```text
ScopeHost.exe --scan-visa --out visa_scan.json
ScopeHost.exe --scan-visa --visa-backend @ivi --out visa_system.json
ScopeHost.exe --scan-visa --visa-backend @py --out visa_py.json
```

这些命令在隔离进程中执行扫描，将实际后端与资源列表写入 JSON。错误返回非零退出码，并在文件中保留原因。扫描不发送 `*IDN?`，USB 资源出现不等于已验证示波器命令及波形读取。
