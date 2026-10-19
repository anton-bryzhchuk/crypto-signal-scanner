import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen_messages (
                hash TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS monitored_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                title TEXT,
                topic_ids TEXT DEFAULT '',
                added_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS priority_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT UNIQUE NOT NULL,
                username TEXT,
                note TEXT,
                added_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                score INTEGER,
                source TEXT,
                feedback INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS funding_seen (
                key TEXT PRIMARY KEY,
                rate REAL,
                seen_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS vip_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT UNIQUE NOT NULL,
                note TEXT,
                added_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS spam_sources (
                source TEXT PRIMARY KEY,
                count INTEGER DEFAULT 1,
                added_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS alert_cooldown (
                key TEXT PRIMARY KEY,
                sent_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS vip_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                topic_id INTEGER NOT NULL,
                note TEXT,
                added_at TEXT DEFAULT (datetime('now')),
                UNIQUE(chat_id, topic_id)
            );

            CREATE TABLE IF NOT EXISTS funding_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                rate REAL NOT NULL,
                recorded_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_fh_key ON funding_history(exchange, symbol, recorded_at);
        """)


def is_seen(text: str) -> bool:
    h = hashlib.md5(text.encode()).hexdigest()
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM seen_messages WHERE hash=?", (h,)).fetchone()
    return row is not None


def mark_seen(text: str):
    h = hashlib.md5(text.encode()).hexdigest()
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO seen_messages(hash) VALUES(?)", (h,))


def save_alert(type_: str, content: str, score: int, source: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts(type, content, score, source) VALUES(?,?,?,?)",
            (type_, content, score, source)
        )
        return cur.lastrowid


def update_feedback(alert_id: int, value: int):
    """value: 1 = корисно, -1 = непотріб"""
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET feedback=? WHERE id=?", (value, alert_id))


def get_chats() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM monitored_chats").fetchall()]


def add_chat(chat_id: str, title: str = "", topic_ids: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO monitored_chats(chat_id, title, topic_ids) VALUES(?,?,?)",
            (chat_id, title, topic_ids)
        )


def set_chat_topics(chat_id: str, topic_ids: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE monitored_chats SET topic_ids=? WHERE chat_id=?",
            (topic_ids, chat_id)
        )


def remove_chat(chat_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM monitored_chats WHERE chat_id=?", (chat_id,))


def get_priority_users() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM priority_users").fetchall()]


def add_priority_user(user_id: str, username: str = "", note: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO priority_users(user_id, username, note) VALUES(?,?,?)",
            (user_id, username, note)
        )


def remove_priority_user(user_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM priority_users WHERE user_id=?", (user_id,))


def save_funding_rate(exchange: str, symbol: str, rate: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO funding_history(exchange, symbol, rate) VALUES(?,?,?)",
            (exchange, symbol, rate)
        )
        # Зберігаємо тільки останні 20 записів на пару
        conn.execute("""
            DELETE FROM funding_history WHERE id IN (
                SELECT id FROM funding_history
                WHERE exchange=? AND symbol=?
                ORDER BY recorded_at DESC
                LIMIT -1 OFFSET 20
            )
        """, (exchange, symbol))


def get_funding_history(exchange: str, symbol: str, hours: int = 2) -> list[float]:
    """Повертає список ставок за останні N годин"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT rate FROM funding_history
            WHERE exchange=? AND symbol=?
            AND recorded_at >= datetime('now', ? || ' hours')
            ORDER BY recorded_at DESC
        """, (exchange, symbol, f"-{hours}")).fetchall()
    return [r["rate"] for r in rows]


def add_vip_user(user_id: str, note: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO vip_users(user_id, note) VALUES(?,?)",
            (user_id, note)
        )


def remove_vip_user(user_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM vip_users WHERE user_id=?", (user_id,))


def get_vip_users() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM vip_users").fetchall()]


def add_vip_topic(chat_id: str, topic_id: int, note: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO vip_topics(chat_id, topic_id, note) VALUES(?,?,?)",
            (chat_id, topic_id, note)
        )


def remove_vip_topic(chat_id: str, topic_id: int):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM vip_topics WHERE chat_id=? AND topic_id=?",
            (chat_id, topic_id)
        )


def get_vip_topics() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM vip_topics").fetchall()]


def add_spam_source(source: str):
    """Додає джерело в список спам-джерел."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO spam_sources(source, count) VALUES(?,1) "
            "ON CONFLICT(source) DO UPDATE SET count=count+1",
            (source,)
        )


def is_spam_source(source: str) -> bool:
    """Перевіряє чи джерело позначено як спам (≥2 разів)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM spam_sources WHERE source=?", (source,)
        ).fetchone()
    return row is not None and row["count"] >= 2


def is_cooldown(key: str, minutes: int = 60) -> bool:
    """True якщо ключ вже надсилався протягом N хвилин."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM alert_cooldown WHERE key=? "
            "AND sent_at >= datetime('now', ? || ' minutes')",
            (key, f"-{minutes}")
        ).fetchone()
    return row is not None


def set_cooldown(key: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO alert_cooldown(key, sent_at) VALUES(?, datetime('now'))",
            (key,)
        )


def is_funding_seen(key: str, rate: float) -> bool:
    """Не спамити однаковим funding rate"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rate FROM funding_seen WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return False
        return abs(row["rate"] - rate) < 0.005  # різниця менше 0.005% — вже бачили


def upsert_funding_seen(key: str, rate: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO funding_seen(key, rate) VALUES(?,?)",
            (key, rate)
        )
