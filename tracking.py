"""Persistent chat watches. Accessed by the bot's single polling thread."""

import json
import hashlib
import secrets
import re
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
        if "empty_checks" not in columns:
            self.db.execute("ALTER TABLE watches ADD COLUMN empty_checks INTEGER NOT NULL DEFAULT 0")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, title TEXT, kind TEXT,
            first_seen REAL, last_seen REAL, updates INTEGER DEFAULT 0, alerts INTEGER DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS metrics (
            day TEXT, name TEXT, value INTEGER DEFAULT 0, PRIMARY KEY (day, name))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS api_usage (
            day TEXT, provider TEXT, scope TEXT, requests INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0, rate_limits INTEGER DEFAULT 0,
            PRIMARY KEY(day, provider, scope))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS api_status (
            provider TEXT, scope TEXT, remaining INTEGER, observed_at REAL,
            retry_until REAL, PRIMARY KEY(provider, scope))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS promos (
            id INTEGER PRIMARY KEY, code_hash TEXT UNIQUE, label TEXT, max_uses INTEGER,
            redeem_until REAL, grant_days INTEGER, enabled INTEGER DEFAULT 1, created_at REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS redemptions (
            promo_id INTEGER, chat_id INTEGER, redeemed_at REAL, PRIMARY KEY(promo_id, chat_id))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS access (
            chat_id INTEGER PRIMARY KEY, mode TEXT, credentials TEXT,
            promo_id INTEGER, expires_at REAL, configured_by INTEGER)""")
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
            self.db.execute("""UPDATE watches SET seen=?, next_check=?, empty_checks=? WHERE chat_id=?""",
                            (json.dumps(watch["seen"]), watch["next_check"], watch.get("empty_checks", 0), watch["chat_id"]))

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
        chats = [dict(row) for row in self.db.execute("""SELECT c.*, w.radius, w.lat, w.lon, w.next_check, w.empty_checks,
            CASE WHEN w.chat_id IS NULL THEN 0 ELSE 1 END AS watching
            FROM chats c LEFT JOIN watches w ON c.chat_id=w.chat_id ORDER BY c.last_seen DESC""")]
        metrics = [dict(row) for row in self.db.execute("SELECT * FROM metrics ORDER BY day")]
        heartbeat = self.db.execute("SELECT value FROM settings WHERE key='heartbeat'").fetchone()
        for chat in chats:
            grant = self.get_access(chat["chat_id"])
            chat["access_mode"] = grant["mode"] if grant else "free"
            chat["access_expires_at"] = grant["expires_at"] if grant else None
            chat["watch_paused"] = bool(chat["watching"] and not grant)
            chat["watching"] = int(bool(chat["watching"] and grant))
        promos = [dict(row) for row in self.db.execute("""SELECT p.id, p.label, p.max_uses,
            p.redeem_until, p.grant_days, p.enabled, COUNT(r.chat_id) AS uses
            FROM promos p LEFT JOIN redemptions r ON r.promo_id=p.id GROUP BY p.id ORDER BY p.id DESC""")]
        today = datetime.now(timezone.utc).date().isoformat()
        usage = [dict(row) for row in self.db.execute("SELECT * FROM api_usage WHERE day=?", (today,))]
        status = [dict(row) for row in self.db.execute("SELECT * FROM api_status")]
        return dict(chats=chats, metrics=metrics, promos=promos, api_usage=usage, api_status=status,
                    heartbeat=heartbeat[0] if heartbeat else None,
                    active_watches=sum(chat["watching"] for chat in chats),
                    generated_at=time.time())

    def heartbeat(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('heartbeat', ?)", (int(time.time()),))

    def create_promo(self, label, max_uses=1, redeem_days=7, grant_days=30, code=""):
        if not (1 <= max_uses <= 10000 and 1 <= redeem_days <= 365 and 1 <= grant_days <= 365):
            raise ValueError("Use limits must be 1–10000; expiry and access days must be 1–365.")
        if not isinstance(code, str):
            raise ValueError("Custom code must be text.")
        code = code.strip().upper()
        if code and not re.fullmatch(r"[A-Z0-9_-]{6,64}", code):
            raise ValueError("Custom code must be 6–64 letters, numbers, hyphens or underscores.")
        code = code or "AIR-" + secrets.token_hex(12).upper()
        digest = hashlib.sha256(code.encode()).hexdigest()
        try:
            with self.db:
                cursor = self.db.execute("""INSERT INTO promos
                    (code_hash, label, max_uses, redeem_until, grant_days, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                    (digest, str(label).strip()[:80] or "Promo", max_uses, time.time() + redeem_days * 86400, grant_days, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError("This promo code already exists. Choose a different code.") from None
        return dict(id=cursor.lastrowid, code=code)

    def redeem(self, code, chat_id, user_id):
        digest = hashlib.sha256(code.strip().upper().encode()).hexdigest()
        now = time.time()
        # Serialize redemption with revocation/other users so usage limits cannot be raced.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            promo = self.db.execute("SELECT * FROM promos WHERE code_hash=?", (digest,)).fetchone()
            if not promo or not promo["enabled"] or promo["redeem_until"] <= now:
                return False
            used = self.db.execute("SELECT 1 FROM redemptions WHERE promo_id=? AND chat_id=?", (promo["id"], chat_id)).fetchone()
            if used:
                return False  # Replaying a code never extends or restores a grant.
            count = self.db.execute("SELECT COUNT(*) FROM redemptions WHERE promo_id=?", (promo["id"],)).fetchone()[0]
            if count >= promo["max_uses"]:
                return False
            self.db.execute("INSERT INTO redemptions VALUES (?, ?, ?)", (promo["id"], chat_id, now))
            self.db.execute("INSERT OR REPLACE INTO access VALUES (?, 'promo', NULL, ?, ?, ?)",
                            (chat_id, promo["id"], now + promo["grant_days"] * 86400, user_id))
        return True

    def set_credentials(self, chat_id, ciphertext, user_id):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO access VALUES (?, 'own', ?, NULL, NULL, ?)",
                            (chat_id, ciphertext, user_id))

    def get_access(self, chat_id):
        row = self.db.execute("SELECT * FROM access WHERE chat_id=?", (chat_id,)).fetchone()
        if not row or (row["expires_at"] is not None and row["expires_at"] <= time.time()):
            return None
        if row["mode"] == "promo":
            promo = self.db.execute("SELECT enabled FROM promos WHERE id=?", (row["promo_id"],)).fetchone()
            if not promo or not promo["enabled"]:
                return None
        return dict(row)

    def revoke_access(self, chat_id):
        with self.db:
            self.db.execute("DELETE FROM access WHERE chat_id=?", (chat_id,))
            self.db.execute("DELETE FROM watches WHERE chat_id=?", (chat_id,))

    def revoke_promo(self, promo_id):
        with self.db:
            self.db.execute("UPDATE promos SET enabled=0 WHERE id=?", (promo_id,))


class APIUsage:
    """Persist only allowlisted service names and counters, using a connection per worker."""

    def __init__(self, path):
        self.path = path

    def record(self, provider, scope, status, remaining=None, retry_after=0):
        now = time.time()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        try:
            remaining = int(remaining) if remaining is not None else None
            if remaining is not None and remaining < 0:
                remaining = None
        except (ValueError, TypeError, OverflowError):
            remaining = None
        # Customer quotas belong to separate accounts; never combine their balances.
        if scope != "owner":
            remaining = None
        retry_until = now + retry_after if status == 429 else 0
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                db.execute("""INSERT INTO api_usage VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(day, provider, scope) DO UPDATE SET requests=requests+1,
                    errors=errors+excluded.errors, rate_limits=rate_limits+excluded.rate_limits""",
                    (day, provider, scope, int(status == 0 or status >= 400), int(status == 429)))
                db.execute("""INSERT INTO api_status VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(provider, scope) DO UPDATE SET
                    remaining=CASE WHEN excluded.remaining IS NOT NULL THEN excluded.remaining ELSE remaining END,
                    observed_at=CASE WHEN excluded.remaining IS NOT NULL THEN excluded.observed_at ELSE observed_at END,
                    retry_until=MAX(retry_until, excluded.retry_until)""",
                    (provider, scope, remaining, now if remaining is not None else None, retry_until))
        finally:
            db.close()
