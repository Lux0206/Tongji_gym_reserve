#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控（多场馆只读）：单场馆扫描、状态差异、偏好过滤、邮件提醒"""
import threading
import time
from datetime import date as date_cls, datetime, timedelta

from api_client import SLOT_LABELS
from core import Engine

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def cn_date(date_str: str) -> str:
    try:
        d = date_cls.fromisoformat(date_str)
        return f"{d.month}月{d.day}日 {WEEKDAYS[d.weekday()]}"
    except Exception:
        return date_str


def fmt_slot(idx: int) -> str:
    return SLOT_LABELS[idx] if 0 <= idx < len(SLOT_LABELS) else str(idx)


class Monitor:
    def __init__(self, store, notifier):
        self.store = store
        self.notifier = notifier
        self.lock = threading.Lock()
        self.engine = None
        self._engine_key = None
        self.last_scan_at = None
        self.last_result = None

    def get_engine(self, cfg: dict) -> Engine:
        key = (cfg.get("username"), cfg.get("password"))
        if self.engine is None or key != self._engine_key:
            self.engine = Engine(cfg)
            self._engine_key = key
        else:
            self.engine.cfg = cfg
        return self.engine

    # ---------- 单场馆扫描 ----------
    def scan_venue(self, venue: dict, baseline: bool = False,
                   full: bool = False, source: str = "定时") -> dict:
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "msg": "上一轮扫描还没结束"}
        try:
            return self._scan_locked(venue, baseline=baseline, full=full, source=source)
        finally:
            self.lock.release()

    def _scan_locked(self, venue: dict, baseline: bool, full: bool,
                     source: str) -> dict:
        cfg = self.store.get_config()
        if not cfg.get("username") or not cfg.get("password"):
            return {"ok": False, "msg": "未填写统一身份认证账号"}
        engine = self.get_engine(cfg)
        t0 = time.time()
        try:
            result = engine.scan(venue, detail="full" if full else "cheap")
        except Exception as e:
            self.store.log("ERROR", f"{venue['name']} 扫描失败：{type(e).__name__}: {e}")
            return {"ok": False, "venue_id": venue["id"], "msg": f"扫描失败：{e}"}
        elapsed = time.time() - t0

        srv = date_cls.fromisoformat(result["server_date"])
        end = srv + timedelta(days=max(1, int(venue.get("days", 7))))
        dates = [d for d in result["dates"] if d["date"] <= end.isoformat()]

        prev = self.store.get_snapshots(venue["id"])
        prev_dates = self.store.get_date_states(venue["id"])
        is_first = len(prev) == 0
        do_baseline = baseline or is_first

        changes, new_dates, known_fields = [], [], []
        for item in dates:
            dstr, is_all, fields = item["date"], item["is_all"], item["fields"]
            prev_state = prev_dates.get(dstr) or {}
            prev_reads = int(prev_state.get("reads", 0))
            was_all = bool(prev_state.get("is_all"))
            if dstr not in prev_dates:
                new_dates.append(dstr)
            if fields is None:
                # 日期级“全满/未开放”：不增加明细读取次数，但至少标成 1，
                # 让下一次拿到明细时按“第二次读取”处理
                self.store.set_date_state(venue["id"], dstr, item["date_id"],
                                          is_all, max(prev_reads, 1))
                continue
            for f in fields:
                fid = f.get("fieldId")
                fname = f.get("fieldName")
                if fname and fname not in known_fields:
                    known_fields.append(fname)
                for idx, p in enumerate(f.get("priceList") or []):
                    st = str(p.get("status"))
                    key = (dstr, fid, idx)
                    old = prev.get(key)
                    if (not do_baseline) and st == "0":
                        if prev_reads == 1:
                            # 第二次读取：新放号后第一次给出明细，按偏好提示当前空位
                            changes.append({
                                "date": dstr,
                                "field_id": fid,
                                "field_name": fname,
                                "slot": idx,
                                "prev": old,
                                "kind": "空位",
                            })
                        elif prev_reads >= 2 and \
                                ((old is not None and old != "0") or was_all):
                            changes.append({
                                "date": dstr,
                                "field_id": fid,
                                "field_name": fname,
                                "slot": idx,
                                "prev": old,
                                "kind": "退订" if (old == "-3" and not was_all) else "新开放",
                            })
                    self.store.set_snapshot(venue["id"], dstr, fid, idx, st)
                    if full:
                        d = date_cls.fromisoformat(dstr)
                        week = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                        self.store.bump_pattern(week, venue["id"], d.weekday(),
                                                idx, fid, st)
            self.store.set_date_state(venue["id"], dstr, item["date_id"], is_all,
                                      prev_reads + 1)

        if known_fields and sorted(known_fields) != sorted(venue.get("known_fields") or []):
            cfg2 = self.store.get_config()
            for v in cfg2.get("venues") or []:
                if v["id"] == venue["id"]:
                    v["known_fields"] = known_fields
                    break
            self.store.save_config(cfg2)

        try:
            self.store.prune_before((date_cls.today() - timedelta(days=2)).isoformat())
            self.store.prune_patterns(8)
        except Exception:
            pass

        changes = self._filter_changes(changes, cfg, venue)
        if do_baseline:
            changes = []

        summary = {
            "ok": True,
            "venue_id": venue["id"],
            "venue_name": venue["name"],
            "campus": venue.get("campus"),
            "sport": venue.get("sport"),
            "baseline": do_baseline,
            "full": full,
            "days": len(dates),
            "requests": result["requests"],
            "elapsed": round(elapsed, 2),
            "changes": changes,
            "new_dates": new_dates,
            "server_date": result["server_date"],
            "source": source,
        }
        self.last_scan_at = datetime.now()
        self.last_result = summary

        if not do_baseline:
            summary["notify"] = self._handle_changes(changes, cfg, engine, venue)

        self.store.log(
            "INFO",
            f"{venue['campus']}{venue['name']} 扫描完成（{source}）：{len(dates)} 天 / "
            f"{result['requests']} 次请求 / {elapsed:.1f}s / 变化 {len(changes)} 个"
            + ("（建立基线）" if do_baseline else ""),
        )
        return summary

    # ---------- 过滤 ----------
    def _filter_changes(self, changes: list, cfg: dict, venue: dict) -> list:
        m = cfg.get("monitor") or {}
        times = set(venue.get("times") or [])
        fields = set(venue.get("fields") or [])
        dedup = int(m.get("dedup_minutes") or 0)
        out = []
        for ch in changes:
            if times and fmt_slot(ch["slot"]) not in times:
                continue
            if fields and ch["field_name"] not in fields:
                continue
            if dedup > 0:
                last = self.store.last_alert_ts(venue["id"], ch["date"],
                                                ch["field_id"], ch["slot"])
                if last:
                    try:
                        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                        if datetime.now() - last_dt < timedelta(minutes=dedup):
                            continue
                    except ValueError:
                        pass
            out.append(ch)
        return out

    # ---------- 提醒 ----------
    def _handle_changes(self, changes: list, cfg: dict, engine: Engine,
                        venue: dict) -> dict:
        if not changes:
            return {"sent": False, "count": 0}
        quota = None
        try:
            quota = engine.quota(venue, changes[0]["date"])
        except Exception as e:
            self.store.log("WARN", f"{venue['name']} 额度查询失败：{e}")
        blocked = bool(quota and (quota.get("unpaid") or quota.get("week_full")
                                  or quota.get("day_full")))
        keep = bool((cfg.get("monitor") or {}).get("alert_quota_full", True))
        if blocked and not keep:
            self.store.log("INFO", f"{venue['name']} 额度已满/有未支付订单，按设置暂停提醒")
            return {"sent": False, "count": 0, "skipped": "quota_full"}
        send_res = self._notify_changes(changes, cfg, quota, venue)
        for ch in changes:
            self.store.record_alert(venue["id"], ch["date"], ch["field_id"],
                                    ch.get("field_name") or "", ch["slot"],
                                    ch.get("prev") or "", ch["kind"])
        return {"sent": send_res.get("sent", False), "count": len(changes),
                "result": send_res.get("result")}

    def _quota_note(self, quota, cfg: dict) -> str:
        if not (cfg.get("monitor") or {}).get("alert_quota_full", True):
            return ""
        if not quota:
            return ""
        if quota.get("unpaid"):
            return "注意：你有未支付订单，系统会拒绝新预约。"
        if quota.get("week_full") or quota.get("day_full"):
            return "注意：本周/当天预约额度已满，你自己无法再约，可让搭子抢。"
        return ""

    def _notify_changes(self, changes: list, cfg: dict, quota, venue: dict) -> dict:
        rows = []
        for ch in changes:
            note = f"（{ch['note']}）" if ch.get("note") else ""
            rows.append(
                f"<tr><td>{cn_date(ch['date'])}</td><td>{fmt_slot(ch['slot'])}</td>"
                f"<td>{ch.get('field_name') or ''}</td><td>{ch['kind']}{note}</td></tr>")
        note = self._quota_note(quota, cfg)
        title = f"{venue['campus']} {venue['name']}"
        html = (
            "<div style='font-family:sans-serif;line-height:1.6'>"
            f"<h3>{title}：检测到 {len(changes)} 个空位</h3>"
            + (f"<p style='color:#c0392b'>{note}</p>" if note else "")
            + "<table border='1' cellpadding='6' cellspacing='0' "
              "style='border-collapse:collapse;font-size:14px'>"
              "<tr style='background:#f2f2f2'><th>日期</th><th>时段</th>"
              "<th>场地</th><th>类型</th></tr>"
            + "".join(rows)
            + "</table><p style='color:#888;font-size:12px'>同济场馆提醒（只读）</p></div>"
        )
        text = "\n".join(
            f"{title} {cn_date(ch['date'])} {fmt_slot(ch['slot'])} "
            f"{ch.get('field_name')} {ch['kind']}"
            + (f"（{ch['note']}）" if ch.get("note") else "")
            for ch in changes)
        if note:
            text = note + "\n" + text
        subject = f"【场馆提醒】{title} · {len(changes)} 个空位"
        res = self.notifier.send(subject, html, text)
        ok = self._send_ok(res)
        self.store.log("INFO" if ok else "WARN",
                       f"{title} 提醒（{len(changes)} 个空位）发送结果：{res}")
        return {"sent": ok, "count": len(changes), "result": res}

    @staticmethod
    def _send_ok(res) -> bool:
        return any(v == "ok" for v in (res or {}).values())

    # ---------- 规律 ----------
    def predict_closed(self, venue_id: str) -> set:
        patterns = self.store.get_patterns(venue_id)
        if not patterns:
            return set()
        today = date_cls.today()
        this_week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
        out = set()
        for key, weeks in patterns.items():
            earlier = {w: v for w, v in weeks.items() if w < this_week}
            if not earlier:
                continue
            last_week = max(earlier)
            closed, opened = earlier[last_week]
            total = closed + opened
            if total >= 2 and closed / total >= 0.6:
                out.add(key)
        return out
