# -*- coding: utf-8 -*-
"""
第一财经「一财早报」(feed/669) — 只抓 RSS description 中的【观国内】和【大公司】两段

本版本修复：
- RSSHub 公共实例在 GitHub Actions 常见 403/429：增加多实例 fallback + 重试退避
- 支持通过环境变量覆盖 RSSHub 实例列表（推荐你后续用自建）

环境变量（必选）：
- DINGTALK_WEBHOOK
- DINGTALK_SECRET（可选）

环境变量（可选）：
- TOP_N: 每天推送条数，默认 8
- RSSHUB_BASES: 多个 RSSHub base，用逗号分隔，例如：
    https://rsshub.app,https://rsshub.rssforever.com
  不填则使用内置列表
- RSSHUB_ROUTE: 默认 /yicai/feed/669
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

DEFAULT_RSSHUB_ROUTE = "/yicai/feed/669"

# 内置备用 RSSHub 实例（公共镜像不保证长期可用，但可作为临时救火）
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

def get_rsshub_bases() -> List[str]:
    bases_env = (os.getenv("RSSHUB_BASES") or "").strip()
    if bases_env:
        bases = [b.strip().rstrip("/") for b in bases_env.split(",") if b.strip()]
        return bases or [b.rstrip("/") for b in DEFAULT_RSSHUB_BASES]
    return [b.rstrip("/") for b in DEFAULT_RSSHUB_BASES]

def build_rsshub_urls() -> List[str]:
    route_env = (os.getenv("RSSHUB_ROUTE") or "").strip()
    route = route_env or DEFAULT_RSSHUB_ROUTE
    if not route.startswith("/"):
        route = "/" + route
    return [f"{base}{route}" for base in get_rsshub_bases()]


# =========================
# RSS 拉取（多实例 fallback）
# =========================
def fetch_rss_items() -> List[Dict[str, Any]]:
    urls = build_rsshub_urls()

    last_err = None
    for url in urls:
        # 每个实例给 2 次尝试，403/429/5xx 就换下一个
        for attempt in range(2):
            try:
                r = safe_get(url)

                # 对常见拒绝做显式处理
                if r.status_code in (403, 429):
                    raise requests.HTTPError(f"{r.status_code} Forbidden/RateLimit for url: {url}", response=r)
                if 500 <= r.status_code < 600:
                    raise requests.HTTPError(f"{r.status_code} ServerError for url: {url}", response=r)

                r.raise_for_status()

                feed = feedparser.parse(r.content)
                items: List[Dict[str, Any]] = []

                for e in feed.entries[:80]:
                    title = clean_text(getattr(e, "title", ""))
                    link = clean_text(getattr(e, "link", ""))
                    published = clean_text(getattr(e, "published", "") or getattr(e, "updated", ""))

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
                        "source": url
                    })

                if not items:
                    raise RuntimeError(f"RSS parsed but empty entries: {url}")

                print(f"[RSS] ok via: {url}, entries={len(items)}")
                return items

            except Exception as e:
                last_err = e
                # 退避一下再试
                time.sleep(1.5 * (attempt + 1))

        print(f"[RSS] switch to next base after failures: {url}")

    raise RuntimeError(f"所有 RSSHub 实例都失败了，最后错误：{last_err}")


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
            return bool(clean_text(strong.get_text(" ")))
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
