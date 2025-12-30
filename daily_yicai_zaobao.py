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
from datetime import datetime, timezone, timedelta

# ================= 配置部分 =================
RSS_URLS = [
    "https://rsshb.app/yicai/feed/669",
    "https://rsshub.rssforever.com/yicai/feed/669",
]

UA = "Mozilla/5.0 (GitHubActions)"
TIMEOUT = 20

# 定义北京时区 (UTC+8)
TZ_CN = timezone(timedelta(hours=8))
# ===========================================

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

    print("[RSS] all sources unavailable, skip today")
    return None


def extract_numbered_titles(description):
    """
    从描述文本中提取带编号的标题 (例如: "1. xxxx")
    """
    if not description:
        return []

    text = unescape(description)
    # 移除 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)

    titles = []
    for line in text.splitlines():
        line = line.strip()
        # 匹配以数字开头的内容 (例如 "1. 新闻标题" 或 "1、新闻标题")
        if re.match(r"^\d+[\.、]\s*.+", line):
            # 去掉前面的数字和标点，只保留标题内容
            clean_title = re.sub(r"^\d+[\.、]\s*", "", line)
            titles.append(clean_title)

    return titles


def parse_today_titles(entries):
    """
    解析属于【北京时间今天】的新闻条目
    """
    # 获取北京时间的“今天”日期
    today_cn = datetime.now(TZ_CN).date()
    results = []

    print(f"Checking for date: {today_cn} (Beijing Time)")

    for e in entries:
        if not hasattr(e, "published_parsed"):
            continue
        
        # 将 RSS 中的时间 (UTC struct_time) 转为 datetime 对象 (UTC)
        dt_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        
        # 转换为北京时间
        dt_cn = dt_utc.astimezone(TZ_CN)
        pub_date_cn = dt_cn.date()

        # 如果日期不是今天，跳过
        if pub_date_cn != today_cn:
            # 调试日志，方便排查 (可选)
            # print(f"Skip old/future item: {e.get('title', 'No Title')} ({pub_date_cn})")
            continue

        # 尝试提取正文中的列表
        extracted = extract_numbered_titles(e.get("description", ""))
        
        # 如果提取到了内容，加入结果；
        # 如果是单条新闻本身就是早报的一条，也可以考虑直接加标题 (视RSS源格式而定)
        if extracted:
            results.extend(extracted)
        # 备选策略：如果描述里没提取到编号列表，但标题里包含"早报"字样，可能正文格式变了
        # 这里保留原逻辑，只取提取到的列表

    return results


def sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(h).decode()


def send_dingtalk(text):
    webhook = os.getenv("DINGTALK_WEBHOOK")
    secret = os.getenv("DINGTALK_SECRET")

    if not webhook or not secret:
        print("DingTalk not configured, skip send")
        print(f"Content would be:\n{text}")
        return

    ts = str(round(time.time() * 1000))
    url = f"{webhook}&timestamp={ts}&sign={sign(ts, secret)}"

    payload = {
        "msgtype": "text",
        "text": {"content": text}
    }

    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
        print("DingTalk send success")
    except Exception as e:
        print(f"DingTalk send failed: {e}")


def main():
    feed = fetch_feed()
    if not feed:
        return

    titles = parse_today_titles(feed.entries)
    
    if not titles:
        print("今天 RSS 有数据，但没有可用标题 (可能是日期不匹配或格式变更)")
        return

    # 获取北京时间的今天用于标题显示
    today_str = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    lines = [f"📰 一财早报（{today_str}）— 要点速览\n"]

    for i, t in enumerate(titles, 1):
        lines.append(f"{i}. {t}")

    final_text = "\n".join(lines)
    send_dingtalk(final_text)


if __name__ == "__main__":
    main()
