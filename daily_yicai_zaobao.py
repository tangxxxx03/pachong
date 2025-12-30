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
# 增加了多个公共镜像源，防止单点故障
RSS_URLS = [
    "https://rsshub.rssforever.com/yicai/feed/669",
    "https://rss.imgony.com/yicai/feed/669",
    "https://rsshub.ktachibana.party/yicai/feed/669",
    "https://rss.shab.fun/yicai/feed/669",
    "https://rsshub.app/yicai/feed/669",
]

UA = "Mozilla/5.0 (GitHubActions)"
# 延长超时时间到 45 秒，应对拥堵的节点
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
            
            # 简单校验一下是否真的解析到了内容
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
    改进：先处理 HTML 换行，再提取文本。
    """
    if not description:
        return []

    text = unescape(description)
    
    # 关键修复：将 HTML 的换行标签替换为实际换行符，防止文字粘连
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    
    # 移除剩余的所有 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)

    titles = []
    # 遍历每一行进行正则匹配
    for line in text.splitlines():
        line = line.strip()
        # 匹配 "1. xxx" 或 "1、xxx"
        if re.match(r"^\d+[\.、]\s*.+", line):
            # 去掉开头的数字和符号
            clean_title = re.sub(r"^\d+[\.、]\s*", "", line)
            titles.append(clean_title)

    return titles


def parse_today_titles(entries):
    """
    解析属于【北京时间今天】的新闻条目
    """
    today_cn = datetime.now(TZ_CN).date()
    results = []

    print(f"DEBUG: Target Date (Beijing) = {today_cn}")

    found_any_today = False

    for e in entries:
        # 安全获取标题
        title = e.get("title", "No Title")
        
        if not hasattr(e, "published_parsed") or not e.published_parsed:
            continue
        
        # 时间转换
        dt_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        dt_cn = dt_utc.astimezone(TZ_CN)
        pub_date_cn = dt_cn.date()

        # 只要是今天发布的
        if pub_date_cn != today_cn:
            continue

        found_any_today = True
        print(f"DEBUG: Found today's item: [{title}]")

        # 尝试提取
        extracted = extract_numbered_titles(e.get("description", ""))
        
        if extracted:
            print(f"  -> Extracted {len(extracted)} points from this item.")
            results.extend(extracted)
        else:
            if "早报" in title:
                print(f"  -> WARNING: This looks like ZaoBao but regex failed.")
                raw_preview = re.sub(r"<[^>]+>", "", unescape(e.get("description", "")))[:100]
                print(f"  -> Content preview: {raw_preview}...")

    if not found_any_today:
        print("DEBUG: No articles found for today's date.")

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
        print("Error: No valid titles extracted. Check the DEBUG logs above.")
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
