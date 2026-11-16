"""Persistent chat watches. Accessed by the bot's single polling thread."""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


class WatchStore:
    def __init__(self, path=":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS watches (
            chat_id INTEGER PRIMARY KEY, lat REAL, lon REAL, radius REAL,
            seen TEXT NOT NULL DEFAULT '{}', next_check REAL NOT NULL DEFAULT 0)""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(watches)")}
        if "location_message_id" not in columns:
            self.db.execute("ALTER TABLE watches ADD COLUMN location_message_id INTEGER")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, title TEXT, kind TEXT,
            first_seen REAL, last_seen REAL, updates INTEGER DEFAULT 0, alerts INTEGER DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS metrics (
            day TEXT, name TEXT, value INTEGER DEFAULT 0, PRIMARY KEY (day, name))""")
        self.db.commit()

    def get(self, chat_id):
        row = self.db.execute("SELECT * FROM watches WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            return None
        watch = dict(row)
        watch["seen"] = json.loads(watch["seen"])
        return watch

    def set_location(self, chat_id, lat, lon, radius, location_message_id=None):
        old = self.get(chat_id)
        # Moving a live pin keeps arrival history to avoid repeat alerts.
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO watches
                (chat_id, lat, lon, radius, seen, next_check, location_message_id)
                VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (chat_id, lat, lon, radius, json.dumps(old["seen"] if old else {}), location_message_id))

    def save(self, watch):
        with self.db:
            # UPDATE cannot resurrect a watch stopped concurrently from the admin page.
            self.db.execute("""UPDATE watches SET seen=?, next_check=? WHERE chat_id=?""",
                            (json.dumps(watch["seen"]), watch["next_check"], watch["chat_id"]))

    def delete(self, chat_id):
        with self.db:
            self.db.execute("DELETE FROM watches WHERE chat_id=?", (chat_id,))

    def due(self, now):
        ids = self.db.execute("SELECT chat_id FROM watches WHERE next_check <= ? ORDER BY next_check", (now,)).fetchall()
        return [watch for row in ids if (watch := self.get(row["chat_id"])) is not None]

    def offset(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        return row[0] if row else 0

    def set_offset(self, offset):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('offset', ?)", (offset,))

    def record_chat(self, chat):
        now = time.time()
        title = chat.get("title") or " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name")).strip()
        title = title or chat.get("username") or str(chat["id"])
        with self.db:
            self.db.execute("""INSERT INTO chats (chat_id, title, kind, first_seen, last_seen, updates)
                VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title, kind=excluded.kind, last_seen=excluded.last_seen, updates=updates+1""",
                (chat["id"], str(title)[:160], chat.get("type", "unknown"), now, now))
        self.metric("updates")

    def metric(self, name, amount=1):
        day = datetime.fromtimestamp(time.time(), timezone.utc).date().isoformat()
        with self.db:
            self.db.execute("""INSERT INTO metrics VALUES (?, ?, ?)
                ON CONFLICT(day, name) DO UPDATE SET value=value+excluded.value""", (day, name, amount))

    def alert_sent(self, chat_id, aircraft_count):
        with self.db:
            self.db.execute("UPDATE chats SET alerts=alerts+1 WHERE chat_id=?", (chat_id,))
        self.metric("alerts")
        self.metric("aircraft_alerted", aircraft_count)

    def dashboard(self):
        chats = [dict(row) for row in self.db.execute("""SELECT c.*, w.radius, w.lat, w.lon, w.next_check,
            CASE WHEN w.chat_id IS NULL THEN 0 ELSE 1 END AS watching
            FROM chats c LEFT JOIN watches w ON c.chat_id=w.chat_id ORDER BY c.last_seen DESC""")]
        metrics = [dict(row) for row in self.db.execute("SELECT * FROM metrics ORDER BY day")]
        heartbeat = self.db.execute("SELECT value FROM settings WHERE key='heartbeat'").fetchone()
        return dict(chats=chats, metrics=metrics, heartbeat=heartbeat[0] if heartbeat else None,
                    active_watches=self.db.execute("SELECT COUNT(*) FROM watches").fetchone()[0],
                    generated_at=time.time())

    def heartbeat(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('heartbeat', ?)", (int(time.time()),))
