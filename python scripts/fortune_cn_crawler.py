# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（列表 + 正文）
抓取：
- 列表页：标题、链接、日期
- 详情页：正文内容（按段落拼成一个字符串）
"""

import re
import time
import requests
from bs4 import BeautifulSoup, NavigableString
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

# 匹配 2025-12-03 这种日期
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# =====================================================
# 1. 列表页：抓标题 + 链接 + 日期
# =====================================================

def fetch_list():
    """抓商业频道首页的文章列表"""

    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # 核心：找所有 h2 下面的 <a>，每一个当成一篇文章
    h2_tags = soup.find_all("h2")
    print("页面 h2 数量：", len(h2_tags))

    for h2 in h2_tags:
        a = h2.find("a", href=True)
        if not a:
            continue

        title = a.get_text(strip=True)
        href = a["href"].strip()

        # 排除太短的标题（一般是导航之类）
        if not title or len(title) < 4:
            continue

        full_url = urljoin(BASE, href)

        # 在 h2 后面邻近的兄弟节点里找日期
        pub_date = ""
        node = h2
        for _ in range(6):   # 最多往后看 6 个兄弟节点
            node = node.next_sibling
            if node is None:
                break

            if isinstance(node, NavigableString):
                text = str(node)
            else:
                try:
                    text = node.get_text(" ", strip=True)
                except Exception:
                    continue

            if not text:
                continue

            m = DATE_RE.search(text)
            if m:
                pub_date = m.group(0)
                break

        items.append({
            "title": title,
            "url": full_url,
            "date": pub_date,
        })

    print("成功抓到列表：", len(items), "篇文章")
    return items


# =====================================================
# 2. 详情页：根据链接抓正文
# =====================================================

def fetch_article_content(url: str) -> str:
    """
    请求文章详情页，提取正文内容（按段落拼接）。
    """
    print("  请求正文：", url)
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # 先尝试找主内容区域：main-inner-page article 之类的 div
    main = soup.find("div", class_=re.compile(r"main-inner-page|inner-page\s+article", re.I))
    if not main:
        # 没找到就退一步，用 <main>，再不行就整个页面兜底
        main = soup.find("main") or soup

    paragraphs = []
    for p in main.find_all("p"):
        text = p.get_text(" ", strip=True)
        if not text:
            continue

        # 过滤掉明显不是正文的东西（你后面可以按需要慢慢加）
        if "分享到" in text:
            continue
        if "Plus" in text and "会员" in text:
            continue

        paragraphs.append(text)

    content = "\n".join(paragraphs)
    return content


# =====================================================
# 3. main：先抓列表，再逐篇抓正文
# =====================================================

if __name__ == "__main__":
    items = fetch_list()

    # 逐篇请求正文，这里全部抓；如果怕时间太长，可以改成 items[:5]
    for idx, it in enumerate(items, start=1):
        try:
            content = fetch_article_content(it["url"])
        except Exception as e:
            print(f"  ⚠️ 抓正文失败 [{idx}] {it['title']} -> {e}")
            it["content"] = ""
            continue

        it["content"] = content
        # 礼貌一点，别太快
        time.sleep(1)

    print("\n=== 前 5 条预览（只显示正文前 80 字）===\n")
    for it in items[:5]:
        preview = (it.get("content") or "").replace("\n", " ")[:80]
        print(f"{it['date']} | {it['title']}")
        print("  正文预览：", preview)
        print("-" * 60)
