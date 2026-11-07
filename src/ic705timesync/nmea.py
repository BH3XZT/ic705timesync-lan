"""NMEA 0183 语句合成。

用途：把 IC-705 的 GPS 数据重新包装成标准 NMEA，喂给
BktTimeSync / NMEATime2 / 自研虚拟串口等只认 NMEA 的软件。

IC-705 自己的 USB "GPS 串口 B" 输出的就是这类语句；本模块让 WiFi
（CI-V）路径也能产出等价数据，从而实现"改一个传输层、上层软件不变"。
"""

from __future__ import annotations

from datetime import datetime, timezone

from .civ import GpsFix


def _checksum(body: str) -> str:
    value = 0
    for ch in body:
        value ^= ord(ch)
    return f"{value:02X}"


def sentence(body: str) -> str:
    """给不含 $ 和校验和的语句体加上 $...*XX 包装。"""
    return f"${body}*{_checksum(body)}\r\n"


def _fmt_lat(value: float) -> tuple[str, str]:
    hemi = "N" if value >= 0 else "S"
    value = abs(value)
    deg = int(value)
    minutes = (value - deg) * 60.0
    return f"{deg:02d}{minutes:07.4f}", hemi


def _fmt_lon(value: float) -> tuple[str, str]:
    hemi = "E" if value >= 0 else "W"
    value = abs(value)
    deg = int(value)
    minutes = (value - deg) * 60.0
    return f"{deg:03d}{minutes:07.4f}", hemi


def gprmc(fix: GpsFix, utc: datetime | None = None) -> str:
    """$GPRMC —— 推荐最小定位信息（含 UTC 日期时间）。"""
    when = utc or fix.utc
    if when is None:
        raise ValueError("缺少 UTC 时间，无法生成 GPRMC")
    when = when.astimezone(timezone.utc)
    lat, ns = _fmt_lat(fix.latitude)
    lon, ew = _fmt_lon(fix.longitude)
    speed_knots = (fix.speed_kmh or 0.0) / 1.852
    course = fix.course_deg if fix.course_deg is not None else 0
    body = (
        f"GPRMC,{when:%H%M%S}.00,A,{lat},{ns},{lon},{ew},"
        f"{speed_knots:.1f},{course:03d},{when:%d%m%y},,,A"
    )
    return sentence(body)


def gpzda(fix: GpsFix, utc: datetime | None = None) -> str:
    """$GPZDA —— UTC 日期时间（对时软件最常吃的一条）。"""
    when = utc or fix.utc
    if when is None:
        raise ValueError("缺少 UTC 时间，无法生成 ZDA")
    when = when.astimezone(timezone.utc)
    body = f"GPZDA,{when:%H%M%S}.00,{when:%d},{when:%m},{when:%Y},,"
    return sentence(body)


def gga(fix: GpsFix, utc: datetime | None = None) -> str:
    """$GPGGA —— 定位质量、星数等（星数未知时填 0）。"""
    when = utc or fix.utc
    if when is None:
        raise ValueError("缺少 UTC 时间，无法生成 GGA")
    when = when.astimezone(timezone.utc)
    lat, ns = _fmt_lat(fix.latitude)
    lon, ew = _fmt_lon(fix.longitude)
    alt = fix.altitude_m if fix.altitude_m is not None else 0.0
    body = (
        f"GPGGA,{when:%H%M%S}.00,{lat},{ns},{lon},{ew},1,00,1.0,"
        f"{alt:.1f},M,0.0,M,,"
    )
    return sentence(body)


def from_fix(fix: GpsFix, utc: datetime | None = None) -> list[str]:
    """生成一组 NMEA 语句（ZDA + RMC + GGA）。"""
    return [gpzda(fix, utc), gprmc(fix, utc), gga(fix, utc)]


def blank_sentences() -> list[str]:
    """无定位时的占位语句（RMC 状态为 V），避免下游软件因无数据超时。"""
    return [
        sentence("GPZDA,,,,,,,"),
        sentence("GPRMC,,V,,,,,,,,,,"),
    ]
