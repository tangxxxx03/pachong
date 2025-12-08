# -*- coding: utf-8 -*-
"""
财富中文网爬虫 V10 - 摘要总结版

更新内容：
1. 【核心修正】调整 AI Prompt：要求生成一句客观、完整的陈述句，用于总结新闻核心内容，帮助省略阅读过程。
2. 其他代码逻辑（抓取、路径修复、日期限定）保持不变。
"""

import re
import time
import requests
import csv
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
from openai import OpenAI

# --- 配置参数 ---
BASE = "https://www.fortunechina.com"
LIST_URL_BASE = "https://www.fortunechina.com/shangye/" 
MAX_PAGES = 3
MAX_RETRY = 3
OUTPUT_FILENAME = "fortunechina_ai_summary_v10.csv"
TARGET_DATE = "2025-12-07" 

# --- AI 配置 (已填入你的 Key) ---
API_KEY = "sk-lTg1L3LAYY1rGfWH21QgK7bkCoe4SIQZJIYiW0c9W2Gg4Zlq"
API_BASE_URL = None  
AI_MODEL = "gpt-3.5-turbo" 

client = OpenAI(
    api_key=API_KEY,
    base_url=API_BASE_URL
)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}

def get_ai_summary(content):
    """
    调用 AI 接口生成完整内容总结（一句话陈述句）
    """
    if not content or len(content) < 50:
        return "内容太短，无法概括"

    print("  🤖 AI 正在生成总结...")
    try:
        # **【核心修改】**：提示词调整为要求生成客观、完整的陈述句总结
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "你是一个严谨的商务分析师，负责将长篇新闻快速提炼。"},
                {"role": "user", "content": f"请阅读以下新闻正文，将其核心内容提炼概括为**一句完整的陈述句总结**，用于内部沟通，要求客观、信息完整、不超过50个字：\n\n{content[:2000]}"}
            ],
            temperature=0.3, # 降低温度，获取更客观的输出
            max_tokens=150 # 适当增加最大 token，确保句子完整
        )
        summary = response.choices[0].message.content.strip()
        print(f"  ✨ AI 总结: {summary}")
        return summary
    except Exception as e:
        print(f"  ⚠️ AI 接口调用失败: {e}")
        return f"[AI 概括失败: {e}]"

# --- 列表抓取函数 (fetch_list) ---
def fetch_list(page=1):
    if page == 1:
        current_list_url = LIST_URL_BASE
    else:
        current_list_url = f"{LIST_URL_BASE}?page={page}"
        
    print(f"\n--- 正在请求列表页: 第 {page} 页 ({current_list_url}) ---")

    try:
        r = requests.get(current_list_url, headers=DEFAULT_HEADERS, timeout=15)
        r.raise_for_status() 
    except requests.exceptions.RequestException as e:
        print(f"⚠️ 列表页请求失败: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    
    for li in soup.select("ul.news-list li.news-item"):
        h2 = li.find("h2")
        a = li.find("a", href=True)
        date_div = li.find("div", class_="date")
        
        if not (h2 and a and date_div): continue
            
        href = a["href"].strip()
        pub_date = date_div.get_text(strip=True) if date_div else ""

        if pub_date != TARGET_DATE: continue
        if not re.search(r"content_\d+\.htm", href): continue
        
        url_full = urljoin(current_list_url, href)
        items.append({
            "title": h2.get_text(strip=True),
            "url": url_full,
            "date": pub_date,
            "content": "",
            "ai_summary": "" 
        })

    print(f"  ✅ 第 {page} 页抓到目标日期({TARGET_DATE})文章数：{len(items)}")
    return items

# --- 正文抓取函数 (fetch_article_content) ---
def fetch_article_content(item):
    url = item["url"]
    headers = DEFAULT_HEADERS.copy()
    headers["Referer"] = LIST_URL_BASE 
    
    for attempt in range(MAX_RETRY):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status() 
            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.select_one("div.article-mod div.word-text-con")
            if not container:
                container = soup.select_one("div.article-content")

            if not container:
                item["content"] = "[正文容器未找到]"
                print(f"  ⚠️ 未找到正文: {url}")
                return

            paras = [p.get_text(strip=True) for p in container.find_all("p") if p.get_text(strip=True)]
            full_text = "\n".join(paras)
            item["content"] = full_text
            
            if full_text:
                item["ai_summary"] = get_ai_summary(full_text)
            
            time.sleep(0.5) 
            return

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRY - 1:
                time.sleep(1)
            else:
                print(f"  ⛔️ 最终失败: {url} | 错误: {e}")
                item["content"] = f"[获取失败: {e}]"

# --- CSV 保存函数 (save_to_csv) ---
def save_to_csv(data: list, filename: str):
    if not data: return
    fieldnames = ["title", "date", "url", "ai_summary", "content"]
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        print(f"\n🎉 成功保存到 CSV：{filename}，包含 AI 总结数据！")
    except Exception as e:
        print(f"\n❌ CSV 保存失败：{e}")

# --- 主函数 (main) ---
def main():
    all_articles = []
    print(f"=== 🚀 爬虫启动 + AI 总结 (目标日期: {TARGET_DATE}) ===")

    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)
        if not list_items: 
            if page == 1:
                print(f"⚠️ 第 1 页未找到 {TARGET_DATE} 的文章。")
            break
        all_articles.extend(list_items)
        time.sleep(1) 
    
    print(f"\n=== 📥 链接收集完成，共 {len(all_articles)} 篇。开始抓取正文并生成总结... ===")

    count = 0
    for item in all_articles:
        count += 1
        print(f"🔥 ({count}/{len(all_articles)}) 处理: {item['title']}")
        fetch_article_content(item)
        
    save_to_csv(all_articles, OUTPUT_FILENAME)

if __name__ == "__main__":
    main()
