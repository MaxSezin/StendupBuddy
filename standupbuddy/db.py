import sqlite3

from .config import DB_PATH


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (tg_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            invite_code TEXT UNIQUE NOT NULL,
            tz TEXT NOT NULL DEFAULT 'UTC',
            reminder_time TEXT,
            reminder_days TEXT,
            managers_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS team_members (team_id INTEGER NOT NULL, tg_id INTEGER NOT NULL, UNIQUE(team_id, tg_id));
        CREATE TABLE IF NOT EXISTS standups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            date_iso TEXT NOT NULL,
            started_utc TEXT NOT NULL,
            remind_job_key TEXT,
            summary_job_key TEXT
        );
        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            standup_id INTEGER NOT NULL,
            tg_id INTEGER NOT NULL,
            text TEXT,
            created_utc TEXT,
            answered INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()


