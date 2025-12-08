# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（PC 版结构）- V7 修正日期限定与终极修复版

功能：
1. 【核心修正】日期限定功能：改为抓取配置中指定的固定日期（默认为 2025-12-07）。
2. 使用动态、逼真的头部进行正文请求，解决 404 错误。
3. 支持多页列表抓取（默认前 3 页）。
4. 自动获取每篇文章的完整正文。
5. 将结果保存为 CSV 文件。
"""

import re
import time
import requests
import csv
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime

# --- 配置参数 ---
BASE = "https://www.fortunechina.com"
MAX_PAGES = 3  
MAX_RETRY = 3 
OUTPUT_FILENAME = "fortunechina_articles.csv"
# **【关键修正】**：将日期设定为固定的 2025-12-07
TARGET_DATE = "2025-12-07" 
# ----------------

# 列表页请求头部
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36" 
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def fetch_list(page=1):
    """
    抓取指定页码的文章列表，并限定日期为 TARGET_DATE。
    """
    url = f"{BASE}/shangye/" if page == 1 else f"{BASE}/shangye/?page={page}"
    print(f"\n--- 正在请求列表页: 第 {page} 页 ---")

    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
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
        
        if not (h2 and a and date_div):
            continue
            
        href = a["href"].strip()
        pub_date = date_div.get_text(strip=True) if date_div else ""

        # **【限定日期功能修正】**：只抓取发布日期等于 TARGET_DATE 的文章
        if pub_date != TARGET_DATE:
            continue
            
        # 严格校验链接格式：/YYYY-MM/DD/content_ID.htm
        match = re.search(r"(/(\d{4}-\d{2}/\d{2})/content_\d+\.htm)", href)
        if not match:
            continue
        
        # 尝试两种可能的正确 URL 格式
        url_full_standard = urljoin(BASE, href) 
        content_id_path = re.search(r"/content_\d+\.htm", href)
        url_full_alternate = urljoin(BASE, content_id_path.group(0)) if content_id_path else ""


        items.append({
            "title": h2.get_text(strip=True),
            "url_standard": url_full_standard,
            "url_alternate": url_full_alternate, 
            "url": url_full_standard,
            "date": pub_date,
            "content": "",
        })

    print(f"  ✅ 第 {page} 页抓到文章数：{len(items)} (限定日期: {TARGET_DATE})")
    return items


def fetch_article_content(item):
    """
    请求文章正文内容，并包含失败重试机制和 Referer 头部。
    """
    headers = DEFAULT_HEADERS.copy()
    headers["Referer"] = f"{BASE}/shangye/"
    headers["Sec-Fetch-Site"] = "same-origin" 
    headers["Sec-Fetch-Mode"] = "navigate"
    
    # 尝试访问的 URL 列表，先尝试标准 URL，失败后尝试备用 URL
    urls_to_try = [item["url_standard"], item["url_alternate"]]
    
    for current_url in urls_to_try:
        if not current_url:
            continue
            
        for attempt in range(MAX_RETRY):
            try:
                r = requests.get(current_url, headers=headers, timeout=15)
                r.raise_for_status() 

                soup = BeautifulSoup(r.text, "html.parser")
                container = soup.select_one("div.article-mod div.word-text-con")

                if not container:
                    item["content"] = "[正文内容容器未找到]"
                    return
                
                item["url"] = current_url
                paras = [p.get_text(strip=True) for p in container.find_all("p") if p.get_text(strip=True)]
                item["content"] = "\n".join(paras)
                time.sleep(0.5) 
                return

            except requests.exceptions.HTTPError as e:
                if r.status_code == 404:
                    print(f"  ⚠️ 404 链接无效，尝试下一个 URL 或重试：{current_url}")
                    break 
                
                print(f"  ❌ 正文请求失败，正在重试第 {attempt + 1}/{MAX_RETRY} 次 ({current_url}): {e}")
                time.sleep(2 ** attempt) 
            
            except requests.exceptions.RequestException as e:
                print(f"  ❌ 正文请求失败，正在重试第 {attempt + 1}/{MAX_RETRY} 次 ({current_url}): {e}")
                time.sleep(2 ** attempt) 
                
    item["content"] = f"[正文获取失败: 超过最大重试次数或所有 URL 404]"
    print(f"  ⛔️ 正文获取失败，所有尝试均失败：{item['title']}")


def save_to_csv(data: list, filename: str):
    """
    将文章数据列表保存到 CSV 文件中。
    """
    if not data:
        print("💡 没有数据可保存。")
        return
        
    fieldnames = ["title", "date", "url", "content"]
    
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
            
        print(f"\n🎉 数据保存成功！文件名为：{filename}，共 {len(data)} 条记录。")
    except Exception as e:
        print(f"\n❌ CSV 文件写入失败：{e}")


def main():
    all_articles = []
    
    print(f"=== 🚀 财富中文网爬虫开始执行（目标页数：{MAX_PAGES}，限定日期：{TARGET_DATE}） ===")

    # 1. 实现多页抓取循环
    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)
        
        if not list_items:
            if page == 1:
                print(f"--- 第 1 页未抓到任何 {TARGET_DATE} 发布的文章，停止 ---")
            else:
                print(f"--- 第 {page} 页没有抓到文章，停止翻页 ---")
            break
            
        all_articles.extend(list_items)
        time.sleep(1) 
    
    print(f"\n=== 📥 列表抓取完成，共收集到 {len(all_articles)} 篇符合日期的文章链接。===")

    # 2. 遍历所有文章，抓取正文
    count = 0
    for item in all_articles:
        count += 1
        print(f"🔥 正在处理第 {count}/{len(all_articles)} 篇：{item['title']}")
        fetch_article_content(item)
        
    # 3. 统计失败文章数
    failed_count = sum(1 for item in all_articles if item["content"].startswith("[正文获取失败"))
    print(f"\n=== 统计结果：成功获取 {len(all_articles) - failed_count} 篇，失败 {failed_count} 篇。===")

    # 4. 保存为 CSV 文件
    save_to_csv(all_articles, OUTPUT_FILENAME)
    
    return all_articles


if __name__ == "__main__":
    main()
