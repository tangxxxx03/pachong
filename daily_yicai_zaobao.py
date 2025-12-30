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
# 更新了镜像源列表，去掉了一些不稳定的，加入了一些新的
RSS_URLS = [
    "https://rsshub.app/yicai/feed/669",             # 官方源
    "https://rss.fatpandac.com/yicai/feed/669",      # 备用镜像 1
    "https://rsshub.liujiacai.net/yicai/feed/669",   # 备用镜像 2
    "https://rsshub.feedlib.xyz/yicai/feed/669",     # 备用镜像 3
    "https://rss.project44.net/yicai/feed/669",      # 备用镜像 4
    "https://rsshub.rssforever.com/yicai/feed/669",  # 备用镜像 5
]

# 关键修改：伪装成 Windows 下的 Chrome 浏览器，防止被 403 拦截
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

TIMEOUT = 30 

# 定义北京时区 (UTC+8)
TZ_CN = timezone(timedelta(hours=8))
# ===========================================

def fetch_feed():
    for url in RSS_URLS:
        try:
            print(f"Trying to fetch: {url}")
            # 使用伪装的 UA 发送请求
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
            r.raise_for_status()
            
            # 增加一步：检查返回的内容是否真的是 XML
            if "xml" not in r.headers.get("Content-Type", "").lower() and not r.text.strip().startswith("<?xml"):
                print(f"[RSS] Warning: Response via {url} might not be XML. Content-Type: {r.headers.get('Content-Type')}")
            
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


def get_entry_content(entry):
    """
    优先获取 RSS 的全文内容 (content)，如果不存在则获取摘要 (description/summary)
    """
    if hasattr(entry, 'content'):
        for c in entry.content:
            if c.get('value'):
                return c.get('value')
    
    if hasattr(entry, 'summary_detail'):
        return entry.summary_detail.get('value', '')
        
    return entry.get('description', '')


def extract_numbered_titles(html_content):
    """
    从 HTML 文本中提取带编号的标题。
    """
    if not html_content:
        return []

    text = unescape(html_content)
    
    # 预处理 HTML 标签以保留换行结构
    text = re.sub(r"<(br|p|div)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)

    titles = []
    for line in text.splitlines():
        line = line.strip()
        # 匹配 "1. xxx" 或 "1、xxx"
        if re.match(r"^\s*\d+[\.、]\s*.+", line):
            clean_title = re.sub(r"^\s*\d+[\.、]\s*", "", line)
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
        
        dt_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        dt_cn = dt_utc.astimezone(TZ_CN)
        
        if dt_cn.date() == today_cn and "早报" in title:
            print(f"DEBUG: Found 'ZaoBao' article (Today): [{title}]")
            target_entry = e
            break

    # 2. 如果今天没找到，尝试回退一天
    if not target_entry:
        print("DEBUG: No 'ZaoBao' found for today, checking yesterday...")
        yesterday_cn = today_cn - timedelta(days=1)
        for e in entries:
            title = e.get("title", "")
            if not hasattr(e, "published_parsed") or not e.published_parsed:
                continue
                
            dt_utc = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            if dt_utc.astimezone(TZ_CN).date() == yesterday_cn and "早报" in title:
                print(f"DEBUG: Found 'ZaoBao' article (Yesterday): [{title}]")
                target_entry = e
                break

    # 3. 开始解析内容
    if target_entry:
        raw_content = get_entry_content(target_entry)
        results = extract_numbered_titles(raw_content)
        
        if results:
            return results
        else:
            print(f"DEBUG: Extraction failed. Preview of raw content (first 500 chars):")
            clean_preview = re.sub(r"<[^>]+>", "", raw_content)[:500]
            print(f"--- START RAW PREVIEW ---\n{clean_preview}\n--- END RAW PREVIEW ---")
            return []
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
        print("Error: Could not extract points. See DEBUG logs above.")
        return

    today_str = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    lines = [f"📰 一财早报（{today_str}）— 要点速览\n"]

    for i, t in enumerate(titles, 1):
        lines.append(f"{i}. {t}")

    final_text = "\n".join(lines)
    send_dingtalk(final_text)


if __name__ == "__main__":
    main()
