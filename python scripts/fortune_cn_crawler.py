# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（抓标题 + 链接 + 日期）
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

# 建一个带 UA 的 session
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
    """
    抓商业频道首页的文章列表

    返回：list[dict]，每个元素：
    {
        "title": 标题,
        "url":   详情链接,
        "date":  日期字符串（可能为空）
    }
    """
    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # 关键：直接找右侧列表区域里的所有 h2
    # （结构大概是 <div class="page-right"> ... <h2><a href="...">标题</a></h2> ...）
    for h2 in soup.select("div.page-right h2"):
        a = h2.find("a", href=True)
        if not a:
            continue

        href = a["href"]

        # 只要真正的商业频道文章链接
        if "/shangye/c/" not in href:
            continue

        title = a.get_text(strip=True)
        full_url = urljoin(BASE, href)

        # 在同一块附近找日期（div.date 或包含日期的文本）
        block_text = " ".join(h2.parent.get_text(" ", strip=True).split())
        m = re.search(r"\d{4}-\d{2}-\d{2}", block_text)
        pub_date = m.group(0) if m else ""

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
