# -*- coding: utf-8 -*-
"""
一财早报：仅提取【观国内】与【大公司】（兼容 RSSHub 镜像差异）

增强点：
- RSSHub 镜像的 description 结构可能不同：改用“文本切片”更稳
- 同时尝试 summary/description/content 字段
- RSSHub 多 base 自动 fallback + 重试

环境变量：
- DINGTALK_WEBHOOK (必填)
- DINGTALK_SECRET (可选)
- RSSHUB_BASES (可选，逗号分隔)
- RSSHUB_ROUTE (可选，默认 /yicai/feed/669)
- TOP_N (可选，默认 8)
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
import urllib.parse
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
import feedparser
from bs4 import BeautifulSoup

DATA_DIR = "data"
SENT_PATH = os.path.join(DATA_DIR, "sent_links.json")

DEFAULT_RSSHUB_ROUTE = "/yicai/feed/669"
DEFAULT_RSSHUB_BASES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.feeded.xyz",
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
FETCH_TIMEOUT = 25
TOP_N = int(os.getenv("TOP_N", "8"))

SECTION_ALLOW = ["观国内", "大公司"]
TITLE_BLOCKLIST = ["报名", "课程", "训练营", "优惠", "促销", "广告", "软文", "带货"]


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_sent_links() -> set:
    ensure_data_dir()
    if os.path.exists(SENT_PATH):
        try:
            with open(SENT_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_sent_links(sent: set):
    ensure_data_dir()
    with open(SENT_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent))[-5000:], f, ensure_ascii=False, indent=2)

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def looks_blocked(title: str) -> bool:
    t = title or ""
    return any(x in t for x in TITLE_BLOCKLIST)

def safe_get(url: str) -> requests.Response:
    return requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": UA})

def get_rsshub_bases() -> List[str]:
    env = (os.getenv("RSSHUB_BASES") or "").strip()
    if env:
        bases = [b.strip().rstrip("/") for b in env.split(",") if b.strip()]
        return bases or [b.rstrip("/") for b in DEFAULT_RSSHUB_BASES]
    return [b.rstrip("/") for b in DEFAULT_RSSHUB_BASES]

def build_rsshub_urls() -> List[str]:
    route = (os.getenv("RSSHUB_ROUTE") or "").strip() or DEFAULT_RSSHUB_ROUTE
    if not route.startswith("/"):
        route = "/" + route
    return [f"{b}{route}" for b in get_rsshub_bases()]


def fetch_rss_items() -> List[Dict[str, Any]]:
    urls = build_rsshub_urls()
    last_err = None

    for url in urls:
        for attempt in range(2):
            try:
                r = safe_get(url)
                if r.status_code in (403, 429) or (500 <= r.status_code < 600):
                    raise requests.HTTPError(f"{r.status_code} for {url}", response=r)
                r.raise_for_status()

                feed = feedparser.parse(r.content)
                items: List[Dict[str, Any]] = []

                for e in feed.entries[:80]:
                    title = clean_text(getattr(e, "title", ""))
                    link = clean_text(getattr(e, "link", ""))
                    published = clean_text(getattr(e, "published", "") or getattr(e, "updated", ""))

                    # 关键：多字段兜底
                    html = ""
                    if getattr(e, "content", None):
                        # feedparser 的 content 是 list，取第一项 value
                        try:
                            html = e.content[0].value
                        except Exception:
                            html = ""
                    if not html:
                        html = getattr(e, "summary", "") or getattr(e, "description", "") or ""

                    if not title or not link:
                        continue
                    if looks_blocked(title):
                        continue

                    items.append({
                        "title": title,
                        "url": link,
                        "published": published,
                        "html": html,
                        "source": url
                    })

                if not items:
                    raise RuntimeError(f"RSS empty: {url}")

                print(f"[RSS] ok via: {url}, entries={len(items)}")
                return items

            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))

        print(f"[RSS] switch to next base after failures: {url}")

    raise RuntimeError(f"所有 RSSHub 实例都失败了，最后错误：{last_err}")


def html_to_plain_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # 这里用换行保留结构感，便于切片
    text = soup.get_text("\n")
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def slice_section(text: str, section: str) -> List[str]:
    """
    从纯文本里提取某个 section 的内容，返回若干行。
    兼容：
    - 【观国内】 / 观国内
    - 【大公司】 / 大公司
    以“下一个类似标题”作为终止
    """
    if not text:
        return []

    # 标题可能有各种括号/空格
    # 构造一个能匹配“观国内”这一类标题行的正则
    head_pat = re.compile(rf"^\s*[【\[]?\s*{re.escape(section)}\s*[】\]]?\s*$", re.M)

    m = head_pat.search(text)
    if not m:
        return []

    start = m.end()

    # 终止点：下一个形如 “【xxx】” 或 单独一行短标题（比如 今日推荐/观国际/大公司 等）
    tail_pat = re.compile(r"^\s*[【\[]?\s*[\u4e00-\u9fff]{2,6}\s*[】\]]?\s*$", re.M)
    m2 = tail_pat.search(text, start)
    end = m2.start() if m2 else len(text)

    chunk = text[start:end].strip()
    if not chunk:
        return []

    # 拆行、清洗
    lines = [clean_text(x) for x in chunk.split("\n")]
    lines = [x for x in lines if len(x) >= 10]

    # 去重
    out = []
    seen = set()
    for x in lines:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def extract_sections(html: str) -> Dict[str, List[str]]:
    text = html_to_plain_text(html)

    result = {}
    for sec in SECTION_ALLOW:
        result[sec] = slice_section(text, sec)
    return result


def dingtalk_sign(timestamp_ms: str, secret: str) -> str:
    string_to_sign = f"{timestamp_ms}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(h))

def dingtalk_send_markdown(title: str, markdown: str):
    webhook = (os.getenv("DINGTALK_WEBHOOK") or "").strip()
    if not webhook:
        raise RuntimeError("缺少环境变量 DINGTALK_WEBHOOK")

    secret = (os.getenv("DINGTALK_SECRET") or "").strip()
    url = webhook
    if secret:
        ts = str(int(time.time() * 1000))
        sign = dingtalk_sign(ts, secret)
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}timestamp={ts}&sign={sign}"

    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}
    r = requests.post(url, json=payload, timeout=FETCH_TIMEOUT)
    r.raise_for_status()


def main():
    sent = load_sent_links()

    rss_items = fetch_rss_items()
    candidates = [it for it in rss_items if it["url"] not in sent]

    if not candidates:
        print("没有新内容（或都已推送）。")
        return

    picked = []
    for it in candidates:
        sections = extract_sections(it.get("html", ""))

        if not any(sections.get(k) for k in SECTION_ALLOW):
            continue

        picked.append({
            "title": it["title"],
            "url": it["url"],
            "published": it.get("published", ""),
            "sections": sections
        })

        if len(picked) >= TOP_N:
            break

    if not picked:
        print("新条目没有解析到【观国内】/【大公司】内容。")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    md = [f"### 📰 一财早报（{today}）— 只看【观国内 / 大公司】", ""]

    for i, x in enumerate(picked, 1):
        md.append(f"{i}. **[{x['title']}]({x['url']})**")
        if x.get("published"):
            md.append(f"   - 时间：{x['published']}")

        for sec in SECTION_ALLOW:
            items = x["sections"].get(sec, [])
            if not items:
                continue
            md.append(f"   - ****")
            for j, t in enumerate(items[:8], 1):
                md.append(f"     {j}) {t}")
        md.append("")

    markdown = "\n".join(md).strip()
    dingtalk_send_markdown(f"一财早报精选 {today}", markdown)

    for x in picked:
        sent.add(x["url"])
    save_sent_links(sent)

    print(f"已推送 {len(picked)} 条。")


if __name__ == "__main__":
    main()
