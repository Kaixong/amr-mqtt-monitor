"""Simulated AMR: moves between stations on a warehouse floor, drains/charges battery."""
import math
import random
import time
from collections import deque

# Warehouse floor in metres
MAP_W, MAP_H = 40.0, 25.0

STATIONS = {
    # pick-up shelves
    "A1": (6, 5), "A2": (14, 5), "A3": (22, 5), "A4": (30, 5),
    "B1": (6, 13), "B2": (14, 13), "B3": (22, 13), "B4": (30, 13),
    # drop-off / packing
    "P1": (36, 20), "P2": (36, 15),
}
CHARGERS = {"C1": (3, 21), "C2": (7, 21), "C3": (11, 21)}

SPEED = 1.2          # m/s
DRAIN_MOVING = 0.08  # %/s
DRAIN_IDLE = 0.01    # %/s
CHARGE_RATE = 1.5    # %/s
LOW_BATTERY = 20.0
FULL_BATTERY = 95.0
ERROR_CHANCE = 0.0008  # per tick
ERROR_SECONDS = 8.0
QUEUEABLE = ("go", "charge")   # new work: queued while busy, run once idle
MAX_QUEUE = 3


class AMR:
    def __init__(self, amr_id, x, y, battery=None):
        self.id = amr_id
        self.x, self.y = float(x), float(y)
        self.heading = 0.0
        self.battery = battery if battery is not None else random.uniform(40, 100)
        self.state = "idle"        # idle | moving | charging | error | paused
        self.task = None           # e.g. "A3->P1"
        self.route = []            # list of (name, (x, y))
        self.target = None         # current station name
        self.charger = None
        self.error_until = 0.0
        self.error_msg = None
        self.paused_from = None
        self.idle_until = time.time() + random.uniform(0, 3)
        self.tasks_done = 0
        self.odometer = 0.0
        self.queue = deque()       # go / charge waiting for the AMR to become idle

    # ---------- commands ----------
    def request(self, cmd):
        """Entry point for remote commands: "accepted" | "queued" | "rejected".

        go / charge sent while the AMR is busy are queued (at most MAX_QUEUE) and run, in
        order, as soon as it is idle again; command() itself still only accepts them while
        idle. cancel empties the queue. Everything else goes straight to command().
        """
        if cmd == "cancel":
            if not self.queue:
                return "rejected"
            self.queue.clear()
            return "accepted"
        if cmd in QUEUEABLE and self.state != "idle":
            if cmd == "charge" and (self.charger or "charge" in self.queue):
                return "rejected"      # already charging / on its way / queued
            if len(self.queue) >= MAX_QUEUE:
                return "rejected"
            self.queue.append(cmd)
            return "queued"
        return "accepted" if self.command(cmd) else "rejected"

    # Commands that start new work (go, charge) are only accepted while idle: they never
    # preempt a running task, a charge or a paused task. pause / resume / clear only change
    # how the current work runs. Returns True if the command was accepted.
    def command(self, cmd):
        if cmd == "pause" and self.state in ("idle", "moving", "charging"):
            self.paused_from, self.state = self.state, "paused"
        elif cmd == "resume" and self.state == "paused":
            self.state, self.paused_from = self.paused_from or "idle", None
        elif cmd == "clear" and self.state == "error":
            self.state, self.error_msg = ("moving" if self.route else "idle"), None
        elif cmd == "charge" and self.state == "idle":
            self._go_charge()
        elif cmd == "go" and self.state == "idle":
            self._new_task()
        else:
            return False
        return True

    # ---------- simulation ----------
    def _go_charge(self):
        name = min(CHARGERS, key=lambda c: math.dist((self.x, self.y), CHARGERS[c]))
        self.task = f"charge@{name}"
        self.route = [(name, CHARGERS[name])]
        self.charger = name
        self.state = "moving"

    def _new_task(self):
        pick = random.choice([s for s in STATIONS if not s.startswith("P")])
        drop = random.choice(["P1", "P2"])
        self.task = f"{pick}->{drop}"
        self.route = [(pick, STATIONS[pick]), (drop, STATIONS[drop])]
        self.state = "moving"

    def step(self, dt):
        now = time.time()

        if self.state == "paused":
            self.battery -= DRAIN_IDLE * dt
        elif self.state == "error":
            if now >= self.error_until:
                self.command("clear")
        elif self.state == "charging":
            self.battery = min(100.0, self.battery + CHARGE_RATE * dt)
            if self.battery >= FULL_BATTERY:
                self.state, self.task, self.charger = "idle", None, None
                self.idle_until = now + 1
        elif self.state == "idle":
            self.battery -= DRAIN_IDLE * dt
            if self.battery < LOW_BATTERY:
                self._go_charge()
                if "charge" in self.queue:   # already done by itself
                    self.queue.remove("charge")
            elif self.queue:
                self.command(self.queue.popleft())
            elif now >= self.idle_until:
                self._new_task()
        elif self.state == "moving":
            self._move(dt, now)

        self.battery = max(0.0, self.battery)

    def _move(self, dt, now):
        if random.random() < ERROR_CHANCE:
            self.state = "error"
            self.error_msg = random.choice(["obstacle detected", "lidar timeout", "wheel slip"])
            self.error_until = now + ERROR_SECONDS
            return
        if not self.route:
            self.state = "idle"
            return
        self.target, (tx, ty) = self.route[0]
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        self.heading = math.degrees(math.atan2(dy, dx)) % 360
        travel = SPEED * dt
        self.battery -= DRAIN_MOVING * dt
        if dist <= travel:
            self.x, self.y = float(tx), float(ty)
            self.odometer += dist
            self.route.pop(0)
            if not self.route:
                self._arrive(now)
        else:
            self.x += dx / dist * travel
            self.y += dy / dist * travel
            self.odometer += travel

    def _arrive(self, now):
        self.target = None
        if self.charger:
            self.state = "charging"
        else:
            self.tasks_done += 1
            self.state, self.task = "idle", None
            self.idle_until = now + random.uniform(1, 4)
            if self.battery < LOW_BATTERY:
                self._go_charge()

    def status(self):
        return {
            "id": self.id,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "heading": round(self.heading, 1),
            "battery": round(self.battery, 1),
            "state": self.state,
            "task": self.task,
            "target": self.target,
            "error": self.error_msg,
            "queue": list(self.queue),
            "tasks_done": self.tasks_done,
            "odometer": round(self.odometer, 1),
            "ts": round(time.time(), 3),
        }
