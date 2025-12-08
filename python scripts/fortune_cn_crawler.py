# -*- coding: utf-8 -*-
"""
财富中文网 商业频道爬虫（PC 版） + SiliconFlow AI 摘要 + 钉钉 Markdown 推送

功能：
1. 抓取财富中文网·商业频道指定日期的新闻（默认抓“北京时间昨天”的）。
2. 修复列表页 href 相对路径（c/2025-12/07/...）丢失 /shangye/ 的问题。
3. 调用 SiliconFlow（OpenAI 兼容接口）生成「一句话中文摘要」。
4. 导出 CSV（标题 + AI 摘要 + 日期 + URL + 正文）。
5. 生成 Markdown 列表（每条 [AI 摘要](URL)）。
6. 将 Markdown 内容通过钉钉机器人推送到群里（支持多机器人）。

依赖（requirements.txt）：
- requests
- beautifulsoup4
"""

import os
import re
import time
import csv
import hmac
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

# ============= 抓取基础配置 =============

BASE = "https://www.fortunechina.com"
LIST_URL_BASE = "https://www.fortunechina.com/shangye/"
MAX_PAGES = 1
MAX_RETRY = 3

OUTPUT_CSV = "fortunechina_articles_with_ai_title.csv"
OUTPUT_MD = "fortunechina_articles_with_ai_title.md"


def get_target_date() -> str:
    """
    决定要抓取的目标日期：
    1. 如果设置了环境变量 TARGET_DATE（例如 "2025-12-07"），优先用它；
    2. 否则默认抓「北京时间昨天」，格式 YYYY-MM-DD。
    """
    env_date = os.getenv("TARGET_DATE", "").strip()
    if env_date:
        return env_date

    tz_cn = timezone(timedelta(hours=8))
    yesterday_cn = (datetime.now(tz_cn) - timedelta(days=1)).strftime("%Y-%m-%d")
    return yesterday_cn


TARGET_DATE = get_target_date()

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

# ============= SiliconFlow AI 配置 =============

# 你的 sk- 开头的 Key（从 GitHub Secrets 的 OPENAI_API_KEY 传进来）
AI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# 商家给的基础地址：https://api.siliconflow.cn/v1
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.siliconflow.cn/v1").rstrip("/")

# ChatCompletions 完整 URL
AI_CHAT_URL = f"{AI_API_BASE}/chat/completions"

# 模型：如果你在商家后台看到别的，就填完整模型名到 Secrets 的 AI_MODEL
AI_MODEL = os.getenv("AI_MODEL", "Qwen/Qwen2.5-7B-Instruct")


def get_ai_summary(content: str, fallback_title: str = "") -> str:
    """
    使用 SiliconFlow 生成一句话摘要。
    - content: 文章正文
    - fallback_title: AI 失败时用原始标题兜底
    """
    if not content or len(content) < 30:
        return fallback_title or "内容过短，无需摘要"

    if not AI_API_KEY:
        print("  ⚠️ 未配置 OPENAI_API_KEY，跳过 AI 摘要。")
        return fallback_title or "（未配置 AI 摘要）"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个严谨的中文新闻编辑，请将新闻正文提炼成一句中文摘要，"
                    "要求：客观、务实、不标题党，长度控制在 25 个字以内。"
                ),
            },
            {
                "role": "user",
                "content": content[:2000],
            },
        ],
        "max_tokens": 120,
        "temperature": 0.3,
    }

    print(f"  🤖 正在调用 AI（{AI_CHAT_URL}，模型={AI_MODEL}）生成摘要...")

    try:
        resp = requests.post(AI_CHAT_URL, headers=headers, json=payload, timeout=30)

        if resp.status_code != 200:
            print(f"  ❌ AI 状态码：{resp.status_code}")
            try:
                print("  ❌ AI 返回内容：", resp.text)
            except Exception:
                pass
            resp.raise_for_status()

        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
        summary = summary.splitlines()[0].strip()
        print(f"  ✨ AI 摘要：{summary}")
        return summary or (fallback_title or "（AI 摘要为空）")

    except Exception as e:
        print(f"  ⚠️ AI 调用失败：{e}")
        return fallback_title or f"[AI 调用失败: {e}]"


# ============= 列表页抓取 =============


def fetch_list(page: int = 1):
    """
    抓取指定页码的文章列表（使用 current_list_url 修复相对路径）。
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

    for li in soup.select("ul.news-list li.news-item"):
        h2 = li.find("h2")
        a = li.find("a", href=True)
        date_div = li.find("div", class_="date")

        if not (h2 and a and date_div):
            continue

        href = a["href"].strip()
        pub_date = date_div.get_text(strip=True) if date_div else ""

        # 只要目标日期的
        if pub_date != TARGET_DATE:
            continue

        # 只要包含 content_数字 的链接
        if not re.search(r"content_\d+\.htm", href):
            continue

        url_full = urljoin(current_list_url, href)

        items.append(
            {
                "title": h2.get_text(strip=True),
                "url": url_full,
                "date": pub_date,
                "content": "",
                "ai_summary": "",
            }
        )

    print(f"  ✅ 第 {page} 页抓到目标日期({TARGET_DATE})文章数：{len(items)}")
    return items


# ============= 正文抓取 =============


def fetch_article_content(item: dict):
    """
    请求文章正文内容
    """
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


# ============= 保存 CSV =============


def save_to_csv(data: list, filename: str):
    if not data:
        print("💡 没有数据可保存。")
        return

    fieldnames = ["title", "ai_summary", "date", "url", "content"]
    try:
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        print(f"\n🎉 成功保存到 CSV：{filename}，共 {len(data)} 条。")
    except Exception as e:
        print(f"\n❌ CSV 保存失败：{e}")


# ============= 生成 Markdown =============


def build_markdown(items: list) -> str:
    """
    生成适合钉钉发送的 Markdown 文本。
    """
    if not items:
        return f"### 财富中文网·商业频道精选（{TARGET_DATE}）\n\n今日未抓到符合条件的新闻。"

    lines = [
        f"### 财富中文网·商业频道精选（{TARGET_DATE}）",
        "",
    ]

    for idx, item in enumerate(items, start=1):
        title = item.get("ai_summary") or item.get("title") or "（无标题）"
        url = item.get("url", "")
        lines.append(f"{idx}. [{title}]({url})")

    return "\n".join(lines)


def save_markdown(content: str, filename: str):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n📄 已保存 Markdown 文件：{filename}")
    except Exception as e:
        print(f"\n❌ Markdown 保存失败：{e}")


# ============= 钉钉 Markdown 推送 =============


def sign_dingtalk(secret: str, timestamp_ms: int) -> str:
    """
    按钉钉官方文档生成签名。
    """
    string_to_sign = f"{timestamp_ms}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return quote_plus(base64.b64encode(hmac_code))


def send_dingtalk_markdown(title: str, text: str):
    """
    将 Markdown 文本发送到一个或多个钉钉机器人。
    需要环境变量：
    - DINGTALK_BASES   : webhook 基础 URL，多个用英文逗号分隔
    - DINGTALK_SECRETS : 对应的 secret，多个用英文逗号分隔
    """
    bases_raw = os.getenv("DINGTALK_BASES", "").strip()
    secrets_raw = os.getenv("DINGTALK_SECRETS", "").strip()

    if not bases_raw or not secrets_raw:
        print("💡 未配置 DINGTALK_BASES / DINGTALK_SECRETS，跳过钉钉推送。")
        return

    bases = [b.strip() for b in bases_raw.split(",") if b.strip()]
    secrets = [s.strip() for s in secrets_raw.split(",") if s.strip()]

    if not bases or len(bases) != len(secrets):
        print("⚠️ DINGTALK_BASES 与 DINGTALK_SECRETS 数量不一致，跳过钉钉推送。")
        return

    for idx, (base_url, secret) in enumerate(zip(bases, secrets), start=1):
        try:
            ts = int(time.time() * 1000)
            sign = sign_dingtalk(secret, ts)
            full_url = f"{base_url}&timestamp={ts}&sign={sign}"

            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": text,
                },
                "at": {
                    "isAtAll": False,
                },
            }

            print(f"\n📨 正在向第 {idx} 个钉钉机器人发送消息...")
            resp = requests.post(full_url, json=payload, timeout=10)
            print(f"  钉钉返回状态码：{resp.status_code}")
            try:
                print("  钉钉返回：", resp.text)
            except Exception:
                pass

        except Exception as e:
            print(f"  ⚠️ 第 {idx} 个钉钉机器人发送失败：{e}")


# ============= 主流程 =============


def main():
    all_articles = []
    print(f"=== 🚀 爬虫启动 (目标日期: {TARGET_DATE}) ===")
    print(f"=== 🛠️ 路径策略: 基于列表页 URL ({LIST_URL_BASE}) 进行相对路径拼接 ===")

    # 1. 抓取列表
    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)
        if not list_items:
            if page == 1:
                print(
                    f"⚠️ 第 1 页未找到 {TARGET_DATE} 的文章，请确认网站上确实有该日期的内容。"
                )
            break
        all_articles.extend(list_items)
        time.sleep(1)

    print(
        f"\n=== 📥 链接收集完成，共 {len(all_articles)} 篇。开始抓取正文 + 生成 AI 摘要... ==="
    )

    # 2. 抓取正文 + AI 摘要
    count = 0
    for item in all_articles:
        count += 1
        print(f"\n🔥 ({count}/{len(all_articles)}) 处理: {item['title']}")
        fetch_article_content(item)
        item["ai_summary"] = get_ai_summary(item["content"], item["title"])

    # 3. 统计与保存 CSV
    success_count = sum(
        1
        for item in all_articles
        if "获取失败" not in item["content"] and item["content"]
    )
    print(f"\n=== 统计: 成功 {success_count} 篇，失败 {len(all_articles) - success_count} 篇 ===")
    save_to_csv(all_articles, OUTPUT_CSV)

    # 4. 生成 Markdown
    md_content = build_markdown(all_articles)
    print("\n=== Markdown 预览（可用于钉钉 Markdown 消息） ===\n")
    print(md_content)
    save_markdown(md_content, OUTPUT_MD)

    # 5. 推送到钉钉
    md_title = f"财富中文网·商业频道精选（{TARGET_DATE}）"
    send_dingtalk_markdown(md_title, md_content)


if __name__ == "__main__":
    main()
