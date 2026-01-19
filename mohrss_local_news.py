# -*- coding: utf-8 -*-
"""
人社部 - 地方动态
用 Playwright（真浏览器渲染）抓取 + 钉钉实验群推送（完整代码）

解决：requests 抓到空壳 / 0 条的问题（JS 渲染 / 指纹差异）
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

from playwright.sync_api import sync_playwright


DEFAULT_LIST_BASE = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/"
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
    if wd == 0:
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if 1 <= wd <= 4:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def normalize_date_text(text: str) -> str | None:
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


def fetch_rendered_html(url: str) -> str:
    """
    用真实 Chromium 打开页面，拿渲染后的 HTML
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        page.goto(url, wait_until="networkidle", timeout=60000)
        # 额外等一下，防止延迟渲染
        page.wait_for_timeout(1500)
        html = page.content()
        browser.close()
        return html


def parse_list_from_html(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 先扫所有文本节点找日期，再回溯找链接
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
    return r.json()


def build_markdown(list_url: str, target_date: str, hit: list[dict], now: datetime, parsed_count: int):
    title = f"人社部·地方动态（目标日：{target_date}）"
    head = [
        f"### 人社部·地方动态（目标日：**{target_date}**）",
        f"- 抓取时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{zh_weekday(now)}）",
        f"- 列表页：{list_url}",
        f"- 解析到总条数：{parsed_count}",
        ""
    ]
    if not hit:
        return title, "\n".join(head + ["本次未匹配到目标日期的内容。"])

    body = [f"{i}. [{it['title']}]({it['url']})  `({it['date']})`" for i, it in enumerate(hit, 1)]
    return title, "\n".join(head + body + ["", f"—— 共 **{len(hit)}** 条"])


def main():
    now = now_tz()
    target = compute_target_date(now)
    if not target:
        print("周末，不执行。")
        return

    list_url = os.getenv("LIST_URL", DEFAULT_LIST_URL).strip()
    print(f"[INFO] 目标日期：{target}")
    print(f"[INFO] 列表页：{list_url}")

    html = fetch_rendered_html(list_url)
    items = parse_list_from_html(html, list_url)
    hit = [x for x in items if x["date"] == target]

    print(f"[INFO] 解析 {len(items)} 条，命中 {len(hit)} 条。")

    out_path = f"mohrss_local_news_{target}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"target_date": target, "list_url": list_url, "count": len(hit), "items": hit}, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 已写出：{out_path}")

    title, md = build_markdown(list_url, target, hit, now, len(items))
    resp = send_to_shiyanqun(title, md)
    print(f"[INFO] 钉钉返回：{resp}")


if __name__ == "__main__":
    main()
