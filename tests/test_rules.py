"""Unit tests for alarm rules:  .venv/bin/python -m unittest discover -s tests -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alarm"))
from rules import AlarmEngine, Condition  # noqa: E402
from store import AlarmStore  # noqa: E402

T0 = 1_000_000.0


def status(**kw):
    s = {"x": 5.0, "y": 5.0, "heading": 0, "battery": 80.0, "state": "moving",
         "task": "A1->P1", "error": None, "ts": T0}
    s.update(kw)
    return s


def stats(**kw):
    s = {"cpu": 10, "mem_used": 4, "mem_total": 16,
         "disks": [{"name": "C:", "free": 50, "total": 100}], "ts": T0}
    s.update(kw)
    return s


class AmrRules(unittest.TestCase):
    def setUp(self):
        self.e = AlarmEngine()

    def test_healthy_amr_has_no_alarms(self):
        self.e.on_amr_status("AMR-01", status(), T0)
        self.assertEqual(self.e.evaluate(T0 + 0.5), {})

    def test_offline_after_silence(self):
        self.e.on_amr_status("AMR-01", status(), T0)
        self.assertNotIn("AMR-01:OFFLINE", self.e.evaluate(T0 + 2.9))
        out = self.e.evaluate(T0 + 3.5)
        self.assertEqual(out["AMR-01:OFFLINE"].severity, "critical")
        self.assertIn("(5.0, 5.0)", out["AMR-01:OFFLINE"].message)

    def test_offline_from_last_will(self):
        self.e.on_amr_status("AMR-01", status(), T0)
        self.e.on_amr_online("AMR-01", False)
        self.assertIn("AMR-01:OFFLINE", self.e.evaluate(T0 + 0.1))

    def test_old_retained_status_counts_as_offline(self):
        self.e.on_amr_status("AMR-01", status(ts=T0 - 60), T0)
        self.assertIn("AMR-01:OFFLINE", self.e.evaluate(T0))

    def test_offline_keeps_other_alarms_open(self):
        self.e.on_amr_status("AMR-01", status(state="error", error="lidar timeout"), T0)
        self.assertIn("AMR-01:ERROR", self.e.evaluate(T0))
        out = self.e.evaluate(T0 + 10)        # no data: can't tell if the error cleared
        self.assertIn("AMR-01:OFFLINE", out)
        self.assertIn("AMR-01:ERROR", out)
        self.assertIsNone(out["AMR-01:ERROR"])

    def test_error_state(self):
        self.e.on_amr_status("AMR-01", status(state="error", error="wheel slip"), T0)
        out = self.e.evaluate(T0)
        self.assertIn("wheel slip", out["AMR-01:ERROR"].message)
        self.e.on_amr_status("AMR-01", status(ts=T0 + 1), T0 + 1)
        self.assertNotIn("AMR-01:ERROR", self.e.evaluate(T0 + 1))

    def test_battery_low_with_hysteresis(self):
        self.e.on_amr_status("AMR-01", status(battery=19), T0)
        self.assertIn("AMR-01:BATTERY_LOW", self.e.evaluate(T0))
        self.e.on_amr_status("AMR-01", status(battery=22, ts=T0 + 1), T0 + 1)
        self.assertIn("AMR-01:BATTERY_LOW", self.e.evaluate(T0 + 1))   # not yet > 25
        self.e.on_amr_status("AMR-01", status(battery=26, ts=T0 + 2), T0 + 2)
        self.assertNotIn("AMR-01:BATTERY_LOW", self.e.evaluate(T0 + 2))

    def test_battery_critical_replaces_low(self):
        self.e.on_amr_status("AMR-01", status(battery=8), T0)
        out = self.e.evaluate(T0)
        self.assertIn("AMR-01:BATTERY_CRITICAL", out)
        self.assertNotIn("AMR-01:BATTERY_LOW", out)

    def test_stuck_while_moving(self):
        for i in range(25):   # 12 s of "moving" at the same spot
            t = T0 + i * 0.5
            self.e.on_amr_status("AMR-01", status(ts=t), t)
        self.assertIn("AMR-01:STUCK", self.e.evaluate(T0 + 12))

    def test_moving_amr_is_not_stuck(self):
        for i in range(25):
            t = T0 + i * 0.5
            self.e.on_amr_status("AMR-01", status(x=5 + i * 0.6, ts=t), t)
        self.assertNotIn("AMR-01:STUCK", self.e.evaluate(T0 + 12))


class PcRules(unittest.TestCase):
    def setUp(self):
        self.e = AlarmEngine()

    def test_cpu_must_stay_high(self):
        self.e.on_sys_stats("PC", stats(cpu=95), T0)
        self.assertNotIn("PC:PC:CPU_HIGH", self.e.evaluate(T0))
        for t in range(1, 12):
            self.e.on_sys_stats("PC", stats(cpu=95, ts=T0 + t), T0 + t)
        self.assertIn("PC:PC:CPU_HIGH", self.e.evaluate(T0 + 11))

    def test_memory_and_disk(self):
        self.e.on_sys_stats("PC", stats(mem_used=15.5,
                                        disks=[{"name": "C:", "free": 5, "total": 100}]), T0)
        out = self.e.evaluate(T0)
        self.assertIn("PC:PC:MEM_HIGH", out)
        self.assertIn("PC:PC:DISK_LOW_C", out)

    def test_monitor_offline(self):
        self.e.on_sys_stats("PC", stats(), T0)
        self.assertIn("PC:PC:MONITOR_OFFLINE", self.e.evaluate(T0 + 11))


class Store(unittest.TestCase):
    def test_raise_clear_ack(self):
        with tempfile.TemporaryDirectory() as d:
            s = AlarmStore(f"{d}/a.db")
            a = s.raise_(Condition("AMR-01", "OFFLINE", "critical", "lost"), T0)
            self.assertIn("AMR-01:OFFLINE", s.open_alarms())
            s.clear(a["id"], T0 + 5)
            self.assertEqual(s.open_alarms(), {})
            self.assertEqual(len(s.ack(None, "test", T0 + 6)), 1)
            self.assertEqual(s.ack(None, "test", T0 + 7), [])   # already acknowledged
            self.assertEqual(s.recent(10)[0]["acked_by"], "test")


if __name__ == "__main__":
    unittest.main()
