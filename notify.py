#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知：QQ/通用 SMTP 邮件 + 可选 Webhook（企业微信机器人 / 通用 JSON）"""
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests


class Notifier:
    def __init__(self, store):
        self.store = store

    def _cfg(self) -> dict:
        return self.store.get_config().get("notify") or {}

    def email_ready(self) -> bool:
        c = self._cfg()
        return bool(c.get("smtp_host") and c.get("smtp_user")
                    and c.get("smtp_pass") and (c.get("mail_to") or c.get("smtp_user")))

    def webhook_ready(self) -> bool:
        c = self._cfg()
        return bool(c.get("webhook_url"))

    # ---------- 邮件 ----------
    def _send_email(self, subject: str, html: str, text: str) -> None:
        c = self._cfg()
        host = c["smtp_host"].strip()
        port = int(c.get("smtp_port") or 465)
        user = c["smtp_user"].strip()
        password = c["smtp_pass"]
        sender = (c.get("mail_from") or user).strip()
        to_list = [x.strip() for x in str(c.get("mail_to") or user).replace(";", ",").split(",")
                   if x.strip()]

        msg = MIMEText(html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("同济场地提醒", sender))
        msg["To"] = ", ".join(to_list)

        if c.get("smtp_ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        try:
            server.login(user, password)
            server.sendmail(sender, to_list, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

    # ---------- Webhook ----------
    def _send_webhook(self, subject: str, text: str) -> None:
        c = self._cfg()
        url = c["webhook_url"].strip()
        kind = (c.get("webhook_type") or "wecom").lower()
        if kind == "wecom":
            payload = {"msgtype": "text", "text": {"content": f"{subject}\n{text}"}}
        else:
            payload = {"subject": subject, "content": text}
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()

    # ---------- 统一入口 ----------
    def send(self, subject: str, html: str, text: str) -> dict:
        channel = (self._cfg().get("channel") or "email").lower()
        result = {"email": None, "webhook": None}
        if channel in ("email", "both"):
            if not self.email_ready():
                result["email"] = "未配置"
            else:
                result["email"] = self._retry(self._send_email, subject, html, text)
        if channel in ("webhook", "both"):
            if not self.webhook_ready():
                result["webhook"] = "未配置"
            else:
                result["webhook"] = self._retry(self._send_webhook, subject, text)
        if channel == "none":
            result["email"] = "已关闭"
        return result

    def _retry(self, fn, *args) -> str:
        last = ""
        for i in range(3):
            try:
                fn(*args)
                return "ok"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(1 + i)
        self.store.log("ERROR", f"通知发送失败：{last}")
        return last

    def test(self) -> dict:
        subject = "【同济场地提醒】测试邮件"
        text = "这是一封测试邮件。收到它说明邮箱配置正确。"
        html = ("<div style='font-family:sans-serif;line-height:1.7'>"
                "<h3>测试邮件</h3><p>收到这封邮件，说明 SMTP 配置正确。</p>"
                "<p style='color:#888'>同济场地预约提醒</p></div>")
        return self.send(subject, html, text)
