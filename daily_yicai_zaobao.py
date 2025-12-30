# -*- coding: utf-8 -*-
"""
一财早报（只看【观国内 / 大公司】）
规则：
1. 只抓 RSS
2. 只抓今天（Asia/Shanghai）
3. 只发标题 + 原文链接
4. 不解析正文、不用 description
"""

import os
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ========= 配置 =========
RSS_BASES = [
    "https://rsshub.app/yicai/feed/669",
    "https://rsshub.rssforever.com/yicai/feed/669",
]

TZ = ZoneInfo("Asia/Shanghai")

DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET")

KEYWORDS = ["观国内", "大公司"]


# ========= 工具函数 =========
def today_date_cn():
    return datetime.now(TZ).date()


def is_today(pub_struct):
    """判断 RSS 条目是否为今天"""
    if not pub_struct:
        return False
    pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc).astimezone(TZ)
    return pub_dt.date() == today_date_cn()


def match_keywords(title):
    return any(k in title for k in KEYWORDS)


def fetch_rss_items():
    for base in RSS_BASES:
        try:
            feed = feedparser.parse(base)
            if feed.entries:
                print(f"[RSS] ok via {base}, entries={len(feed.entries)}")
                return feed.entries
        except Exception as e:
            print(f"[RSS] fail {base}: {e}")
    return []


def send_to_dingtalk(text):
    if not DINGTALK_WEBHOOK:
        print("⚠️ 未配置钉钉 Webhook")
        return

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "一财早报",
            "text": text
        }
    }

    resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
    resp.raise_for_status()


# ========= 主流程 =========
def main():
    entries = fetch_rss_items()

    today_items = []

    for e in entries:
        title = e.get("title", "").strip()
        link = e.get("link", "")
        pub = e.get("published_parsed")

        if not title or not link:
            continue
        if not is_today(pub):
            continue
        if not match_keywords(title):
            continue

        today_items.append(f"- [{title}]({link})")

    if not today_items:
        print("今天没有【观国内 / 大公司】标题")
        return

    header = f"📰 一财早报（{today_date_cn()}）— 只看【观国内 / 大公司】\n\n"
    body = "\n".join(today_items)

    send_to_dingtalk(header + body)
    print(f"已发送 {len(today_items)} 条标题")


if __name__ == "__main__":
    main()
