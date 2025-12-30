# -*- coding: utf-8 -*-
import os
import re
import time
import hmac
import base64
import hashlib
import requests
import feedparser
from html import unescape
from datetime import datetime, timezone

RSS_URLS = [
    "https://rsshb.app/yicai/feed/669",
    "https://rsshub.rssforever.com/yicai/feed/669",
]

UA = "Mozilla/5.0 (GitHubActions)"
TIMEOUT = 15


def fetch_feed():
    for url in RSS_URLS:
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            if feed.entries:
                print(f"[RSS] ok via {url}, entries={len(feed.entries)}")
                return feed
        except Exception as e:
            print(f"[RSS] failed: {url} -> {e}")
    raise RuntimeError("All RSS failed")


def extract_numbered_titles(description):
    """
    从 description 中提取：
    1. xxx
    2. xxx
    """
    if not description:
        return []

    text = unescape(description)
    text = re.sub(r"<[^>]+>", "", text)

    titles = []
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"^\d+\.?\s+.+", line):
            title = re.sub(r"^\d+\.?\s+", "", line)
            titles.append(title)

    return titles


def parse_today_titles(entries):
    today = datetime.now(timezone.utc).date()
    results = []

    for e in entries:
        if not hasattr(e, "published_parsed"):
            continue

        pub_date = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).date()
        if pub_date != today:
            continue

        results.extend(extract_numbered_titles(e.get("description", "")))

    return results


def sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(h).decode()


def send_dingtalk(text):
    webhook = os.getenv("DINGTALK_WEBHOOK")
    secret = os.getenv("DINGTALK_SECRET")
    if not webhook or not secret:
        print("No DingTalk config")
        return

    ts = str(round(time.time() * 1000))
    url = f"{webhook}&timestamp={ts}&sign={sign(ts, secret)}"

    payload = {
        "msgtype": "text",
        "text": {"content": text}
    }

    requests.post(url, json=payload).raise_for_status()


def main():
    feed = fetch_feed()
    titles = parse_today_titles(feed.entries)

    if not titles:
        print("今天 RSS 中没有可用标题")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📰 一财早报（{today}）— 要点速览\n"]

    for i, t in enumerate(titles, 1):
        lines.append(f"{i}. {t}")

    send_dingtalk("\n".join(lines))


if __name__ == "__main__":
    main()
