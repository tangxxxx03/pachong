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
