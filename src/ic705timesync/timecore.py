"""时间核心：采样、偏差估计、设置系统时钟。

关键点
------
CI-V ``23 00`` 只给到**整秒**的 UTC。如果直接用"读到的秒 − 电脑时钟"，
误差可达 ±0.5 s（FT8 够用但不理想）。这里用"秒跳变法"把精度提到
几十毫秒：高频轮询，观察到秒数从 S 跳到 S+1 的那次，说明真实边界
（整秒 S+1）落在两次采样之间，取两次采样中点的中点作为边界时刻。

    offset = GPS 时间 − 电脑时间        （秒，正数表示电脑慢了）
"""

from __future__ import annotations

import ctypes
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Sample:
    """一次 GPS 读取：UTC 秒 + 发送/接收的单调时钟时刻。"""

    utc: datetime
    t_send: float
    t_recv: float
    wall_send: datetime

    @property
    def rtt(self) -> float:
        return self.t_recv - self.t_send

    @property
    def wall_mid(self) -> datetime:
        return self.wall_send + timedelta(seconds=self.rtt / 2)


@dataclass
class OffsetEstimate:
    """偏差估计结果。"""

    offset_seconds: float
    uncertainty_seconds: float
    method: str
    samples: int = 0
    transitions: int = 0

    @property
    def offset_ms(self) -> float:
        return self.offset_seconds * 1000.0

    @property
    def uncertainty_ms(self) -> float:
        return self.uncertainty_seconds * 1000.0


def take_sample(link) -> Sample:
    """对传输层做一次计时读取。"""
    wall_send = datetime.now(timezone.utc)
    t_send = time.monotonic()
    fix = link.read_gps()
    t_recv = time.monotonic()
    if fix.utc is None:
        raise ValueError("GPS 应答中没有 UTC 时间（可能未定位）")
    return Sample(utc=fix.utc, t_send=t_send, t_recv=t_recv, wall_send=wall_send)


def collect_samples(link, count: int = 12, interval: float = 0.1) -> list[Sample]:
    """连续采样，用于秒跳变检测。"""
    samples: list[Sample] = []
    for _ in range(count):
        samples.append(take_sample(link))
        time.sleep(interval)
    return samples


def estimate_offset(samples: list[Sample]) -> OffsetEstimate:
    """由采样序列估计 GPS−PC 偏差。"""
    if not samples:
        raise ValueError("没有采样数据")

    # 秒跳变法：相邻两次采样 UTC 正好相差 1 秒
    boundaries: list[float] = []  # 每个跳变边界对应的 (PC 墙钟 - GPS 边界) 秒差
    for prev, cur in zip(samples, samples[1:]):
        delta = (cur.utc - prev.utc).total_seconds()
        if delta != 1.0:
            continue
        # 边界真实时刻落在两次采样中点之间
        mid_prev = prev.wall_mid
        mid_cur = cur.wall_mid
        boundary_wall = mid_prev + (mid_cur - mid_prev) / 2
        # cur.utc 就是新一秒的起点（GPS 绝对时刻）
        boundaries.append((cur.utc - boundary_wall).total_seconds())

    if boundaries:
        offset = statistics.median(boundaries)
        worst = max(abs(b - offset) for b in boundaries)
        # 采样间隔决定的分辨率：取相邻采样中点的最大间隔的一半
        gaps = [
            (cur.wall_mid - prev.wall_mid).total_seconds()
            for prev, cur in zip(samples, samples[1:])
        ]
        resolution = (statistics.median(gaps) / 2.0) if gaps else 0.05
        uncertainty = max(worst, resolution)
        return OffsetEstimate(
            offset_seconds=offset,
            uncertainty_seconds=uncertainty,
            method="秒跳变",
            samples=len(samples),
            transitions=len(boundaries),
        )

    # 退化：没有观察到秒跳变，用 RTT 最小的一次做粗估（±0.5 s）
    best = min(samples, key=lambda s: s.rtt)
    offset = (best.utc - best.wall_mid).total_seconds()
    return OffsetEstimate(
        offset_seconds=offset,
        uncertainty_seconds=0.5 + best.rtt / 2.0,
        method="粗估（未捕捉到秒跳变）",
        samples=len(samples),
        transitions=0,
    )


# ---------------------------------------------------------------------------
# 设置系统时钟
# ---------------------------------------------------------------------------


def is_admin() -> bool:
    """当前进程是否具备管理员权限（Windows 下设置时间需要）。"""
    if sys.platform != "win32":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _enable_systemtime_privilege() -> None:
    """打开 SeSystemtimePrivilege。"""
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x00000002

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", ctypes.c_ulong)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", ctypes.c_ulong), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken 失败")

    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(None, "SeSystemtimePrivilege", ctypes.byref(luid)):
        raise OSError(ctypes.get_last_error(), "LookupPrivilegeValue 失败")

    tp = TOKEN_PRIVILEGES()
    tp.PrivilegeCount = 1
    tp.Privileges[0].Luid = luid
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    if not advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None):
        raise OSError(ctypes.get_last_error(), "AdjustTokenPrivileges 失败")


def set_system_time_utc(target: datetime) -> None:
    """把系统时间设为 ``target``（UTC）。

    需要管理员权限；失败抛 PermissionError 并附带提示。
    """
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    naive = target.astimezone(timezone.utc).replace(tzinfo=None)

    if sys.platform != "win32":
        if not is_admin():
            raise PermissionError("设置系统时间需要 root 权限")
        import subprocess

        subprocess.run(
            ["date", "-u", "-s", naive.strftime("%Y-%m-%d %H:%M:%S")], check=True
        )
        return

    if not is_admin():
        raise PermissionError(
            "设置系统时间需要管理员权限：请以管理员身份打开终端后重试"
        )

    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [
            ("wYear", ctypes.c_ushort),
            ("wMonth", ctypes.c_ushort),
            ("wDayOfWeek", ctypes.c_ushort),
            ("wDay", ctypes.c_ushort),
            ("wHour", ctypes.c_ushort),
            ("wMinute", ctypes.c_ushort),
            ("wSecond", ctypes.c_ushort),
            ("wMilliseconds", ctypes.c_ushort),
        ]

    st = SYSTEMTIME(
        naive.year, naive.month, 0, naive.day, naive.hour, naive.minute, naive.second,
        int(naive.microsecond / 1000),
    )
    _enable_systemtime_privilege()
    if not ctypes.windll.kernel32.SetSystemTime(ctypes.byref(st)):
        raise OSError(ctypes.get_last_error(), "SetSystemTime 失败")


def apply_offset(offset_seconds: float) -> datetime:
    """按偏差把系统时间调到 GPS 时间，返回设置的目标时间。"""
    target = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    set_system_time_utc(target)
    return target
