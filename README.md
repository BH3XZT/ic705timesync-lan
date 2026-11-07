# ic705timesync-lan

用 **Icom IC-705 内置 GPS** 给电脑授时：通过 CI-V 读 UTC 时间，秒跳变检测后校准
Windows 系统时钟（几十毫秒级）。支持两种传输方式：

- **串口（USB / RemoteUty 虚拟串口）** —— 已端到端验证可用；
- **WiFi（Icom LAN / RS-BA1 私有协议）** —— 发现、登录、会话、普通 CI-V 已验证可用，
  但**实测该 IC-705 固件在 LAN CI-V 通道上不返回 GPS（0x23）数据**，所以"纯 WiFi 取
  GPS"目前不可用（详见[验证状态](#验证状态)）。

本项目适合 **POTA 户外 FT8** 这类没有互联网 NTP、但电台就在手边的场景。

```
IC-705 内置 GPS
   └─ CI-V 命令 23 00（USB 串口 或 WiFi 的 Icom LAN 协议）
        └─ 解析 UTC 年月日时分秒（整秒）
             └─ 高频采样 + 秒跳变检测 → 估计偏差（不确定度实测 ±26~50 ms）
                  └─ 设置 Windows 系统时钟（需管理员）
                       └─ 可选：把 GPS 包装成 NMEA（TCP / 串口）给 BktTimeSync 等
```

> 本仓库是原 `ic705timesync` 的**精简公开版**：只保留授时、GPS 解析和
> 串口/LAN 传输相关模块，去掉了电台设置备份、记忆频道等无关功能。

---

## 目录

- [它能做什么](#它能做什么)
- [验证状态](#验证状态)
- [安装](#安装)
- [快速开始](#快速开始)
- [把 GPS 输出给别的软件（NMEA）](#把-gps-输出给别的软件nmea)
- [构建单文件 exe](#构建单文件-exe)
- [项目结构](#项目结构)
- [已知限制](#已知限制)
- [排错](#排错)
- [技术文档](#技术文档)
- [版本记录](#版本记录)
- [许可](#许可)

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| `read` | 读一次 GPS，显示经纬度、海拔、UTC 与**电脑时钟偏差**（不改系统时间） |
| `sync` | 测偏差并设置系统时钟（需要管理员权限；`--dry-run` 可预览） |
| `watch` | 常驻，按间隔自动对时 |
| `ports` | 扫描本机串口，识别哪个串口后面是 Icom 电台（发 `19 00` 读型号 ID） |
| `discover` | 局域网广播发现 Icom 电台（IC-705 热点模式 IP 通常是 `192.168.59.1`） |
| `serve` | 把 GPS 包装成 NMEA 0183（`$GPZDA` / `$GPRMC` / `$GPGGA`），走 TCP 和/或串口输出 |

两种传输方式共用同一套 CI-V 帧与解码逻辑，上层命令完全一致，只改 `--transport`。

---

## 验证状态

以下结论来自真实 IC-705（CI-V 地址 `0xA4`），固件版本未记录，2026-11 实测：

| 链路 | 状态 | 说明 |
|---|---|---|
| USB 串口（COM6，115200 8N1） | ✅ **可用** | `23 00` 返回完整 27 字节；实测偏差 +16~+34 ms，不确定度 ±26~31 ms |
| RemoteUty 虚拟串口（COM20） | ✅ **可用** | 同样能读 GPS；虚拟口会回显请求帧，代码会自动丢弃，不确定度约 ±41~50 ms |
| LAN 发现（`discover`） | ✅ 可用 | UDP 50001 广播 `AreYouThere`，收到 `IAmHere` |
| LAN 登录 / 会话（0x80 / 0x90） | ✅ 可用 | 用户名/密码混淆、token、conninfo、session 建立成功 |
| LAN 普通 CI-V（读频率、读型号） | ✅ 可用 | 能通过 50002 端口读频率、型号，说明 CI-V 通道本身通 |
| LAN 读 GPS（`23 00`） | ❌ **不可用** | 显式请求无应答，也未观察到电台主动推送；没有 GPS 数据可解码 |
| 串口 NMEA 输出（`serve`） | ✅ 可用 | 已按 IC-705 真实语句校验校验和 |

**结论**：就这台 IC-705 的固件而言，Icom LAN 的 CI-V 通道**不承载 `0x23` GPS 数据**
（这与 wfview 社区"网络协议没有为 USB GPS 串口留通道"的结论一致）。因此：

- 想要授时：用 **`--transport serial`**（USB 直连，或 RemoteUty/wfview 建的虚拟串口）；
- `--transport lan` 的登录/会话代码是**已验证可用**的，保留它是为了在固件支持后能直接
  工作，也方便用 LAN CI-V 做其它只读操作；
- LAN 模式下读 GPS 失败时会给出一条明确的中文错误，而不是静默超时。

> 精度上限：CI-V `23 00` 只给整秒，没有 PPS 秒脉冲。本项目用"秒跳变法"把 1 秒
> 分辨率压到几十毫秒，对 FT8（时隙 15 秒）绰绰有余，但**不适合**需要毫秒/微秒级的场合。

---

## 安装

要求：**Windows 10/11 + Python 3.10+**（串口用 ctypes 调 kernel32，零第三方依赖）。

```powershell
# 1) 从源码运行（不安装）
$env:PYTHONPATH = "D:\path\to\ic705timesync-lan\src"
python -m ic705timesync --help

# 2) 或安装为命令（可编辑安装）
python -m pip install -e .

# 3) WiFi（LAN）模式需要 icom-lan（MIT）
python -m pip install icom-lan
# 或：python -m pip install -e ".[lan]"
```

安装后入口是 **`ic705timesync-lan`**（也支持 `python -m ic705timesync`）。

---

## 快速开始

### 1) USB 串口模式（推荐，已验证）

IC-705 插 USB 后，设备管理器里通常出现两个串口：

- **CI-V 控制口**（本机实测 `COM6`）—— 授时要用它；
- **GPS NMEA 口**（例如 `COM7`）—— 只输出 NMEA，不是本项目用的口。

```powershell
# 先扫描，确认哪个口后面是 IC-705（应答型号 ID 0xA4）
ic705timesync-lan ports

# 读一次 GPS（不改系统时间）
ic705timesync-lan read --transport serial --port COM6

# 对时（需要"以管理员身份运行"的终端）
ic705timesync-lan sync --transport serial --port COM6

# 常驻，每 60 秒校准一次
ic705timesync-lan watch --transport serial --port COM6
```

`read` 输出示例（真实数据）：

```
GPS: 37.871867°N, 112.542650°E  海拔 793.2 m  航向 161°  速度 1.1 km/h  时间 2026-11-07 14:31:07 UTC
偏差估计: +16.7 ms (±26 ms, 秒跳变法, 30 采样/2 次秒跳变)
管理员权限: 否（sync 需要管理员）
```

### 2) WiFi（LAN）模式 —— 登录可用，GPS 不可用

电台侧一次性设置：

1. `SET → WLAN Set → Connection Type` 选 **Access Point**；
2. 设 `SSID` / `Password`，记下 **Network User ID** / **Password**（登录凭据）；
3. 打开 WLAN，确认 GPS 已定位（面板出现 GPS 图标）。

热点模式下电台 IP 默认是 `192.168.59.1`，电脑会自动拿到 `192.168.59.x`。

```powershell
# 发现电台
ic705timesync-lan discover

# 尝试通过 LAN 读 GPS（在本机固件上会失败并给出明确错误）
ic705timesync-lan read --transport lan --host 192.168.59.1 --user <用户名> --password <密码>
```

实测输出（本机固件）：

```
错误：LAN 通道没有返回 GPS 数据：显式请求 23 00 无应答，也未收到电台推送。
该 IC-705 固件的 LAN CI-V 通道不提供 0x23 GPS 数据，请改用 USB 串口（--transport serial）。
```

> 如果你的固件**支持** LAN GPS，这条命令应当直接可用——登录、会话和帧解码都已实现并验证。

### 3) 户外现场：IC-705 热点 + RemoteUty + JTDX（无互联网）

1. 电台开热点，电脑连上；
2. 打开 **RemoteUty（Icom Remote Utility）**，填电台 IP/用户名/密码并连接；
3. 在 RemoteUty 里启用 **Virtual COM Port**（例如 `COM20`）；
4. 在启动 JTDX **之前**对时（虚拟串口是独占的）：

   ```powershell
   ic705timesync-lan ports
   ic705timesync-lan sync --port auto
   # 或：ic705timesync-lan sync --transport serial --port COM20
   ```

5. 启动 JTDX，CAT 选同一个虚拟串口。

**为什么只对一次就够**：普通电脑时钟漂移约 1 ppm（每小时几毫秒），一次对时后跑一整天
POTA，误差也远小于 FT8 允许的 ~1 秒。常驻 `watch` 会和 JTDX 抢串口，一般不需要。

---

## 把 GPS 输出给别的软件（NMEA）

如果习惯用 BktTimeSync / NMEATime2 这类只认 NMEA 的软件，可以让本项目做桥：

```powershell
# 输出到 TCP（如软件支持 TCP-NMEA）
ic705timesync-lan serve --transport serial --port COM6 --tcp 0.0.0.0:2101

# 输出到虚拟串口对（需先装 com0com，创建 COM20<->COM21，另一头给 BktTimeSync）
ic705timesync-lan serve --transport serial --port COM6 --serial-out COM20
```

生成的语句包含 `$GPZDA`（UTC 日期时间）、`$GPRMC`、`$GPGGA`，校验和与 IC-705 实际
输出一致。

---

## 构建单文件 exe

仓库自带可复现的打包脚本 `tools/build_exe.py`，**不需要 pip 安装打包工具**：它从 PyPI
下载固定版本的 PyInstaller wheel，解压到 `.build-libs/`，再把 `src/` 与 `icom_lan`
一起打进 onefile exe。

```powershell
# 需要能联网下载 PyInstaller（约 10 MB）
python tools\build_exe.py

# 产物
dist\ic705timesync-lan.exe        # 约 24 MB，内置 icom-lan，免 Python 运行
```

可选参数：

| 参数 | 作用 |
|---|---|
| `--no-download` | 不联网；复用 `.build-libs/pylibs/` 或工作区 `.tools/pylibs/` |
| `--vendor DIR` | 指定 `icom_lan` 所在目录（默认自动探测 `../.tools/vendor`） |
| `--clean` | 打包前删除 `build/`、`dist/` |
| `--patch-acl` | 修补 onefile 临时解包目录 ACL，供受限令牌/沙箱环境启动 |
| `--no-verify` | 跳过打包后的 `--version` / `--help` 自检 |

说明：

- `icom-lan` 是**运行时可选依赖**，仓库本身不附带它；打包时用
  `--paths <vendor>` + `--collect-submodules icom_lan` 静态收集。
- 产物 `dist/`、`build/`、`.build-libs/` 都已写进 `.gitignore`，**不要提交 exe**；
  发布时把 exe 作为 **GitHub Release 资产**上传。
- exe 构建会打印 PyInstaller 命令、产物大小和 SHA256，便于复现和校验。

---

## 项目结构

```
ic705timesync-lan/
├─ README.md                 本文件
├─ LICENSE                   MIT
├─ requirements.txt          icom-lan>=1.1.0（仅 LAN 模式需要）
├─ pyproject.toml            包元数据，入口 ic705timesync-lan
├─ src/ic705timesync/
│  ├─ civ.py                 CI-V 帧构造/解析、GPS 27 字节解码
│  ├─ serial_link.py         Windows 串口（ctypes，零依赖；非 Windows 回退 pyserial）
│  ├─ lan_link.py            WiFi / Icom LAN（基于 icom-lan；含 Windows proactor 补丁）
│  ├─ timecore.py            采样、秒跳变偏差估计、设置系统时钟
│  ├─ nmea.py                NMEA 语句合成（ZDA / RMC / GGA）
│  ├─ cli.py                 命令行
│  ├─ __init__.py            版本与公开 API
│  └─ __main__.py            python -m ic705timesync
├─ tests/                    单元测试（真实抓包帧，可离线运行）
├─ tools/build_exe.py        一键打包单文件 exe
└─ docs/TECHNICAL.md         完整技术思路（协议 / 帧格式 / 实测记录 / 限制）
```

---

## 已知限制

1. **LAN GPS 不可用**（本机固件）：LAN CI-V 不返回 `0x23`，纯 WiFi 无法授时。
   可用替代：USB 串口、RemoteUty/wfview 虚拟串口、或等固件支持。
2. **精度为几十毫秒级**：CI-V 只有整秒，无 PPS；秒跳变法受采样间隔和串口/网络抖动影响。
3. **Windows 优先**：设置系统时钟、串口实现都是 Windows API；其它平台仅能读 GPS
   （设置时间走 `date`，且串口需要 pyserial）。
4. **`sync` 需要管理员权限**：设置系统时间是特权操作。
5. **串口/会话独占**：JTDX、RemoteUty 已占用串口时无法同时读取；电台 LAN 会话同一时间
   只允许一个客户端，未释放时会拒绝新会话（代码会冷却重试）。
6. **电台必须已定位**：GPS 未定位时 `23 00` 数据为全 `0xFF`，会报"无定位"。

---

## 排错

| 现象 | 处理 |
|---|---|
| 打开串口报 `Errno 2 / 错误 2` | 端口名不对或设备未插入；用 `ports` 扫描确认 |
| 串口等待应答超时 | 确认选的是 **CI-V 控制口**，不是 GPS NMEA 口；波特率与电台设置一致 |
| `GPS 位置数据全为 0xFF` | 电台 GPS 未定位或 GPS 功能关闭 |
| `sync` 报需要管理员权限 | 用"以管理员身份运行"的终端 |
| LAN 报 `需要依赖 icom-lan` | `python -m pip install icom-lan` |
| LAN 报 `Radio rejected session allocation` | 上一次会话未释放；等 30–60 秒重试，或断开其它客户端 |
| LAN 读 GPS 超时 | 本机固件不支持（见上文），改用串口 |

---

## 技术文档

完整协议与实现细节（Icom LAN 包格式、登录混淆表、CI-V GPS 27 字节布局、秒跳变
算法、Windows asyncio/IocpProactor 缺陷、真实日志与十六进制）见
**[docs/TECHNICAL.md](docs/TECHNICAL.md)**。

---

## 版本记录

### 0.2.0 — 2026-11（首个公开仓库版本）
- 精简为独立仓库：只保留授时 / GPS 解析 / 串口 / LAN 传输相关模块；
- 移除 `backup` / `restore` / `mem` 子命令及对应模块（属于另一项目的功能）；
- 新增可复现打包脚本 `tools/build_exe.py`（下载 PyInstaller 到 `.build-libs`，
  打 onefile exe，内置 icom-lan）；
- LAN 读 GPS 改为"先显式请求、再等推送"双路径，失败时给出明确错误；
- 补充 README 与 `docs/TECHNICAL.md`，如实记录 LAN GPS 不可用的实测结论；
- 单元测试精简为 13 项（CI-V 解码、NMEA/时间估计、LAN 离线）。

### 0.1.1 — 2026-11（内部版本）
- 修复 Windows asyncio IocpProactor 上连接式 UDP `sendto(addr=None)` 崩溃；
- 增加 LAN 会话拒绝后的冷却重试；
- 串口路径实测通过，LAN 登录/会话实测通过。

### 0.1.0 — 2026-11（内部版本）
- 初版：CI-V `23 00` GPS 解码、串口传输、秒跳变对时、NMEA 输出。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

第三方说明：

- [icom-lan](https://github.com/morozsm/icom-lan)（MIT）—— Icom LAN/RS-BA1 传输层，
  仅 WiFi 模式需要，不随本仓库分发；
- Icom、IC-705 是 Icom Inc. 的商标，本项目与 Icom 无隶属关系。
