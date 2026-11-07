"""CI-V 编解码：IC-705 的 GPS 位置/时间命令。

IC-705 通过 CI-V 命令 ``0x23`` 暴露内置 GPS：

    -> FE FE A4 E0 23 00 FD            读 GPS 位置（含 UTC 时间）
    -> FE FE A4 E0 23 01 FD            读 GPS 数据源（00=关, 01=内置, 03=手动）
    -> FE FE A4 E0 23 02 ... FD        写手动位置（本项目不用）

``23 00`` 的应答数据为 27 字节（去掉帧头、命令、子命令和 ``FD`` 之后），
字段顺序与 ICOM《IC-705 CI-V Reference Guide》pp.21/25 一致：

    纬度 5 字节   DD MM mm m0 0H     H=0 南 / 1 北
    经度 6 字节   0D DD MM mm m0 0H  H=0 西 / 1 东
    海拔 4 字节   0000.0 m（末字节低半字节=1 表示负值）
    航向 2 字节   0..360 度
    速度 3 字节   0.1 km/h
    日期 7 字节   年(2 字节) 月 日 时 分 秒 —— 全部 BCD，UTC

其中"日期 7 字节"就是本项目的时间源：**秒级 UTC，无需 USB 的 GPS 串口**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# CI-V 地址
IC705_CIV_ADDRESS = 0xA4          # IC-705 出厂默认 CI-V 地址
CONTROLLER_ADDRESS = 0xE0         # PC / 控制器地址

# 命令
CMD_GPS = 0x23                    # GPS 命令族
GPS_SUB_POSITION = 0x00           # 读位置 + UTC 时间
GPS_SUB_SOURCE = 0x01             # 读数据源
GPS_SUB_MANUAL = 0x02             # 写手动位置

GPS_POSITION_PAYLOAD_LEN = 27

FRAME_DELIMITER = 0xFD
PREAMBLE = b"\xFE\xFE"

CMD_READ_ID = 0x19                # 19 00 -> 型号 ID
CMD_READ_FREQ = 0x03
CMD_READ_MODE = 0x04


class CivError(Exception):
    """CI-V 帧格式或取值错误。"""


@dataclass(frozen=True)
class GpsFix:
    """一次 GPS 位置/时间快照。"""

    latitude: float
    longitude: float
    altitude_m: float | None
    course_deg: int | None
    speed_kmh: float | None
    utc: datetime | None            # 带 tzinfo=UTC 的时间（秒级）
    source: int | None = None       # 23 01 的结果：0 关 / 1 内置 / 3 手动

    def to_nmea(self) -> list[str]:
        """把定位数据转成 NMEA 语句（供虚拟串口 / TCP 使用）。"""
        from .nmea import from_fix

        return from_fix(self)


# ---------------------------------------------------------------------------
# 帧构造 / 解析
# ---------------------------------------------------------------------------


def build_frame(
    command: int,
    sub: int | None = None,
    data: bytes = b"",
    *,
    to_addr: int = IC705_CIV_ADDRESS,
    from_addr: int = CONTROLLER_ADDRESS,
) -> bytes:
    """构造一条 CI-V 帧：FE FE <to> <from> <cmd> [<sub>] [data] FD。"""
    frame = bytearray(PREAMBLE)
    frame += bytes((to_addr & 0xFF, from_addr & 0xFF, command & 0xFF))
    if sub is not None:
        frame.append(sub & 0xFF)
    frame += data
    frame.append(FRAME_DELIMITER)
    return bytes(frame)


def read_gps_position_frame(*, to_addr: int = IC705_CIV_ADDRESS) -> bytes:
    return build_frame(CMD_GPS, GPS_SUB_POSITION, to_addr=to_addr)


def read_gps_source_frame(*, to_addr: int = IC705_CIV_ADDRESS) -> bytes:
    return build_frame(CMD_GPS, GPS_SUB_SOURCE, to_addr=to_addr)


def extract_frame(raw: bytes, *, from_addr: int = IC705_CIV_ADDRESS) -> bytes:
    """从一串原始字节里取出 CI-V 应答帧，返回 FE 开头、FD 结尾的部分。

    兼容 USB 串口（可能带杂字节）与网络传输（无杂字节）。
    """
    start = raw.find(PREAMBLE)
    if start < 0:
        raise CivError("未找到 CI-V 帧头 FE FE")
    end = raw.find(bytes((FRAME_DELIMITER,)), start + 2)
    if end < 0:
        raise CivError("CI-V 帧缺少结束符 FD")
    frame = raw[start : end + 1]
    if len(frame) < 6:
        raise CivError(f"CI-V 帧过短: {frame.hex(' ')}")
    # FE FE <to> <from> ...
    if frame[3] != from_addr:
        # 接受任意来源，但把异常来源报出来便于排错
        raise CivError(f"应答来源地址不是 0x{from_addr:02X}: {frame.hex(' ')}")
    return frame


def parse_payload(frame: bytes) -> tuple[int, int | None, bytes]:
    """把完整帧拆成 (command, sub, data)。"""
    body = frame[4:-1]  # 去掉 FE FE to from 和 FD
    if not body:
        raise CivError("CI-V 帧体为空")
    command = body[0]
    rest = body[1:]
    if not rest:
        return command, None, b""
    # GPS / SET 等命令族都带子命令；其它命令的 rest 就是数据
    sub = rest[0]
    return command, sub, rest[1:]


# ---------------------------------------------------------------------------
# BCD 工具
# ---------------------------------------------------------------------------


def _bcd(byte: int) -> int:
    hi, lo = (byte >> 4) & 0x0F, byte & 0x0F
    if hi > 9 or lo > 9:
        raise CivError(f"非法 BCD 字节 0x{byte:02X}")
    return hi * 10 + lo


def _is_bcd(byte: int) -> bool:
    return ((byte >> 4) & 0x0F) <= 9 and (byte & 0x0F) <= 9


def _bcd_span(data: bytes) -> int:
    value = 0
    for byte in data:
        value = value * 100 + _bcd(byte)
    return value


def _all_ff(data: bytes) -> bool:
    return all(b == 0xFF for b in data)


# ---------------------------------------------------------------------------
# GPS 位置解码
# ---------------------------------------------------------------------------


def decode_gps_position(payload: bytes) -> GpsFix:
    """解码 ``23 00`` 的 27 字节数据。

    Raises:
        CivError: 字段不是合法 BCD、越界，或整帧为 0xFF（无定位）。
    """
    if len(payload) != GPS_POSITION_PAYLOAD_LEN:
        raise CivError(
            f"GPS 位置数据长度应为 {GPS_POSITION_PAYLOAD_LEN} 字节，实际 {len(payload)}: "
            f"{payload.hex(' ')}"
        )
    if _all_ff(payload):
        raise CivError("GPS 位置数据全为 0xFF（电台无定位或 GPS 未开启）")

    data = payload

    # --- 纬度 DD MM mm m0 0H ------------------------------------------------
    for i in (0, 1, 2):
        if not _is_bcd(data[i]):
            raise CivError(f"纬度第 {i} 字节非法 BCD: 0x{data[i]:02X}")
    if (data[3] & 0x0F) != 0 or ((data[3] >> 4) & 0x0F) > 9:
        raise CivError(f"纬度第 3 字节格式错误: 0x{data[3]:02X}")
    if (data[4] & 0xF0) != 0 or (data[4] & 0x0F) > 1:
        raise CivError(f"纬度半球字节格式错误: 0x{data[4]:02X}")

    lat_deg = _bcd(data[0])
    lat_min = _bcd(data[1]) + _bcd(data[2]) / 100.0 + ((data[3] >> 4) & 0x0F) / 1000.0
    if lat_deg > 90 or lat_min >= 60.0 or (lat_deg == 90 and lat_min > 0.0):
        raise CivError(f"纬度越界: {lat_deg} 度 {lat_min} 分")
    latitude = lat_deg + lat_min / 60.0
    if (data[4] & 0x0F) == 0:
        latitude = -latitude

    # --- 经度 0D DD MM mm m0 0H --------------------------------------------
    for i in range(5, 10):
        if not _is_bcd(data[i]):
            raise CivError(f"经度第 {i} 字节非法 BCD: 0x{data[i]:02X}")
    if (data[5] & 0xF0) != 0 or (data[9] & 0x0F) != 0:
        raise CivError("经度字节半字节格式错误")
    if (data[10] & 0xF0) != 0 or (data[10] & 0x0F) > 1:
        raise CivError(f"经度半球字节格式错误: 0x{data[10]:02X}")

    lon_deg = (data[5] & 0x0F) * 100 + _bcd(data[6])
    lon_min = _bcd(data[7]) + _bcd(data[8]) / 100.0 + ((data[9] >> 4) & 0x0F) / 1000.0
    if lon_deg > 180 or lon_min >= 60.0 or (lon_deg == 180 and lon_min > 0.0):
        raise CivError(f"经度越界: {lon_deg} 度 {lon_min} 分")
    longitude = lon_deg + lon_min / 60.0
    if (data[10] & 0x0F) == 0:
        longitude = -longitude

    # --- 海拔 ---------------------------------------------------------------
    altitude: float | None = None
    alt_span = data[11:15]
    if not _all_ff(alt_span):
        if not (_is_bcd(data[11]) and _is_bcd(data[12]) and _is_bcd(data[13])):
            raise CivError("海拔 BCD 非法")
        if (data[14] & 0xF0) != 0 or (data[14] & 0x0F) > 1:
            raise CivError(f"海拔符号字节格式错误: 0x{data[14]:02X}")
        metres = (_bcd(data[11]) * 10000 + _bcd(data[12]) * 100 + _bcd(data[13])) / 10.0
        if (data[14] & 0x0F) != 0:
            metres = -metres
        altitude = metres

    # --- 航向 / 速度 --------------------------------------------------------
    course: int | None = None
    if not _all_ff(data[15:17]) and all(_is_bcd(b) for b in data[15:17]):
        value = _bcd_span(data[15:17])
        if value <= 360:
            course = value

    speed: float | None = None
    if not _all_ff(data[17:20]) and all(_is_bcd(b) for b in data[17:20]):
        speed = _bcd_span(data[17:20]) / 10.0

    # --- UTC 日期时间 -------------------------------------------------------
    utc: datetime | None = None
    dt_span = data[20:27]
    if not _all_ff(dt_span) and all(_is_bcd(b) for b in dt_span):
        year = _bcd(data[20]) * 100 + _bcd(data[21])
        month, day = _bcd(data[22]), _bcd(data[23])
        hour, minute, second = _bcd(data[24]), _bcd(data[25]), _bcd(data[26])
        try:
            utc = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except ValueError as exc:  # 日期字段越界
            raise CivError(f"GPS 时间字段越界: {exc}") from exc

    return GpsFix(
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude,
        course_deg=course,
        speed_kmh=speed,
        utc=utc,
    )
