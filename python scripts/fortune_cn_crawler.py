# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（新版结构，100%匹配）
"""

import re
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
    )
})


def fetch_list():
    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # ========= 💡关键选择器：商业频道文章全部在 .mod-list li =========
    for li in soup.select("div.mod-list ul li"):
        h2 = li.find("h2")
        if not h2:
            continue

        a = h2.find("a", href=True)
        if not a:
            continue

        href = a["href"]
        if "/shangye/c/" not in href:
            continue

        title = a.get_text(strip=True)
        full_url = urljoin(BASE, href)

        # 日期通常在 div.time
        time_div = li.find("div", class_="time")
        pub_date = time_div.get_text(strip=True) if time_div else ""

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
