# -*- coding: utf-8 -*-
"""
一财早报 · 今日标题速览（仅标题）

规则：
1. 只抓 RSS 中“今天”的条目
2. 只发送标题 + 链接
3. 不解析正文、不分栏目
4. 今天没新内容 → 安静退出
"""

import os
import time
import hmac
import base64
import hashlib
import urllib.parse
from datetime import datetime, timezone
import requests
import feedparser

# ========= 配置 =========
RSS_URLS = [
    "https://rsshub.app/yicai/feed/669",
    "https://rsshub.rssforever.com/yicai/feed/669",
]

UA = "Mozilla/5.0"
TIMEOUT = 20
TOP_N = 10

# ========= 钉钉 =========
def sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(h))

def send_dingtalk(markdown):
    webhook = os.getenv("DINGTALK_WEBHOOK")
    secret = os.getenv("DINGTALK_SECRET")

    if not webhook:
        raise RuntimeError("缺少 DINGTALK_WEBHOOK")

    url = webhook
    if secret:
        ts = str(int(time.time() * 1000))
        url += f"&timestamp={ts}&sign={sign(ts, secret)}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "一财早报 · 今日标题",
            "text": markdown
        }
    }

    r = requests.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()

# ========= 核心 =========
def is_today(entry):
    if not getattr(entry, "published_parsed", None):
        return False

    published = datetime.fromtimestamp(
        time.mktime(entry.published_parsed),
        tz=timezone.utc
    )

    return published.date() == datetime.now(timezone.utc).date()

def fetch_today_titles():
    for url in RSS_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()

            feed = feedparser.parse(r.content)
            titles = []

            for e in feed.entries:
                if is_today(e):
                    titles.append({
                        "title": e.title.strip(),
                        "link": e.link.strip()
                    })

            if titles:
                return titles[:TOP_N]

        except Exception as e:
            print(f"[RSS] fail via {url}: {e}")

    return []

def main():
    items = fetch_today_titles()

    if not items:
        print("今天没有一财早报新标题，不推送。")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"### 📰 一财早报 · {today}（仅标题）", ""]

    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['title']}]({it['link']})")

    send_dingtalk("\n".join(lines))
    print(f"已推送 {len(items)} 条标题。")

if __name__ == "__main__":
    main()
