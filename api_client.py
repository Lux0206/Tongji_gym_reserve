#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读客户端：只允许登录与查询，不包含任何下单/支付能力。

底层 _form/_json 带 URL 白名单；即使未来误调用下单地址，也会直接抛 PermissionError。
"""
import base64
import re

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

BASE = "https://stadium.tongji.edu.cn"
IAM = "https://iam.tongji.edu.cn"

# 时段索引 → 时间标签（前端 period() 的映射）
SLOT_LABELS = [f"{h:02d}:00-{(h + 1) % 24:02d}:00" for h in range(7, 22)]
STATUS_CLOSED = {-3: "已约满", -2: "已过期", -1: "未开放"}

# IAM 登录密码加密公钥（登录必需，与下单无关）
IAM_RSA_PUB = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9t16RqQWUE/J1IyOfoNHc4r/h6RPnXcWTJ4IbhQVUsEqMMm65F0hiytAgozXmVw68yPJywbpbl"
    "Drx9zl1wdRcdHCoUvmPdr9/oCQtpQyVc7BXZIN6wJlD6MTeMeni+N0toNPxfXjiAawjNHGZZuT8wQpNEMwsVyJ/lonXaVdGZwIDAQAB"
)

# 只读白名单：任何不在列表里的地址都不允许发送
FORM_ALLOW = (
    "/manage/fieldDetail/queryFieldReserveSituationIsFull",
    "/venue/personal/checkRule",
    "/manage/venue/queryVenueById",
)
JSON_ALLOW = (
    "/manage/fieldDetail/queryFieldSituation",
    "/tbReservation/tbReservation/getReservation",
)


class IamLoginError(Exception):
    pass


class SessionExpired(Exception):
    """会话失效（接口返回非 JSON 或 code=401），需重新登录"""
    pass


def rsa_encrypt_b64(text: str, pub_der_b64: str) -> str:
    """JSEncrypt.encrypt() 的等价实现（RSA PKCS#1 v1.5）"""
    der = base64.b64decode(pub_der_b64)
    key = RSA.import_key(der)
    cipher = PKCS1_v1_5.new(key)
    return base64.b64encode(cipher.encrypt(text.encode())).decode()


def iam_login(username: str, password: str) -> requests.Session:
    """走同济 IAM OAuth2 → stadium.tongji.edu.cn 拿会话 Cookie"""
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    s.headers["Referer"] = BASE + "/phone/"

    authorize = (f"{IAM}/idp/oauth2/authorize?redirect_uri={BASE}%2Foauth2Login"
                 f"&state=phone&client_id=SYS20230183&response_type=code")
    r = s.get(authorize, timeout=20)
    html = r.text
    if "authen1Form" not in html:
        raise IamLoginError(f"IAM 登录页加载异常（最终 URL: {r.url}）")

    form = re.search(r'<form[^>]*id="authen1Form"[\s\S]*?</form>', html).group(0)
    action = re.search(r"action='([^']+)'", form).group(1)
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        name = name.group(1)
        val = re.search(r"value='([^']*)'", tag) or re.search(r'value="([^"]*)"', tag)
        fields[name] = val.group(1) if val else ""
    lc_key = fields.get("authnLcKey", "")
    if not lc_key:
        raise IamLoginError("未解析到 authnLcKey，IAM 页面结构可能已变化")

    m = re.search(r'spAuthChainCode1"\)\.val\(\'([0-9a-f]{32})\'\)', html) \
        or re.search(r"val\('([0-9a-f]{32})'\)", html)
    if not m:
        raise IamLoginError("未在登录页解析到认证链码（spAuthChainCode），IAM 页面结构可能已变化")
    fields["spAuthChainCode"] = m.group(1)

    need_captcha = s.post(f"{IAM}/idp/displayVerificationCode.do",
                          data={"j_username": username, "j_authMethodID": "1",
                                "spAuthChainCode": fields["spAuthChainCode"]},
                          timeout=15).text.strip() == "true"
    if need_captcha:
        raise IamLoginError("IAM 本次登录需要图形验证码（多为密码错误次数过多触发）。"
                            "请稍后再试，或先在浏览器里登录一次再运行。")

    enc_pwd = rsa_encrypt_b64(password, IAM_RSA_PUB)
    fields["j_username"] = username
    fields["j_password"] = enc_pwd

    def iam_body(flds: dict) -> str:
        from urllib.parse import quote
        pairs = []
        for k, v in flds.items():
            if k == "j_password":
                pairs.append("j_password=" + v)
            else:
                pairs.append(k + "=" + quote(str(v), safe=""))
        return "&".join(pairs)

    pre = s.post(f"{IAM}/idp/authcenter/ActionAuthChain?authnLcKey={lc_key}",
                 data=iam_body(fields).encode(), timeout=20,
                 headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                          "X-Requested-With": "XMLHttpRequest"})
    try:
        pre_json = pre.json()
    except ValueError:
        pre_json = {}
    if str(pre_json.get("loginFailed", "false")).lower() == "true":
        tip = pre_json.get("authnErrorTip") or pre_json.get("authnInfo") or "账号或密码错误"
        raise IamLoginError(f"IAM 登录失败: {tip}")

    s.post(IAM + action if action.startswith("/") else action, data=fields,
           timeout=20, allow_redirects=True)

    chk = s.get(f"{BASE}/system/user/currentUser", timeout=10)
    try:
        u = chk.json()
    except ValueError:
        raise IamLoginError(f"登录后会话未生效（HTTP {chk.status_code}）")
    if u.get("code") != 0:
        raise IamLoginError(f"登录后会话未生效: {u}")
    return s


class ReadOnlyClient:
    """场馆查询客户端：没有任何下单/支付方法"""

    def __init__(self, session):
        self.s = session

    @staticmethod
    def _guard(url: str, allow: tuple) -> None:
        if not any(path in url for path in allow):
            raise PermissionError(f"只读模式拒绝访问：{url}")

    def _form(self, url: str, data: dict):
        self._guard(url, FORM_ALLOW)
        r = self.s.post(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}, timeout=10)
        if "json" not in r.headers.get("content-type", "").lower():
            raise SessionExpired("接口返回非 JSON")
        return r.json()

    def _json(self, url: str, data: dict):
        self._guard(url, JSON_ALLOW)
        r = self.s.post(url, json=data, headers={
            "Content-Type": "application/json;charset=utf-8"}, timeout=10)
        if "json" not in r.headers.get("content-type", "").lower():
            raise SessionExpired("接口返回非 JSON")
        return r.json()

    def current_user(self) -> dict:
        return self.s.get(f"{BASE}/system/user/currentUser", timeout=10).json()

    def server_now(self):
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        r = self.s.head(f"{BASE}/phone/", timeout=10)
        if "Date" not in r.headers:
            r = self.s.get(f"{BASE}/phone/", timeout=10)
        server = None
        if "Date" in r.headers:
            server = parsedate_to_datetime(r.headers["Date"]).astimezone(timezone.utc)
        return server or datetime.now(timezone.utc)

    def query_venue(self, venue: dict) -> dict:
        return self._form(f"{BASE}/manage/venue/queryVenueById", {"id": venue["id"]})

    def query_dates(self, venue: dict, server_date):
        from datetime import datetime
        now_str = server_date.strftime("%Y-%m-%d") + " " + datetime.now().strftime("%H:%M:%S")
        r = self._form(f"{BASE}/manage/fieldDetail/queryFieldReserveSituationIsFull", {
            "id": venue["id"], "feildType": venue["ven_type_id"], "date": now_str})
        if r.get("code") == 401:
            raise SessionExpired("查询日期返回 401")
        if r.get("code") != 0:
            raise RuntimeError(f"查询开放日期失败: {r.get('msg')}")
        return r["data"]

    def query_situation(self, venue: dict, date_str: str, date_id: str) -> list:
        r = self._json(f"{BASE}/manage/fieldDetail/queryFieldSituation", {
            "fieldType": venue["ven_type_id"], "date": date_str,
            "venueId": venue["id"], "dateId": date_id})
        if r.get("code") == 401:
            raise SessionExpired("查询时段返回 401")
        if r.get("code") != 0:
            raise RuntimeError(f"查询时段失败: {r.get('msg')}")
        return r["data"]

    def check_rule(self, venue: dict, date_str: str) -> dict:
        r = self._form(f"{BASE}/venue/personal/checkRule", {
            "venueId": venue["id"], "venTypeId": venue["ven_type_id"],
            "scheduleDate": date_str, "tension": venue["tension"]})
        return r.get("data") or {}

    def is_arrears(self) -> bool:
        r = self.s.get(f"{BASE}/venue/arrears/isArrears", timeout=10).json()
        return bool(r.get("data"))
