"""
Lightweight SQLite-backed stat tracking for the WOLVES | BRAVIA 3953 bot.
Mirrors the core idea behind bots like StatsMaster: governors submit their
own power/kills/deaths periodically, and we snapshot each submission so we
can show growth, leaderboards, and KvK before/after gains.
"""

import sqlite3
import asyncio
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "data" / "stats.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    power INTEGER NOT NULL,
    kills INTEGER NOT NULL,
    deaths INTEGER NOT NULL,
    label TEXT,
    submitted_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user ON snapshots (guild_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_label ON snapshots (guild_id, user_id, label);

CREATE TABLE IF NOT EXISTS kvk_events (
    guild_id INTEGER PRIMARY KEY,
    date_label TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_sync():
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


async def init_db():
    await asyncio.to_thread(_init_sync)


def _add_snapshot_sync(guild_id, user_id, display_name, power, kills, deaths, label, submitted_by):
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO snapshots
               (guild_id, user_id, display_name, power, kills, deaths, label, submitted_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, display_name, power, kills, deaths, label, submitted_by, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


async def add_snapshot(guild_id, user_id, display_name, power, kills, deaths, label, submitted_by):
    await asyncio.to_thread(
        _add_snapshot_sync, guild_id, user_id, display_name, power, kills, deaths, label, submitted_by
    )


def _get_latest_sync(guild_id, user_id):
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM snapshots WHERE guild_id = ? AND user_id = ?
               ORDER BY id DESC LIMIT 1""",
            (guild_id, user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_latest(guild_id, user_id):
    return await asyncio.to_thread(_get_latest_sync, guild_id, user_id)


def _get_first_sync(guild_id, user_id):
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM snapshots WHERE guild_id = ? AND user_id = ?
               ORDER BY id ASC LIMIT 1""",
            (guild_id, user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_first(guild_id, user_id):
    return await asyncio.to_thread(_get_first_sync, guild_id, user_id)


def _get_by_label_sync(guild_id, user_id, label):
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM snapshots WHERE guild_id = ? AND user_id = ? AND label = ?
               ORDER BY id DESC LIMIT 1""",
            (guild_id, user_id, label),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_by_label(guild_id, user_id, label: str):
    return await asyncio.to_thread(_get_by_label_sync, guild_id, user_id, label.strip())


def _get_history_sync(guild_id, user_id, limit):
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM snapshots WHERE guild_id = ? AND user_id = ?
               ORDER BY id DESC LIMIT ?""",
            (guild_id, user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def get_history(guild_id, user_id, limit=10):
    return await asyncio.to_thread(_get_history_sync, guild_id, user_id, limit)


def _get_leaderboard_sync(guild_id, stat, limit):
    # Latest snapshot per user (by row id, which is always unique/ordered),
    # ranked by the requested stat.
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT s.user_id, s.display_name, s.power, s.kills, s.deaths, s.created_at
            FROM snapshots s
            INNER JOIN (
                SELECT user_id, MAX(id) AS max_id
                FROM snapshots
                WHERE guild_id = ?
                GROUP BY user_id
            ) latest ON s.user_id = latest.user_id AND s.id = latest.max_id
            WHERE s.guild_id = ?
            ORDER BY s.{stat} DESC
            LIMIT ?
            """,
            (guild_id, guild_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def get_leaderboard(guild_id, stat: str, limit: int = 10):
    assert stat in ("power", "kills", "deaths")
    return await asyncio.to_thread(_get_leaderboard_sync, guild_id, stat, limit)


def _get_all_labels_users_sync(guild_id, label):
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT s.user_id, s.display_name, s.power, s.kills, s.deaths, s.created_at
            FROM snapshots s
            INNER JOIN (
                SELECT user_id, MAX(created_at) AS max_created
                FROM snapshots
                WHERE guild_id = ? AND label = ?
                GROUP BY user_id
            ) latest ON s.user_id = latest.user_id AND s.created_at = latest.max_created
            WHERE s.guild_id = ? AND s.label = ?
            """,
            (guild_id, label, guild_id, label),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def get_all_with_label(guild_id, label: str):
    """All users' latest snapshot tagged with a given label (e.g. 'KvK Start')."""
    return await asyncio.to_thread(_get_all_labels_users_sync, guild_id, label.strip())


def _set_kvk_event_sync(guild_id, date_label, epoch):
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO kvk_events (guild_id, date_label, epoch, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                 date_label = excluded.date_label,
                 epoch = excluded.epoch,
                 updated_at = excluded.updated_at""",
            (guild_id, date_label, epoch, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


async def set_kvk_event(guild_id, date_label: str, epoch: int):
    await asyncio.to_thread(_set_kvk_event_sync, guild_id, date_label, epoch)


def _get_kvk_event_sync(guild_id):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM kvk_events WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def get_kvk_event(guild_id):
    return await asyncio.to_thread(_get_kvk_event_sync, guild_id)
