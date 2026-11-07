"""串口传输层（USB）：零依赖的 Windows 串口实现 + pyserial 回退。

为什么不直接用 pyserial：本项目希望"插上 USB 就能跑"，不强制装包；
Windows 上通过 ctypes 调用 kernel32 已足够。装了 pyserial 时会优先使用它。
"""

from __future__ import annotations

import sys
import time

from .civ import CivError, extract_frame, parse_payload


def list_com_ports() -> list[str]:
    """列出本机串口（Windows 走注册表，其它平台走 pyserial）。"""
    found: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM"
            ) as key:
                index = 0
                while True:
                    try:
                        _name, value, _type = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    found.append(str(value))
                    index += 1
        except OSError:
            pass
    if not found:
        try:
            from serial.tools import list_ports  # type: ignore

            found = [p.device for p in list_ports.comports()]
        except Exception:
            pass

    def sort_key(name: str) -> tuple[int, str]:
        digits = "".join(ch for ch in name if ch.isdigit())
        return (int(digits) if digits else 9999, name)

    return sorted(set(found), key=sort_key)


class SerialLink:
    """打开一个串口，提供 ``read_gps()`` 同步接口。"""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._impl: _SerialImpl | None = None

    # -- 生命周期 ---------------------------------------------------------
    def open(self) -> "SerialLink":
        if sys.platform == "win32":
            self._impl = _WindowsSerial(self.port, self.baud, self.timeout)
        else:
            self._impl = _PyserialSerial(self.port, self.baud, self.timeout)
        self._impl.open()
        return self

    def close(self) -> None:
        if self._impl is not None:
            self._impl.close()
            self._impl = None

    def __enter__(self) -> "SerialLink":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 读写 -------------------------------------------------------------
    def transmit(self, frame: bytes, *, expect_len: int = 0) -> bytes:
        """发一帧并读到电台的应答，返回原始字节。

        注意：RS-BA1 虚拟串口（RemoteUty 建的 COM 口）以及部分 CI-V 设置会
        **回显请求帧**。这里会丢弃"等于请求帧本身"或"不是发给控制器（0xE0）"
        的帧，继续等到真正的应答。
        """
        if self._impl is None:
            raise RuntimeError("串口未打开")
        self._impl.purge_input()
        self._impl.write(frame)
        deadline = time.monotonic() + self.timeout
        raw = bytearray()
        while time.monotonic() < deadline:
            chunk = self._impl.read(1)
            if not chunk:
                time.sleep(0.002)
                continue
            raw += chunk
            if chunk != b"\xfd":
                continue
            # 从缓冲里截出最后一个完整帧
            start = raw.rfind(b"\xfe\xfe")
            if start < 0:
                raw = bytearray()
                continue
            candidate = bytes(raw[start:])
            raw = bytearray()
            if len(candidate) < 6:
                continue
            if candidate == frame:
                continue                    # 请求帧回显，忽略
            if candidate[2] != 0xE0:
                continue                    # 不是发给控制器的，忽略
            return candidate
        raise CivError(f"串口 {self.port} 等待应答超时")

    def read_gps(self) -> object:
        """读一次 GPS 位置（含 UTC 时间）。"""
        from .civ import CMD_GPS, GPS_SUB_POSITION, decode_gps_position, read_gps_position_frame

        raw = self.transmit(read_gps_position_frame())
        frame = extract_frame(raw)
        command, sub, data = parse_payload(frame)
        if command != CMD_GPS or sub != GPS_SUB_POSITION:
            raise CivError(f"应答不是 GPS 位置: {frame.hex(' ')}")
        return decode_gps_position(data)


# ---------------------------------------------------------------------------
# Windows ctypes 实现
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - 平台相关
    import ctypes
    from ctypes import wintypes as wt

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
    PURGE_RXCLEAR = 0x0008
    PURGE_TXCLEAR = 0x0004

    class _DCB(ctypes.Structure):
        # 32 位标志位用 c_uint 位域；其余字段按 Win32 DCB 布局排列
        _fields_ = [
            ("DCBlength", wt.DWORD),
            ("BaudRate", wt.DWORD),
            ("fBinary", ctypes.c_uint, 1),
            ("fParity", ctypes.c_uint, 1),
            ("fOutxCtsFlow", ctypes.c_uint, 1),
            ("fOutxDsrFlow", ctypes.c_uint, 1),
            ("fDtrControl", ctypes.c_uint, 2),
            ("fDsrSensitivity", ctypes.c_uint, 1),
            ("fTXContinueOnXoff", ctypes.c_uint, 1),
            ("fOutX", ctypes.c_uint, 1),
            ("fInX", ctypes.c_uint, 1),
            ("fErrorChar", ctypes.c_uint, 1),
            ("fNull", ctypes.c_uint, 1),
            ("fRtsControl", ctypes.c_uint, 2),
            ("fAbortOnError", ctypes.c_uint, 1),
            ("fDummy2", ctypes.c_uint, 17),
            ("wReserved", wt.WORD),
            ("XonLim", wt.WORD),
            ("XoffLim", wt.WORD),
            ("ByteSize", wt.BYTE),
            ("Parity", wt.BYTE),
            ("StopBits", wt.BYTE),
            ("XonChar", ctypes.c_char),
            ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char),
            ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char),
            ("wReserved1", wt.WORD),
        ]

    class _COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [
            ("ReadIntervalTimeout", wt.DWORD),
            ("ReadTotalTimeoutMultiplier", wt.DWORD),
            ("ReadTotalTimeoutConstant", wt.DWORD),
            ("WriteTotalTimeoutMultiplier", wt.DWORD),
            ("WriteTotalTimeoutConstant", wt.DWORD),
        ]


class _SerialImpl:  # pragma: no cover - 接口壳
    def open(self) -> None: ...
    def close(self) -> None: ...
    def purge_input(self) -> None: ...
    def write(self, data: bytes) -> int: ...
    def read(self, size: int) -> bytes: ...


class _WindowsSerial(_SerialImpl):  # pragma: no cover - 平台相关
    def __init__(self, port: str, baud: int, timeout: float) -> None:
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle: int | None = None

    def open(self) -> None:
        k = self._kernel32
        k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                  wt.DWORD, wt.DWORD, wt.HANDLE]
        k.CreateFileW.restype = wt.HANDLE
        k.BuildCommDCBW.argtypes = [wt.LPCWSTR, ctypes.POINTER(_DCB)]
        k.BuildCommDCBW.restype = wt.BOOL
        k.SetCommState.argtypes = [wt.HANDLE, ctypes.POINTER(_DCB)]
        k.SetCommState.restype = wt.BOOL
        k.SetCommTimeouts.argtypes = [wt.HANDLE, ctypes.POINTER(_COMMTIMEOUTS)]
        k.SetCommTimeouts.restype = wt.BOOL
        k.WriteFile.argtypes = [wt.HANDLE, ctypes.c_char_p, wt.DWORD,
                                ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
        k.WriteFile.restype = wt.BOOL
        k.ReadFile.argtypes = [wt.HANDLE, ctypes.c_char_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
        k.ReadFile.restype = wt.BOOL

        path = self._port if self._port.startswith("\\\\.\\") else f"\\\\.\\{self._port}"
        handle = k.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
        )
        if handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise OSError(err, f"无法打开串口 {self._port}（错误 {err}）")
        self._handle = handle
        dcb = _DCB()
        dcb.DCBlength = ctypes.sizeof(_DCB)
        if not k.BuildCommDCBW(f"baud={self._baud} parity=N data=8 stop=1", ctypes.byref(dcb)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, f"串口 {self._port} DCB 配置失败（错误 {err}）")
        dcb.fBinary = 1
        dcb.fDtrControl = 0  # 不驱动 DTR/RTS，避免误触发电台键控
        dcb.fRtsControl = 0
        if not k.SetCommState(handle, ctypes.byref(dcb)):
            self.close()
            raise OSError(f"串口 {self._port} SetCommState 失败")
        to = _COMMTIMEOUTS()
        to.ReadIntervalTimeout = 20
        to.ReadTotalTimeoutConstant = int(self._timeout * 1000)
        to.WriteTotalTimeoutConstant = int(self._timeout * 1000)
        k.SetCommTimeouts(handle, ctypes.byref(to))
        k.SetupComm(handle, 4096, 4096)

    def close(self) -> None:
        if self._handle is not None and self._handle != INVALID_HANDLE_VALUE:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def purge_input(self) -> None:
        if self._handle is None:
            raise RuntimeError("串口未打开")
        self._kernel32.PurgeComm(self._handle, PURGE_RXCLEAR | PURGE_TXCLEAR)

    def write(self, data: bytes) -> int:
        if self._handle is None:
            raise RuntimeError("串口未打开")
        written = wt.DWORD(0)
        buf = ctypes.create_string_buffer(data, len(data))
        ok = self._kernel32.WriteFile(self._handle, buf, len(data), ctypes.byref(written), None)
        if not ok:
            raise OSError(ctypes.get_last_error(), "串口写入失败")
        return int(written.value)

    def read(self, size: int) -> bytes:
        if self._handle is None:
            raise RuntimeError("串口未打开")
        buf = ctypes.create_string_buffer(size)
        got = wt.DWORD(0)
        ok = self._kernel32.ReadFile(self._handle, buf, size, ctypes.byref(got), None)
        if not ok:
            return b""
        return bytes(buf.raw[: int(got.value)])


class _PyserialSerial(_SerialImpl):  # pragma: no cover - 非 Windows
    def __init__(self, port: str, baud: int, timeout: float) -> None:
        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._serial = None

    def open(self) -> None:
        import serial  # type: ignore

        self._serial = serial.Serial(
            self._port, self._baud, timeout=self._timeout, dsrdtr=False, rtscts=False
        )
        self._serial.dtr = False
        self._serial.rts = False

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def purge_input(self) -> None:
        if self._serial is None:
            raise RuntimeError("串口未打开")
        self._serial.reset_input_buffer()

    def write(self, data: bytes) -> int:
        if self._serial is None:
            raise RuntimeError("串口未打开")
        return int(self._serial.write(data))

    def read(self, size: int) -> bytes:
        if self._serial is None:
            raise RuntimeError("串口未打开")
        return self._serial.read(size)
