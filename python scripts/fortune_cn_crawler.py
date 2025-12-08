# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（PC 版结构）- V8 + AI 标题 & Markdown 版

在 V8「路径拼接终极修正版」基础上新增：
1. 调用 OpenAI 接口，为每篇文章生成一句客观的中文标题（非标题党）；
2. 输出 CSV 时增加 ai_title 字段；
3. 生成 Markdown 文本，每一行形如 `[AI 标题](URL)`，可直接用于钉钉 Markdown 消息，标题可点击查看详情。

注意：
- OpenAI Key 不再写死在代码里，而是从环境变量 OPENAI_API_KEY 读取，方便在 GitHub Secrets 里配置；
- 仍然使用固定 TARGET_DATE（例如 "2025-12-07"），你可以手动修改，或之后再改成自动「昨天」。
"""

import os
import re
import time
import csv
import json
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- 配置参数 ---
BASE = "https://www.fortunechina.com"
# 确保列表页 URL 结尾有斜杠，这对 urljoin 处理相对路径非常重要
LIST_URL_BASE = "https://www.fortunechina.com/shangye/"
MAX_PAGES = 3
MAX_RETRY = 3
OUTPUT_FILENAME = "fortunechina_articles_with_ai_title.csv"
OUTPUT_MD = "fortunechina_articles_with_ai_title.md"

# 限定日期 (根据你的截图，目标是 2025-12-07)
# 你可以手动改成想抓的那一天，比如 "2025-12-08"
TARGET_DATE = "2025-12-07"
# ----------------

# --- OpenAI 配置（从环境变量读取 Key，适配 GitHub Secrets） ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = "gpt-4.1-mini"  # 你可以按需改成其他模型
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}


# === AI 标题生成 ===
def ai_summarize_title(content: str, fallback_title: str) -> str:
    """
    调用 OpenAI，把正文内容概括成一句「内部用标题」：
    - 中文
    - 非标题党，客观准确
    - 不超过 25 个字
    如果无法调用，则返回 fallback_title（原始标题）
    """
    if not OPENAI_API_KEY:
        print("  ⚠️ 未配置 OPENAI_API_KEY，使用原始标题。")
        return fallback_title

    if not content or content.startswith("[获取失败"):
        return fallback_title

    snippet = content[:2000]

    prompt = (
        "你是一名严谨的中文新闻编辑，请根据下面的新闻正文，"
        "写出一句不超过 25 个字的中文新闻标题，用于公司内部阅读：\n"
        "要求：客观准确、非标题党、不要加引号，只输出标题本身。\n\n"
        f"{snippet}"
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是一名严谨的中文新闻编辑，只输出新闻标题文本。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 64,
    }

    try:
        resp = requests.post(
            OPENAI_API_URL, headers=headers, data=json.dumps(payload), timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        ai_title = data["choices"][0]["message"]["content"].strip()
        # 只取第一行，防止模型顺带解释
        ai_title = ai_title.splitlines()[0].strip()
        if not ai_title:
            return fallback_title
        print(f"  🧠 AI 标题：{ai_title}")
        return ai_title
    except Exception as e:
        print(f"  ⚠️ AI 调用失败，使用原始标题。错误: {e}")
        return fallback_title


def fetch_list(page=1):
    """
    抓取指定页码的文章列表，使用正确的相对路径拼接。
    """
    # 构造当前列表页的完整 URL
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

        if not (h2 and a and date_div):
            continue

        # 获取原始 href (例如: "c/2025-12/07/content_470761.htm")
        href = a["href"].strip()
        pub_date = date_div.get_text(strip=True) if date_div else ""

        # 1. 日期过滤：只处理 TARGET_DATE
        if pub_date != TARGET_DATE:
            continue

        # 2. 简单的正则检查，只要包含 content_数字 即可
        if not re.search(r"content_\d+\.htm", href):
            continue

        # 3. 【核心修正】使用 current_list_url 进行拼接
        # 如果 href 是 "c/2025..."，list_url 是 ".../shangye/"
        # 结果自动变为 ".../shangye/c/2025..."
        url_full = urljoin(current_list_url, href)

        items.append(
            {
                "title": h2.get_text(strip=True),
                "url": url_full,
                "date": pub_date,
                "content": "",
                "ai_title": "",
            }
        )

    print(f"  ✅ 第 {page} 页抓到目标日期({TARGET_DATE})文章数：{len(items)}")
    return items


def fetch_article_content(item):
    """
    请求文章正文内容
    """
    url = item["url"]
    headers = DEFAULT_HEADERS.copy()
    # 加上 Referer，模拟从列表页点过去
    headers["Referer"] = LIST_URL_BASE

    for attempt in range(MAX_RETRY):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            # 尝试多种正文选择器，以防页面结构微调
            container = soup.select_one("div.article-mod div.word-text-con")
            if not container:
                container = soup.select_one("div.article-content")  # 备用选择器

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
                print(
                    f"  ❌ 请求失败 ({r.status_code if 'r' in locals() else 'Error'}), 重试中...: {url}"
                )
                time.sleep(1)
            else:
                print(f"  ⛔️ 最终失败: {url} | 错误: {e}")
                item["content"] = f"[获取失败: {e}]"


def save_to_csv(data: list, filename: str):
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


# === 生成 Markdown，可用于钉钉 Markdown 消息 ===
def build_markdown(items: list) -> str:
    """
    生成一个 Markdown 字符串：
    - 顶部是标题
    - 每一行都是：1. [AI 标题](URL)
    """
    if not items:
        return f"### 财富中文网·商业频道精选（{TARGET_DATE}）\n\n今日未抓到符合条件的新闻。"

    lines = [f"### 财富中文网·商业频道精选（{TARGET_DATE}）", ""]

    for idx, item in enumerate(items, start=1):
        title = item.get("ai_title") or item.get("title") or "（无标题）"
        url = item.get("url", "")
        lines.append(f"{idx}. [{title}]({url})")

    return "\n".join(lines)


def save_markdown(content: str, filename: str):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n📄 已保存 Markdown 到文件：{filename}")
    except Exception as e:
        print(f"\n❌ Markdown 文件保存失败：{e}")


def main():
    all_articles = []
    print(f"=== 🚀 爬虫启动 (目标日期: {TARGET_DATE}) ===")
    print(f"=== 🛠️ 修复策略: 基于列表页 URL ({LIST_URL_BASE}) 进行相对路径拼接 ===")

    # 1. 抓取列表
    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)
        if not list_items:
            # 如果第一页就没数据，可能是日期不对，或者没加载出来
            if page == 1:
                print(
                    f"⚠️ 第 1 页未找到 {TARGET_DATE} 的文章，请确认网站上确实有该日期的内容。"
                )
            break
        all_articles.extend(list_items)
        time.sleep(1)

    print(f"\n=== 📥 链接收集完成，共 {len(all_articles)} 篇。开始抓取正文 + 生成 AI 标题... ===")

    # 2. 抓取正文 + 生成 AI 标题
    count = 0
    for item in all_articles:
        count += 1
        print(f"\n🔥 ({count}/{len(all_articles)}) 处理: {item['title']}")
        fetch_article_content(item)
        item["ai_title"] = ai_summarize_title(item["content"], item["title"])

    # 3. 统计与保存 CSV
    success_count = sum(
        1
        for item in all_articles
        if "获取失败" not in item["content"] and item["content"]
    )
    print(f"\n=== 统计: 成功 {success_count} 篇，失败 {len(all_articles) - success_count} 篇 ===")
    save_to_csv(all_articles, OUTPUT_FILENAME)

    # 4. 生成 Markdown 预览 & 保存
    md_content = build_markdown(all_articles)
    print("\n=== Markdown 预览（可用于钉钉 Markdown 消息） ===\n")
    print(md_content)
    save_markdown(md_content, OUTPUT_MD)


if __name__ == "__main__":
    main()
