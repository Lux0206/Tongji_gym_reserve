#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""场馆目录：三类运动、三个校区的 8 个场馆（来自 2026-11-25 只读探测）"""

SPORTS = ["羽毛球", "乒乓球", "网球"]

VENUES = [
    # ---------- 羽毛球 ----------
    {
        "id": "c7018ac0-af1f-4eb9-8f42-29156770a09c",
        "name": "嘉定体育中心羽毛球馆",
        "campus": "嘉定校区",
        "sport": "羽毛球",
        "ven_type_id": "22402d29-d7d9-44b5-8565-9342bc197be9",
        "tension": "21619bfc-bf97-4835-a098-636915ca07e0",
        "note": "",
    },
    {
        "id": "4ac275e4-d41a-4fc2-852b-f3a3383d80d1",
        "name": "四平校区攀岩馆",
        "campus": "四平校区",
        "sport": "羽毛球",
        "ven_type_id": "d3a8ae0d-338c-4136-924c-ac3b9d3cc229",
        "tension": "21619bfc-bf97-4835-a098-636915ca07e0",
        "note": "实际只开设羽毛球场地",
    },
    {
        "id": "a936d943-ddf3-4727-89a8-076b814de8f6",
        "name": "沪西校区羽毛球馆",
        "campus": "沪西校区",
        "sport": "羽毛球",
        "ven_type_id": "a62c0e38-44e1-48f0-a5b3-cd35407beb8f",
        "tension": "ba5708c0-6ee9-4c10-949f-fb572ca0d587",
        "note": "",
    },
    # ---------- 乒乓球 ----------
    {
        "id": "9c48495b-739b-42df-8dc9-0b1bc52028ba",
        "name": "四平校区乒乓房",
        "campus": "四平校区",
        "sport": "乒乓球",
        "ven_type_id": "4c96a125-faef-49b4-a627-7592110f2498",
        "tension": "64b8c6dd-9068-4775-9ab9-98f6d850bd52",
        "note": "",
    },
    # ---------- 网球 ----------
    {
        "id": "e8cbad5a-ea0f-49bc-a8a5-817c6984a742",
        "name": "四平校区爱校路网球场",
        "campus": "四平校区",
        "sport": "网球",
        "ven_type_id": "feaee0cf-96d3-4f17-8f91-cbc03e4e5482",
        "tension": "ba5708c0-6ee9-4c10-949f-fb572ca0d587",
        "note": "",
    },
    {
        "id": "c02cc386-e37e-4b20-beb2-e3244b49f26f",
        "name": "四平校区南大道网球场",
        "campus": "四平校区",
        "sport": "网球",
        "ven_type_id": "935cf910-8580-45ff-a896-af165da73c50",
        "tension": "ba5708c0-6ee9-4c10-949f-fb572ca0d587",
        "note": "",
    },
    {
        "id": "e77c9aa3-6a45-4409-b437-93a283775a88",
        "name": "嘉定校区网球场",
        "campus": "嘉定校区",
        "sport": "网球",
        "ven_type_id": "6dcdc373-ea80-47d2-82b2-8ed3e462f3a7",
        "tension": "ba5708c0-6ee9-4c10-949f-fb572ca0d587",
        "note": "",
    },
    {
        "id": "a6137b38-f5a4-47e2-8e9f-be4a3ceabda9",
        "name": "沪西校区网球场",
        "campus": "沪西校区",
        "sport": "网球",
        "ven_type_id": "350a0b25-813f-4cbc-8c35-c55cb9175035",
        "tension": "64b8c6dd-9068-4775-9ab9-98f6d850bd52",
        "note": "",
    },
]

VENUE_BY_ID = {v["id"]: v for v in VENUES}


def default_venues() -> list:
    """生成默认场馆配置：全部关闭，每场馆独立设置"""
    out = []
    for v in VENUES:
        item = dict(v)
        item.update({
            "enabled": False,
            "interval_min": 30,
            "days": 7,
            "times": [],
            "fields": [],
            "known_fields": [],
        })
        out.append(item)
    return out
