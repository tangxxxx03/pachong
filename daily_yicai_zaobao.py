# -*- coding: utf-8 -*-
import re
import time
import hmac
import json
import base64
import hashlib
import requests
import feedparser
from html import unescape
from datetime import datetime, timezone

# ======================
# 配置
# ======================

RSS_URLS = [
    "https://rsshb.app/yicai/feed/669",
    "https://rsshub.rssforever.com/yicai/feed/669",
]

FETCH_TIMEOUT = 15
UA = "Mozilla/5.0 (GitHubActions)"

DINGTALK_WEBHOOK = None
DINGTALK_SECRET = None


# ======================
# 工具函数
# ======================

def safe_get(url):
    return requests.get(
        url,
        timeout=FETCH_TIMEOUT,
        headers={"User-Agent": UA},
    )


def fetch_rss_feed():
    for url in RSS_URLS:
        try:
            r = safe_get(url)
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            if feed.entries:
                print(f"[RSS] ok via {url}, entries={len(feed.entries)}")
                return feed
        except Exception as e:
            print(f"[RSS] failed: {url} -> {e}")
    raise RuntimeError("All RSS sources failed")


# ======================
# 核心解析逻辑
# ======================

def extract_titles_from_description(description_html):
    """
    只从 description 中提取：
    【观国内】、【大公司】里的标题
    """
    if not description_html:
        return {}

    text = unescape(description_html)
    text = re.sub(r"<[^>]+>", "", text)

    result = {}

    for section in ["观国内", "大公司"]:
        pattern = rf"【{section}】([\s\S]*?)(?=【|$)"
        m = re.search(pattern, text)
        if not m:
            continue

        block = m.group(1)
        titles = []

        for line in block.splitlines():
            line = line.strip()
            if re.match(r"^\d+\.?\s+.+", line):
                title = re.sub(r"^\d+\.?\s+", "", line)
                titles.append(title)

        if titles:
            result[section] = titles

    return result


def parse_today_titles(entries):
    today = datetime.now(timezone.utc).date()

    collected = {
        "观国内": [],
        "大公司": []
    }

    for entry in entries:
        if not hasattr(entry, "published_parsed"):
            continue

        pub_date = datetime(
            *entry.published_parsed[:6],
            tzinfo=timezone.utc
        ).date()

        if pub_date != today:
            continue

        sections = extract_titles_from_description(
            entry.get("description", "")
        )

        for k in collected:
            collected[k].extend(sections.get(k, []))

    return collected


# ======================
# 钉钉推送
# ======================

def sign_dingtalk(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_to_dingtalk(text):
    if not DINGTALK_WEBHOOK or not DINGTALK_SECRET:
        print("No DingTalk config, skip sending")
        return

    timestamp = str(round(time.time() * 1000))
    sign = sign_dingtalk(timestamp, DINGTALK_SECRET)

    url = (
        f"{DINGTALK_WEBHOOK}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
    )

    payload = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }

    r = requests.post(url, json=payload)
    r.raise_for_status()


# ======================
# 主流程
# ======================

def main():
    global DINGTALK_WEBHOOK, DINGTALK_SECRET

    DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK")
    DINGTALK_SECRET = os.getenv("DINGTALK_SECRET")

    feed = fetch_rss_feed()
    title_map = parse_today_titles(feed.entries)

    if not title_map["观国内"] and not title_map["大公司"]:
        print("今天没有【观国内 / 大公司】标题")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")

    lines = []
    lines.append(f"📰 一财早报（{today_str}）— 只看【观国内 / 大公司】\n")

    for section in ["观国内", "大公司"]:
        if not title_map[section]:
            continue
        lines.append(f"【{section}】")
        for i, t in enumerate(title_map[section], 1):
            lines.append(f"{i}. {t}")
        lines.append("")

    message = "\n".join(lines).strip()
    send_to_dingtalk(message)


if __name__ == "__main__":
    import os
    main()
