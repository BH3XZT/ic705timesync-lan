"""单元测试：用真实电台抓到的原始帧验证 CI-V 解码。

帧是 2026-11-07 14:26 UTC 在 COM6 上从 IC-705（地址 0xA4）抓到的。
运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ic705timesync.civ import (  # noqa: E402
    CivError,
    decode_gps_position,
    extract_frame,
    parse_payload,
)

# 真实抓包：第一次读取
RAW_FRAME_1 = bytes.fromhex(
    "FE FE E0 A4 23 00 37 52 31 30 01 01 12 32 56 00 01 00 78 70 00 01 50 00 00 09 "
    "20 26 11 07 14 26 14 FD".replace(" ", "")
)
# 真实抓包：1 秒后再次读取
RAW_FRAME_2 = bytes.fromhex(
    "FE FE E0 A4 23 00 37 52 31 30 01 01 12 32 56 00 01 00 78 70 00 01 67 00 00 00 "
    "20 26 11 07 14 26 15 FD".replace(" ", "")
)


class TestGpsDecode(unittest.TestCase):
    def _decode(self, raw: bytes):
        frame = extract_frame(raw)
        command, sub, data = parse_payload(frame)
        self.assertEqual(command, 0x23)
        self.assertEqual(sub, 0x00)
        self.assertEqual(len(data), 27)
        return decode_gps_position(data)

    def test_first_frame(self):
        fix = self._decode(RAW_FRAME_1)
        self.assertAlmostEqual(fix.latitude, 37 + 52.313 / 60, places=6)
        self.assertAlmostEqual(fix.longitude, 112 + 32.56 / 60, places=6)
        self.assertAlmostEqual(fix.altitude_m, 787.0, places=1)
        self.assertEqual(fix.course_deg, 150)
        self.assertAlmostEqual(fix.speed_kmh, 0.9, places=1)
        self.assertIsNotNone(fix.utc)
        self.assertEqual(fix.utc.year, 2026)
        self.assertEqual(fix.utc.month, 11)
        self.assertEqual(fix.utc.day, 7)
        self.assertEqual((fix.utc.hour, fix.utc.minute, fix.utc.second), (14, 26, 14))

    def test_second_frame_second_rollover(self):
        fix = self._decode(RAW_FRAME_2)
        self.assertEqual(fix.utc.second, 15)
        self.assertEqual(fix.course_deg, 167)
        self.assertEqual(fix.speed_kmh, 0.0)

    def test_bad_length_rejected(self):
        with self.assertRaises(CivError):
            decode_gps_position(b"\x00" * 10)

    def test_all_ff_rejected(self):
        with self.assertRaises(CivError):
            decode_gps_position(b"\xff" * 27)

    def test_extract_frame_tolerates_noise(self):
        noisy = b"\x00\x11" + RAW_FRAME_1
        frame = extract_frame(noisy)
        self.assertTrue(frame.startswith(b"\xfe\xfe"))
        self.assertEqual(frame[-1], 0xFD)


if __name__ == "__main__":
    unittest.main()
