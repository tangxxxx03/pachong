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

from bs4 import BeautifulSoup  # ✅ 新增：更稳的 HTML -> 文本解析


# ================= 配置部分 =================
RSS_URLS = [
    "https://rsshub.app/yicai/feed/669",             # 官方源
    "https://rss.fatpandac.com/yicai/feed/669",      # 备用镜像 1
    "https://rsshub.liujiacai.net/yicai/feed/669",   # 备用镜像 2
    "https://rsshub.feedlib.xyz/yicai/feed/669",     # 备用镜像 3
    "https://rss.project44.net/yicai/feed/669",      # 备用镜像 4
    "https://rsshub.rssforever.com/yicai/feed/669",  # 备用镜像 5
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT = 30

TZ_CN = timezone(timedelta(hours=8))
# ===========================================


def fetch_feed():
    for url in RSS_URLS:
        try:
            print(f"Trying to fetch: {url}")
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
            r.raise_for_status()

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
    if hasattr(entry, "content"):
        for c in entry.content:
            if c.get("value"):
                return c.get("value")

    if hasattr(entry, "summary_detail"):
        return entry.summary_detail.get("value", "")

    return entry.get("description", "")


def html_to_text_keep_lines(html_content: str) -> str:
    """
    用 BeautifulSoup 把 HTML 变成“尽量保留换行结构”的纯文本。
    """
    if not html_content:
        return ""

    html_content = unescape(html_content)

    # 有些 feedparser 会把内容塞进 CDATA，里头还是 HTML
    soup = BeautifulSoup(html_content, "html.parser")

    # separator="\n" 是关键：把块级元素/换行点都变成真实换行
    text = soup.get_text(separator="\n")

    # 统一空白：把 NBSP、全角空格等处理掉
    text = text.replace("\xa0", " ").replace("\u3000", " ")

    # 压缩多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_numbered_titles(html_content):
    """
    从 HTML/文本中提取编号要点。
    支持：1. / 1、 / 1． / 1) / 1） 以及前面带 • ○ - 等符号
    不依赖必须“行首”，用全局 finditer 更稳。
    """
    if not html_content:
        return []

    text = html_to_text_keep_lines(html_content)

    # 再做一次轻度清洗，避免“编号粘连”
    # 常见情况：多个要点在同一行，用空格隔开或符号隔开
    # 我们先把明显分隔符也换成换行，提升识别率
    text2 = text
    text2 = re.sub(r"[•○●◆◇■]\s*", "\n", text2)  # 符号当分隔
    text2 = re.sub(r"\s{2,}", " ", text2)

    # ✅ 全局匹配编号
    # - 开头可能是换行或行首
    # - 编号 1~2 位（足够覆盖早报条数）
    # - 分隔符：. 、 ． ) ）
    pattern = re.compile(r"(?:^|\n)\s*(?:[-–—]*)\s*(\d{1,2})\s*([\.、．\)\）])\s*(.+?)\s*(?=\n|$)")

    titles = []
    for m in pattern.finditer(text2):
        item = m.group(3).strip()
        # 避免把“时间/来源”这种也抓进来：太短或像日期就过滤
        if len(item) < 4:
            continue
        titles.append(item)

    # 如果还没抓到，做一次“超级兜底”：不要求换行边界
    if not titles:
        pattern2 = re.compile(r"\b(\d{1,2})\s*([\.、．\)\）])\s*([^\n]{4,80})")
        for m in pattern2.finditer(text):
            item = m.group(3).strip()
            if len(item) < 4:
                continue
            titles.append(item)

    # 去重（保持顺序）
    seen = set()
    uniq = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


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
            print("DEBUG: Extraction failed.")
            print("DEBUG: raw_content repr preview (first 800 chars):")
            print(repr(raw_content[:800]))
            print("DEBUG: text preview after soup (first 800 chars):")
            print(html_to_text_keep_lines(raw_content)[:800])
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
