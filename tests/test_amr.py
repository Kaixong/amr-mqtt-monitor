"""Unit tests for the simulated AMR state machine:  .venv/bin/python -m pytest tests"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))
import amr as amr_mod  # noqa: E402
from amr import AMR  # noqa: E402

STATES = ["idle", "moving", "charging", "paused", "error"]


@pytest.fixture(autouse=True)
def no_random_errors(monkeypatch):
    monkeypatch.setattr(amr_mod, "ERROR_CHANCE", 0.0)


def make(state="idle", battery=80.0):
    """An AMR put into `state` the way the simulation would get it there."""
    a = AMR("AMR-T", 3, 19, battery=battery)
    a.idle_until = float("inf")   # don't start a task on its own during the test
    if state == "moving":
        assert a.command("go")
    elif state == "charging":
        a.state, a.charger, a.task = "charging", "C1", "charge@C1"
    elif state == "paused":
        assert a.command("go") and a.command("pause")
    elif state == "error":
        assert a.command("go")
        a.state, a.error_msg, a.error_until = "error", "lidar timeout", float("inf")
    assert a.state == state
    return a


# ---------- only IDLE accepts new work ----------

@pytest.mark.parametrize("cmd", ["go", "charge"])
def test_idle_accepts_new_work(cmd):
    a = make("idle")
    assert a.command(cmd)
    assert a.state == "moving"
    assert a.route
    assert a.task.startswith("charge@") == (cmd == "charge")


@pytest.mark.parametrize("state", [s for s in STATES if s != "idle"])
@pytest.mark.parametrize("cmd", ["go", "charge"])
def test_new_work_is_rejected_unless_idle(state, cmd):
    a = make(state)
    before = (a.state, a.task, list(a.route), a.charger, a.paused_from)
    assert not a.command(cmd)
    assert (a.state, a.task, list(a.route), a.charger, a.paused_from) == before


def test_running_task_is_not_preempted_and_finishes():
    a = make("moving")
    task = a.task
    assert not a.command("charge")
    assert not a.command("go")
    for _ in range(400):          # 200 s at 2 Hz is plenty to cross the 40 x 25 m floor
        a.step(0.5)
        if a.state != "moving":
            break
    assert a.state == "idle"
    assert a.tasks_done == 1
    assert a.task is None and task is not None


def test_new_work_accepted_again_once_idle():
    a = make("moving")
    a.route = a.route[-1:]
    a.x, a.y = a.route[0][1]      # standing on the last waypoint
    a.step(0.5)
    assert a.state == "idle"
    assert a.command("go")


# ---------- pause / resume / clear ----------

@pytest.mark.parametrize("state", ["idle", "moving", "charging"])
def test_pause_and_resume_restore_previous_state(state):
    a = make(state)
    task = a.task
    assert a.command("pause")
    assert a.state == "paused"
    assert not a.command("pause")
    assert a.command("resume")
    assert a.state == state and a.task == task
    assert a.paused_from is None


def test_paused_amr_does_not_move():
    a = make("paused")
    x, y = a.x, a.y
    for _ in range(10):
        a.step(0.5)
    assert (a.x, a.y) == (x, y)
    assert a.state == "paused"


@pytest.mark.parametrize("state", ["idle", "moving", "charging", "paused"])
def test_clear_only_in_error(state):
    assert not make(state).command("clear")


def test_error_rejects_everything_but_clear():
    a = make("error")
    for cmd in ("pause", "resume", "go", "charge"):
        assert not a.command(cmd)
    assert a.command("clear")
    assert a.state == "moving"     # it still has its route, so it carries on
    assert a.error_msg is None


def test_error_clears_itself_after_timeout():
    a = make("error")
    a.error_until = 0.0
    a.step(0.5)
    assert a.state == "moving"


@pytest.mark.parametrize("state", STATES)
def test_unknown_command_is_rejected(state):
    assert not make(state).command("dance")


# ---------- autonomous behaviour still works ----------

def test_low_battery_idle_amr_goes_charging_by_itself():
    a = make("idle", battery=15)
    a.step(0.5)
    assert a.state == "moving"
    assert a.task.startswith("charge@")


def test_charging_ends_at_full_battery():
    a = make("charging", battery=94)
    a.step(1.0)
    assert a.state == "idle"
    assert a.charger is None
