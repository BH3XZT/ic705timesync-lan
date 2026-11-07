"""命令行入口：ic705timesync-lan。

子命令：
    ports      扫描本机串口，找出哪个口后面是 Icom 电台
    discover   在局域网里广播发现 Icom 电台（WiFi 模式用）
    read       读一次 GPS，显示位置/时间与电脑时钟偏差（不改系统时间）
    sync       测偏差并设置系统时钟（需管理员权限）
    watch      常驻，按间隔自动对时
    serve      把 GPS 数据以 NMEA 输出（TCP 和/或串口），供 BktTimeSync 等使用
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from .civ import CivError, GpsFix, extract_frame
from .timecore import (
    OffsetEstimate,
    apply_offset,
    collect_samples,
    estimate_offset,
    is_admin,
    set_system_time_utc,
    take_sample,
)


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------


def add_transport_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("传输层")
    g.add_argument("--transport", choices=("serial", "lan"), default="serial",
                   help="serial=USB 串口（默认）, lan=WiFi/局域网")
    g.add_argument("--port", default="COM6", help="串口名（serial 模式），默认 COM6")
    g.add_argument("--baud", type=int, default=115200, help="串口波特率，默认 115200")
    g.add_argument("--host", default="192.168.59.1",
                   help="电台 IP（lan 模式）。IC-705 热点模式默认 192.168.59.1")
    g.add_argument("--net-port", type=int, default=50001,
                   help="Icom LAN 控制端口，默认 50001")
    g.add_argument("--user", default="", help="电台网络用户名（lan 模式）")
    g.add_argument("--password", default="", help="电台网络密码（lan 模式）")
    g.add_argument("--civ-addr", default="0xA4",
                   help="电台 CI-V 地址，默认 0xA4（IC-705）")
    g.add_argument("--timeout", type=float, default=2.0, help="单次操作超时秒数")


def build_link(args: argparse.Namespace):
    addr = int(args.civ_addr, 0) if isinstance(args.civ_addr, str) else args.civ_addr
    if args.transport == "lan":
        from .lan_link import LanLink

        return LanLink(
            host=args.host,
            port=args.net_port,
            username=args.user,
            password=args.password,
            radio_addr=addr,
            timeout=args.timeout,
        )
    from .serial_link import SerialLink, list_com_ports

    port = args.port
    if str(port).lower() == "auto":
        found, reasons = _autodetect_port(addr, args.baud)
        if not found:
            detail = "；".join(reasons) if reasons else "没有可用串口"
            raise RuntimeError(
                "自动探测失败：没有串口对 CI-V 作出应答。"
                "常见原因：1) RemoteUty 还没连上电台；2) 串口被 JTDX 等程序占用；"
                "3) 电台未开机。" + " 端口详情：" + detail
            )
        port = found
        print(f"自动选择串口: {port}")
    return SerialLink(port=port, baud=args.baud, timeout=args.timeout)


def _autodetect_port(civ_addr: int, baud: int) -> tuple[str | None, list[str]]:
    """扫描所有串口，返回 (可用端口, 每个端口的失败原因)。"""
    from .serial_link import SerialLink, list_com_ports

    reasons: list[str] = []
    for port in list_com_ports():
        try:
            with SerialLink(port, baud, 0.6) as link:
                body = extract_frame(
                    link.transmit(bytes([0xFE, 0xFE, civ_addr, 0xE0, 0x19, 0x00, 0xFD]))
                )[4:-1]
            if len(body) >= 3 and body[0] == 0x19:
                return port, reasons
            reasons.append(f"{port} 无有效应答")
        except Exception as exc:
            message = str(exc)
            if "拒绝访问" in message or "Errno 5" in message or "错误 5" in message or "error 5" in message.lower():
                message = "被占用（拒绝访问，可能是 JTDX/RemoteUty 正在使用）"
            reasons.append(f"{port} {message}")
    return None, reasons


def cmd_ports(args: argparse.Namespace) -> int:
    """扫描本机串口，识别哪个口后面是 Icom 电台（CI-V 19 00）。"""
    from .serial_link import SerialLink, list_com_ports

    ports = list_com_ports()
    if not ports:
        print("没有发现任何串口。")
        return 1
    addr = _civ_addr(args)
    print(f"发现 {len(ports)} 个串口，逐个探测 CI-V（地址 0x{addr:02X}）...")
    working = []
    for port in ports:
        label = "未知"
        try:
            with SerialLink(port, args.baud, 0.6) as link:
                body = extract_frame(
                    link.transmit(bytes([0xFE, 0xFE, addr, 0xE0, 0x19, 0x00, 0xFD]))
                )[4:-1]
            if len(body) >= 3 and body[0] == 0x19:
                model_id = body[2]
                label = f"Icom 电台，型号 ID 0x{model_id:02X}"
                if model_id == 0xA4:
                    label += "（IC-705）"
                working.append(port)
            elif body and 0xFA in body:
                label = "有串口但 CI-V 不应答（可能不是电台/地址不对）"
            else:
                label = "无应答"
        except Exception as exc:
            label = f"打开失败: {exc}"
        print(f"  {port:<8} {label}")
    if working:
        print(f"可用: {', '.join(working)}")
    return 0


def _civ_addr(args: argparse.Namespace) -> int:
    value = getattr(args, "civ_addr", "0xA4")
    return int(value, 0) if isinstance(value, str) else int(value)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def describe_fix(fix: GpsFix) -> str:
    lat = f"{abs(fix.latitude):.6f}°{'N' if fix.latitude >= 0 else 'S'}"
    lon = f"{abs(fix.longitude):.6f}°{'E' if fix.longitude >= 0 else 'W'}"
    alt = "?" if fix.altitude_m is None else f"{fix.altitude_m:.1f} m"
    course = "?" if fix.course_deg is None else f"{fix.course_deg}°"
    speed = "?" if fix.speed_kmh is None else f"{fix.speed_kmh:.1f} km/h"
    utc = "?" if fix.utc is None else fix.utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"GPS: {lat}, {lon}  海拔 {alt}  航向 {course}  速度 {speed}  时间 {utc}"


def describe_estimate(est: OffsetEstimate) -> str:
    sign = "+" if est.offset_seconds >= 0 else ""
    return (
        f"偏差估计: {sign}{est.offset_ms:.1f} ms (±{est.uncertainty_ms:.0f} ms, "
        f"{est.method}, {est.samples} 采样/{est.transitions} 次秒跳变)"
    )


def print_json(fix: GpsFix, est: OffsetEstimate | None = None) -> None:
    payload: dict[str, object] = {
        "latitude": fix.latitude,
        "longitude": fix.longitude,
        "altitude_m": fix.altitude_m,
        "course_deg": fix.course_deg,
        "speed_kmh": fix.speed_kmh,
        "utc": fix.utc.isoformat() if fix.utc else None,
        "pc_utc": datetime.now(timezone.utc).isoformat(),
    }
    if est is not None:
        payload["offset_seconds"] = est.offset_seconds
        payload["uncertainty_seconds"] = est.uncertainty_seconds
        payload["method"] = est.method
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    from .lan_link import LanLink

    radios = LanLink.discover(timeout=args.timeout)
    if not radios:
        print("没有发现电台。检查：电台 WLAN 已开、与电脑在同一网段、"
              "或热点模式下电台 IP 是否为 192.168.59.1。")
        return 1
    print(f"发现 {len(radios)} 台：")
    for r in radios:
        print("  " + json.dumps(r, ensure_ascii=False, default=str))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    with build_link(args) as link:
        fix = link.read_gps()
        if not args.json:
            print(describe_fix(fix))
        samples = collect_samples(link, count=args.samples, interval=args.interval)
        est = estimate_offset(samples)
        if args.json:
            print_json(fix, est)
        else:
            print(describe_estimate(est))
            print(f"管理员权限: {'是' if is_admin() else '否（sync 需要管理员）'}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    with build_link(args) as link:
        samples = collect_samples(link, count=args.samples, interval=args.interval)
        est = estimate_offset(samples)
        print(describe_estimate(est))
        if abs(est.offset_seconds) < args.threshold and not args.force:
            print(f"偏差小于阈值 {args.threshold * 1000:.0f} ms，无需调整。")
            return 0
        if args.dry_run:
            target = datetime.now(timezone.utc) + timedelta(seconds=est.offset_seconds)
            print(f"[dry-run] 将把系统时间设为 {target.strftime('%Y-%m-%d %H:%M:%S.%f')} UTC")
            return 0
        try:
            target = apply_offset(est.offset_seconds)
        except PermissionError as exc:
            print(f"对时失败：{exc}", file=sys.stderr)
            return 2
        print(f"已对时 -> {target.strftime('%Y-%m-%d %H:%M:%S.%f')} UTC")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    print(f"常驻对时：每 {args.interval_s} 秒测一次，阈值 {args.threshold * 1000:.0f} ms")
    try:
        with build_link(args) as link:
            while True:
                try:
                    samples = collect_samples(link, count=args.samples, interval=0.1)
                    est = estimate_offset(samples)
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"[{stamp}] {describe_estimate(est)}")
                    if abs(est.offset_seconds) > args.threshold:
                        if args.dry_run:
                            print("  [dry-run] 跳过设置系统时间")
                        else:
                            target = apply_offset(est.offset_seconds)
                            print(f"  已对时 -> {target.strftime('%H:%M:%S.%f')} UTC")
                except CivError as exc:
                    print(f"  读取失败：{exc}", file=sys.stderr)
                time.sleep(args.interval_s)
    except KeyboardInterrupt:
        print("已退出。")
        return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """把 GPS 数据以 NMEA 输出给别的软件（BktTimeSync 等）。"""
    from .nmea import from_fix

    if args.tcp:
        host, _, port_s = args.tcp.rpartition(":")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host or "0.0.0.0", int(port_s)))
        server.listen(4)
        server.setblocking(False)
        print(f"NMEA TCP 服务: {host or '0.0.0.0'}:{port_s}")

        def accept_loop() -> None:
            while True:
                try:
                    conn, addr = server.accept()
                    conn.setblocking(True)
                    _TCP_CLIENTS.add(conn)
                    print(f"  + 客户端 {addr}")
                except BlockingIOError:
                    pass
                except OSError:
                    break
                for c in list(_TCP_CLIENTS):
                    try:
                        if c.fileno() < 0:
                            _TCP_CLIENTS.discard(c)
                    except OSError:
                        _TCP_CLIENTS.discard(c)
                time.sleep(0.2)

        threading.Thread(target=accept_loop, daemon=True).start()

    serial_out = None
    if args.serial_out:
        serial_out = _open_nmea_serial(args.serial_out, args.baud)
        print(f"NMEA 串口输出: {args.serial_out} @ {args.baud}")

    with build_link(args) as link:
        last_second = None
        while True:
            try:
                fix = link.read_gps()
                if fix.utc is None:
                    continue
                if last_second == fix.utc.second:
                    time.sleep(0.02)
                    continue
                last_second = fix.utc.second
                lines = "".join(from_fix(fix))
                if serial_out is not None:
                    serial_out.write(lines.encode("ascii"))
                if args.tcp:
                    _broadcast(lines)
                print(f"[{fix.utc:%H:%M:%S}] {describe_fix(fix)}")
            except CivError as exc:
                print(f"读取失败：{exc}", file=sys.stderr)
                time.sleep(1)


def _broadcast(text: str) -> None:
    """向所有已连接的 TCP 客户端广播一段 NMEA。"""
    for conn in list(_TCP_CLIENTS):
        try:
            conn.sendall(text.encode("ascii"))
        except OSError:
            _TCP_CLIENTS.discard(conn)


_TCP_CLIENTS: set[socket.socket] = set()


def _open_nmea_serial(port: str, baud: int):
    """打开一个只写的串口（供 com0com 之类的虚拟串口对使用）。"""
    if sys.platform == "win32":
        from .serial_link import _WindowsSerial

        impl = _WindowsSerial(port, baud, 1.0)
        impl.open()
        return impl
    from .serial_link import _PyserialSerial

    impl = _PyserialSerial(port, baud, 1.0)
    impl.open()
    return impl


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ic705timesync-lan",
        description="用 IC-705 内置 GPS（USB 串口 / WiFi LAN）给电脑授时",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="发现局域网内的电台")
    d.add_argument("--timeout", type=float, default=3.0)
    d.set_defaults(func=cmd_discover)

    def add_common(sp: argparse.ArgumentParser, *, samples: int, interval: float) -> None:
        add_transport_args(sp)
        sp.add_argument("--samples", type=int, default=samples, help="采样次数（秒跳变检测用）")
        sp.add_argument("--interval", type=float, default=interval, help="采样间隔秒")
        sp.add_argument("--json", action="store_true", help="输出 JSON")

    r = sub.add_parser("read", help="读 GPS 并显示与电脑时钟的偏差（不改时间）")
    add_common(r, samples=25, interval=0.06)
    r.set_defaults(func=cmd_read)

    s = sub.add_parser("sync", help="测偏差并设置系统时钟（需管理员）")
    add_common(s, samples=25, interval=0.06)
    s.add_argument("--threshold", type=float, default=0.05, help="超过该偏差(秒)才调整，默认 0.05")
    s.add_argument("--force", action="store_true", help="忽略阈值强制对时")
    s.add_argument("--dry-run", action="store_true", help="只显示将设置的时间")
    s.set_defaults(func=cmd_sync)

    w = sub.add_parser("watch", help="常驻自动对时")
    add_transport_args(w)
    w.add_argument("--interval-s", type=float, default=60, help="两轮之间的间隔秒")
    w.add_argument("--samples", type=int, default=20)
    w.add_argument("--threshold", type=float, default=0.1, help="超过该偏差(秒)才调整")
    w.add_argument("--dry-run", action="store_true")
    w.set_defaults(func=cmd_watch)

    v = sub.add_parser("serve", help="把 GPS 数据以 NMEA 输出（TCP/串口）")
    add_transport_args(v)
    v.add_argument("--tcp", default="0.0.0.0:2101", help="TCP 监听地址，空字符串=关闭")
    v.add_argument("--serial-out", default="", help="额外输出到串口（如 com0com 的另一端）")
    v.set_defaults(func=cmd_serve)

    pt = sub.add_parser("ports", help="扫描本机串口并识别 Icom 电台（找 RemoteUty 虚拟串口）")
    pt.add_argument("--baud", type=int, default=115200)
    pt.add_argument("--civ-addr", default="0xA4")
    pt.set_defaults(func=cmd_ports)

    return p


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认不是 UTF-8，中文提示会乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("ic705timesync-lan 是命令行工具，需要指定子命令。")
        print("")
        print("常用命令：")
        print("  read       检测连接（读 GPS 与电脑时钟偏差，不改时间）")
        print("  sync       立即对时（需要管理员权限）")
        print("  ports      扫描串口，找出哪个是 IC-705")
        print("  discover   发现局域网内的 IC-705（WiFi）")
        print("")
        print("示例：")
        print("  ic705timesync-lan read --transport serial --port COM6")
        print("  ic705timesync-lan read --transport lan --host 192.168.59.1 --user U --password P")
        print("完整用法：ic705timesync-lan --help")
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                input("\n按回车键关闭…")
        except Exception:
            pass
        return 2
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except (CivError, OSError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1



