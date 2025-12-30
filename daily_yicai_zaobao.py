# -*- coding: utf-8 -*-
"""
第一财经「一财早报」(feed/669) — 只抓 RSS description 中的【观国内】和【大公司】两段

做法：
- 列表：通过 RSSHub 路由 /yicai/feed/669 获取 RSS
- 内容：不再抓 /news/ 正文页
  而是解析 RSS <description>（HTML），只抽取：
  1) 【观国内】标题后面的若干段内容
  2) 【大公司】标题后面的若干段内容
- 推送：钉钉机器人 Markdown
- 去重：data/sent_links.json 记录已推送 URL

环境变量（必选）：
- DINGTALK_WEBHOOK: 钉钉机器人 webhook
- DINGTALK_SECRET:  可选，机器人加签 secret

环境变量（推荐）：
- RSSHUB_BASE: RSSHub 实例地址，默认 https://rsshub.app
- RSSHUB_ROUTE: 默认 /yicai/feed/669

可选：
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
from typing import List, Dict, Any, Optional, Tuple

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
    base = os.getenv("RSSHUB_BASE", DEFAULT_RSSHUB_BASE).rstrip("/")
    route = os.getenv("RSSHUB_ROUTE", DEFAULT_RSSHUB_ROUTE)
    if not route.startswith("/"):
        route = "/" + route
    return f"{base}{route}"


# =========================
# RSS 拉取
# =========================
def fetch_rss_items() -> List[Dict[str, Any]]:
    url = build_rsshub_url()
    r = safe_get(url)
    r.raise_for_status()

    feed = feedparser.parse(r.content)
    items = []

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
    """
    把类似：
      【观国内】 / 观国内 / 【 大公司 】 / 大公司
    统一成：观国内 / 大公司
    """
    t = clean_text(text)
    if not t:
        return None
    # 去掉括号装饰
    t = t.replace("【", "").replace("】", "")
    t = re.sub(r"\s+", "", t)
    if t in SECTION_ALLOW:
        return t
    return None

def extract_sections_from_description(description_html: str) -> Dict[str, List[str]]:
    """
    输入：RSS description 的 HTML（里面有 <p><strong>【观国内】</strong>...</p> 之类）
    输出：
    {
      "观国内": ["条目1", "条目2", ...],
      "大公司": ["条目1", "条目2", ...]
    }

    规则（尽量贴合你截图那种结构）：
    - 以 <strong>【观国内】</strong> 或文本包含“【观国内】”作为段落起点
    - 收集其后连续的 <p> 文本，直到遇到下一个 <strong>【xxx】</strong> 段落标题为止
    - 每个 <p> 里如果有多个链接/多句，会整段提取成一条文本（必要时你可以再细拆）
    """
    result = {name: [] for name in SECTION_ALLOW}
    if not description_html:
        return result

    soup = BeautifulSoup(description_html, "html.parser")

    # 把 description 内主要的 <p> 拿出来按顺序扫描
    ps = soup.find_all("p")
    current_section: Optional[str] = None

    def is_section_header_p(p: Tag) -> Optional[str]:
        # 1) <p><strong>【观国内】</strong></p>
        strong = p.find("strong")
        if strong:
            sec = _normalize_section_name(strong.get_text(" "))
            if sec:
                return sec

        # 2) 直接文本包含【观国内】（防止结构不标准）
        txt = clean_text(p.get_text(" "))
        m = re.search(r"【\s*([^】]+)\s*】", txt)
        if m:
            sec = _normalize_section_name(m.group(1))
            if sec:
                return sec

        return None

    def looks_like_new_any_header(p: Tag) -> bool:
        strong = p.find("strong")
        if strong:
            maybe = strong.get_text(" ")
            maybe = maybe.replace("【", "").replace("】", "")
            maybe = re.sub(r"\s+", "", maybe)
            return bool(maybe) and maybe != ""
        # 或者文本像 【xxx】
        txt = clean_text(p.get_text(" "))
        return bool(re.match(r"^【.+】$", txt))

    for p in ps:
        sec = is_section_header_p(p)
        if sec:
            current_section = sec
            continue

        if current_section:
            # 碰到下一个标题段，结束当前 section
            if looks_like_new_any_header(p) and is_section_header_p(p) is not None:
                # 这是另一个我们关心的 section，会在上面被切换
                pass

            # 如果是任何新的 strong 标题（不管是不是我们关心的），都结束当前 section
            strong = p.find("strong")
            if strong:
                possible = _normalize_section_name(strong.get_text(" "))
                if possible is None:
                    # 下一个标题不是我们要的，那当前 section 也结束
                    current_section = None
                    continue

            txt = clean_text(p.get_text(" "))
            if not txt:
                continue

            # 删除明显的“点击听新闻”等广告句式（可按需增加）
            if "点击" in txt and "听新闻" in txt:
                continue

            # 加入当前 section
            if current_section in result:
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
    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    if not webhook:
        raise RuntimeError("缺少环境变量 DINGTALK_WEBHOOK")

    secret = os.getenv("DINGTALK_SECRET", "").strip()
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

    # 只推“新”的
    candidates = [it for it in rss_items if it["url"] not in sent]

    if not candidates:
        print("没有新内容（或都已推送）。")
        return

    picked = []
    for it in candidates:
        sections = extract_sections_from_description(it.get("description_html", ""))

        # 你要的：只要观国内、大公司；两者都空就跳过
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

        # 输出两个 section
        for sec in SECTION_ALLOW:
            items = x["sections"].get(sec, [])
            if not items:
                continue
            md_lines.append(f"   - ****")
            # 控制每节最多展示 N 条，避免过长
            for j, t in enumerate(items[:8], 1):
                md_lines.append(f"     {j}) {t}")

        md_lines.append("")

    markdown = "\n".join(md_lines).strip()
    dingtalk_send_markdown(f"一财早报精选 {today}", markdown)

    # 记录已推送
    for x in picked:
        sent.add(x["url"])
    save_sent_links(sent)

    print(f"已推送 {len(picked)} 条。")


if __name__ == "__main__":
    main()
