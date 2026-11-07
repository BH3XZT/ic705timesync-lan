"""ic705timesync-lan —— 用 IC-705 内置 GPS 给电脑授时（USB 串口 / WiFi LAN）。

核心链路：
    IC-705 内置 GPS
        └─ CI-V 0x23 0x00（USB 串口 或 WiFi 的 Icom LAN/RS-BA1 协议）
             └─ 解析出 UTC 年月日时分秒
                  └─ 秒跳变法估计偏差 → 设置系统时钟
                       └─ （可选）再以 NMEA 输出给 BktTimeSync 等软件

当前验证状态：串口路径已端到端可用；WiFi 路径登录/会话/普通 CI-V 可用，
但实测该固件在 LAN CI-V 通道上不返回 GPS（0x23）数据，详见 README 与
docs/TECHNICAL.md。
"""

from __future__ import annotations

__version__ = "0.2.0"

from .civ import (  # noqa: F401
    IC705_CIV_ADDRESS,
    CivError,
    GpsFix,
    decode_gps_position,
    read_gps_position_frame,
)

__all__ = [
    "__version__",
    "IC705_CIV_ADDRESS",
    "CivError",
    "GpsFix",
    "decode_gps_position",
    "read_gps_position_frame",
]
