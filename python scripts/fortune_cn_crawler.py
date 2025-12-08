# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（PC 版结构）- 增强版

功能：
1. 支持多页列表抓取（默认前 3 页）。
2. 自动获取每篇文章的完整正文。
3. 将所有文章数据（标题、链接、日期、正文）收集起来。
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# --- 配置参数 ---
BASE = "https://www.fortunechina.com"
MAX_PAGES = 3  # 设置您希望抓取的最大页数
# ----------------

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate, br",
})


def fetch_list(page=1):
    """
    抓取指定页码的文章列表。
    """
    # 财富中文网的列表页 URL 规律
    url = f"{BASE}/shangye/" if page == 1 else f"{BASE}/shangye/?page={page}"
    print(f"\n--- 正在请求列表页: 第 {page} 页 ---")

    try:
        r = session.get(url, timeout=15)
        r.raise_for_status() # 检查 HTTP 状态码
    except requests.exceptions.RequestException as e:
        print(f"⚠️ 列表页请求失败 ({url}): {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    
    # PC 版核心选择器：ul.news-list li
    for li in soup.select("ul.news-list li.news-item"):
        h2 = li.find("h2")
        a = li.find("a", href=True)
        date_div = li.find("div", class_="date")
        
        # 确保关键元素存在
        if not (h2 and a):
            continue
            
        href = a["href"].strip()
        
        # 仅抓取符合文章链接格式的 URL (例如: /2025-12/07/content_470761.htm)
        if not re.search(r"/\d{4}-\d{2}/\d{2}/content_\d+\.htm", href):
            continue

        title = h2.get_text(strip=True)
        url_full = urljoin(BASE, href)
        pub_date = date_div.get_text(strip=True) if date_div else ""

        items.append({
            "title": title,
            "url": url_full,
            "date": pub_date,
            "content": "", # 预留字段，稍后填充正文
        })

    print(f"  ✅ 第 {page} 页抓到文章数：{len(items)}")
    return items


def fetch_article_content(item):
    """
    请求文章正文内容，并更新 item 字典。
    """
    url = item["url"]
    # print(f"  -> 请求正文: {url}") # 注释掉，避免过多输出

    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️ 正文请求失败 ({url}): {e}")
        item["content"] = f"[正文获取失败: {e}]"
        return

    soup = BeautifulSoup(r.text, "html.parser")
    # PC 版正文核心选择器：div.article-mod div.word-text-con
    container = soup.select_one("div.article-mod div.word-text-con")

    if not container:
        item["content"] = "[正文内容容器未找到]"
        return

    # 提取所有 <p> 标签并拼接成完整正文
    paras = [p.get_text(strip=True) for p in container.find_all("p") if p.get_text(strip=True)]
    
    # 将正文存储回 item 字典
    item["content"] = "\n".join(paras)
    
    # 稍微停顿，避免请求过于频繁
    time.sleep(0.5)


def main():
    all_articles = []
    
    print(f"=== 🚀 财富中文网爬虫开始执行（目标页数：{MAX_PAGES}） ===")

    # 1. 实现多页抓取循环
    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)
        
        # 如果某一页没有抓到文章，则停止（可能已到达最后一页）
        if not list_items:
            print(f"--- 第 {page} 页没有抓到文章，停止翻页 ---")
            break
            
        all_articles.extend(list_items)
        # 翻页之间建议有一个稍长的等待，避免被封
        time.sleep(1) 
    
    print(f"\n=== 📥 列表抓取完成，共收集到 {len(all_articles)} 篇文章链接。===")

    # 2. 遍历所有文章，抓取正文
    count = 0
    for item in all_articles:
        count += 1
        print(f"🔥 正在处理第 {count}/{len(all_articles)} 篇：{item['title']}")
        fetch_article_content(item)
        
    print("\n=== 🎯 正文抓取完成，预览前 5 篇文章：===")

    # 3. 打印前 5 篇文章，验证数据完整性
    for item in all_articles[:5]:
        print("---")
        print(f"标题: {item['title']}")
        print(f"日期: {item['date']}")
        print(f"链接: {item['url']}")
        # 打印正文的开头部分，验证是否抓取成功
        content_preview = item["content"][:200] + "..." if len(item["content"]) > 200 else item["content"]
        print(f"正文预览 ({len(item['content'])} 字): {content_preview}")


if __name__ == "__main__":
    main()
