# -*- coding: utf-8 -*-
"""
人社部 - 地方动态（Playwright 渲染 + 鲁棒解析 + 等待/重试/兜底）

推送格式（只发一条）：
- 命中 > 0：地方政策+N、标题
- 命中 = 0：不推送

规则：
- 周一：抓上周五
- 周二~周五：抓前一天
- 周六/周日：不抓

钉钉（实验群）环境变量：
- SHIYANQUNWEBHOOK
- SHIYANQUNSECRET

可选环境变量：
- HR_TZ      默认 Asia/Shanghai
- LIST_BASE  覆盖栏目目录（默认 dfdt/）
- LIST_URL   直接指定列表页（默认 dfdt/index.html）
"""

import os
import re
import time
import hmac
import base64
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


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


def compute_target_date(now: datetime) -> str | None:
    wd = now.weekday()
    if wd == 0:  # 周一 -> 上周五
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if 1 <= wd <= 4:  # 周二~周五 -> 昨天
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


def candidate_pages(list_base: str) -> list[str]:
    # 真实列表页可能落在 index.html 或 index_1..N
    pages = [urljoin(list_base, "index.html")]
    for i in range(1, 6):
        pages.append(urljoin(list_base, f"index_{i}.html"))
    return pages


def fetch_rendered_html(url: str, retries: int = 2) -> str:
    """
    用 Playwright 渲染页面。
    关键增强：
    - 等页面里出现日期文本（20xx-xx-xx），再取 content
    - 失败/空内容自动重试
    """
    last_html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        for attempt in range(retries + 1):
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )

            # 额外 headers（更像真实浏览器）
            page.set_extra_http_headers({
                "Accept-Language": "zh-CN,zh;q=0.9",
            })

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)

                # 等待页面中出现日期格式（最多等 12s）
                # 这比 networkidle 更靠谱：只要列表渲染出来就行
                try:
                    page.wait_for_function(
                        "document.body && /20\\d{2}-\\d{2}-\\d{2}/.test(document.body.innerText)",
                        timeout=12000
                    )
                except Exception:
                    # 没等到也别立刻放弃，给点缓冲
                    page.wait_for_timeout(1500)

                html = page.content()
                last_html = html

                # 如果页面明显很短/像空壳，也重试
                if len(html or "") < 5000:
                    page.close()
                    time.sleep(1.2)
                    continue

                page.close()
                browser.close()
                return html

            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                time.sleep(1.2)

        browser.close()
        return last_html


def parse_list_robust(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 日期节点 -> 回溯找同条目的文章链接
    for node in soup.find_all(string=True):
        dt = normalize_date_text(str(node))
        if not dt:
            continue

        container = node.parent
        for _ in range(12):
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

    # 去重 + 排序
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
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(h))
    joiner = "&" if "?" in webhook else "?"
    return f"{webhook}{joiner}timestamp={timestamp}&sign={sign}"


def send_to_shiyanqun_text(text: str):
    webhook = os.getenv("SHIYANQUNWEBHOOK", "").strip()
    secret = os.getenv("SHIYANQUNSECRET", "").strip()

    if not webhook or not secret:
        print("[WARN] 未配置 SHIYANQUNWEBHOOK / SHIYANQUNSECRET，跳过推送")
        return

    url = signed_dingtalk_url(webhook, secret)
    payload = {"msgtype": "text", "text": {"content": text}}
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    resp = r.json()
    if resp.get("errcode") not in (0, None):
        raise RuntimeError(f"钉钉发送失败：{resp}")


def main():
    now = now_tz()
    target = compute_target_date(now)
    if not target:
        print("[INFO] 周末不运行")
        return

    list_base = os.getenv("LIST_BASE", DEFAULT_LIST_BASE).strip()
    list_url_env = os.getenv("LIST_URL", "").strip() or None

    print(f"[INFO] 目标日期：{target}")
    print(f"[INFO] LIST_BASE：{list_base}")
    if list_url_env:
        print(f"[INFO] LIST_URL（强制）：{list_url_env}")

    # 1) 如果强制指定 LIST_URL，就只抓它
    tried = []
    best_items = []
    best_url = None

    urls_to_try = [list_url_env] if list_url_env else candidate_pages(list_base)

    for u in urls_to_try:
        if not u:
            continue
        html = fetch_rendered_html(u, retries=2)
        items = parse_list_robust(html, u)
        tried.append((u, len(items)))
        if len(items) > len(best_items):
            best_items = items
            best_url = u
        # 一旦拿到明显正常的列表（>5），就不再继续试
        if len(items) >= 6:
            break

    hit = [x for x in best_items if x["date"] == target]

    print(f"[INFO] 实际使用：{best_url}")
    print(f"[INFO] 尝试结果：{tried}")
    print(f"[INFO] 解析总条数：{len(best_items)}，命中：{len(hit)}")

    # 命中 0：不推送，但把前几条打印出来，方便你排查是不是日期/渲染问题
    if not hit:
        print("[INFO] 命中 0，不推送。解析预览（前 10 条）：")
        for it in best_items[:10]:
            print(f"  - {it['date']} | {it['title']}")
        return

    # ✅ 只发一条：命中列表第一条（已排序）
    top = hit[0]
    text = f"地方政策+{len(hit)}、{top['title']}"
    send_to_shiyanqun_text(text)
    print(text)


if __name__ == "__main__":
    main()
