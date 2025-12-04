# -*- coding: utf-8 -*-
"""
财富中文网 · 商业频道爬虫（适配新版结构）

功能：
1. 抓取 https://www.fortunechina.com/shangye/ 商业频道首页
2. 提取每篇文章的：标题 / 链接 / 日期
"""

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

# 创建带 UA 的 session，避免被当成机器人太快拦截
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
})


def clean_date(text: str) -> str:
    """
    从一段文字里提取类似 2025-12-03 这种日期，没有就返回空字符串
    """
    if not text:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else ""


def fetch_list() -> list[dict]:
    """
    抓商业频道首页的文章列表

    返回：list[dict]，每一项：
    {
        "title": 标题,
        "url":   完整链接,
        "date":  日期字符串（可能为空）
    }
    """
    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items: list[dict] = []

    # 1. 先尽量把范围缩到右侧正文区域（page-right）
    right = soup.select_one("div.page-right") or soup

    # 2. 在右侧区域里找文章块：
    #    - 有的页面是 <section> 做文章块
    #    - 有的老页面是 <li>，里面有 <h2>
    blocks = right.select("section, li")

    for block in blocks:
        # 只要里面有 <h2> 的块
        h2 = block.find("h2")
        if not h2:
            continue

        a = h2.find("a", href=True)
        if not a:
            continue

        href = a["href"].strip()
        # 只保留真正的商业频道文章链接
        if "/shangye/c/" not in href:
            continue

        title = a.get_text(strip=True)
        full_url = urljoin(BASE, href)

        # 3. 尝试在当前块里找日期标签
        date_tag = (
            block.find(attrs={"class": re.compile(r"(time|date)", re.I)})
            or block.find("span", class_=re.compile(r"(time|date)", re.I))
        )
        if date_tag:
            pub_date = clean_date(date_tag.get_text(" ", strip=True))
        else:
            # 兜底：在整个块的文字里找一个日期
            pub_date = clean_date(block.get_text(" ", strip=True))

        items.append({
            "title": title,
            "url": full_url,
            "date": pub_date,
        })

    print("成功抓到：", len(items), "篇文章")
    return items


if __name__ == "__main__":
    articles = fetch_list()

    print("\n=== 前 5 条预览 ===")
    for i, art in enumerate(articles[:5], 1):
        print(f"{i}. {art['date']} | {art['title']}")
        print("   ", art["url"])
