#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地数据层（多场馆只读版）：设置、快照、提醒记录、每周规律、日志"""
import copy
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from venues import default_venues

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
LEGACY_CONFIG = BASE_DIR / "config.json"
SCHEMA_VERSION = 2

DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "notify": {
        "channel": "email",
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_user": "",
        "smtp_pass": "",
        "mail_from": "",
        "mail_to": "",
        "webhook_type": "wecom",
        "webhook_url": "",
    },
    "monitor": {
        "min_interval_min": 15,
        "default_interval_min": 30,
        "jitter": 0.2,
        "active_start": "07:00",
        "active_end": "22:00",
        "max_requests_per_hour": 60,
        "dedup_minutes": 0,
        "alert_quota_full": True,
    },
    "venues": default_venues(),
    "system": {
        "prevent_sleep": False,
        "local_alert": False,
    },
    "paused": True,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
    venue_id TEXT NOT NULL,
    date TEXT NOT NULL,
    field_id TEXT NOT NULL,
    slot INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(venue_id, date, field_id, slot)
);
CREATE TABLE IF NOT EXISTS date_state(
    venue_id TEXT NOT NULL,
    date TEXT NOT NULL,
    date_id TEXT,
    is_all INTEGER DEFAULT 0,
    reads INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(venue_id, date)
);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    date TEXT NOT NULL,
    field_id TEXT NOT NULL,
    field_name TEXT,
    slot INTEGER NOT NULL,
    prev TEXT,
    kind TEXT
);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pattern_week(
    week TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    field_id TEXT NOT NULL,
    closed INTEGER DEFAULT 0,
    open INTEGER DEFAULT 0,
    PRIMARY KEY(week, venue_id, weekday, slot, field_id)
);
"""


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _seed_from_legacy() -> dict:
    """首次运行：从旧 config.json 读取账号（文件已归档时返回空）"""
    if not LEGACY_CONFIG.exists():
        return {}
    try:
        raw = LEGACY_CONFIG.read_bytes()
        try:
            old = json.loads(raw.decode("utf-8-sig"))
        except UnicodeDecodeError:
            old = json.loads(raw.decode("gbk"))
    except Exception:
        return {}
    seed = {}
    for key in ("username", "password"):
        if old.get(key):
            seed[key] = old[key]
    return seed


def _normalize_venues(cfg: dict) -> list:
    """按场馆 id 合并用户设置，补齐新增场馆，夹紧数值范围"""
    old = {}
    for v in (cfg.get("venues") or []):
        if isinstance(v, dict) and v.get("id"):
            old[v["id"]] = v
    out = []
    for d in default_venues():
        o = old.get(d["id"], {})
        item = dict(d)
        for key in ("enabled", "interval_min", "days", "times", "fields", "known_fields"):
            if key in o:
                item[key] = o[key]
        item["enabled"] = bool(item.get("enabled"))
        try:
            item["interval_min"] = max(15, int(item.get("interval_min") or 30))
        except (TypeError, ValueError):
            item["interval_min"] = 30
        try:
            item["days"] = max(1, min(7, int(item.get("days") or 7)))
        except (TypeError, ValueError):
            item["days"] = 7
        item["times"] = list(item.get("times") or [])
        item["fields"] = list(item.get("fields") or [])
        item["known_fields"] = list(item.get("known_fields") or [])
        out.append(item)
    return out


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = str(path)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)
        self._ensure_columns()
        self._migrate_if_needed()
        if self._get_setting("config") is None:
            self.save_config(deep_merge(DEFAULT_CONFIG, _seed_from_legacy()))

    # ---------- 基础 ----------
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _get_setting(self, key: str):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def _set_setting(self, key: str, value: str):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _ensure_columns(self):
        with self._lock, self._conn() as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(date_state)").fetchall()]
            if "reads" not in cols:
                c.execute("ALTER TABLE date_state ADD COLUMN reads INTEGER DEFAULT 0")

    def _migrate_if_needed(self):
        try:
            version = int(self._get_setting("schema_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= SCHEMA_VERSION:
            return
        # 旧版单场馆数据全部丢弃，保留账号/邮箱/系统设置
        old = {}
        raw = self._get_setting("config")
        if raw:
            try:
                old = json.loads(raw)
            except json.JSONDecodeError:
                old = {}
        with self._lock, self._conn() as c:
            for table in ("snapshots", "date_state", "alerts", "pattern_week", "orders"):
                c.execute(f"DROP TABLE IF EXISTS {table}")
            c.executescript(SCHEMA)
        cfg = deep_merge(DEFAULT_CONFIG, {
            "username": old.get("username", ""),
            "password": old.get("password", ""),
            "notify": old.get("notify") or DEFAULT_CONFIG["notify"],
            "system": old.get("system") or DEFAULT_CONFIG["system"],
            "paused": True,
        })
        self.save_config(cfg)
        self._set_setting("schema_version", str(SCHEMA_VERSION))
        self.log("INFO", "已清空旧快照/规律/提醒，切换到多场馆只读版")

    # ---------- 配置 ----------
    @staticmethod
    def _strip_legacy(cfg: dict) -> dict:
        for key in ("venue", "grab", "auto_grab", "clientUid"):
            cfg.pop(key, None)
        mon = cfg.get("monitor")
        if isinstance(mon, dict):
            mon.pop("alert_on_release", None)
            mon.pop("alert_on_full", None)
        return cfg

    def get_config(self) -> dict:
        raw = self._get_setting("config")
        if not raw:
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            cfg = deep_merge(DEFAULT_CONFIG, json.loads(raw))
        except json.JSONDecodeError:
            return copy.deepcopy(DEFAULT_CONFIG)
        cfg = self._strip_legacy(cfg)
        cfg["venues"] = _normalize_venues(cfg)
        cfg["paused"] = bool(cfg.get("paused", True))
        return cfg

    def save_config(self, cfg: dict) -> dict:
        merged = deep_merge(DEFAULT_CONFIG, cfg)
        merged = self._strip_legacy(merged)
        merged["venues"] = _normalize_venues(merged)
        merged["paused"] = bool(merged.get("paused", True))
        self._set_setting("config", json.dumps(merged, ensure_ascii=False))
        return merged

    # ---------- 日志 ----------
    def log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = str(msg)
        if len(msg) > 2000:
            msg = msg[:2000] + "…（已截断）"
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO logs(ts,level,msg) VALUES(?,?,?)", (ts, level, msg))
            c.execute("DELETE FROM logs WHERE id NOT IN "
                      "(SELECT id FROM logs ORDER BY id DESC LIMIT 5000)")

    def get_logs(self, limit: int = 200) -> list:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT ts,level,msg FROM logs ORDER BY id DESC LIMIT ?",
                             (int(limit),)).fetchall()
        return [dict(r) for r in rows][::-1]

    # ---------- 快照 ----------
    def get_snapshots(self, venue_id: str) -> dict:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT date,field_id,slot,status FROM snapshots WHERE venue_id=?",
                (venue_id,)).fetchall()
        return {(r["date"], r["field_id"], r["slot"]): r["status"] for r in rows}

    def set_snapshot(self, venue_id: str, date: str, field_id: str,
                     slot: int, status: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO snapshots(venue_id,date,field_id,slot,status,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(venue_id,date,field_id,slot) DO UPDATE SET "
                "status=excluded.status, updated_at=excluded.updated_at",
                (venue_id, date, field_id, int(slot), str(status), ts),
            )

    def get_date_states(self, venue_id: str) -> dict:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT date,date_id,is_all,reads FROM date_state WHERE venue_id=?",
                (venue_id,)).fetchall()
        return {r["date"]: {"date_id": r["date_id"], "is_all": bool(r["is_all"]),
                            "reads": int(r["reads"] or 0)}
                for r in rows}

    def set_date_state(self, venue_id: str, date: str, date_id: str | None,
                       is_all: bool, reads: int = 0) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO date_state(venue_id,date,date_id,is_all,reads,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(venue_id,date) DO UPDATE SET "
                "date_id=excluded.date_id, is_all=excluded.is_all, "
                "reads=excluded.reads, updated_at=excluded.updated_at",
                (venue_id, date, date_id, 1 if is_all else 0, int(reads), ts),
            )

    def prune_before(self, date_str: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM snapshots WHERE date < ?", (date_str,))
            c.execute("DELETE FROM date_state WHERE date < ?", (date_str,))

    def venue_summary(self, venue_id: str, date_from: str,
                      date_to: str | None = None) -> dict:
        where = "venue_id=? AND date>=?"
        params = [venue_id, date_from]
        if date_to:
            where += " AND date<=?"
            params.append(date_to)
        with self._lock, self._conn() as c:
            row = c.execute(
                f"SELECT COUNT(*) AS free, MAX(updated_at) AS updated "
                f"FROM snapshots WHERE {where} AND status='0'", params).fetchone()
            total = c.execute(
                f"SELECT COUNT(*) AS n FROM snapshots WHERE {where}", params).fetchone()
        return {"free": int(row["free"] or 0), "cells": int(total["n"] or 0),
                "updated": row["updated"]}

    def clear_venue(self, venue_id: str) -> None:
        """重新启用场馆时清掉旧快照，让首轮扫描重新建基线"""
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM snapshots WHERE venue_id=?", (venue_id,))
            c.execute("DELETE FROM date_state WHERE venue_id=?", (venue_id,))

    # ---------- 提醒去重 ----------
    def last_alert_ts(self, venue_id: str, date: str, field_id: str,
                      slot: int) -> str | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT ts FROM alerts WHERE venue_id=? AND date=? AND field_id=? "
                "AND slot=? ORDER BY id DESC LIMIT 1",
                (venue_id, date, field_id, int(slot))).fetchone()
        return row["ts"] if row else None

    def record_alert(self, venue_id: str, date: str, field_id: str,
                     field_name: str, slot: int, prev: str, kind: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO alerts(ts,venue_id,date,field_id,field_name,slot,prev,kind) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ts, venue_id, date, field_id, field_name, int(slot), str(prev), kind),
            )

    def last_alert_id(self) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT COALESCE(MAX(id),0) AS n FROM alerts").fetchone()
        return int(row["n"] or 0)

    # ---------- 每周规律 ----------
    def bump_pattern(self, week: str, venue_id: str, weekday: int, slot: int,
                     field_id: str, status: str) -> None:
        if status not in ("-1", "0", "-3"):
            return
        col = "closed" if status == "-1" else "open"
        with self._lock, self._conn() as c:
            c.execute(
                f"INSERT INTO pattern_week(week,venue_id,weekday,slot,field_id,{col}) "
                f"VALUES(?,?,?,?,?,1) ON CONFLICT(week,venue_id,weekday,slot,field_id) "
                f"DO UPDATE SET {col}={col}+1",
                (week, venue_id, int(weekday), int(slot), field_id),
            )

    def get_patterns(self, venue_id: str) -> dict:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT week,weekday,slot,field_id,closed,open FROM pattern_week "
                "WHERE venue_id=?", (venue_id,)).fetchall()
        out = {}
        for r in rows:
            out.setdefault((r["weekday"], r["slot"], r["field_id"]), {})[r["week"]] = (
                r["closed"], r["open"])
        return out

    def prune_patterns(self, keep_weeks: int = 8) -> None:
        with self._lock, self._conn() as c:
            weeks = [r["week"] for r in c.execute(
                "SELECT DISTINCT week FROM pattern_week ORDER BY week DESC LIMIT ?",
                (keep_weeks,)).fetchall()]
            if weeks:
                marks = ",".join("?" * len(weeks))
                c.execute(f"DELETE FROM pattern_week WHERE week NOT IN ({marks})", weeks)
