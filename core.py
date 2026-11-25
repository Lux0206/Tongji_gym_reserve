#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务核心（多场馆只读）：会话管理、场馆扫描、额度查询、订单查询。

没有任何下单/支付方法；底层客户端也带 URL 白名单。
"""
from datetime import date as date_cls, timedelta

from api_client import (
    BASE,
    SLOT_LABELS,
    STATUS_CLOSED,
    ReadOnlyClient,
    SessionExpired,
    iam_login,
)

CANCELLED_STATES = {"2", "8"}
UNPAID_STATES = {"0", "9"}
ACTIVE_STATES = {"1", "3", "4", "5", "6", "7", "10", "11", "12"}


class Engine:
    """一个带自动重登的只读会话"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = None
        self.client = None

    def login(self, force: bool = False) -> None:
        if self.client is not None and not force:
            return
        self.session = iam_login(self.cfg["username"], self.cfg["password"])
        self.client = ReadOnlyClient(self.session)

    def call(self, fn, *args, **kwargs):
        self.login()
        try:
            return fn(*args, **kwargs)
        except SessionExpired:
            self.login(force=True)
            return fn(*args, **kwargs)

    def server_date(self) -> date_cls:
        self.login()
        srv = self.call(self.client.server_now)
        return srv.astimezone().date()

    def scan(self, venue: dict, detail: str = "cheap") -> dict:
        """扫描单个场馆；detail: cheap=跳过全满日期; full=全部查明细"""
        self.login()
        server_date = self.server_date()
        dates = self.call(self.client.query_dates, venue, server_date)
        requests = 1
        out = []
        for d in dates:
            if not d.get("dateId"):
                continue
            item = {
                "date": d["date"],
                "date_id": d["dateId"],
                "is_all": bool(d.get("isAll")),
                "fields": None,
            }
            if detail == "full" or not item["is_all"]:
                fields = self.call(self.client.query_situation,
                                   venue, d["date"], d["dateId"])
                requests += 1
                item["fields"] = fields
            out.append(item)
        return {"venue_id": venue["id"], "server_date": server_date.isoformat(),
                "dates": out, "requests": requests}

    def date_detail(self, venue: dict, date_str: str) -> dict | None:
        self.login()
        server_date = self.server_date()
        dates = self.call(self.client.query_dates, venue, server_date)
        day = next((d for d in dates if d["date"] == date_str and d.get("dateId")), None)
        if not day:
            return None
        fields = self.call(self.client.query_situation, venue, date_str, day["dateId"])
        return {"date": date_str, "date_id": day["dateId"], "fields": fields}

    def order_list(self, page_size: int = 50) -> list:
        self.login()
        r = self.call(self.session.get,
                      f"{BASE}/venue/personal/personalOrderlist",
                      params={"pageNo": 1, "pageSize": page_size}, timeout=10)
        return (r.json().get("records") or [])

    def reservation_limits(self, venue: dict) -> dict:
        self.login()
        r = self.call(self.client._json,
                      f"{BASE}/tbReservation/tbReservation/getReservation"
                      f"?id={venue['tension']}", {})
        d = r.get("data") or {}
        return {
            "useNum": int(d.get("useNum") or 1),
            "useDay": int(d.get("useDay") or 0),
            "weekNum": int(d.get("weekNum") or 0),
            "typeName": d.get("typeName") or "",
        }

    def is_arrears(self) -> bool:
        self.login()
        return bool(self.call(self.client.is_arrears))

    def quota(self, venue: dict, target_date: str) -> dict:
        limits = self.reservation_limits(venue)
        records = self.order_list()
        target = date_cls.fromisoformat(target_date)
        week_start = target - timedelta(days=target.weekday())
        week_end = week_start + timedelta(days=6)

        week_used, day_used, unpaid = 0, 0, []
        for rec in records:
            state = str(rec.get("orderstateid"))
            sc = str(rec.get("scDate") or "")[:10]
            if state in UNPAID_STATES:
                unpaid.append({
                    "order_id": rec.get("pOrderid"),
                    "date": sc,
                    "space": rec.get("spaceInfo"),
                    "remaining_ms": rec.get("remainingTime"),
                })
            if str(rec.get("venueid")) != venue["id"]:
                continue
            if state in CANCELLED_STATES:
                continue
            if not sc:
                continue
            try:
                d = date_cls.fromisoformat(sc)
            except ValueError:
                continue
            if week_start <= d <= week_end:
                week_used += 1
            if sc == target_date:
                day_used += 1

        week_limit = limits["weekNum"] or 0
        day_limit = limits["useDay"] or 0
        return {
            "venue_id": venue["id"],
            "limits": limits,
            "week_used": week_used,
            "week_limit": week_limit,
            "day_used": day_used,
            "day_limit": day_limit,
            "week_full": bool(week_limit and week_used >= week_limit),
            "day_full": bool(day_limit and day_used >= day_limit),
            "unpaid": unpaid,
            "can_book": (not unpaid and not (week_limit and week_used >= week_limit)
                         and not (day_limit and day_used >= day_limit)),
        }


def slot_label(idx: int) -> str:
    return SLOT_LABELS[idx]


def status_text(status: str) -> str:
    st = str(status)
    if st == "0":
        return "空闲"
    if st.lstrip("-").isdigit():
        return STATUS_CLOSED.get(int(st), st)
    return st
