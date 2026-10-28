"""Unit tests for the state recorder (tracker + SQLite):  .venv/bin/python -m pytest tests"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "recorder"))
from statedb import StateDB  # noqa: E402
from tracker import StateTracker, utilization  # noqa: E402

T0 = 1_000_000.0


@pytest.fixture
def t():
    return StateTracker(offline_after=3.0)


@pytest.fixture
def db(tmp_path):
    return StateDB(str(tmp_path / "t.db"))


# ---------- tracker ----------

def test_same_state_closes_nothing(t):
    assert t.observe("A", "moving", T0, T0) == []
    assert t.observe("A", "moving", T0 + 0.5, T0 + 0.5) == []


def test_state_change_closes_previous_interval(t):
    t.observe("A", "moving", T0, T0)
    assert t.observe("A", "idle", T0 + 30, T0 + 30) == [("A", "moving", T0, T0 + 30)]
    assert t.cur["A"] == ("idle", T0 + 30)


def test_publisher_timestamp_is_used(t):
    t.observe("A", "moving", T0, T0 + 0.2)          # arrived 0.2 s late
    assert t.observe("A", "idle", T0 + 10, T0 + 10.3) == [("A", "moving", T0, T0 + 10)]


def test_stale_retained_status_is_ignored(t):
    assert t.observe("A", "moving", T0 - 60, T0) == []
    assert "A" not in t.cur


def test_silence_becomes_offline_from_last_seen(t):
    t.observe("A", "moving", T0, T0)
    t.observe("A", "moving", T0 + 5, T0 + 5)
    assert t.tick(T0 + 7) == []                     # 2 s of silence: still fine
    assert t.tick(T0 + 9) == [("A", "moving", T0, T0 + 5)]
    assert t.cur["A"] == ("offline", T0 + 5)
    assert t.tick(T0 + 20) == []                    # already offline


def test_last_will_marks_offline_and_status_brings_it_back(t):
    t.observe("A", "moving", T0, T0)
    assert t.set_online("A", False, T0 + 4) == [("A", "moving", T0, T0 + 4)]
    assert t.set_online("A", True, T0 + 10) == []
    assert t.observe("A", "idle", T0 + 11, T0 + 11) == [("A", "offline", T0 + 4, T0 + 11)]


def test_retained_offline_is_ignored(t):
    t.observe("A", "idle", T0, T0)                  # restarted AMR's first status ...
    assert t.set_online("A", False, T0 + 0.1, retained=True) == []   # ... then the old Will
    assert t.cur["A"] == ("idle", T0)


def test_offline_for_unknown_amr_is_ignored(t):
    assert t.set_online("A", False, T0) == []
    assert t.cur == {}


def test_close_all_on_shutdown(t):
    t.observe("A", "moving", T0, T0)
    t.observe("B", "charging", T0 + 1, T0 + 1)
    assert sorted(t.close_all(T0 + 10)) == [("A", "moving", T0, T0 + 10),
                                            ("B", "charging", T0 + 1, T0 + 10)]
    assert t.cur == {} and t.tick(T0 + 100) == []


def test_add_open_clips_to_window(t):
    t.observe("A", "moving", T0, T0)
    assert t.add_open({}, T0 + 50, T0 + 80) == {"A": {"moving": 30}}
    assert t.add_open({"A": {"idle": 5}}, T0 - 10, T0 + 20) == {"A": {"idle": 5, "moving": 20}}


# ---------- utilization ----------

def test_utilization_percentages():
    u = utilization({"A": {"moving": 45, "idle": 10, "charging": 45}, "B": {"offline": 60}})
    assert u["A"] == {"total": 100, "utilization": 45.0,
                      "states": {"moving": 45.0, "charging": 45.0, "idle": 10.0}}
    assert u["B"]["utilization"] == 0.0
    assert u["B"]["states"] == {"offline": 100.0}


def test_utilization_skips_empty():
    assert utilization({"A": {}}) == {}


# ---------- SQLite ----------

def test_durations_are_clipped_to_window(db):
    db.add([("A", "moving", T0, T0 + 100),
            ("A", "idle", T0 + 100, T0 + 110),
            ("A", "moving", T0 + 110, T0 + 200),
            ("B", "charging", T0 - 500, T0 - 400)])          # outside the window
    d = db.durations(T0 + 50, T0 + 150)
    assert d == {"A": {"moving": 90, "idle": 10}}


def test_tracker_and_db_end_to_end(db, t):
    for i in range(0, 60):                    # 30 s moving, then 30 s idle at 2 Hz
        ts = T0 + i * 0.5
        db.add(t.observe("A", "moving" if i < 30 else "idle", ts, ts))
    now = T0 + 30
    d = t.add_open(db.durations(now - 3600, now), now - 3600, now)
    u = utilization(d)["A"]
    assert u["utilization"] == 50.0           # 15 s moving, 15 s idle (still open)
    assert db.first_record() == T0


def test_prune_drops_old_intervals(db):
    db.add([("A", "moving", T0, T0 + 10), ("A", "idle", T0 + 10, T0 + 1000)])
    db.prune(T0 + 500)
    assert db.durations(T0, T0 + 1000) == {"A": {"idle": 990}}
