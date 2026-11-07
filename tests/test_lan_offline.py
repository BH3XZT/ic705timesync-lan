"""LAN（WiFi）路径的离线测试：不连电台，用假 radio 验证帧构造与解码。

真实 WiFi 联调需要 IC-705 开启热点/接入同一网络，见 README。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ic705timesync.lan_link import LanLink  # noqa: E402

# 真实抓包中 23 00 的 27 字节数据（去掉帧头/命令/子命令/FD）
GPS_PAYLOAD = bytes.fromhex(
    "37 52 31 30 01 01 12 32 56 00 01 00 78 70 00 01 50 00 00 09 20 26 11 07 14 26 14".replace(
        " ", ""
    )
)


@dataclass
class FakeFrame:
    data: bytes


class FakeRadio:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int | None]] = []

    async def send_civ(self, command, sub=None, data=None, *, wait_response=True):
        self.sent.append((command, sub))
        return FakeFrame(data=GPS_PAYLOAD)


class TestLanLinkOffline(unittest.TestCase):
    def test_read_gps_uses_command_23_00(self):
        link = LanLink(host="127.0.0.1", username="u", password="p")
        fake = FakeRadio()
        link._radio = fake  # 注入假电台
        link._loop = asyncio.new_event_loop()
        try:
            fix = link.read_gps()
        finally:
            link._loop.close()
        self.assertEqual(fake.sent, [(0x23, 0x00)])
        self.assertAlmostEqual(fix.latitude, 37 + 52.313 / 60, places=6)
        self.assertEqual(fix.utc.second, 14)

    def test_radio_addr_default_is_ic705(self):
        link = LanLink(host="192.168.59.1")
        self.assertEqual(link.radio_addr, 0xA4)


if __name__ == "__main__":
    unittest.main()
