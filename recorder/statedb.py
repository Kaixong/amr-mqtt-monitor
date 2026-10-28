"""SQLite state log: one row per closed state interval of an AMR."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_intervals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    amr_id      TEXT NOT NULL,
    state       TEXT NOT NULL,
    started_at  REAL NOT NULL,
    ended_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS state_intervals_ended ON state_intervals (ended_at);
"""


class StateDB:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)

    def add(self, intervals):
        if intervals:
            self.db.executemany("INSERT INTO state_intervals (amr_id, state, started_at, ended_at)"
                                " VALUES (?, ?, ?, ?)", intervals)
            self.db.commit()

    def durations(self, since, now):
        """Seconds per AMR and state within [since, now]: {amr: {state: seconds}}."""
        rows = self.db.execute(
            "SELECT amr_id, state, SUM(MIN(ended_at, :now) - MAX(started_at, :since))"
            " FROM state_intervals WHERE ended_at > :since AND started_at < :now"
            " GROUP BY amr_id, state", {"since": since, "now": now})
        out = {}
        for amr_id, state, secs in rows:
            out.setdefault(amr_id, {})[state] = secs
        return out

    def first_record(self):
        return self.db.execute("SELECT MIN(started_at) FROM state_intervals").fetchone()[0]

    def prune(self, before):
        self.db.execute("DELETE FROM state_intervals WHERE ended_at < ?", (before,))
        self.db.commit()
