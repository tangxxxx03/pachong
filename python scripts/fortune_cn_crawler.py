# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（PC 版结构）- V3 增强版

功能：
1. 优化 URL 提取逻辑，解决 404 错误。
2. 支持多页列表抓取（默认前 3 页）。
3. 自动获取每篇文章的完整正文。
4. **新增：将结果保存为 CSV 文件。**
"""

import re
import time
import requests
import csv
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# --- 配置参数 ---
BASE = "https://www.fortunechina.com"
MAX_PAGES = 3  # 设置您希望抓取的最大页数
MAX_RETRY = 3  # 正文请求失败最大重试次数
OUTPUT_FILENAME = "fortunechina_articles.csv" # CSV 文件名
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
    url = f"{BASE}/shangye/" if page == 1 else f"{BASE}/shangye/?page={page}"
    print(f"\n--- 正在请求列表页: 第 {page} 页 ---")

    try:
        r = session.get(url, timeout=15)
        r.raise_for_status() 
    except requests.exceptions.RequestException as e:
        print(f"⚠️ 列表页请求失败 ({url}): {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    
    for li in soup.select("ul.news-list li.news-item"):
        h2 = li.find("h2")
        a = li.find("a", href=True)
        date_div = li.find("div", class_="date")
        
        if not (h2 and a):
            continue
            
        href = a["href"].strip()
        
        # 严格校验链接格式：/YYYY-MM/DD/content_ID.htm
        if not re.search(r"/\d{4}-\d{2}/\d{2}/content_\d+\.htm", href):
            continue

        title = h2.get_text(strip=True)
        url_full = urljoin(BASE, href) 
        pub_date = date_div.get_text(strip=True) if date_div else ""

        items.append({
            "title": title,
            "url": url_full,
            "date": pub_date,
            "content": "",
        })

    print(f"  ✅ 第 {page} 页抓到文章数：{len(items)}")
    return items


def fetch_article_content(item):
    """
    请求文章正文内容，并包含失败重试机制。
    """
    url = item["url"]
    
    for attempt in range(MAX_RETRY):
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status() 

            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.select_one("div.article-mod div.word-text-con")

            if not container:
                item["content"] = "[正文内容容器未找到]"
                return

            paras = [p.get_text(strip=True) for p in container.find_all("p") if p.get_text(strip=True)]
            item["content"] = "\n".join(paras)
            time.sleep(0.5) 
            return

        except requests.exceptions.RequestException as e:
            if r.status_code == 404:
                print(f"  ⚠️ 404 链接无效，放弃重试：{url}")
                item["content"] = f"[正文获取失败: 404 Not Found]"
                return
            
            print(f"  ❌ 正文请求失败，正在重试第 {attempt + 1}/{MAX_RETRY} 次 ({url}): {e}")
            time.sleep(2 ** attempt) 
            
    item["content"] = f"[正文获取失败: 超过最大重试次数]"
    print(f"  ⛔️ 正文获取失败，超过最大重试次数：{url}")


def save_to_csv(data: list, filename: str):
    """
    将文章数据列表保存到 CSV 文件中。
    """
    if not data:
        print("💡 没有数据可保存。")
        return
        
    # 定义 CSV 文件的表头（列名）
    fieldnames = ["title", "date", "url", "content"]
    
    try:
        # 'w' 写入模式，newline='' 确保在 Windows 上不会出现额外的空行
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            # 写入表头
            writer.writeheader()

            # 写入每一行数据
            writer.writerows(data)
            
        print(f"\n🎉 数据保存成功！文件名为：{filename}，共 {len(data)} 条记录。")
    except Exception as e:
        print(f"\n❌ CSV 文件写入失败：{e}")


def main():
    all_articles = []
    
    print(f"=== 🚀 财富中文网爬虫开始执行（目标页数：{MAX_PAGES}） ===")

    # 1. 实现多页抓取循环
    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)
        
        if not list_items:
            print(f"--- 第 {page} 页没有抓到文章，停止翻页 ---")
            break
            
        all_articles.extend(list_items)
        time.sleep(1) 
    
    print(f"\n=== 📥 列表抓取完成，共收集到 {len(all_articles)} 篇文章链接。===")

    # 2. 遍历所有文章，抓取正文
    count = 0
    for item in all_articles:
        count += 1
        print(f"🔥 正在处理第 {count}/{len(all_articles)} 篇：{item['title']}")
        fetch_article_content(item)
        
    print("\n=== 🎯 正文抓取完成，预览前 5 篇文章：===")

    # 3. 打印前 5 篇文章，验证数据完整性 (省略，确保流程流畅)
    for item in all_articles[:5]:
        print("---")
        print(f"标题: {item['title']}")
        content_preview = item["content"][:200] + "..." if len(item["content"]) > 200 else item["content"]
        print(f"正文预览: {content_preview}")
        
    # 4. 统计失败文章数
    failed_count = sum(1 for item in all_articles if item["content"].startswith("[正文获取失败"))
    print(f"\n=== 统计结果：成功获取 {len(all_articles) - failed_count} 篇，失败 {failed_count} 篇。===")

    # 5. 【新增】保存为 CSV 文件
    save_to_csv(all_articles, OUTPUT_FILENAME)
    
    return all_articles


if __name__ == "__main__":
    main()
