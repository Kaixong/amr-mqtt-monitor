"""Turns the AMR status stream into state intervals. Pure logic (no MQTT, no I/O).

Feed observations in with observe() / set_online() / tick(); each returns the intervals that
just closed as (amr_id, state, started_at, ended_at), ready to be stored. Besides the AMR
states (idle, moving, charging, paused, error) an AMR can be "offline": its Last Will
arrived, or it has been silent for longer than offline_after.
"""

OFFLINE = "offline"
STATES = ["moving", "charging", "idle", "paused", "error", OFFLINE]
WORKING = "moving"   # utilization = share of observed time spent driving a task


class StateTracker:
    def __init__(self, offline_after=3.0):
        self.offline_after = offline_after
        self.cur = {}          # amr_id -> (state, since)
        self.last_seen = {}    # amr_id -> publisher timestamp of the last fresh status

    def _set(self, amr_id, state, at):
        cur = self.cur.get(amr_id)
        if cur and cur[0] == state:
            return []
        closed = []
        if cur:
            at = max(at, cur[1])
            if at > cur[1]:
                closed.append((amr_id, cur[0], cur[1], at))
        self.cur[amr_id] = (state, at)
        return closed

    def observe(self, amr_id, state, ts, now):
        """A status message. Retained ones can be old: trust the publisher's timestamp."""
        seen = min(now, ts)
        if now - seen > self.offline_after:
            return []   # stale retained message: tells nothing about the present
        self.last_seen[amr_id] = seen
        return self._set(amr_id, state, seen)

    def set_online(self, amr_id, online, now, retained=False):
        """amr/<id>/online. "online" alone changes nothing: the next status tells the state.
        A retained "offline" is old news (it can even arrive after the restarted AMR's first
        status); a real outage is caught by tick() when the statuses stop."""
        if online or retained or amr_id not in self.cur:
            return []
        return self._set(amr_id, OFFLINE, now)

    def tick(self, now):
        """Mark AMRs offline that stopped reporting (no Last Will yet, or no clean exit)."""
        closed = []
        for amr_id, seen in self.last_seen.items():
            if now - seen > self.offline_after and self.cur[amr_id][0] != OFFLINE:
                closed += self._set(amr_id, OFFLINE, seen)
        return closed

    def close_all(self, now):
        """On shutdown: close every open interval at `now` (nothing is observed after it)."""
        closed = [(a, s, since, now) for a, (s, since) in self.cur.items() if now > since]
        self.cur.clear()
        self.last_seen.clear()
        return closed

    def add_open(self, durations, since, now):
        """Add the still-open intervals, clipped to [since, now], to {amr: {state: seconds}}."""
        for amr_id, (state, start) in self.cur.items():
            secs = now - max(start, since)
            if secs > 0:
                d = durations.setdefault(amr_id, {})
                d[state] = d.get(state, 0.0) + secs
        return durations


def utilization(durations):
    """{amr: {state: seconds}} -> {amr: {"total", "utilization" (%), "states": {state: %}}}."""
    out = {}
    for amr_id, d in sorted(durations.items()):
        total = sum(d.values())
        if total <= 0:
            continue
        out[amr_id] = {
            "total": round(total, 1),
            "utilization": round(d.get(WORKING, 0.0) / total * 100, 1),
            "states": {s: round(d[s] / total * 100, 1) for s in STATES if d.get(s)},
        }
    return out
