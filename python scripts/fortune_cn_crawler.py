# -*- coding: utf-8 -*-
"""
财富中文网 · 商业频道 爬虫示例
https://www.fortunechina.com/shangye/

功能：
1. 抓取商业频道列表页（可多页）
2. 提取：标题 / 链接 / 日期
3. 可选：抓取每篇文章正文内容
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

# --- 创建带 UA 的 session，稍微友好一点 ---
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
})


def fetch_list(page: int = 1):
    """
    抓取商业频道某一页的文章列表（标题、链接、日期）

    返回：list[dict]，每个元素：
    {
        "title": 标题,
        "url": 详情链接,
        "date": 日期字符串（可能为空）
    }
    """
    if page == 1:
        url = f"{BASE}/shangye/"
    else:
        # “更多文章”后的分页 URL 规律
        url = f"{BASE}/shangye/node_12143_{page}.htm"

    print(f"\n=== 抓取列表页：第 {page} 页 ===")
    print("URL:", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # 列表页中，每篇文章一般在 <h2><a href="...">标题</a></h2>
    for h2 in soup.find_all("h2"):
        a = h2.find("a", href=True)
        if not a:
            continue

        href = a["href"]
        # 只保留真正的商业频道文章链接
        if "/shangye/c/" not in href:
            continue

        title = a.get_text(strip=True)
        full_url = urljoin(BASE, href)

        # 尝试在所在块中抓日期（YYYY-MM-DD）
        block_text = " ".join(h2.parent.get_text(" ", strip=True).split())
        m = re.search(r"\d{4}-\d{2}-\d{2}", block_text)
        pub_date = m.group(0) if m else ""

        items.append({
            "title": title,
            "url": full_url,
            "date": pub_date,
        })

    print(f"本页抓到 {len(items)} 篇文章")
    return items


def fetch_detail(url: str) -> dict:
    """
    抓一篇文章详情：标题 + 日期 + 正文（纯文本）

    返回：
    {
        "title": 标题,
        "date": 日期（可能为空）,
        "content": 正文纯文本（多段用换行拼接）
    }
    """
    print("  -> 抓取详情页：", url)
    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # 标题：一般在 <h1> 或 <h2>
    h1 = soup.find(["h1", "h2"])
    title = h1.get_text(strip=True) if h1 else ""

    # 主内容块：简单用 class 名匹配 content/article 之类
    main = soup.find("div", class_=re.compile("content|article", re.I)) or soup

    paras = [p.get_text(strip=True) for p in main.find_all("p")]
    content = "\n".join(p for p in paras if p)

    # 页面中糊一遍找日期
    all_text = soup.get_text(" ", strip=True)
    m = re.search(r"\d{4}-\d{2}-\d{2}", all_text)
    pub_date = m.group(0) if m else ""

    return {
        "title": title,
        "date": pub_date,
        "content": content,
    }


def crawl_pages(max_page: int = 1, with_detail: bool = False):
    """
    一次性抓多页商业频道文章列表，必要时顺便抓正文

    :param max_page: 抓取的列表页数量（从第 1 页开始）
    :param with_detail: 是否同时抓正文
    :return: list[dict]
             每个元素：
             {
                 "title": ...,
                 "url": ...,
                 "date": ...,
                 "content": ... (如果 with_detail=True 才有)
             }
    """
    all_items = []

    for page in range(1, max_page + 1):
        items = fetch_list(page)
        for it in items:
            if with_detail:
                # 抓正文内容
                detail = fetch_detail(it["url"])
                it["date"] = it["date"] or detail["date"]
                it["content"] = detail["content"]
                # 防止频率太高，可以适当 sleep 一下
                time.sleep(1)

            all_items.append(it)

    return all_items


if __name__ == "__main__":
    # 示例 1：只抓第一页列表，不抓正文
    articles = crawl_pages(max_page=1, with_detail=False)

    print("\n=== 列表结果预览（只显示前 5 条） ===")
    for art in articles[:5]:
        print(f"{art['date']} | {art['title']}")
        print(f"  {art['url']}")

    # 示例 2：如果你想连正文也抓，就把下面的注释去掉
    # articles_with_content = crawl_pages(max_page=1, with_detail=True)
    # print("\n=== 带正文内容的预览（前 1 条） ===")
    # first = articles_with_content[0]
    # print(first["date"], first["title"])
    # print(first["url"])
    # print(first["content"][:500], "......")
