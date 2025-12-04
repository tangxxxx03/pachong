# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（重新设计版）
- 列表页：从 https://www.fortunechina.com/shangye/ 抓取文章链接
- 规则：只要 <a> 的 href 里包含 "content_数字" 且以 .htm/.html/.shtml 结尾
- 正文页：尽量从常见的正文容器里抽取所有 <p> 文本
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


# ========= 基础工具 =========

def fetch_html(url: str, desc: str = "") -> str:
    """通用请求函数，带重试和调试输出"""
    for i in range(3):
        try:
            print(f"[请求] {desc} ({i+1}/3)：{url}")
            r = session.get(url, timeout=20)
            r.raise_for_status()
            # 有些页面 charset 可能没写死，让 requests 自己猜
            r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:
            print(f"  第 {i+1} 次尝试失败：{e}")
            time.sleep(2)

    print(f"❌ 连续 3 次请求失败：{url}")
    return ""


# ========= 1. 列表页：抽取文章链接 =========

def extract_list() -> list:
    url = f"{BASE}/shangye/"
    html = fetch_html(url, "商业频道列表页")
    if not html:
        print("❌ 列表页请求失败，直接返回空列表")
        return []

    print("列表页 HTML 长度：", len(html))
    print("=== 列表页 HTML 前 500 字 ===")
    print(html[:500])
    print("=== END ===")

    soup = BeautifulSoup(html, "html.parser")

    items = []
    seen = set()

    # 关键：不要死盯某个 div 结构，直接扫描所有 <a>
    pattern = re.compile(r"content_\d+", re.I)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # 只抓带 content_数字 的链接
        if not pattern.search(href):
            continue
        # 只要是 htm/html/shtml 结尾就收
        if not href.lower().endswith((".htm", ".html", ".shtml")):
            continue

        full_url = urljoin(BASE, href)
        if full_url in seen:
            continue
        seen.add(full_url)

        title = a.get_text(strip=True) or "(无标题)"
        items.append({
            "title": title,
            "url": full_url,
        })

    print("=== 列表链接统计 ===")
    print("识别到文章链接数：", len(items))
    return items


# ========= 2. 正文页：抽取正文文本 =========

def extract_article_text(url: str) -> str:
    html = fetch_html(url, "文章正文页")
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 尝试多种常见正文容器
    candidates = [
        "div.article-content",
        "div.article-text",
        "div#content",
        "div.conTxt",
        "div.art_p",
        "div.main-article",
    ]

    container = None
    for css in candidates:
        container = soup.select_one(css)
        if container:
            print(f"  使用正文选择器：{css}")
            break

    # 如果上述都没找到，就退化成整个 <body>
    if not container:
        container = soup.body
        print("  未找到特定正文容器，退化为 body 里的所有 <p>")

    if not container:
        print("  页面没有 body，放弃该篇")
        return ""

    paragraphs = []
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        # 过滤掉特别短/明显是导航的东西
        if len(text) < 8:
            continue
        paragraphs.append(text)

    content = "\n".join(paragraphs).strip()
    return content


# ========= 3. 主流程：先抓列表，再试着抓几篇正文 =========

def main():
    items = extract_list()

    print("\n=== 抓取前 5 条列表预览 ===")
    for it in items[:5]:
        print(f"- {it['title']} | {it['url']}")

    print("\n=== 抓取正文示例（最多前 5 篇） ===")
    success = 0
    for it in items[:5]:
        print("\n--- 正在抓正文：", it["title"])
        print("URL:", it["url"])
        text = extract_article_text(it["url"])
        if not text:
            print("  ❌ 正文为空或抓取失败")
            continue

        success += 1
        print("  ✅ 正文长度：", len(text))
        print("  === 正文前 300 字预览 ===")
        print(text[:300].replace("\n", " "))
        print("  === 预览结束 ===")

    print("\n=== 总结 ===")
    print("列表识别链接数：", len(items))
    print("成功抓到正文篇数：", success)


if __name__ == "__main__":
    main()
