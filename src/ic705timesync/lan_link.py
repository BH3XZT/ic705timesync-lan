"""WiFi（LAN）传输层：基于 ``icom-lan`` 走 Icom RS-BA1 协议。

为什么用 icom-lan：Icom 的 LAN 协议（UDP 50001/50002/50003、会话握手、
登录鉴权、重传层）没有公开文档，自己实现容易踩坑；icom-lan（MIT）已经
把传输层做稳。本项目只在它上面加一条"读 GPS"的最小通道。

实测状态（2026-11，IC-705，固件版本未记录）
--------------------------------------------
* 发现（Are You There / I Am Here）、ARE_YOU_READY、登录（0x80）、
  conninfo（0x90）均成功，能通过 LAN CI-V 读频率、读型号；
* 但经 LAN 的 CI-V 通道请求 ``23 00``（GPS）**没有应答**，也没有观察到
  电台主动推送 GPS 帧。串口路径同一命令有完整 27 字节应答。
  因此就本机固件而言，"纯 WiFi 取 GPS"目前不可用；代码保留两条尝试路径
  （先显式请求，再等推送帧），并在失败时给出明确错误。

Windows 注意事项
----------------
1. asyncio 的 IocpProactor 对"已连接 UDP socket"的 ``sendto(data)`` 会抛
   TypeError（addr=None），这里在导入时打补丁修复。
2. 会话是互斥的：上一次连接未释放时，电台会返回 session rejection；
   这里对 "rejected session" 做 35 秒冷却重试。

依赖：``pip install icom-lan``（打包版 exe 已内置）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import deque
from typing import Any

from .civ import (
    CMD_GPS,
    GPS_SUB_POSITION,
    CivError,
    decode_gps_position,
    extract_frame,
    parse_payload,
    read_gps_position_frame,
)

INSTALL_HINT = "WiFi 模式需要依赖 icom-lan，请先执行：pip install icom-lan"


def _patch_windows_proactor() -> None:
    """修 Windows proactor 上“连接式 UDP sendto(addr=None)”崩溃。"""
    if sys.platform != "win32":
        return
    try:
        import asyncio.windows_events as windows_events

        original = windows_events.IocpProactor.sendto
        if getattr(original, "_ic705_patched", False):
            return

        def patched(self, conn, buf, flags=0, addr=None):
            if addr is None:
                try:
                    addr = conn.getpeername()
                except Exception:
                    pass
            return original(self, conn, buf, flags, addr)

        patched._ic705_patched = True  # type: ignore[attr-defined]
        windows_events.IocpProactor.sendto = patched
    except Exception:
        pass


_patch_windows_proactor()


class LanLink:
    """IC-705 的 WiFi 连接，暴露与 SerialLink 相同的 ``read_gps()``。"""

    def __init__(
        self,
        host: str,
        port: int = 50001,
        username: str = "",
        password: str = "",
        radio_addr: int = 0xA4,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.radio_addr = radio_addr
        self.timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._radio: Any = None
        self._gps_frames: deque[bytes] = deque(maxlen=50)

    # -- 生命周期 ---------------------------------------------------------
    def open(self) -> "LanLink":
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self._open_once()
                return self
            except Exception as exc:  # 电台可能还在释放上一次会话
                last_error = exc
                self._cleanup()
                message = str(exc)
                if "rejected session" in message or "previous session" in message:
                    time.sleep(35)
                    continue
                if isinstance(exc, (TimeoutError, OSError)) and attempt < 2:
                    time.sleep(3)
                    continue
                raise
        raise last_error if last_error else RuntimeError("LAN 连接失败")

    def _open_once(self) -> None:
        try:
            from icom_lan import LanBackendConfig, create_radio
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError(INSTALL_HINT) from exc

        self._loop = asyncio.new_event_loop()
        config = LanBackendConfig(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            radio_addr=self.radio_addr,
            timeout=self.timeout,
            model="IC-705",
        )
        self._radio = create_radio(config)
        self._run(self._radio.connect())
        self._install_gps_tap()

    def _install_gps_tap(self) -> None:
        """尽力监听电台推送到 CI-V 事件流里的 0x23 GPS 帧。

        注意：这是**best-effort**。实测（icom-lan 1.1.0 / 本机固件）即使 DEBUG
        日志里能看到 civ-rx 的 0x23 帧，这个私有钩子也没有把帧送到这里，因此
        ``read_gps()`` 依赖它时仍会超时。保留它不增加风险（异常全部吞掉），
        未来库版本若暴露公开的 CI-V 事件订阅接口，可以替换成正式订阅。
        """
        try:
            runtime = self._radio._civ_runtime
            original = runtime._publish_civ_event
            frames = self._gps_frames

            def patched(event):
                try:
                    frame = getattr(event, "frame", None)
                    if frame is not None and getattr(frame, "command", None) == 0x23:
                        frames.append(bytes(getattr(frame, "data", b"")))
                except Exception:
                    pass
                return original(event)

            runtime._publish_civ_event = patched
        except Exception:
            pass

    def _cleanup(self) -> None:
        try:
            if self._radio is not None and self._loop is not None:
                self._run(self._radio.disconnect())
        except Exception:
            pass
        finally:
            if self._loop is not None:
                try:
                    self._loop.close()
                except Exception:
                    pass
            self._radio = None
            self._loop = None

    def close(self) -> None:
        self._cleanup()

    def __enter__(self) -> "LanLink":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 内部 -------------------------------------------------------------
    def _run(self, coro):
        if self._loop is None:
            raise RuntimeError("LAN 连接未打开")
        return self._loop.run_until_complete(coro)

    # -- 读写 -------------------------------------------------------------
    def transmit(self, frame: bytes) -> bytes:
        """把一条完整 CI-V 帧通过 LAN 发出去，并把应答还原成同样的帧格式。"""
        if self._radio is None:
            raise RuntimeError("LAN 连接未打开")
        body = frame[4:-1]
        if not body:
            raise CivError("CI-V 帧体为空")
        command = body[0]
        rest = body[1:]
        sub = rest[0] if rest else None
        data = bytes(rest[1:]) if len(rest) > 1 else b""
        resp = self._run(self._radio.send_civ(command, sub, data or None))
        if resp is None:
            raise CivError("电台没有应答")
        out = bytearray(b"\xfe\xfe")
        out += bytes((0xE0, self.radio_addr, command))
        if sub is not None:
            out.append(sub)
        out += bytes(getattr(resp, "data", b""))
        out.append(0xFD)
        return bytes(out)

    def read_gps(self):
        """读 GPS 位置/时间。

        先尝试显式发送 ``23 00``（与串口路径完全相同的 CI-V 命令）；若电台
        没有应答，再等待它主动推送的 ``0x23`` 帧（``_install_gps_tap``）。
        两条路径都不行时抛 ``CivError``。
        """
        if self._radio is None:
            raise RuntimeError("LAN 连接未打开")

        # 路径 1：显式请求（现有固件在 LAN 通道上通常无应答）
        if not self._gps_frames:
            try:
                return self._read_gps_actively()
            except (CivError, TimeoutError, OSError):
                pass

        # 路径 2：等电台主动推送
        async def _wait_next():
            loop = asyncio.get_event_loop()
            end = loop.time() + max(8.0, float(self.timeout) + 6.0)
            while loop.time() < end:
                if self._gps_frames:
                    return decode_gps_position(self._pop_gps_payload())
                await asyncio.sleep(0.05)
            raise CivError(
                "LAN 通道没有返回 GPS 数据：显式请求 23 00 无应答，"
                "也未收到电台推送。该 IC-705 固件的 LAN CI-V 通道不提供 "
                "0x23 GPS 数据，请改用 USB 串口（--transport serial）。"
            )

        return self._run(_wait_next())

    def _read_gps_actively(self):
        raw = self.transmit(read_gps_position_frame(to_addr=self.radio_addr))
        frame = extract_frame(raw, from_addr=self.radio_addr)
        command, sub, data = parse_payload(frame)
        if command != CMD_GPS or sub != GPS_SUB_POSITION:
            raise CivError(f"应答不是 GPS 位置: {frame.hex(' ')}")
        return decode_gps_position(data)

    def _pop_gps_payload(self) -> bytes:
        raw = self._gps_frames.popleft()
        return raw[-27:] if len(raw) >= 27 else raw

    # -- 发现 -------------------------------------------------------------
    @staticmethod
    def discover(timeout: float = 3.0) -> list[dict[str, Any]]:
        """广播发现局域网内的 Icom 电台。"""
        try:
            from icom_lan.backends.discovery import discover_lan_radios
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError(INSTALL_HINT) from exc

        async def _discover() -> list[dict[str, Any]]:
            return await discover_lan_radios(timeout=timeout)

        return asyncio.run(_discover())
