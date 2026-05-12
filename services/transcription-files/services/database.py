"""
services/database.py - SQLite לוגים ומצב שיחות
"""
import sqlite3, os, logging
from datetime import datetime

log = logging.getLogger(__name__)
DB_PATH = os.environ.get('DATABASE_PATH', 'transcription.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                id            TEXT PRIMARY KEY,
                caller        TEXT,
                status        TEXT DEFAULT 'incoming',
                destination   TEXT DEFAULT 'email',
                target_address TEXT,
                transcript    TEXT,
                summary       TEXT,
                created_at    TEXT,
                updated_at    TEXT
            )
        """)
    log.info("DB מוכן")


def save_call(call_id, caller):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO calls (id, caller, created_at, updated_at) VALUES (?,?,?,?)", (call_id, caller, now, now))


def update_call_status(call_id, status):
    with get_db() as conn:
        conn.execute("UPDATE calls SET status=?, updated_at=? WHERE id=?", (status, datetime.utcnow().isoformat(), call_id))


def set_delivery_preference(call_id, destination, target_address=None):
    with get_db() as conn:
        conn.execute("UPDATE calls SET destination=?, target_address=?, updated_at=? WHERE id=?",
                     (destination, target_address, datetime.utcnow().isoformat(), call_id))


def save_transcript(call_id, transcript, summary=''):
    with get_db() as conn:
        conn.execute("UPDATE calls SET transcript=?, summary=?, status='transcribed', updated_at=? WHERE id=?",
                     (transcript, summary, datetime.utcnow().isoformat(), call_id))


def get_call(call_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return dict(row) if row else None
