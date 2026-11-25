#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地网页（多场馆只读版）：场馆选择、定时提醒、实时空位

没有下单/支付接口。默认暂停；暂停时所有读取接口直接拒绝。
"""
import ctypes
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from api_client import SLOT_LABELS
from core import status_text
from monitor import Monitor, cn_date
from notify import Notifier
from store import Store, deep_merge

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True

store = Store()
notifier = Notifier(store)
monitor = Monitor(store, notifier)

STATE = {
    "avail": {},          # venue_id -> payload
    "avail_ts": {},       # venue_id -> timestamp
    "last_scan": {},      # venue_id -> datetime
    "next_scan": {},      # venue_id -> datetime
    "request_times": [],  # 最近一小时的请求时间戳
    "last_error": "",
}

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _paused() -> bool:
    return bool(store.get_config().get("paused"))


def _hm(text: str) -> tuple:
    h, m = (int(x) for x in str(text).split(":")[:2])
    return h, m


def in_active_hours(cfg: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    m = cfg.get("monitor") or {}
    sh, sm = _hm(m.get("active_start") or "07:00")
    eh, em = _hm(m.get("active_end") or "22:00")
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start <= now < end


def _enabled(cfg: dict) -> list:
    return [v for v in (cfg.get("venues") or []) if v.get("enabled")]


def _budget_used() -> int:
    cutoff = time.time() - 3600
    STATE["request_times"] = [t for t in STATE["request_times"] if t > cutoff]
    return len(STATE["request_times"])


def _budget_ok(estimate: int) -> bool:
    cfg = store.get_config()
    cap = int((cfg.get("monitor") or {}).get("max_requests_per_hour", 60))
    return _budget_used() + estimate <= cap


class Worker(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="monitor-worker")
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                STATE["last_error"] = f"{type(e).__name__}: {e}"
                store.log("ERROR", f"后台线程异常：{STATE['last_error']}")
                time.sleep(5)
            time.sleep(1)

    def _tick(self):
        cfg = store.get_config()

        if (cfg.get("system") or {}).get("prevent_sleep"):
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            except Exception:
                pass

        if cfg.get("paused") or not in_active_hours(cfg):
            return

        venues = _enabled(cfg)
        if not venues:
            return

        now = datetime.now()
        due = []
        for v in venues:
            nxt = STATE["next_scan"].get(v["id"])
            if nxt is None or now >= nxt:
                due.append(v)
        if not due:
            return
        due.sort(key=lambda v: STATE["next_scan"].get(v["id"]) or datetime.min)
        venue = due[0]

        cap = int((cfg.get("monitor") or {}).get("max_requests_per_hour", 60))
        estimate = 1 + min(int(venue.get("days", 7)), 7)
        if _budget_used() + estimate > cap:
            STATE["next_scan"][venue["id"]] = now + timedelta(seconds=60)
            return

        res = monitor.scan_venue(venue, source="定时")
        used = int(res.get("requests") or 1)
        STATE["request_times"].extend([time.time()] * used)
        STATE["last_scan"][venue["id"]] = now
        STATE["avail"].pop(venue["id"], None)

        interval = max(15, int(venue.get("interval_min", 30)))
        jitter = float((cfg.get("monitor") or {}).get("jitter", 0.2) or 0)
        offset = interval * 60 * jitter * (random.random() * 2 - 1)
        STATE["next_scan"][venue["id"]] = now + timedelta(seconds=interval * 60 + offset)


worker = Worker()


# ---------------------------------------------------------------- 工具

def _public_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg, ensure_ascii=False))
    out.pop("venues", None)
    out["has_password"] = bool(cfg.get("password"))
    out["password"] = ""
    n = out.get("notify") or {}
    n["has_smtp_pass"] = bool((cfg.get("notify") or {}).get("smtp_pass"))
    n["smtp_pass"] = ""
    out["notify"] = n
    return out


def _validate(cfg: dict) -> list:
    errs = []
    m = cfg.get("monitor") or {}
    try:
        cap = int(m.get("max_requests_per_hour", 60))
        if not (1 <= cap <= 600):
            errs.append("每小时请求上限应在 1~600 之间")
    except (TypeError, ValueError):
        errs.append("每小时请求上限必须是整数")
    for key in ("active_start", "active_end"):
        try:
            h, mi = (int(x) for x in str(m.get(key, "")).split(":")[:2])
            if not (0 <= h < 24 and 0 <= mi < 60):
                raise ValueError
        except ValueError:
            errs.append(f"{key} 时间格式应为 HH:MM")
    try:
        port = int((cfg.get("notify") or {}).get("smtp_port", 465))
        if not (0 < port < 65536):
            raise ValueError
    except (TypeError, ValueError):
        errs.append("SMTP 端口不合法")
    return errs


def _venue_or_404(venue_id: str):
    cfg = store.get_config()
    venue = next((v for v in cfg.get("venues") or [] if v["id"] == venue_id), None)
    return cfg, venue


def _availability_payload(venue: dict) -> dict:
    cfg = store.get_config()
    engine = monitor.get_engine(cfg)
    result = engine.scan(venue, detail="full")
    srv = datetime.strptime(result["server_date"], "%Y-%m-%d").date()
    end = srv + timedelta(days=max(1, int(venue.get("days", 7))))
    dates_raw = [d for d in result["dates"] if d["date"] <= end.isoformat()]

    predicted = monitor.predict_closed(venue["id"])
    for item in dates_raw:
        fields = item.get("fields")
        if not fields:
            continue
        d = datetime.strptime(item["date"], "%Y-%m-%d").date()
        week = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        for f in fields:
            for idx, p in enumerate(f.get("priceList") or []):
                store.bump_pattern(week, venue["id"], d.weekday(), idx,
                                   f.get("fieldId"), str(p.get("status")))

    dates = []
    for item in dates_raw:
        d = datetime.strptime(item["date"], "%Y-%m-%d").date()
        row = {
            "date": item["date"],
            "label": cn_date(item["date"]),
            "weekday": d.weekday(),
            "is_all": item["is_all"],
            "fields": [],
        }
        for f in item.get("fields") or []:
            slots = []
            for idx, p in enumerate(f.get("priceList") or []):
                st = str(p.get("status"))
                slots.append({
                    "idx": idx,
                    "label": SLOT_LABELS[idx],
                    "status": st,
                    "text": status_text(st),
                    "predicted_closed": (d.weekday(), idx, f.get("fieldId")) in predicted
                                        and st != "0",
                })
            row["fields"].append({
                "field_id": f.get("fieldId"),
                "field_name": f.get("fieldName"),
                "slots": slots,
            })
        dates.append(row)
    return {
        "venue_id": venue["id"],
        "server_date": result["server_date"],
        "requests": result["requests"],
        "dates": dates,
        "slots": SLOT_LABELS,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------- 页面 / API

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    cfg = store.get_config()
    paused = bool(cfg.get("paused"))
    enabled = _enabled(cfg)
    today = datetime.now().date().isoformat()

    venues = []
    for v in cfg.get("venues") or []:
        date_to = (datetime.now().date() + timedelta(days=max(1, int(v.get("days", 7))))).isoformat()
        summary = store.venue_summary(v["id"], today, date_to)
        venues.append({
            "id": v["id"],
            "name": v["name"],
            "campus": v["campus"],
            "sport": v["sport"],
            "note": v.get("note", ""),
            "enabled": bool(v.get("enabled")),
            "interval_min": v.get("interval_min"),
            "days": v.get("days"),
            "times": v.get("times") or [],
            "fields": v.get("fields") or [],
            "known_fields": v.get("known_fields") or [],
            "free": summary["free"],
            "cells": summary["cells"],
            "last_scan": STATE["last_scan"].get(v["id"]).strftime("%H:%M:%S")
                         if STATE["last_scan"].get(v["id"]) else None,
            "next_scan": STATE["next_scan"].get(v["id"]).strftime("%H:%M:%S")
                         if STATE["next_scan"].get(v["id"]) and not paused else None,
        })

    return jsonify({
        "ok": True,
        "config": _public_config(cfg),
        "venues": venues,
        "status": {
            "paused": paused,
            "read_only": True,
            "active_hours": f"{(cfg.get('monitor') or {}).get('active_start')}"
                            f"~{(cfg.get('monitor') or {}).get('active_end')}",
            "in_active_hours": in_active_hours(cfg),
            "enabled_count": len(enabled),
            "requests_last_hour": _budget_used(),
            "max_requests_per_hour": int((cfg.get("monitor") or {})
                                         .get("max_requests_per_hour", 60)),
            "last_error": STATE["last_error"],
            "logged_in": monitor.engine is not None,
        },
        "email_ready": notifier.email_ready(),
        "last_alert_id": store.last_alert_id(),
    })


@app.get("/api/venues")
def api_venues():
    cfg = store.get_config()
    today = datetime.now().date().isoformat()
    out = []
    for v in cfg.get("venues") or []:
        date_to = (datetime.now().date() + timedelta(days=max(1, int(v.get("days", 7))))).isoformat()
        summary = store.venue_summary(v["id"], today, date_to)
        item = dict(v)
        item["free"] = summary["free"]
        item["cells"] = summary["cells"]
        out.append(item)
    return jsonify({"ok": True, "venues": out})


@app.post("/api/venues")
def api_save_venues():
    body = request.get_json(force=True, silent=True) or {}
    incoming = body.get("venues") or []
    cfg = store.get_config()
    by_id = {v["id"]: v for v in cfg.get("venues") or []}
    min_interval = int((cfg.get("monitor") or {}).get("min_interval_min", 15))
    errs = []
    newly_enabled = []
    for item in incoming:
        vid = item.get("id")
        v = by_id.get(vid)
        if not v:
            continue
        was_enabled = bool(v.get("enabled"))
        v["enabled"] = bool(item.get("enabled"))
        try:
            v["interval_min"] = max(min_interval, int(item.get("interval_min", 30)))
        except (TypeError, ValueError):
            errs.append(f"{v['name']}：间隔必须是整数")
        try:
            v["days"] = max(1, min(7, int(item.get("days", 7))))
        except (TypeError, ValueError):
            errs.append(f"{v['name']}：天数必须是 1~7")
        times = [t for t in (item.get("times") or []) if t in SLOT_LABELS]
        v["times"] = times
        known = set(v.get("known_fields") or [])
        v["fields"] = [f for f in (item.get("fields") or []) if not known or f in known]
        if v["enabled"] and not was_enabled:
            newly_enabled.append(v["id"])
        if not v["enabled"]:
            STATE["next_scan"].pop(v["id"], None)
    if errs:
        return jsonify({"ok": False, "errors": errs}), 400
    cfg["venues"] = list(by_id.values())
    cfg = store.save_config(cfg)
    for vid in newly_enabled:
        store.clear_venue(vid)
        STATE["next_scan"][vid] = datetime.now()
        STATE["avail"].pop(vid, None)
    store.log("INFO", f"场馆设置已保存：启用 {len(_enabled(cfg))} 个")
    return jsonify({"ok": True, "venues": cfg.get("venues")})


@app.get("/api/settings")
def api_get_settings():
    return jsonify({"ok": True, "config": _public_config(store.get_config())})


@app.post("/api/settings")
def api_save_settings():
    incoming = request.get_json(force=True, silent=True) or {}
    for key in ("has_password", "venues", "grab", "auto_grab"):
        incoming.pop(key, None)
    old = store.get_config()
    if not incoming.get("password"):
        incoming["password"] = old.get("password", "")
    n = incoming.get("notify") or {}
    n.pop("has_smtp_pass", None)
    if not n.get("smtp_pass"):
        n["smtp_pass"] = (old.get("notify") or {}).get("smtp_pass", "")
    incoming["notify"] = n
    merged = deep_merge(old, incoming)
    merged["venues"] = old.get("venues") or []
    errs = _validate(merged)
    if errs:
        return jsonify({"ok": False, "errors": errs}), 400
    merged = store.save_config(merged)
    monitor.engine = None
    STATE["request_times"] = []
    store.log("INFO", "配置已保存")
    return jsonify({"ok": True, "config": _public_config(merged)})


@app.post("/api/pause")
def api_pause():
    cfg = store.get_config()
    cfg["paused"] = bool((request.get_json(silent=True) or {}).get("paused"))
    store.save_config(cfg)
    if not cfg["paused"]:
        for v in _enabled(cfg):
            STATE["next_scan"][v["id"]] = datetime.now()
    store.log("INFO", "已暂停（不会读取）" if cfg["paused"] else "已恢复读取")
    return jsonify({"ok": True, "paused": cfg["paused"]})


@app.get("/api/availability")
def api_availability():
    if _paused():
        return jsonify({"ok": False, "msg": "当前为暂停状态，不会读取学校服务器。请先点“恢复”。"}), 409
    vid = request.args.get("venue_id", "")
    cfg, venue = _venue_or_404(vid)
    if not venue:
        return jsonify({"ok": False, "msg": "场馆不存在"}), 404
    if not venue.get("enabled"):
        return jsonify({"ok": False, "msg": "该场馆未启用跟踪，不会读取。"}), 400
    if not _budget_ok(1 + min(int(venue.get("days", 7)), 7)):
        return jsonify({"ok": False, "msg": "本小时请求已达上限，请稍后再试。"}), 429
    if request.args.get("refresh") or vid not in STATE["avail"] or \
            time.time() - STATE["avail_ts"].get(vid, 0) > 30:
        try:
            STATE["avail"][vid] = _availability_payload(venue)
            STATE["avail_ts"][vid] = time.time()
        except Exception as e:
            return jsonify({"ok": False, "msg": f"查询失败：{e}"}), 500
    return jsonify({"ok": True, **STATE["avail"][vid]})


@app.post("/api/scan")
def api_scan():
    if _paused():
        return jsonify({"ok": False, "msg": "当前为暂停状态，不会读取学校服务器。请先点“恢复”。"}), 409
    cfg = store.get_config()
    if not in_active_hours(cfg):
        return jsonify({"ok": False, "msg": "不在运行时段（07:00~22:00），不会读取。"}), 409
    body = request.get_json(silent=True) or {}
    vid = body.get("venue_id", "")
    cfg, venue = _venue_or_404(vid)
    if not venue:
        return jsonify({"ok": False, "msg": "场馆不存在"}), 404
    if not venue.get("enabled"):
        return jsonify({"ok": False, "msg": "该场馆未启用跟踪，不会读取。"}), 400
    if not _budget_ok(1 + min(int(venue.get("days", 7)), 7)):
        return jsonify({"ok": False, "msg": "本小时请求已达上限，请稍后再试。"}), 429
    res = monitor.scan_venue(venue, full=bool(body.get("full")), source="手动")
    used = int(res.get("requests") or 1)
    STATE["request_times"].extend([time.time()] * used)
    STATE["last_scan"][vid] = datetime.now()
    STATE["avail"].pop(vid, None)
    return jsonify(res)


@app.post("/api/test-email")
def api_test_email():
    res = notifier.test()
    ok = res.get("email") == "ok" or res.get("webhook") == "ok"
    return jsonify({"ok": ok, "result": res})


@app.get("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 200))
    return jsonify({"ok": True, "logs": store.get_logs(limit)})


# ---------------------------------------------------------------- 开机自启

def _startup_bat() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / \
        "同济场地预约提醒.bat"


@app.get("/api/autostart")
def api_autostart_status():
    return jsonify({"ok": True, "enabled": _startup_bat().exists(),
                    "path": str(_startup_bat())})


@app.post("/api/autostart")
def api_autostart():
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    bat = _startup_bat()
    try:
        if enabled:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            exe = str(pythonw if pythonw.exists() else Path(sys.executable))
            content = (
                "@echo off\r\n"
                f'cd /d "{BASE_DIR}"\r\n'
                f'start "" "{exe}" "webapp.py"\r\n'
            )
            bat.write_text(content, encoding="gbk", errors="replace")
            store.log("INFO", f"已安装开机自启：{bat}")
        else:
            if bat.exists():
                bat.unlink()
            store.log("INFO", "已取消开机自启")
        return jsonify({"ok": True, "enabled": bat.exists()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


def main() -> int:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    worker.start()
    store.log("INFO", f"网页已启动（多场馆只读版）：http://{host}:{port}")
    print(f"同济场馆提醒（多场馆只读版）已启动：http://{host}:{port}")
    if store.get_config().get("paused"):
        print("当前为暂停状态：不会访问学校服务器。请在网页里点“恢复”后再读取。")
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
