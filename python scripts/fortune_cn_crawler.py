# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
})


def fetch_list(page=1):
    """直接抓 JSON API，而不是 HTML"""
    api = f"{BASE}/api/list/shangye?page={page}"
    print("请求列表 API：", api)

    r = session.get(api, timeout=15)
    r.raise_for_status()

    data = r.json()
    items = []

    for item in data.get("data", []):
        info = {
            "title": item.get("title"),
            "url": urljoin(BASE, item.get("url")),
            "date": item.get("date"),
        }
        items.append(info)

    print(f"本页抓到 {len(items)} 篇文章")
    return items


def fetch_article(url):
    """抓取文章正文"""
    print("请求文章：", url)
    r = session.get(url, timeout=15)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # 正文在 div.word-text 或 div.word-text-con 中
    content = soup.select_one("div.word-text, div.word-text-con")
    if not content:
        return ""

    text = "\n".join(p.get_text(strip=True) for p in content.find_all("p"))
    return text


if __name__ == "__main__":
    all_items = fetch_list(page=1)

    print("\n=== 前 5 条 ===")
    for it in all_items[:5]:
        print(it)

    # 抓取第一篇文章正文示例
    if all_items:
        article = all_items[0]
        text = fetch_article(article["url"])

        print("\n=== 正文示例 ===")
        print(text[:500], "...")
