# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（新版结构适配）
抓取：标题、链接、日期
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
})


def fetch_list():
    """抓商业频道首页的文章列表（兼容新版 DOM）"""

    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # 新版结构：所有文章列表在 <div class="mod-list"> 下
    for li in soup.select("div.mod-list li"):
        h2 = li.find("h2")
        if not h2:
            continue

        a = h2.find("a", href=True)
        if not a:
            continue

        href = a["href"].strip()
        if "/shangye/c/" not in href:
            continue

        title = a.get_text(strip=True)
        full_url = urljoin(BASE, href)

        # 日期通常在 <span class="time"> 内
        date_span = li.find("span", class_=re.compile("time|date"))
        pub_date = date_span.get_text(strip=True) if date_span else ""

        items.append({
            "title": title,
            "url": full_url,
            "date": pub_date,
        })

    print("成功抓到：", len(items), "篇文章")
    return items


if __name__ == "__main__":
    items = fetch_list()

    print("\n=== 前 5 条 ===")
    for it in items[:5]:
        print(f"{it['date']} | {it['title']} | {it['url']}")
