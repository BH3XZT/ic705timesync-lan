"""NMEA 合成与时间估计的单元测试。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ic705timesync.civ import GpsFix  # noqa: E402
from ic705timesync.nmea import gpzda, gprmc, sentence  # noqa: E402
from ic705timesync.timecore import Sample, estimate_offset  # noqa: E402


def _fix(utc: datetime) -> GpsFix:
    return GpsFix(
        latitude=37.8719,
        longitude=112.5427,
        altitude_m=787.0,
        course_deg=150,
        speed_kmh=0.9,
        utc=utc,
    )


class TestNmea(unittest.TestCase):
    def test_checksum_matches_real_radio_sentence(self):
        # IC-705 实际输出的语句：$GPZDA,140419.00,07,11,2026,,*6E
        fix = _fix(datetime(2026, 11, 7, 14, 4, 19, tzinfo=timezone.utc))
        line = gpzda(fix)
        self.assertEqual(line, "$GPZDA,140419.00,07,11,2026,,*6E\r\n")

    def test_checksum_function(self):
        # 取自 IC-705 实际输出的 $GPVTG 语句
        line = sentence("GPVTG,281.4,T,285.9,M,0.4,N,0.7,K,A")
        self.assertEqual(line, "$GPVTG,281.4,T,285.9,M,0.4,N,0.7,K,A*29\r\n")

    def test_gprmc_contains_hemispheres(self):
        fix = _fix(datetime(2026, 11, 7, 14, 4, 19, tzinfo=timezone.utc))
        line = gprmc(fix)
        self.assertIn("3752.3140,N", line)
        self.assertIn("11232.5620,E", line)


class TestOffsetEstimate(unittest.TestCase):
    def _samples(self, *, offset: float, count: int = 15, step: float = 0.1):
        """构造 PC 比 GPS 慢 ``offset`` 秒的采样序列。"""
        base = datetime(2026, 11, 7, 14, 26, 14, tzinfo=timezone.utc)
        samples = []
        for i in range(count):
            t = i * step
            gps = base + timedelta(seconds=t)
            wall = gps - timedelta(seconds=offset)
            samples.append(
                Sample(
                    utc=base + timedelta(seconds=int(t)),
                    t_send=t,
                    t_recv=t + 0.02,
                    wall_send=wall,
                )
            )
        return samples

    def test_detects_transitions_and_offset(self):
        est = estimate_offset(self._samples(offset=0.2))
        self.assertEqual(est.method, "秒跳变")
        self.assertGreaterEqual(est.transitions, 1)
        self.assertAlmostEqual(est.offset_seconds, 0.2, delta=0.06)
        self.assertLess(est.uncertainty_seconds, 0.15)

    def test_negative_offset(self):
        est = estimate_offset(self._samples(offset=-0.35))
        self.assertAlmostEqual(est.offset_seconds, -0.35, delta=0.06)

    def test_fallback_when_no_transition(self):
        base = datetime(2026, 11, 7, 14, 26, 14, tzinfo=timezone.utc)
        samples = [
            Sample(utc=base, t_send=0.0, t_recv=0.01, wall_send=base - timedelta(seconds=0.1))
            for _ in range(3)
        ]
        est = estimate_offset(samples)
        self.assertIn("粗估", est.method)
        self.assertGreaterEqual(est.uncertainty_seconds, 0.5)


if __name__ == "__main__":
    unittest.main()
