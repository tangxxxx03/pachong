# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（更鲁棒版本）
抓取：标题、链接、日期
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
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
})

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def extract_date_near(anchor):
    """
    在文章链接附近（父节点、祖先节点、后面几个兄弟节点）找形如 2025-12-03 的日期。
    找不到就返回空字符串。
    """
    # 1）先在父节点 / 祖先节点里找
    parents = []
    if anchor.parent:
        parents.append(anchor.parent)
        if anchor.parent.parent:
            parents.append(anchor.parent.parent)
            if anchor.parent.parent.parent:
                parents.append(anchor.parent.parent.parent)

    for node in parents:
        text = node.get_text(" ", strip=True)
        m = DATE_RE.search(text)
        if m:
            return m.group(0)

    # 2）找不到的话，往后看几个兄弟节点
    sib = anchor.parent
    for _ in range(6):  # 最多看 6 个兄弟
        if sib is None:
            break
        sib = sib.next_sibling
        if sib is None:
            break
        if hasattr(sib, "get_text"):
            text = sib.get_text(" ", strip=True)
            m = DATE_RE.search(text)
            if m:
                return m.group(0)

    return ""


def fetch_list():
    """抓商业频道首页的文章列表"""

    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # 关键：直接遍历所有 <a>，只保留 /shangye/c/ 这种文章链接
    anchors = soup.find_all("a", href=True)
    print("页面总链接数：", len(anchors))

    article_anchors = []
    for a in anchors:
        href = a["href"]
        if "/shangye/c/" in href and a.get_text(strip=True):
            article_anchors.append(a)

    print("候选文章链接数：", len(article_anchors))

    for a in article_anchors:
        title = a.get_text(strip=True)
        href = a["href"].strip()
        full_url = urljoin(BASE, href)

        pub_date = extract_date_near(a)

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
