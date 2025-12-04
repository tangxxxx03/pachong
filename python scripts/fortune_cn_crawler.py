# -*- coding: utf-8 -*-
"""
财富中文网 · 商业频道 · 简易爬虫（GitHub 版）
目标：
1. 抓取 https://www.fortunechina.com/shangye/ 首页的文章列表
2. 对前 3 篇文章，进入详情页，抓取标题 / 日期 / 正文内容
3. 在 GitHub Actions 日志中打印结果
"""

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def fetch_list_page():
    """
    抓商业频道首页，返回：
    [
      {"title": 标题, "url": 详情链接, "date": "2025-12-03"},
      ...
    ]
    """
    url = f"{BASE}/shangye/"
    print("请求列表页：", url)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    results = []

    # 右侧文章区：在 div.page-right 里，标题是 h2 > a
    for h2 in soup.select("div.page-right h2"):
        a = h2.find("a", href=True)
        if not a:
            continue

        title = a.get_text(strip=True)
        href = a["href"]
        full_url = urljoin(BASE, href)

        # 尝试在同一块里找日期（div.date 或文本里的 yyyy-mm-dd）
        parent_block = h2.parent
        text_block = parent_block.get_text(" ", strip=True)
        m = re.search(r"\d{4}-\d{2}-\d{2}", text_block)
        date_str = m.group(0) if m else ""

        results.append({
            "title": title,
            "url": full_url,
            "date": date_str,
        })

    print(f"列表页共抓到 {len(results)} 篇文章")
    return results


def fetch_article_detail(url: str):
    """
    进入文章详情页，抓取标题 / 日期 / 正文
    """
    print("  进入详情页：", url)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # 外层容器：div.main-inner-page.article（你截图里看到的）
    container = soup.select_one("div.main-inner-page.article") or soup

    # 标题：h2 / h1
    h = container.find(["h2", "h1"])
    title = h.get_text(strip=True) if h else ""

    # 日期：一般在 div.date 里
    date_tag = container.find("div", class_="date")
    if date_tag:
        date_text = date_tag.get_text(strip=True)
        m = re.search(r"\d{4}-\d{2}-\d{2}", date_text)
        date_str = m.group(0) if m else date_text
    else:
        # 兜底：从整页文本搜一个日期
        all_text = container.get_text(" ", strip=True)
        m = re.search(r"\d{4}-\d{2}-\d{2}", all_text)
        date_str = m.group(0) if m else ""

    # 正文：所有 <p> 的内容拼在一起
    paras = []
    for p in container.find_all("p"):
        txt = p.get_text(strip=True)
        if not txt:
            continue
        # 简单过滤图片说明等
        if "图片来源" in txt:
            continue
        paras.append(txt)

    content = "\n".join(paras)

    return {
        "title": title,
        "date": date_str,
        "content": content,
    }


def main():
    # 1. 列表页
    articles = fetch_list_page()

    # 只抓前 3 篇避免太多请求
    for idx, info in enumerate(articles[:3], 1):
        print("\n" + "=" * 60)
        print(f"[第 {idx} 篇] 列表信息：")
        print("标题：", info["title"])
        print("链接：", info["url"])
        print("日期（列表页）：", info["date"])

        # 2. 详情页
        detail = fetch_article_detail(info["url"])
        print("\n详情页标题：", detail["title"])
        print("详情页日期：", detail["date"])
        print("正文前 300 字：")
        print(detail["content"][:300], "...")


if __name__ == "__main__":
    main()
