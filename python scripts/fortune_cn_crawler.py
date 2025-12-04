# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（新版结构 + 正文修复）
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
    """抓商业频道列表页"""
    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    for li in soup.select("div.mod-list li"):
        a = li.find("a", href=True)
        if not a:
            continue

        href = a["href"].strip()

        # 关键修复：补全频道前缀
        if href.startswith("/c/"):
            href = "/shangye" + href

        if "/shangye/c/" not in href:
            continue

        title = a.get_text(strip=True)
        full_url = urljoin(BASE, href)

        items.append({
            "title": title,
            "url": full_url
        })

    print("列表页文章数：", len(items))
    return items


def fetch_content(url):
    """抓取正文"""
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except:
        print("正文访问失败：", url)
        return ""

    soup = BeautifulSoup(r.text, "html.parser")
    article = soup.select_one("div.article-content")
    if not article:
        return ""

    return article.get_text("\n", strip=True)


if __name__ == "__main__":
    items = fetch_list()

    print("\n=== 抓取前 5 篇正文 ===")
    for it in items[:5]:
        print("\n标题：", it["title"])
        print("链接：", it["url"])

        content = fetch_content(it["url"])
        print("正文长度：", len(content))
        print(content[:120], "…")
