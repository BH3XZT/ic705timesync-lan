# ic705timesync-lan 技术文档

> 本文是项目的"完整技术思路"：协议、帧格式、时间算法、Windows 平台坑、
> 以及**真实实测记录**（含原始十六进制与日志片段）。目标是让一个没接触过
> Icom LAN 协议的读者，读完能独立复现验证、也能判断哪里能改、哪里不能改。

**一句话结论**：IC-705 的 GPS 时间确实能通过 CI-V `23 00` 读出来；USB 串口
路径已端到端验证。Icom LAN（RS-BA1）的发现/登录/会话/普通 CI-V 都已验证，
但本机固件在 LAN CI-V 通道上**不返回 GPS 数据**，所以纯 WiFi 取 GPS 目前不可用。
原因、证据和替代方案见[第 7 节](#7-实测记录)与[第 8 节](#8-局限与后续路线)。

---

## 目录

1. [总体架构](#1-总体架构)
2. [CI-V 基础与 GPS 帧](#2-ci-v-基础与-gps-帧)
3. [串口传输层](#3-串口传输层)
4. [Icom LAN（RS-BA1）协议](#4-icom-lanrs-ba1协议)
5. [时间估计：秒跳变法](#5-时间估计秒跳变法)
6. [Windows asyncio 缺陷与会话重试](#6-windows-asyncio-缺陷与会话重试)
7. [实测记录](#7-实测记录)
8. [局限与后续路线](#8-局限与后续路线)
9. [代码地图与复现实验](#9-代码地图与复现实验)
10. [参考资料](#10-参考资料)

---

## 1. 总体架构

```
                    ┌─────────────────────────────┐
                    │  IC-705 内置 GPS（u-blox 类）│
                    └──────────────┬──────────────┘
                                   │ 固件内部
                    ┌──────────────▼──────────────┐
                    │ CI-V 命令 0x23 / 0x00        │
                    │ 应答 27 字节（位置+UTC）      │
                    └───────┬──────────────┬──────┘
                            │              │
             USB CDC 串口   │              │  Icom LAN（UDP，私有协议）
             （COM6 等）    │              │  （50001 控制 / 50002 CI-V / 50003 音频）
                            │              │
                    ┌───────▼──────┐ ┌─────▼─────────┐
                    │ serial_link  │ │ lan_link       │
                    │ ctypes/      │ │ icom-lan (MIT) │
                    │ kernel32     │ │ + 补丁         │
                    └───────┬──────┘ └─────┬─────────┘
                            └──────┬───────┘
                         统一接口 read_gps() -> GpsFix
                                   │
                    ┌──────────────▼──────────────┐
                    │ civ.decode_gps_position      │
                    │ 27 字节 BCD -> datetime/经纬度│
                    └──────────────┬──────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     timecore.collect_samples  nmea.from_fix       cli 输出/JSON
     秒跳变偏差估计              $GPZDA/$GPRMC/...    read/sync/watch/serve
              │
     timecore.apply_offset -> SetSystemTime（管理员）
```

设计原则：

- **传输层可替换**：`SerialLink.read_gps()` 与 `LanLink.read_gps()` 返回同一个
  `GpsFix` 数据类，上层 `timecore` / `cli` / `nmea` 完全不知道数据从哪来。
- **零依赖优先**：串口用 `ctypes` 直接调 Windows `kernel32`，不强制装 pyserial；
  LAN 才需要 `icom-lan`（MIT），且是可选依赖。
- **只读、不键控**：只发 `23 00` / `19 00` / `03` 等只读命令；打开串口时显式
  关闭 DTR/RTS，避免误触发电台发射。

---

## 2. CI-V 基础与 GPS 帧

### 2.1 帧格式

Icom CI-V 是半双工串行总线协议，一帧：

```
FE FE <to> <from> <cmd> [<subcmd>] [<data...>] FD
```

| 字段 | 含义 |
|---|---|
| `FE FE` | 前导码（preamble） |
| `<to>` | 目标地址；`0xA4` = IC-705，`0xE0` = PC/控制器 |
| `<from>` | 源地址 |
| `<cmd>` | 命令号，如 `0x23` = GPS |
| `<subcmd>` | 子命令（命令族才需要） |
| `<data>` | 载荷 |
| `FD` | 结束符 |

本项目默认 `IC705_CIV_ADDRESS = 0xA4`、`CONTROLLER_ADDRESS = 0xE0`
（`--civ-addr` 可改）。

### 2.2 用到的命令

| 命令 | 用途 | 说明 |
|---|---|---|
| `19 00` | 读型号 ID | IC-705 返回 `A4`，用于 `ports` 子命令识别串口 |
| `03` | 读工作频率 | 只用于联调验证 CI-V 通道（LAN 可用） |
| `23 00` | **读 GPS 位置 + UTC** | 本项目的时间源 |
| `23 01` | 读 GPS 数据源 | 实测应答 `23 01 01`，`01` = 内置 GPS |
| `23 02 ...` | 写手动位置 | 本项目不用 |

请求 GPS 的真实帧：

```
FE FE A4 E0 23 00 FD                 # PC -> 电台
FE FE E0 A4 23 00 <27 bytes> FD      # 电台 -> PC
```

`23 00` 的应答在去掉 `FE FE E0 A4 23 00` 与 `FD` 后正好 **27 字节**。

### 2.3 27 字节字段布局

依据《IC-705 CI-V Reference Guide》pp.21/25，并用真机抓包验证：

| 偏移 | 长度 | 字段 | 编码 |
|---|---|---|---|
| `[0..4]` | 5 | 纬度 | `DD MM mm m0 0H`（BCD；`H=0` 南纬，`1` 北纬） |
| `[5..10]` | 6 | 经度 | `0D DD MM mm m0 0H`（BCD；`H=0` 西经，`1` 东经） |
| `[11..14]` | 4 | 海拔 | `0000.0 m`（BCD，末字节低半字节 `1` = 负值） |
| `[15..16]` | 2 | 航向 | `0..360` 度（BCD） |
| `[17..19]` | 3 | 速度 | `0.1 km/h`（BCD） |
| `[20..26]` | 7 | **UTC 日期时间** | 年(2) 月 日 时 分 秒，全 BCD |

注意几个"坑"：

- **纬度/经度不是纯十进制浮点**，而是"度 + 分 + 分的小数部分"三段 BCD 拼接：
  纬度 `37 52 31 30` → 37° + 52 分 + `31`/100 分 + `3`/1000 分 = 37°52.313′。
  经度多一个字节：`01 12 32 56 10` → `1` 是百位数字，`12` 是十位/个位。
- **半球只看最后半字节**，高半字节必须为 0；`0` 表示南/西，`1` 表示北/东。
- **海拔的负号**在末字节低半字节；`0xFF` 段表示"无数据"。
- **UTC 是 BCD**：`20 26 11 07 14 26 14` → 2026-11-07 14:26:14 UTC。
- 若整帧 27 字节全 `0xFF`，表示电台无定位/GPS 关闭；解码器直接抛 `CivError`。
- 任何字段出现 `A–F` 的 BCD 半字节都视为非法，解码器会报错而不是猜值。

### 2.4 一次真实帧的逐字段解码

真机（USB COM6）抓到的第一帧：

```
FE FE E0 A4 23 00
37 52 31 30 01  01 12 32 56 00 01  00 78 70 00
01 50  00 00 09  20 26 11 07 14 26 14
FD
```

逐段解码：

| 原始字节 | 解码 |
|---|---|
| `37 52 31 30 01` | 37°52.313′ N |
| `01 12 32 56 00 01` | 112°32.56′ E |
| `00 78 70 00` | 787.0 m |
| `01 50` | 航向 150° |
| `00 00 09` | 0.9 km/h |
| `20 26 11 07 14 26 14` | 2026-11-07 14:26:14 UTC |

1 秒后的第二帧只有航向/速度/秒变化：`... 00 01 67 ...` → 航向 167°、速度 0.0、
UTC `14:26:15`。这两帧已固化进 `tests/test_civ.py` 作为回归测试。

---

## 3. 串口传输层

源码：`src/ic705timesync/serial_link.py`。

### 3.1 Windows 实现（零依赖）

Windows 下不用 pyserial，而是 `ctypes` 调 `kernel32`：

1. `CreateFileW("\\\\.\\COM6", GENERIC_READ|GENERIC_WRITE, ...)` 打开串口；
2. `BuildCommDCBW("baud=115200 parity=N data=8 stop=1")` 填 `DCB`；
3. **显式 `fDtrControl = 0`、`fRtsControl = 0`**，避免 DTR/RTS 电平被电台当成
   PTT/键控信号；
4. `SetCommTimeouts`：读间隔 20 ms、总超时 = `--timeout`；
5. `ReadFile` / `WriteFile` 收发字节。

非 Windows 回退到 pyserial（`_PyserialSerial`），但设置系统时钟等仍按平台分支。

### 3.2 为什么必须处理"回显"

`RemoteUty` 建的虚拟串口（实测 COM20，IcomVSerial）会把发出去的 CI-V 帧**原样
回显**；USB 直连一般不会。于是 `SerialLink.transmit()` 的接收循环：

- 丢弃 `candidate == frame`（请求帧回显）；
- 丢弃 `candidate[2] != 0xE0`（不是发给 PC 的帧）；
- 只接受以 `FD` 结尾、且至少 6 字节的完整帧。

这是 RemoteUty 虚拟串口能用的关键补丁之一。

### 3.3 时序与采样

每次 `transmit()` 记录：

- `t_send = time.monotonic()`（高精度，不受系统时间调整影响）；
- `wall_send = datetime.now(timezone.utc)`（用于和 GPS UTC 对齐）；
- `t_recv`（收到应答）。

RTT = `t_recv - t_send`，采样中点 `wall_mid = wall_send + RTT/2`，用来消除一半
传输延迟。见第 5 节。

---

## 4. Icom LAN（RS-BA1）协议

源码：`src/ic705timesync/lan_link.py`（封装 `icom-lan` 1.1.0，MIT）。

Icom 的 LAN 遥控协议没有公开文档，wfview 逆向出的实现是事实标准；`icom-lan`
在此基础上做了 Python 实现。本节按 wfview/icom-lan 的实现整理，并在真机上验证过
握手、鉴权和 CI-V。

### 4.1 端口

| 端口 | 用途 |
|---|---|
| UDP **50001** | 控制/发现/登录/会话（`base_port`） |
| UDP **50002** | CI-V 数据（`base_port + 1`，由 50001 的状态包告知） |
| UDP **50003** | 音频流（`base_port + 2`，本项目不订阅音频） |

IC-705 热点模式下电台 IP 默认 `192.168.59.1`；普通局域网则用路由器分配的 IP
（实测环境为 `192.168.0.104`）。发现阶段向 `255.255.255.255:50001` 广播。

### 4.2 通用 16 字节包头

所有控制包共享 16 字节头（小端，除 payloadsize 外）：

| 偏移 | 大小 | 字段 | 说明 |
|---|---|---|---|
| `0x00` | 4 | `len` | 包总长度（LE） |
| `0x04` | 2 | `type` | 包类型（LE）：`0x00` DATA、`0x01` CONTROL、`0x03` ARE_YOU_THERE、`0x04` I_AM_HERE、`0x05` DISCONNECT、`0x06` ARE_YOU_READY、`0x07` PING |
| `0x06` | 2 | `seq` | 序号（LE），重传/去重用 |
| `0x08` | 4 | `sender_id` | 发送方连接 ID（LE） |
| `0x0C` | 4 | `remote_id` | 接收方连接 ID（LE）；发现阶段未知填 0 |

> `sender_id` 不能为 0（IC-7610 会直接忽略）。`icom-lan` 用本地 UDP 端口派生
> `my_id = (local_port & 0xFFFF) | 0x10000`，保证非零且可复现。

### 4.3 发现：Are You There → I Am Here

PC 广播 `type=0x03`，电台回 `type=0x04`，并把**自己的连接 ID 放在 `sender_id`
位置**（这正是 `remote_id` 的来源）。真机抓包：

```
发送（PC -> 255.255.255.255:50001）：
10 00 00 00 03 00 00 00 C7 F8 06 C5 00 00 00 00
│len=16   │type=3  │seq=0   │sender_id  │remote_id=0

收到（电台 -> PC）：
10 00 00 00 04 00 00 00 74 05 ED C2 C7 F8 06 C5
│len=16   │type=4  │seq=0   │sender=0xC2ED0574（电台 ID）│receiver=0xC506F8C7（本机）
```

代码里 `LanLink.discover()` 直接调用 `icom_lan.backends.discovery.discover_lan_radios`，
返回 `{"host": ..., "remote_id": ...}`。

### 4.4 ARE_YOU_READY（0x06）

发现成功后，PC 发 `type=0x06`；电台会回若干控制包：`0x06`（ready）、`0x00`
（DATA，带递增序号）以及 `0x07` PING。实测片段（第一次握手）：

```
PC  -> 10 00 00 00 06 00 00 00 ...   type=0x06 (ARE_YOU_READY)
电台 -> 10 00 00 00 06 00 00 00 A3 43 B0 64 CB 62 CD 15
电台 -> 10 00 00 00 00 00 00 00 00 A3 43 B0 64 CB 62 CD 15
```

其中 `A3 43 B0 64` 是电台新连接 ID，`CB 62 CD 15` 是 PC 的 `sender_id`。
（电台还会发 `0x07` PING 和 `0x00` DATA 包，长度/序号各不同，此处从略。）
`0x06` 的应答不是必须的：有些固件不回显，代码会继续（"No explicit 'I Am Ready'
reply, proceeding"）。

### 4.5 登录包（0x80）

PC 发一个固定 0x80 字节的登录包：

| 偏移 | 大小 | 内容 |
|---|---|---|
| `0x00` | 4 | `0x80`（包长，LE） |
| `0x04` | 2 | `type = 0x0000` |
| `0x08` / `0x0C` | 4+4 | `sender_id` / `receiver_id` |
| `0x10` | 4 | payload size = `0x70`（BE） |
| `0x14` | 1 | `requestreply = 0x01` |
| `0x15` | 1 | `requesttype = 0x00`（login） |
| `0x16` | 2 | `innerseq`（BE） |
| `0x1A` | 2 | `tok_request`（随机 token 请求号，LE） |
| `0x40` | 16 | 混淆后的 **用户名** |
| `0x50` | 16 | 混淆后的 **密码** |
| `0x60` | 16 | 电脑名（ASCII，如 `icom-lan`） |

电台回一个 0x60 字节的应答：

| 偏移 | 内容 |
|---|---|
| `0x1A` | `tok_request` 回显 |
| `0x1C` | `token`（会话 token，后续 conninfo 要用） |
| `0x30` | `error`（LE）：`0` = 成功；`0xFEFFFFFF` / `0xFFFFFFFF` = 失败 |
| `0x40` | `connection_type` 字符串（如 `FTTH`、`WFVIEW`） |

实测错误应答（凭据错）里能在偏移 `0x30` 看到 `ff ff ff fe`（即 `0xFEFFFFFF`），
`AuthResponse.success = False`，`icom-lan` 抛 `AuthenticationError`。

### 4.6 conninfo（0x90）与状态（0x50）

登录成功后发 0x90 字节的 conninfo，告诉电台"我要用哪些端口、什么音频编码"：

| 偏移 | 内容 |
|---|---|
| `0x00` | `0x90` |
| `0x04` | `type = 0x0000` |
| `0x08` / `0x0C` | sender / receiver |
| `0x10` | payload size = `0x80`（BE） |
| `0x14` | `requestreply = 0x01` |
| `0x15` | `requesttype = 0x03`（conninfo） |
| `0x1A` / `0x1C` | `tok_request` / `token` |
| `0x20` | 16 字节 GUID（从电台的 conninfo 应答里取；无则用 MAC 兜底） |
| `0x27` | `commoncap = 0x8010`（无 GUID 时） |
| `0x2A` | 电台 MAC（6 字节） |
| `0x40` | 电台名（`IC-705`） |
| `0x60` | 混淆后的用户名 |
| `0x70` / `0x71` | rxenable / txenable = 1 |
| `0x72` / `0x73` | RX / TX 音频 codec（`0x04` = 16-bit PCM 单声道） |
| `0x74` / `0x78` | RX / TX 采样率（BE，48000） |
| `0x7C` / `0x80` | **本机 CI-V / 音频 UDP 端口**（BE） |
| `0x84` | TX buffer（ms） |
| `0x88` | convert = 1 |

电台用 0x50 字节状态包回应，告知实际分配的端口：

```
Status: civ_port=50002, audio_port=50003, error=0x00000000, disconnected=False
```

代码据此把 CI-V 通道切到 50002。如果 `civ_port == 0` 且 `error == 0xFFFFFFFF`，
说明会话被拒；详见 4.8。

### 4.7 凭据"混淆"：encodePassport 表

**这不是加密，也不是哈希，只是可逆的查表替换**，防的是肉眼/日志明文，不防抓包。
算法（来自 wfview `icomudpbase.h` 的 `passcode()`，`icom-lan` 的
`core/auth.py:PASSCODE_SEQUENCE`）：

```python
table = PASSCODE_SEQUENCE            # 159 项常量表（ASCII 凭据实际只用到前 127 项）
out = []
for i, ch in enumerate(text[:16]):
    p = ord(ch) + i                  # 位置相关：每个字符的索引随位置偏移
    if p > 126:
        p = 32 + p % 127             # 回绕（非 ASCII 字符会用到高索引）
    out.append(table[p])
```

性质：

- 逐字符替换，**相同明文在不同位置得到不同密文**；
- 最长 16 字符，超出截断；
- 没有密钥、没有随机数，表固定 ⇒ 知道表就能还原；
- 所以文档/日志里不要打印明文凭据，也不要把它当安全机制。

（本项目不在日志里打印凭据；本文档也隐去真实用户名/密码。）

### 4.8 会话互斥与冷却

电台同一时间**只允许一个 LAN 客户端会话**。上一次会话没释放（进程崩溃、网络断、
别的软件连着）时，新的 conninfo 会得到：

```
Status: civ_port=0, audio_port=50003, error=0xFFFFFFFF, disconnected=False
```

`icom-lan` 的处理策略：

1. 若 `error == 0xFFFFFFFF` 且当前是立体声 codec（IC-705 是单 RX），降级为
   `PCM_1CH_16BIT` 重试一次；
2. 单声道仍被拒 ⇒ 抛
   `Radio rejected session allocation ... A previous session may still be active. Wait 30-60s and retry.`；
3. 若只是 `civ_port=0`（还没就绪），进入 busy-retry：暂停后重发 conninfo，最多 3 次。

本项目 `LanLink.open()` 在外面再包一层：

- 捕获消息含 `rejected session` / `previous session` 的异常 → **sleep 35 秒后重试**，
  最多 3 次；
- `TimeoutError` / `OSError` → sleep 3 秒重试；
- 其余异常直接抛。

此外 `reconnect()` 会**复用旧的 `my_id`**，因为电台可能还缓存着"上一个 sender_id
的会话"，换新 ID 反而更容易被拒。

### 4.9 序号、重传、心跳

- 每个 tracked 包写 `seq`（`0x06` 偏移），送进 `tx_buffer`；
- 收到对端包时记录 `rx_last_seq`；发现缺口就请求重传（`BUFSIZE` 上限）；
- 每隔一段时间发 `0x07` PING（21 字节，带毫秒时间戳）保活；
- 无 tracked 包发送时，每 `IDLE_PERIOD` 发一个空 CONTROL 包维持 CI-V/音频会话；
- 断开时发 `0x05` DISCONNECT。

这些都在 `icom-lan.core.transport` 里，本项目不重复实现。

### 4.10 为什么用 icom-lan

LAN 协议没有官方文档，自己从零实现要处理：

- 16 字节头/大小端混用（长度 LE、payload size BE）；
- 登录 token、conninfo GUID/MAC、状态包里的动态端口；
- 序号、重传、PING、idle、会话冷却；
- 不同固件（IC-705/7300/7610/9700）的差异（单/双 RX、codec 支持）。

`icom-lan`（MIT）已经把这些踩平，并在 IC-705 上验证过登录/频率/型号。
本项目只做两件事：在它之上发/收 CI-V；把 GPS 27 字节解出来。

---

## 5. 时间估计：秒跳变法

源码：`src/ic705timesync/timecore.py`。

### 5.1 问题：只有整秒

`23 00` 的 UTC 精度是**整秒**，没有毫秒字段，也没有 PPS 秒脉冲。如果直接拿
"读到的秒 − 电脑时钟"，误差最大可达 **±0.5 s**。FT8 时隙 15 秒，±0.5 s 勉强
能用但不理想；而且采样时刻本身有 RTT 抖动。

### 5.2 原理

高频轮询，当观察到 GPS 秒数从 `S` 跳到 `S+1`：

- 真实整秒边界 `S+1` 一定落在**两次采样之间**；
- 两次采样各有自己的中点时刻（`wall_mid`，已做 RTT/2 修正）；
- 取两个中点的中点作为边界时刻的估计：

```
boundary = mid(prev) + [mid(cur) - mid(prev)] / 2
offset   = GPS 边界时刻(cur.utc) - boundary        # 正数 = 电脑慢了
```

多次跳变取 **中位数**（抗单次抖动）；不确定度取
`max(各跳变偏差的最大离散, 采样中点间隔中位数/2)`。

退化情况（一次跳变都没抓到）：用 **RTT 最小**的一次采样做粗估，
不确定度记为 `0.5 s + RTT/2`，并在结果里标明"粗估（未捕捉到秒跳变）"。

### 5.3 代码映射

```python
Sample(utc, t_send, t_recv, wall_send)
    .rtt      = t_recv - t_send
    .wall_mid = wall_send + rtt/2

collect_samples(link, count=25, interval=0.06)   # 默认 25 次 / 60 ms
estimate_offset(samples) -> OffsetEstimate(offset_seconds, uncertainty_seconds,
                                           method, samples, transitions)
apply_offset(offset_seconds)                     # 设置系统时间
```

`read` 子命令默认 25 采样 / 0.06 s；实测通常能抓到 1–2 次秒跳变。

### 5.4 精度与实测

| 路径 | 实测偏差 | 不确定度 |
|---|---|---|
| USB 直连 COM6 | +16 ~ +34 ms | ±26 ~ 31 ms |
| WiFi + RemoteUty 虚拟串口 COM20 | 同量级 | ±41 ~ 50 ms（多一跳网络+虚拟串口） |

精度上限由采样间隔（默认 60 ms）和传输抖动决定，不是由 GPS 本身决定。
对 FT8 完全够用；**不适合**毫秒/微秒级授时。

### 5.5 设置系统时钟

- 需要管理员权限（`SeSystemtimePrivilege`）；
- Windows 调 `SetSystemTime(SYSTEMTIME)`，毫秒取 `microsecond/1000`；
- 失败抛 `PermissionError`，`cli` 捕获后返回退出码 2；
- `sync --dry-run` 只打印目标时间，不落盘。

---

## 6. Windows asyncio 缺陷与会话重试

### 6.1 现象

在 Windows 上用 `icom-lan` 连电台，`connect()` 偶尔直接崩：

```
TypeError: IocpProactor.sendto() missing 1 required positional argument: 'addr'
```

### 6.2 根因

`asyncio` 的 Windows proactor 事件循环里，`create_datagram_endpoint(remote_addr=...)`
会创建一个**已连接**的 UDP socket。后续 `transport.sendto(data)`（不带 addr）在
`IocpProactor.sendto()` 里被实现为必须显式给 `addr`，于是 `addr=None` 时抛
`TypeError`。POSIX selector 实现允许 `sendto(data)` 走 `getpeername()`，Windows
proactor 没有这个兜底。

### 6.3 补丁

`lan_link.py` 在**模块导入时**给 `IocpProactor.sendto` 打补丁（幂等）：

```python
def patched(self, conn, buf, flags=0, addr=None):
    if addr is None:
        try:
            addr = conn.getpeername()      # 已连接 UDP 取对端地址
        except Exception:
            pass
    return original(self, conn, buf, flags, addr)

patched._ic705_patched = True
```

只在 `sys.platform == "win32"` 时生效；打补丁失败会静默跳过（不影响串口路径）。

### 6.4 会话拒绝重试

见 4.8。关键是**冷却**：电台释放旧会话需要时间（实测约 30–60 秒），立刻重试只会
继续被拒。代码用 35 秒 sleep + 最多 3 次；`--timeout` 控制单次操作超时。

---

## 7. 实测记录

### 7.1 环境

| 项 | 值 |
|---|---|
| 电台 | Icom IC-705，CI-V 地址 `0xA4`，固件版本未记录 |
| USB 串口 | `COM6`（CI-V 控制口），115200 8N1 |
| 虚拟串口 | `COM20`（RemoteUty / IcomVSerial） |
| 网络 | 电台普通局域网 IP `192.168.0.104`；热点模式默认 `192.168.59.1` |
| 主机 | Windows 11，Python 3.12 |
| 库 | `icom-lan==1.1.0`（MIT） |

### 7.2 USB：GPS 真实帧

请求 `FE FE A4 E0 23 00 FD`，两次连续读取（间隔约 1 秒）原始应答：

```
FE FE E0 A4 23 00 37 52 31 30 01 01 12 32 56 00 01 00 78 70 00 01 50 00 00 09 20 26 11 07 14 26 14 FD
FE FE E0 A4 23 00 37 52 31 30 01 01 12 32 56 00 01 00 78 70 00 01 67 00 00 00 20 26 11 07 14 26 15 FD
```

解码：

```
UTC  2026-11-07 14:26:14 -> 14:26:15
位置 37°52.313′N, 112°32.56′E，海拔 787.0 m
航向 150° -> 167°，速度 0.9 -> 0.0 km/h
```

`23 01` 数据源应答 `FE FE E0 A4 23 01 01 FD`，即 `01` = 内置 GPS。

时间估计实测输出：

```
GPS: 37.871867°N, 112.542650°E  海拔 793.2 m  航向 161°  速度 1.1 km/h  时间 2026-11-07 14:31:07 UTC
偏差估计: +16.7 ms (±26 ms, 秒跳变法, 30 采样/2 次秒跳变)
管理员权限: 否（sync 需要管理员）
```

### 7.3 LAN：发现成功

真实抓包（本机 `sender_id=0xC506F8C7`）：

```
local sid = 0xC506F8C7
sent ARE_YOU_THERE to ('255.255.255.255', 50001)
sent ARE_YOU_THERE to ('192.168.0.104', 50001)
reply 16 bytes:
10 00 00 00 04 00 00 00 74 05 ED C2 C7 F8 06 C5
type=0x0004, remote_id=0xC2ED0574
```

### 7.4 LAN：握手与登录成功

登录成功后 `icom-lan` 的日志（凭据已脱敏）：

```
INFO icom_lan.runtime._control_phase: Status: civ_port=50002, audio_port=50003, error=0x00000000, disconnected=False
INFO icom_lan.runtime._control_phase: Connected to 192.168.0.104 (control=50001, civ=50002)
CONNECT OK
```

登录失败时（错误凭据）应答是一个 0x60 字节状态包，偏移 `0x30` 处的错误码为
`0xFEFFFFFF`：

```
RX ... 96 bytes   （包头 len=0x60、type=0）
offset 0x30: ff ff ff fe        → error = 0xFEFFFFFF
FAIL AuthenticationError Authentication failed (error=0xFEFFFFFF)
```

成功后用同一个会话读频率、读型号：

```
RESULT FREQ  cmd=0x03 sub=None len(data)=5 data=00 40 17 44 01
RESULT ID    cmd=0x19 sub=0x00 data=a4
```

- `00 40 17 44 01` 是 BCD 小端频率 → `144 174 000 Hz` = **144.174 MHz**；
- `a4` = IC-705。

**这证明 LAN 的发现、登录、会话、CI-V 通道都是通的。**

### 7.5 LAN：GPS 失败（关键结论）

同一个会话里发 `23 00`（`send_civ`，6 秒超时），4 次全部超时：

```
CONNECT OK
ATTEMPT 1 ERR TimeoutError CI-V response timed out
ATTEMPT 2 ERR TimeoutError CI-V response timed out
ATTEMPT 3 ERR TimeoutError CI-V response timed out
ATTEMPT 4 ERR TimeoutError CI-V response timed out
```

`23 01`（数据源）同样超时。再用**被动监听** 25 秒，收到 0 个 `0x23` 帧：

```
CONNECT OK
（25 秒内没有任何 GPS 帧）
```

> **一个容易误判的细节**：调试日志里确实短暂出现过 4 帧 `0x23`，例如：
>
> ```
> DEBUG icom_lan.runtime._civ_rx: civ-rx: cmd=0x23 sub=0x00 to=0xE0
>   data=00375231300101123256100100793800016200000520261107162758
> ```
>
> 把这 28 字节的 `data` 取末 27 字节解码，得到 37°52.31′N / 112°32.56′E /
> 793.8 m / 航向 162° / 0.5 km/h / `2026-11-07 16:27:58 UTC`，格式完全正确。
> 但这几帧只出现在**连接建立瞬间**，随后新会话里再也收不到；判断是电台把上一次
> 会话缓存的包补发过来（或只投递给某个旧 socket），并**不是**对当前请求的应答，
> 也不是周期性推送。因此不能作为可靠数据源。

结论：

- 该固件的 LAN CI-V 通道**不响应 `23 00`**；
- 也没有稳定的 GPS 主动推送；
- 所以"纯 WiFi 直接取 GPS"在这台机器上不可用。这是**固件/协议侧限制**，
  不是解析或帧构造的问题（同一套 `decode_gps_position` 在 USB 帧上工作正常）。

本项目把两条尝试路径都保留在 `LanLink.read_gps()`：先显式请求，再等推送；
失败时给出明确中文错误，提示改用串口。

### 7.6 串口 vs LAN 对照

| 能力 | USB 串口 | RemoteUty 虚拟串口 | LAN 直连 |
|---|---|---|---|
| 发现/登录/会话 | — | 由 RemoteUty 负责 | ✅ |
| 读频率/型号 | ✅ | ✅ | ✅ |
| `23 00` GPS | ✅ | ✅ | ❌ 超时 |
| 授时（`sync`） | ✅ | ✅ | ❌ |

---

## 8. 局限与后续路线

### 8.1 已知限制

1. **LAN GPS 不可用**：见 7.5。只能等固件支持，或走串口/虚拟串口。
2. **整秒精度**：CI-V 没有 PPS/毫秒；秒跳变法把精度压到几十毫秒，上限由采样间隔
   和传输抖动决定。
3. **Windows 优先**：串口与设置系统时钟都是 Win32 API；其他平台只能读 GPS。
4. **权限**：`sync` 必须管理员。
5. **独占资源**：串口被 JTDX/RemoteUty 占用时打不开；电台 LAN 会话同一时间只允许
   一个客户端，未释放会拒绝新会话。
6. **GPS 未定位**：`23 00` 返回全 `0xFF`，解码器报错（不会给出错误时间）。

### 8.2 可行替代方案

| 方案 | 是否仍"无 USB" | 说明 |
|---|---|---|
| **A. USB 串口直连** | 否 | 最稳、零依赖；`sync --port COM6` |
| **B. WiFi + RemoteUty 虚拟串口** | 是 | 实测可用：RemoteUty 建 COM20，对时要在 JTDX 之前（虚拟串口独占） |
| **C. WiFi + wfview 会话/虚拟串口** | 是 | 原理同 B；wfview 也建虚拟串口，但会占 LAN 会话，二者别同时开 |
| **D. 等固件支持 LAN GPS** | 是 | 代码已就绪；只需电台能在 LAN CI-V 上应答 `23 00` |
| **E. 从电台 NTP/时钟取时** | 是 | 实测 `1A 05` 设置表里没有可读时钟项（76 秒无变化），不可行 |

其中 **B 是当前最实用的"纯 WiFi"路径**：先 RemoteUty 连电台 → 用 `--transport serial
--port COM20` 对时 → 再启动 JTDX。注意虚拟串口是独占的，顺序不能反。

### 8.3 如果将来要自己实现 LAN 协议

可从这些点入手（`icom-lan` 已实现，可作为参考）：

1. 发现：广播 `0x03`，从 `0x04` 的 `sender_id` 取 `remote_id`；
2. `0x06` ready → 0x80 login（token/混淆凭据）→ 0x90 conninfo（本机 CI-V 端口）；
3. 0x50 状态包确认 `civ_port`，切换到 50002；
4. 用 16 字节头 + 序号/重传维护 CI-V 帧；
5. 谨慎处理会话互斥与冷却。

---

## 9. 代码地图与复现实验

| 文件 | 职责 |
|---|---|
| `src/ic705timesync/civ.py` | 帧构造/提取/拆分、27 字节 GPS 解码、BCD 校验 |
| `src/ic705timesync/serial_link.py` | Windows ctypes 串口、回显过滤、`read_gps` |
| `src/ic705timesync/lan_link.py` | `icom-lan` 封装、proactor 补丁、会话重试、GPS 双路径 |
| `src/ic705timesync/timecore.py` | 采样、RTT 修正、秒跳变估计、SetSystemTime |
| `src/ic705timesync/nmea.py` | `$GPZDA` / `$GPRMC` / `$GPGGA` 合成与校验和 |
| `src/ic705timesync/cli.py` | `ports/discover/read/sync/watch/serve` |
| `tests/test_civ.py` | 用真机两帧验证解码（含秒跳变） |
| `tests/test_nmea_and_time.py` | NMEA 校验和、秒跳变估计（正/负偏差、退化） |
| `tests/test_lan_offline.py` | 不连电台，用假 radio 验证 LAN 帧构造与解码 |

离线复现（不需要电台）：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

联调复现：

```powershell
# USB
python -m ic705timesync read --transport serial --port COM6
# LAN（预期在本文所述固件上失败）
python -m ic705timesync read --transport lan --host 192.168.0.104 --user U --password P
```

---

## 10. 参考资料

- Icom《IC-705 CI-V Reference Guide》——GPS 位置 pp.21/25；`1A 05` 设置表
- [icom-lan](https://github.com/morozsm/icom-lan)（MIT）——Icom LAN/RS-BA1 的 Python 实现
- [wfview](https://wfview.org/)——Icom LAN 协议逆向的事实参考（`icomudpbase.h` 的
  passcode 表、`kControlPort=50001` / `kSerialPort=50002` / `kAudioPort=50003`）
- [wfview 论坛：IC-705 GPS Serial Port "B"](https://forum.wfview.org/t/ic-705-gps-serial-port-b/4292)
  —— 网络协议不携带 USB GPS 串口数据
- [wfview 论坛：Access point mode](https://forum.wfview.org/t/field-operation-aka-access-point-mode/712)
  —— 热点模式电台 IP `192.168.59.1`

---

*文档中的凭据均已脱敏。`192.168.x.x` 为私有地址，仅用于说明。*
