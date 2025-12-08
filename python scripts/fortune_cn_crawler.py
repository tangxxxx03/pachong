# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（PC 版结构）- V8 + AI 标题版

功能：
1. 列表页：支持多页抓取（默认前 3 页），基于列表页 URL 做相对路径拼接，避免丢失 /shangye/ 目录。
2. 日期限定：只抓取指定日期（默认 2025-12-07）的文章。
3. 正文抓取：带 Referer、模拟真实浏览器头，支持简单重试。
4. AI 概括：用大模型根据正文生成一句「内部用」标题，准确概括内容，避免标题党。
5. 输出：保存为 CSV，字段包括：原始标题、AI 标题、日期、URL、正文。
"""

import os
import re
import time
import csv
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# ================== 基本配置 ==================
BASE = "https://www.fortunechina.com"

# 列表页基准 URL（用于 urljoin，务必以 / 结尾）
LIST_URL_BASE = "https://www.fortunechina.com/shangye/"

# 最大翻页数
MAX_PAGES = 3

# 正文请求最大重试次数
MAX_RETRY = 3

# 输出文件名
OUTPUT_FILENAME = "fortunechina_articles_with_ai_title.csv"

# 目标日期（只抓这一日的文章）
TARGET_DATE = "2025-12-07"   # 格式：YYYY-MM-DD

# ================== AI 接口配置 ==================
# 建议在系统环境变量里配置：OPENAI_API_KEY
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# 这里以 OpenAI 兼容接口为例，你可以改成自己的网关地址
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4.1-mini"  # 或你自己的模型名称

# ================== HTTP 头 ==================
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}


# ================== 列表抓取 ==================
def fetch_list(page: int):
    """
    抓取指定页码的文章列表，限定日期为 TARGET_DATE，并用列表页 URL 做相对路径拼接。
    返回：[{title, url, date, content, ai_title}, ...]
    """
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

    # 这里根据实际结构选一个尽量稳的选择器
    # 财富中文网商业频道列表大致结构：div.list-mod / div.mod-list / li
    # 如有偏差，你可以对照页面调一下选择器
    for box in soup.select("div.list-mod li, div.mod-list li, div.list-item"):
        # 尝试取标题和链接
        h2 = box.find("h2") or box.find("h3")
        a = h2.find("a") if h2 else box.find("a")
        date_div = box.find("span", class_="time") or box.find("div", class_="time") or box.find("span", class_="date")

        if not (h2 and a and date_div):
            continue

        href = a.get("href", "").strip()
        pub_date_raw = date_div.get_text(strip=True)

        # 有的站会带时间，比如 "2025-12-07 10:23"
        # 用 startswith，宽松匹配同一天
        if not pub_date_raw.startswith(TARGET_DATE):
            continue

        # href 中必须含有 content_xxx.htm 才当成文章链接
        if not re.search(r"content_\d+\.htm", href):
            continue

        # 【关键】基于当前列表页 URL 拼接，保证保留 /shangye/
        url_full = urljoin(current_list_url, href)

        items.append({
            "title": h2.get_text(strip=True),
            "url": url_full,
            "date": TARGET_DATE,
            "content": "",
            "ai_title": "",  # 预留字段，稍后填 AI 概括标题
        })

    print(f"  ✅ 第 {page} 页抓到目标日期({TARGET_DATE})文章数：{len(items)}")
    return items


# ================== 正文抓取 ==================
def fetch_article_content(item: dict):
    """
    请求文章正文内容，带简单重试。
    成功后写入 item["content"]。
    """
    url = item["url"]
    headers = DEFAULT_HEADERS.copy()

    # 模拟从列表页点击进入
    headers["Referer"] = LIST_URL_BASE
    headers["Sec-Fetch-Site"] = "same-origin"
    headers["Sec-Fetch-Mode"] = "navigate"

    for attempt in range(MAX_RETRY):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")

            # 主选择器
            container = soup.select_one("div.article-mod div.word-text-con")
            # 备用选择器
            if not container:
                container = soup.select_one("div.article-content")

            if not container:
                item["content"] = "[正文容器未找到]"
                print(f"  ⚠️ 警告：URL {url} 访问成功但未找到正文容器")
                return

            paras = [
                p.get_text(strip=True)
                for p in container.find_all("p")
                if p.get_text(strip=True)
            ]
            item["content"] = "\n".join(paras)
            time.sleep(0.5)
            return

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRY - 1:
                print(f"  ❌ 正文请求失败，重试 {attempt + 1}/{MAX_RETRY} ... -> {url} | 错误: {e}")
                time.sleep(1)
            else:
                print(f"  ⛔️ 最终失败: {url} | 错误: {e}")
                item["content"] = f"[获取失败: {e}]"


# ================== AI 概括标题 ==================
def ai_summarize_title(content: str, fallback_title: str) -> str:
    """
    用大模型把正文概括成一句标题。
    要求：中文、准确、非标题党，控制在 25 字以内。
    如果调用失败，则返回原始标题。
    """
    if not OPENAI_API_KEY:
        print("⚠️ 未配置 OPENAI_API_KEY，使用原始标题。")
        return fallback_title

    # 文本太长会贵也会超 token，这里截一段上下文就够概括了
    if not content or content.startswith("[获取失败"):
        # 没正文就没法概括，只能用原标题
        return fallback_title

    snippet = content[:2000]

    prompt = (
        "你是一个新闻编辑，请根据下面的文章内容，用中文写一个不超过 25 个字的新闻标题，"
        "要求：准确概括核心信息，避免夸张和标题党，不要加引号：\n\n"
        f"{snippet}"
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "你是一名严谨的中文新闻编辑，只输出标题文本。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 64,
    }

    try:
        resp = requests.post(OPENAI_API_URL, headers=headers, data=json.dumps(payload), timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # 兼容 chat/completions 结构
        ai_title = data["choices"][0]["message"]["content"].strip()
        # 防御：有些模型会输出多行说明，这里取首行
        ai_title = ai_title.splitlines()[0].strip()

        # 极端情况下模型返回空，就回退
        return ai_title or fallback_title

    except Exception as e:
        print(f"⚠️ AI 概括失败，使用原始标题。错误: {e}")
        return fallback_title


# ================== 保存 CSV ==================
def save_to_csv(data: list, filename: str):
    """
    将文章数据列表保存到 CSV 文件中。
    """
    if not data:
        print("💡 没有数据可保存。")
        return

    fieldnames = ["title", "ai_title", "date", "url", "content"]

    try:
        with open(filename, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)

        print(f"\n🎉 成功保存到 CSV：{filename}，共 {len(data)} 条。")
    except Exception as e:
        print(f"\n❌ CSV 保存失败：{e}")


# ================== 主流程 ==================
def main():
    all_articles = []

    print(f"=== 🚀 财富中文网爬虫启动 (目标日期: {TARGET_DATE}) ===")
    print(f"=== 🛠️ 路径策略: 基于列表页 URL ({LIST_URL_BASE}) 进行相对路径拼接 ===")

    # 1. 抓取列表
    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)

        if not list_items:
            if page == 1:
                print(f"--- 第 1 页未抓到任何 {TARGET_DATE} 发布的文章，停止 ---")
                print(f"⚠️ 请确认网站上是否存在 {TARGET_DATE} 的内容。")
            else:
                print(f"--- 第 {page} 页没有抓到文章，停止翻页 ---")
            break

        all_articles.extend(list_items)
        time.sleep(1)

    print(f"\n=== 📥 链接收集完成，共 {len(all_articles)} 篇。开始抓取正文... ===")

    # 2. 抓取正文并用 AI 概括标题
    for idx, item in enumerate(all_articles, start=1):
        print(f"\n🔥 ({idx}/{len(all_articles)}) 处理：{item['title']}")
        fetch_article_content(item)

        # AI 概括标题
        item["ai_title"] = ai_summarize_title(item["content"], item["title"])
        print(f"   🧠 AI 标题：{item['ai_title']}")

    # 3. 统计 + 保存
    success_count = sum(
        1
        for item in all_articles
        if item["content"] and not item["content"].startswith("[获取失败")
    )
    print(f"\n=== 统计: 正文成功 {success_count} 篇，失败 {len(all_articles) - success_count} 篇 ===")

    save_to_csv(all_articles, OUTPUT_FILENAME)

    return all_articles


if __name__ == "__main__":
    main()
