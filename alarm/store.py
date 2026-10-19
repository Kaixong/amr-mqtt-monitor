"""SQLite alarm log: one row per alarm occurrence (raised -> cleared, acknowledged)."""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    source      TEXT NOT NULL,
    type        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}',
    raised_at   REAL NOT NULL,
    cleared_at  REAL,
    acked_at    REAL,
    acked_by    TEXT
);
CREATE INDEX IF NOT EXISTS alarms_open ON alarms (key) WHERE cleared_at IS NULL;
CREATE INDEX IF NOT EXISTS alarms_raised ON alarms (raised_at);
"""


class AlarmStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    @staticmethod
    def _row(r):
        d = dict(r)
        d["details"] = json.loads(d["details"])
        return d

    def get(self, alarm_id):
        r = self.db.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        return self._row(r) if r else None

    def open_alarms(self):
        """Alarms whose condition has not cleared yet, keyed by condition key."""
        rows = self.db.execute("SELECT * FROM alarms WHERE cleared_at IS NULL ORDER BY id")
        return {r["key"]: self._row(r) for r in rows}

    def raise_(self, cond, now):
        cur = self.db.execute(
            "INSERT INTO alarms (key, source, type, severity, message, details, raised_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cond.key, cond.source, cond.type, cond.severity, cond.message,
             json.dumps(cond.details, ensure_ascii=False), now))
        self.db.commit()
        return self.get(cur.lastrowid)

    def clear(self, alarm_id, now):
        self.db.execute("UPDATE alarms SET cleared_at = ? WHERE id = ? AND cleared_at IS NULL",
                        (now, alarm_id))
        self.db.commit()
        return self.get(alarm_id)

    def ack(self, ids, by, now):
        """Acknowledge the given alarm ids (or every unacknowledged one if ids is None)."""
        if ids is None:
            rows = self.db.execute("SELECT id FROM alarms WHERE acked_at IS NULL").fetchall()
            ids = [r["id"] for r in rows]
        done = []
        for i in ids:
            cur = self.db.execute(
                "UPDATE alarms SET acked_at = ?, acked_by = ? WHERE id = ? AND acked_at IS NULL",
                (now, by, i))
            if cur.rowcount:
                done.append(i)
        self.db.commit()
        return [self.get(i) for i in done]

    def recent(self, limit):
        rows = self.db.execute("SELECT * FROM alarms ORDER BY id DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]
