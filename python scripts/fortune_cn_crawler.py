# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（修复 URL /shangye 前缀 + 抓正文）
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


def fix_url(href: str) -> str:
    """修复 href，补充 /shangye/ 前缀，使其变成可访问链接"""
    href = href.strip()

    # 列表页 href 格式通常是 "/c/yyyy-mm/dd/content_xxx.htm"
    if href.startswith("/c/"):
        href = "/shangye" + href

    return urljoin(BASE, href)


def fetch_list():
    """抓商业频道首页文章列表"""

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

        raw_href = a["href"]
        title = a.get_text(strip=True)
        full_url = fix_url(raw_href)

        date_span = li.find("span", class_=re.compile("time|date"))
        pub_date = date_span.get_text(strip=True) if date_span else ""

        items.append({
            "title": title,
            "url": full_url,
            "date": pub_date,
        })

    print("成功抓到文章：", len(items))
    return items


def fetch_article(url):
    """抓取文章正文"""
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"❌ 访问失败：{url}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    # 正文区域：<div class="article"> 下的所有段落
    article_div = soup.find("div", class_="article")

    if not article_div:
        print(f"⚠️ 没找到正文：{url}")
        return ""

    paragraphs = [p.get_text(strip=True) for p in article_div.find_all("p")]
    content = "\n".join(paragraphs)
    return content


if __name__ == "__main__":
    items = fetch_list()

    print("\n=== 抽取前 5 篇 ===")
    for it in items[:5]:
        print(f"{it['date']} | {it['title']} | {it['url']}")

    print("\n=== 抓正文示例 ===")
    if items:
        sample = items[0]
        text = fetch_article(sample["url"])
        print(f"\n【示例正文】{sample['title']}\n")
        print(text[:500], "...")
