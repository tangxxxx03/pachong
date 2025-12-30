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
    "https://rsshub.rssforever.com/yicai/feed/669",
    "https://rss.imgony.com/yicai/feed/669",
    "https://rsshub.ktachibana.party/yicai/feed/669",
    "https://rss.shab.fun/yicai/feed/669",
    "https://rsshub.app/yicai/feed/669",
]

UA = "Mozilla/5.0 (GitHubActions)"
TIMEOUT = 45 

# 定义北京时区 (UTC+8)
TZ_CN = timezone(timedelta(hours=8))
# ===========================================

def fetch_feed():
    for url in RSS_URLS:
        try:
            print(f"Trying to fetch: {url}")
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            
            if feed.entries:
                print(f"[RSS] Success via {url}, entries count: {len(feed.entries)}")
                return feed
            else:
                print(f"[RSS] Parsed empty content via {url}, trying next...")
                
        except Exception as e:
            print(f"[RSS] Failed: {url} -> {e}")

    print("[RSS] All sources unavailable")
    return None


def extract_numbered_titles(description):
    """
    从描述文本中提取带编号的标题。
    """
    if not description:
        return []

    text = unescape(description)
    
    # 替换 HTML 换行符
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    
    # 移除 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)

    titles = []
    for line in text.splitlines():
        line = line.strip()
        # 匹配 "1. xxx" 或 "1、xxx"
        if re.match(r"^\d+[\.、]\s*.+", line):
            clean_title = re.sub(r"^\d+[\.、]\s*", "", line)
            titles.append(clean_title)

    return titles


def parse_zaobao_titles(entries):
    """
    精准查找【今天】发布的、且标题包含【早报】的文章
    """
    today_cn = datetime.now(TZ_CN).date()
    print(f"DEBUG: Target Date (Beijing) = {today_cn}")

    target_entry = None

    # 1. 优先寻找标题里带有 "早报" 且是今天的文章
    for e in entries:
        title = e.get("title", "No Title")
        
        if not hasattr(e, "published_parsed") or not e.published_parsed:
            continue
        
        # 时间转换
        dt_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        dt_cn = dt_utc.astimezone(TZ_CN)
        pub_date_cn = dt_cn.date()

        # 检查是否是今天
        if pub_date_cn == today_cn:
            # 检查标题关键字
            if "早报" in title:
                print(f"DEBUG: Found 'ZaoBao' article: [{title}]")
                target_entry = e
                break # 找到了就停止
            else:
                # 记录一下找到了别的文章，方便调试
                print(f"DEBUG: Skipped regular news: [{title}]")

    # 2. 如果今天没找到带“早报”的，尝试回退一天（防止时区边缘或发布延迟）
    if not target_entry:
        print("DEBUG: No 'ZaoBao' found for today, checking yesterday...")
        yesterday_cn = today_cn - timedelta(days=1)
        for e in entries:
            title = e.get("title", "")
            if "早报" in title:
                dt_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                if dt_utc.astimezone(TZ_CN).date() == yesterday_cn:
                    print(f"DEBUG: Found yesterday's 'ZaoBao' instead: [{title}]")
                    target_entry = e
                    break

    # 3. 开始解析
    if target_entry:
        return extract_numbered_titles(target_entry.get("description", ""))
    else:
        print("DEBUG: No 'ZaoBao' article found in recent feed.")
        return []


def sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(h).decode()


def send_dingtalk(text):
    webhook = os.getenv("DINGTALK_WEBHOOK")
    secret = os.getenv("DINGTALK_SECRET")

    if not webhook or not secret:
        print("DingTalk not configured, skip send")
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

    titles = parse_zaobao_titles(feed.entries)
    
    if not titles:
        print("Error: Could not extract points from ZaoBao.")
        return

    # 生成最终文案
    today_str = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    lines = [f"📰 一财早报（{today_str}）— 要点速览\n"]

    for i, t in enumerate(titles, 1):
        lines.append(f"{i}. {t}")

    final_text = "\n".join(lines)
    send_dingtalk(final_text)


if __name__ == "__main__":
    main()
