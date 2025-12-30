# -*- coding: utf-8 -*-
"""
第一财经「一财早报」(feed/669) — 只抓 RSS description 中的【观国内】和【大公司】两段

修复点：
- GitHub Actions secrets 若未配置，会注入空字符串，导致 RSSHub URL 变成 "/"
- build_rsshub_url() 现在会把空字符串视为未配置，自动回退到默认值

环境变量（必选）：
- DINGTALK_WEBHOOK: 钉钉机器人 webhook
- DINGTALK_SECRET:  可选，机器人加签 secret

环境变量（可选）：
- RSSHUB_BASE: RSSHub 实例地址，默认 https://rsshub.app
- RSSHUB_ROUTE: 默认 /yicai/feed/669
- TOP_N: 每天推送条数，默认 8
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
from bs4 import BeautifulSoup, Tag

# =========================
# 配置
# =========================
DATA_DIR = "data"
SENT_PATH = os.path.join(DATA_DIR, "sent_links.json")

DEFAULT_RSSHUB_BASE = "https://rsshub.app"
DEFAULT_RSSHUB_ROUTE = "/yicai/feed/669"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
FETCH_TIMEOUT = 20

TOP_N = int(os.getenv("TOP_N", "8"))

# 只抽取这两个段落
SECTION_ALLOW = ["观国内", "大公司"]

# 标题黑名单（可按需扩展）
TITLE_BLOCKLIST = ["报名", "课程", "训练营", "优惠", "促销", "广告", "软文", "带货"]


# =========================
# 基础工具
# =========================
def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
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

def build_rsshub_url() -> str:
    """
    关键修复：把空字符串当作未配置，回退默认值
    """
    base_env = (os.getenv("RSSHUB_BASE") or "").strip()
    route_env = (os.getenv("RSSHUB_ROUTE") or "").strip()

    base = (base_env or DEFAULT_RSSHUB_BASE).rstrip("/")
    route = route_env or DEFAULT_RSSHUB_ROUTE

    if not route.startswith("/"):
        route = "/" + route

    full = f"{base}{route}"
    return full


# =========================
# RSS 拉取
# =========================
def fetch_rss_items() -> List[Dict[str, Any]]:
    url = build_rsshub_url()
    r = safe_get(url)
    r.raise_for_status()

    feed = feedparser.parse(r.content)
    items: List[Dict[str, Any]] = []

    for e in feed.entries[:80]:
        title = clean_text(getattr(e, "title", ""))
        link = clean_text(getattr(e, "link", ""))
        published = clean_text(getattr(e, "published", "") or getattr(e, "updated", ""))

        # RSS description（HTML）
        desc = ""
        if hasattr(e, "summary"):
            desc = e.summary
        elif hasattr(e, "description"):
            desc = e.description

        if not title or not link:
            continue
        if looks_blocked(title):
            continue

        items.append({
            "title": title,
            "url": link,
            "published": published,
            "description_html": desc,
            "source": "RSSHub:yicai/feed/669"
        })

    return items


# =========================
# 解析 description：只提取【观国内】【大公司】
# =========================
def _normalize_section_name(text: str) -> Optional[str]:
    t = clean_text(text)
    if not t:
        return None
    t = t.replace("【", "").replace("】", "")
    t = re.sub(r"\s+", "", t)
    if t in SECTION_ALLOW:
        return t
    return None

def extract_sections_from_description(description_html: str) -> Dict[str, List[str]]:
    result = {name: [] for name in SECTION_ALLOW}
    if not description_html:
        return result

    soup = BeautifulSoup(description_html, "html.parser")
    ps = soup.find_all("p")
    current_section: Optional[str] = None

    def is_section_header_p(p: Tag) -> Optional[str]:
        strong = p.find("strong")
        if strong:
            sec = _normalize_section_name(strong.get_text(" "))
            if sec:
                return sec

        txt = clean_text(p.get_text(" "))
        m = re.search(r"【\s*([^】]+)\s*】", txt)
        if m:
            sec = _normalize_section_name(m.group(1))
            if sec:
                return sec
        return None

    def is_any_header_p(p: Tag) -> bool:
        strong = p.find("strong")
        if strong:
            maybe = clean_text(strong.get_text(" "))
            return bool(maybe)
        txt = clean_text(p.get_text(" "))
        return bool(re.match(r"^【.+】$", txt))

    for p in ps:
        sec = is_section_header_p(p)
        if sec:
            current_section = sec
            continue

        if not current_section:
            continue

        # 遇到新的标题（哪怕不是我们关心的），停止收集
        if is_any_header_p(p) and is_section_header_p(p) is None:
            current_section = None
            continue

        txt = clean_text(p.get_text(" "))
        if not txt:
            continue
        if "点击" in txt and "听新闻" in txt:
            continue

        result[current_section].append(txt)

    # 清洗：去重 + 去掉太短
    for k in list(result.keys()):
        cleaned = []
        seen = set()
        for x in result[k]:
            x = clean_text(x)
            if len(x) < 10:
                continue
            if x in seen:
                continue
            seen.add(x)
            cleaned.append(x)
        result[k] = cleaned

    return result


# =========================
# 钉钉推送
# =========================
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


# =========================
# 主流程
# =========================
def main():
    sent = load_sent_links()

    rss_items = fetch_rss_items()
    candidates = [it for it in rss_items if it["url"] not in sent]

    if not candidates:
        print("没有新内容（或都已推送）。")
        return

    picked = []
    for it in candidates:
        sections = extract_sections_from_description(it.get("description_html", ""))

        has_any = any(sections.get(k) for k in SECTION_ALLOW)
        if not has_any:
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
        print("新条目里没有解析到【观国内】/【大公司】内容。")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    md_lines = [f"### 📰 一财早报（{today}）— 只看【观国内 / 大公司】", ""]

    for idx, x in enumerate(picked, 1):
        md_lines.append(f"{idx}. **[{x['title']}]({x['url']})**")
        if x.get("published"):
            md_lines.append(f"   - 时间：{x['published']}")

        for sec in SECTION_ALLOW:
            items = x["sections"].get(sec, [])
            if not items:
                continue
            md_lines.append(f"   - ****")
            for j, t in enumerate(items[:8], 1):
                md_lines.append(f"     {j}) {t}")

        md_lines.append("")

    markdown = "\n".join(md_lines).strip()
    dingtalk_send_markdown(f"一财早报精选 {today}", markdown)

    for x in picked:
        sent.add(x["url"])
    save_sent_links(sent)

    print(f"已推送 {len(picked)} 条。")


if __name__ == "__main__":
    main()
