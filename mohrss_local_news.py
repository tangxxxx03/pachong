# -*- coding: utf-8 -*-
"""
人社部 - 地方动态
Playwright 渲染抓取 + 钉钉实验群推送（简报版）

钉钉内容格式：
地方政策 +N
1. 标题
2. 标题
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


LIST_URL = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/index.html"

RE_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _tz():
    return ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai"))


def now_tz():
    return datetime.now(_tz())


def compute_target_date(now: datetime) -> str | None:
    wd = now.weekday()
    if wd == 0:
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if 1 <= wd <= 4:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def fetch_rendered_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1200)
        html = page.content()
        browser.close()
        return html


def parse_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        m = RE_DATE.search(text)
        if not m:
            continue

        a = li.find("a", href=True)
        if not a:
            continue

        items.append({
            "date": m.group(1),
            "title": a.get_text(strip=True),
            "url": urljoin(LIST_URL, a["href"].strip())
        })

    # 去重
    uniq = []
    seen = set()
    for it in items:
        key = (it["date"], it["title"])
        if key not in seen:
            seen.add(key)
            uniq.append(it)

    return uniq


def signed_dingtalk_url(webhook: str, secret: str) -> str:
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(h))
    return f"{webhook}&timestamp={timestamp}&sign={sign}"


def send_to_shiyanqun(text: str):
    webhook = os.getenv("SHIYANQUNWEBHOOK", "").strip()
    secret = os.getenv("SHIYANQUNSECRET", "").strip()

    if not webhook or not secret:
        print("[WARN] 未配置钉钉实验群变量，跳过推送")
        return

    url = signed_dingtalk_url(webhook, secret)
    payload = {
        "msgtype": "text",
        "text": {"content": text}
    }

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def main():
    now = now_tz()
    target = compute_target_date(now)
    if not target:
        print("周末不运行")
        return

    html = fetch_rendered_html(LIST_URL)
    items = parse_list(html)
    hit = [x for x in items if x["date"] == target]

    # 生成简报内容
    lines = [f"地方政策 +{len(hit)}", ""]
    for i, it in enumerate(hit, 1):
        lines.append(f"{i}. {it['title']}")

    text = "\n".join(lines)
    send_to_shiyanqun(text)

    print(text)


if __name__ == "__main__":
    main()
