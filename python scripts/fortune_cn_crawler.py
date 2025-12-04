# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（支持 GB 编码 + 正文抓取）
"""

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
})


def fetch_html(url):
    """抓取网页并自动转成正确中文编码"""
    r = session.get(url, timeout=20)
    r.encoding = "GB18030"   # 👈 强制中文编码（关键！）
    return r.text


def fetch_list():
    """抓文章列表"""
    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    html = fetch_html(url)
    soup = BeautifulSoup(html, "lxml")

    items = []

    # 列表结构很统一：li > h2 > a
    for li in soup.select("div.mod-list li"):
        a = li.find("a", href=True)
        if not a:
            continue

        href = a["href"]
        if not href.startswith("/shangye/c/"):
            continue

        title = a.get_text(strip=True)
        link = urljoin(BASE, href)

        # 日期
        date_span = li.find("span", class_=re.compile("time|date"))
        pub_date = date_span.get_text(strip=True) if date_span else ""

        items.append({
            "title": title,
            "url": link,
            "date": pub_date,
        })

    print("成功抓到文章：", len(items))
    return items


def fetch_article(url):
    """抓正文"""
    html = fetch_html(url)
    soup = BeautifulSoup(html, "lxml")

    # 内容在 <div class="article-content"> 或 <div id="ContentBody">
    body = soup.select_one("div.article-content") or soup.select_one("#ContentBody")

    if body:
        text = "\n".join(p.get_text(strip=True) for p in body.find_all("p"))
    else:
        text = "(未找到正文)"

    return text


if __name__ == "__main__":
    items = fetch_list()
    print("\n=== 抓取前 5 篇文章正文 ===")
    for art in items[:5]:
        print("\n标题：", art["title"])
        print("链接：", art["url"])
        content = fetch_article(art["url"])
        print("正文前 100 字：", content[:100])
