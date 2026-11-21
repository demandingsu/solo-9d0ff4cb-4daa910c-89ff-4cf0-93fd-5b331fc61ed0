"""SQLite 存取层：连接管理、建表、事务。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
-- 旧仓：定位点按采样日（UTC）入仓，模拟迁仓前的既有数据
CREATE TABLE IF NOT EXISTS legacy_points (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_code   TEXT NOT NULL,
    sampled_at  TEXT NOT NULL,          -- ISO-8601 UTC 采样时刻
    sample_date TEXT NOT NULL,          -- sampled_at 的 UTC 日期（旧仓按采样日归集）
    lat         REAL NOT NULL,
    lon         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_legacy_date ON legacy_points (sample_date, trip_code);

-- 新仓：班次，整趟轨迹按发车日（UTC）归仓
CREATE TABLE IF NOT EXISTS trips (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_code        TEXT NOT NULL UNIQUE,
    departure_at     TEXT NOT NULL,     -- ISO-8601 UTC 发车时刻
    departure_date   TEXT NOT NULL,     -- departure_at 的 UTC 日期
    active_in_new    INTEGER NOT NULL DEFAULT 0,  -- 1=供数已切到新仓（粘性，改期后仍从新仓供数）
    current_batch_id INTEGER REFERENCES batches(id),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_date ON trips (departure_date);

-- 提交批次：每次点位导入生成一个全量快照版本，查询只读 current 版本，
-- 保证同一趟车的点位来自同一提交状态
CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id      INTEGER NOT NULL REFERENCES trips(id),
    version      INTEGER NOT NULL,      -- 每趟车自 1 递增
    submitted_at TEXT NOT NULL,
    point_count  INTEGER NOT NULL,
    points_hash  TEXT NOT NULL,
    UNIQUE (trip_id, version)
);

CREATE TABLE IF NOT EXISTS points (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    seq        INTEGER NOT NULL,        -- 定位点编号
    sampled_at TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_batch ON points (batch_id, seq);

-- 供数来源切换记录：存在即表示该日期已切到新仓（单向，不回退）
CREATE TABLE IF NOT EXISTS date_switches (
    service_date TEXT PRIMARY KEY,
    switched_at  TEXT NOT NULL,
    state_token  TEXT NOT NULL          -- 切换时确认的回填状态指纹
);
"""

DEFAULT_DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "migration.db"))


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(path: str) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 保证导入/切换的原子性。"""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def readonly(path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
