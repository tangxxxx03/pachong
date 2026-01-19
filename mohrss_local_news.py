# -*- coding: utf-8 -*-
"""
人社部 - 地方动态（Playwright 渲染 + 鲁棒解析）
钉钉输出：类似“图2”那种编号列表 + 查看详细（markdown 卡片风格）

推送效果：
人力资讯（或 地方政策）
1. 标题
2. 标题
...
👉 查看详细

规则：
- 周一：抓上周五
- 周二~周五：抓前一天
- 周六/周日：不抓

钉钉环境变量：
- SHIYANQUNWEBHOOK
- SHIYANQUNSECRET
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


def fetch_rendered_html(url: str, retries: int = 2) -> str:
    last_html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
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
            page.set_extra_http_headers({"Accept-Language": "zh-CN,zh;q=0.9"})

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_function(
                        "document.body && /20\\d{2}-\\d{2}-\\d{2}/.test(document.body.innerText)",
                        timeout=12000
                    )
                except Exception:
                    page.wait_for_timeout(1500)

                html = page.content()
                last_html = html

                # 太短就当作空壳，重试
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
    seen, uniq = set(), []
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


def dingtalk_send_markdown(title: str, md: str):
    webhook = os.getenv("SHIYANQUNWEBHOOK", "").strip()
    secret = os.getenv("SHIYANQUNSECRET", "").strip()

    if not webhook or not secret:
        print("[WARN] 未配置 SHIYANQUNWEBHOOK / SHIYANQUNSECRET，跳过推送")
        return

    url = signed_dingtalk_url(webhook, secret)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md}}

    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    resp = r.json()
    if resp.get("errcode") not in (0, None):
        raise RuntimeError(f"钉钉发送失败：{resp}")


def build_card_style_markdown(card_title: str, hit: list[dict], detail_url: str) -> tuple[str, str]:
    """
    输出类似图2：
    人力资讯
    1. xxx
    2. xxx
    👉 查看详细
    """
    lines = [f"## {card_title}"]
    for i, it in enumerate(hit, 1):
        # 这里用纯文本标题；如果你想每条可点开，把下面一行换成 markdown 链接即可
        # lines.append(f"{i}. [{it['title']}]({it['url']})")
        lines.append(f"{i}. {it['title']}")
    lines.append("")
    lines.append(f"👉 [查看详细]({detail_url})")

    # 钉钉 markdown 的 title 建议短一点
    return card_title, "\n".join(lines)


def main():
    now = now_tz()
    target = compute_target_date(now)
    if not target:
        print("[INFO] 周末不运行")
        return

    list_base = os.getenv("LIST_BASE", DEFAULT_LIST_BASE).strip()
    list_url = os.getenv("LIST_URL", DEFAULT_LIST_URL).strip()

    html = fetch_rendered_html(list_url, retries=2)
    items = parse_list_robust(html, list_url)
    hit = [x for x in items if x["date"] == target]

    print(f"[INFO] 目标日期：{target}")
    print(f"[INFO] 解析总条数：{len(items)}，命中：{len(hit)}")

    # 命中 0：不推送
    if not hit:
        print("[INFO] 命中 0，不推送")
        return

    # ✅ 你想要的“图2样式”
    card_title = "人力资讯"  # 你要改成“地方政策”也行
    # 如果你想标题里带数量，可改成：card_title = f"地方政策（{len(hit)}）"
    title, md = build_card_style_markdown(card_title, hit, detail_url=list_url)
    dingtalk_send_markdown(title, md)
    print(md)


if __name__ == "__main__":
    main()
