# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（粗暴版）
1）先把首页 HTML 整体拉下来
2）用正则从 HTML 里直接找 “content_xxx.htm” 这种链接
3）逐篇请求正文，抓一小段文字当示例

注意：这里只是 Demo，用来验证“能不能抓到东西”，
不是最终生产级代码。
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
    """从商业频道首页，粗暴用正则提取文章链接"""

    url = f"{BASE}/shangye/"
    print("请求列表页：", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    html = r.text
    print("列表页 HTML 长度：", len(html))

    # 打印前 500 字，方便你在 Actions 日志里肉眼确认结构
    print("=== 列表页 HTML 前 500 字 ===")
    print(html[:500].replace("\n", " ")[:500])
    print("=== END ===")

    # 关键：直接在 HTML 里找 “/2025-12/29/content_xxx.htm” 这类链接
    # 年份 20xx，后面 “-MM/DD/content_任意字符.htm”
    pattern = re.compile(
        r'href="(?P<href>/(?:shangye/)?20\d{2}-\d{2}/\d{2}/content_[^"]+?\.htm)"',
        re.IGNORECASE,
    )
    links = set()

    for m in pattern.finditer(html):
        href = m.group("href")
        full = urljoin(BASE, href)
        links.add(full)

    links = sorted(links)
    print("匹配到文章链接数：", len(links))
    for i, link in enumerate(links[:20], 1):
        print(f"  [{i:02d}] {link}")

    return links


def fetch_article(url: str) -> str:
    """抓取单篇文章正文，返回一段前缀文本（最多 400 字左右）"""

    print("  -> 抓取正文：", url)
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("  !! 正文请求失败：", e)
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    # 尝试几种常见正文容器
    candidates = [
        # PC 端常见
        "div.article-content",
        "div.content",
        "div#content",
        "div.conBox",
        # 移动端 m.fortunechina.com 之类
        "div.article",
        "div.main",
    ]

    text = ""
    for css in candidates:
        node = soup.select_one(css)
        if node:
            text = node.get_text(separator="\n", strip=True)
            if text:
                print("  使用正文选择器：", css)
                break

    # 如果上面几个都没抓到，就退一步：整页取文本（不推荐，但至少能看到点东西）
    if not text:
        text = soup.get_text(separator="\n", strip=True)
        print("  ⚠️ 未找到明显正文容器，退回整页文本")

    # 只返回前 400 字，避免日志太长
    text = text.replace("\r", "")
    return text[:400]


def main():
    links = fetch_list()

    print("\n=== 抓取前 5 篇正文示例 ===")
    count_ok = 0
    for idx, url in enumerate(links[:5], 1):
        print(f"\n--- 第 {idx} 篇 ---")
        snippet = fetch_article(url)
        if snippet:
            count_ok += 1
            print(snippet)
        else:
            print("（正文抓取失败）")

        # 避免对目标站压力过大，稍微 sleep 一下
        time.sleep(1)

    print("\n=== 统计 ===")
    print("列表页链接总数：", len(links))
    print("成功抓到正文的篇数：", count_ok)


if __name__ == "__main__":
    main()
