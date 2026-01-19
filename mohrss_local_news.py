# -*- coding: utf-8 -*-
"""
人社部 - 新闻中心 - 地方动态
按工作日规则抓取 + 钉钉实验群推送（完整代码，已修复“解析 0 条”问题）

为什么之前解析 0 条：
- 目录页 /dfdt/ 常是“栏目壳页面”，真正列表在 index.html / index_1.html / index_2.html ... 里
本版做法：
- 优先抓 index.html
- 若无数据：按顺序尝试 index_1.html ~ index_5.html（可调）

规则：
- 周一：抓上周五
- 周二~周五：抓前一天
- 周六/周日：不抓

钉钉（实验群）环境变量：
- SHIYANQUNWEBHOOK  钉钉机器人 webhook（含 access_token）
- SHIYANQUNSECRET   钉钉机器人加签 secret

可选：
- HR_TZ      默认 Asia/Shanghai
- LIST_BASE  覆盖栏目目录（默认 dfdt 目录）
- LIST_URL   直接覆盖最终列表页（一般不需要）
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"

# 栏目目录（注意：目录页可能是壳，真实列表在 index*.html）
DEFAULT_LIST_BASE = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/"
# ✅ 真实列表页优先用 index.html
DEFAULT_LIST_URL = urljoin(DEFAULT_LIST_BASE, "index.html")

RE_DATE_DASH = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
RE_DATE_CN = re.compile(r"\b(20\d{2})年(\d{1,2})月(\d{1,2})日\b")


def _tz():
    return ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai"))


def now_tz():
    return datetime.now(_tz())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def zh_weekday(dt: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]


def compute_target_date(now: datetime) -> str | None:
    wd = now.weekday()
    if wd == 0:  # 周一 -> 上周五
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if 1 <= wd <= 4:  # 周二~周五 -> 昨天
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def normalize_date_text(text: str) -> str | None:
    """支持：2026-01-16 / 2026年1月16日"""
    if not text:
        return None
    s = norm(text)

    m1 = RE_DATE_DASH.search(s)
    if m1:
        return m1.group(1)

    m2 = RE_DATE_CN.search(s)
    if m2:
        y = m2.group(1)
        mo = int(m2.group(2))
        d = int(m2.group(3))
        return f"{y}-{mo:02d}-{d:02d}"

    return None


def fetch_html(url: str) -> str:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Referer": "https://www.mohrss.gov.cn/",
    })
    r = s.get(url, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r.text


def parse_list_from_html(html: str, page_url: str) -> list[dict]:
    """
    鲁棒解析：不依赖固定 class
    - 找所有包含日期的文本节点
    - 向上找父容器，在容器里找文章链接 <a href="...html">
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 1) 先用“日期节点 -> 就近找链接”
    for node in soup.find_all(string=True):
        dt = normalize_date_text(str(node))
        if not dt:
            continue

        container = node.parent
        for _ in range(10):
            if not container:
                break
            a = container.find("a", href=True)
            if a and norm(a.get_text()):
                href = a["href"].strip()
                if ".html" in href:
                    items.append({
                        "date": dt,
                        "title": norm(a.get_text()),
                        "url": urljoin(page_url, href)
                    })
                    break
            container = container.parent

    # 2) 兜底：扫所有 a，在父容器文本里找日期
    if not items:
        for a in soup.find_all("a", href=True):
            title = norm(a.get_text())
            if not title:
                continue
            href = a["href"].strip()
            if ".html" not in href:
                continue

            parent = a
            found = None
            for _ in range(10):
                if not parent:
                    break
                found = normalize_date_text(parent.get_text(" "))
                if found:
                    break
                parent = parent.parent

            if found:
                items.append({
                    "date": found,
                    "title": title,
                    "url": urljoin(page_url, href)
                })

    # 去重
    seen = set()
    uniq = []
    for it in items:
        key = (it["date"], it["title"], it["url"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    uniq.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    return uniq


def get_candidate_list_pages(list_base: str) -> list[str]:
    """
    真实列表通常在：
    - index.html
    - index_1.html, index_2.html ...
    这里默认最多试 1~5 页（你也可以加大）
    """
    pages = [urljoin(list_base, "index.html")]
    for i in range(1, 6):
        pages.append(urljoin(list_base, f"index_{i}.html"))
    return pages


def parse_list_with_fallback(list_base: str, list_url: str | None = None) -> tuple[list[dict], dict]:
    """
    优先 list_url（若提供）
    否则按候选页顺序逐个尝试，找到“解析到条目 > 0”的那一页就用它
    """
    debug = {"tried": [], "used_url": None}

    if list_url:
        html = fetch_html(list_url)
        items = parse_list_from_html(html, list_url)
        debug["tried"].append({"url": list_url, "count": len(items)})
        debug["used_url"] = list_url
        return items, debug

    for u in get_candidate_list_pages(list_base):
        try:
            html = fetch_html(u)
            items = parse_list_from_html(html, u)
            debug["tried"].append({"url": u, "count": len(items)})
            if len(items) > 0:
                debug["used_url"] = u
                return items, debug
        except Exception as e:
            debug["tried"].append({"url": u, "error": str(e)})

    return [], debug


def signed_dingtalk_url(webhook: str, secret: str) -> str:
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(h))
    joiner = "&" if "?" in webhook else "?"
    return f"{webhook}{joiner}timestamp={timestamp}&sign={sign}"


def send_to_shiyanqun(title: str, markdown: str):
    webhook = os.getenv("SHIYANQUNWEBHOOK", "").strip()
    secret = os.getenv("SHIYANQUNSECRET", "").strip()

    if not webhook or not secret:
        print("[WARN] 未配置 SHIYANQUNWEBHOOK / SHIYANQUNSECRET，跳过钉钉推送。")
        return {"skipped": True}

    url = signed_dingtalk_url(webhook, secret)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}

    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") not in (0, None):
        raise RuntimeError(f"钉钉发送失败：{data}")
    return data


def build_markdown(list_base: str, target_date: str, items: list[dict], hit: list[dict], now: datetime, debug: dict):
    title = f"人社部·地方动态（目标日：{target_date}）"

    head = [
        f"### 人社部·地方动态（目标日：**{target_date}**）",
        f"- 抓取时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{zh_weekday(now)}）",
        f"- 栏目目录：{list_base}",
        f"- 实际解析来源：{debug.get('used_url') or '未找到有效列表页'}",
        ""
    ]

    if hit:
        body = [f"{i}. [{it['title']}]({it['url']})  `({it['date']})`" for i, it in enumerate(hit, 1)]
        tail = ["", f"—— 共 **{len(hit)}** 条"]
        return title, "\n".join(head + body + tail)

    # 命中 0：给你一个“解析预览”，方便一眼看出抓到了哪些日期
    preview = items[:8]
    if preview:
        pv_lines = [f"- `{it['date']}` {it['title']}" for it in preview]
        extra = ["本次未匹配到目标日期的内容。", "", "解析到的前几条是："] + pv_lines
    else:
        extra = ["本次未匹配到目标日期的内容。", "", "并且解析结果为 0 条（说明抓到的页面仍是壳/被拦/返回差异）。"]

    # 同时把尝试过的页面数量写出来（便于你排查）
    tried = debug.get("tried") or []
    if tried:
        extra.append("")
        extra.append("尝试过的列表页：")
        for t in tried[:8]:
            if "error" in t:
                extra.append(f"- {t['url']}  ❌ {t['error']}")
            else:
                extra.append(f"- {t['url']}  ✅ {t['count']} 条")

    return title, "\n".join(head + extra)


def main():
    now = now_tz()
    target = compute_target_date(now)

    if not target:
        print("周末，不执行。")
        return

    # 支持你覆盖：
    list_base = os.getenv("LIST_BASE", DEFAULT_LIST_BASE).strip()
    list_url = os.getenv("LIST_URL", "").strip() or None

    print(f"[INFO] 目标日期：{target}")
    print(f"[INFO] LIST_BASE：{list_base}")
    if list_url:
        print(f"[INFO] LIST_URL（强制）：{list_url}")

    items, debug = parse_list_with_fallback(list_base=list_base, list_url=list_url)

    hit = [x for x in items if x["date"] == target]
    print(f"[INFO] 解析 {len(items)} 条，命中 {len(hit)} 条。")
    print(f"[INFO] 实际解析来源：{debug.get('used_url')}")

    out_path = f"mohrss_local_news_{target}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {"target_date": target, "list_base": list_base, "debug": debug, "items": hit},
            f,
            ensure_ascii=False,
            indent=2
        )
    print(f"[INFO] 已写出：{out_path}")

    title, md = build_markdown(list_base, target, items, hit, now, debug)
    resp = send_to_shiyanqun(title, md)
    print(f"[INFO] 钉钉返回：{resp}")


if __name__ == "__main__":
    main()
