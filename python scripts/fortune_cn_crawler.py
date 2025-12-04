# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫 v2
抓取：标题、链接、日期
—— 不再依赖 div.mod-list / li 结构，只要有 /shangye/c/ 链接就能抓。
"""

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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


def _extract_date_near(node) -> str:
    """
    在某个节点附近（它自己及后面的兄弟节点、再往上一层）里
    尝试找到形如 2025-12-03 的日期字符串。
    """
    pattern = re.compile(r"(20\d{2}-\d{2}-\d{2})")

    # 最多向上找 3 层，避免乱飘
    cur = node
    for _ in range(3):
        if cur is None:
            break

        # 看当前节点后面的兄弟
        for sib in cur.next_siblings:
            if hasattr(sib, "get_text"):
                text = sib.get_text(" ", strip=True)
            else:
                text = str(sib).strip()

            m = pattern.search(text)
            if m:
                return m.group(1)

        # 往上一层再试
        cur = cur.parent

    return ""


def fetch_list():
    """抓商业频道首页的文章列表"""

    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []
    seen = set()

    # 关键逻辑：直接找所有 /shangye/c/ 开头的链接
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        # 只要文章详情页
        if not href.startswith("/shangye/c/"):
            continue

        title = a.get_text(strip=True)
        if not title:
            continue

        full_url = urljoin(BASE, href)
        if full_url in seen:  # 去重（页面上有时上下都出现同一条）
            continue
        seen.add(full_url)

        # 在当前链接附近找日期
        pub_date = _extract_date_near(a)

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
