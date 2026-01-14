# -*- coding: utf-8 -*-
"""
新浪财经 - 上市公司研究院
只抓【前一天】新闻标题 + 链接（可点击）
页面：https://finance.sina.com.cn/roll/c/221431.shtml

设计原则：
- 不登录
- 不爬正文
- 不碰 API
- 低频翻页
- 只做索引级抓取（合规、安全）
"""

import os
import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


# ================= 基础配置 =================
START_URL = "https://finance.sina.com.cn/roll/c/221431.shtml"
MAX_PAGES = 5          # 最多翻 5 页
SLEEP_SEC = 0.8        # 翻页间隔
OUT_FILE = "sina_yesterday.md"

TZ = ZoneInfo("Asia/Shanghai")
DATE_RE = re.compile(r"\((\d{2})月(\d{2})日\s*(\d{2}):(\d{2})\)")


# ================= 工具函数 =================
def now_cn():
    return datetime.now(TZ)


def get_html(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def parse_datetime(text):
    """
    从 '(01月13日 15:27)' 解析时间
    """
    m = DATE_RE.search(text)
    if not m:
        return None

    month, day, hh, mm = map(int, m.groups())
    now = now_cn()
    year = now.year

    # 跨年兜底
    if now.month == 1 and month == 12:
        year -= 1

    try:
        return datetime(year, month, day, hh, mm, tzinfo=TZ)
    except Exception:
        return None


def find_next_page(soup):
    a = soup.find("a", string=lambda s: s and "下一页" in s)
    if a and a.get("href"):
        return urljoin(START_URL, a["href"])
    return None


# ================= 主逻辑 =================
def main():
    yesterday = (now_cn() - timedelta(days=1)).date()

    results = []
    url = START_URL
    hit_yesterday = False

    for page in range(1, MAX_PAGES + 1):
        html = get_html(url)
        soup = BeautifulSoup(html, "html.parser")

        ul = soup.select_one("ul.listcontent.list_009")
        if not ul:
            print("❌ 未找到列表结构，页面可能改版")
            break

        for li in ul.select("li"):
            a = li.find("a", href=True)
            if not a:
                continue

            title = a.get_text(strip=True)
            link = urljoin(START_URL, a["href"])
            text = li.get_text(" ", strip=True)

            dt = parse_datetime(text)
            if not dt:
                continue

            if dt.date() == yesterday:
                results.append((dt, title, link))
                hit_yesterday = True

        # 如果已经抓到昨天内容，且本页全是更早的，可以停
        if hit_yesterday:
            dts = [
                parse_datetime(li.get_text(" ", strip=True))
                for li in ul.select("li")
            ]
            dts = [d for d in dts if d]
            if dts and all(d.date() < yesterday for d in dts):
                break

        next_url = find_next_page(soup)
        if not next_url:
            break

        url = next_url
        time.sleep(SLEEP_SEC)

    # 排序（按时间倒序）
    results.sort(key=lambda x: x[0], reverse=True)

    # 输出 Markdown
    lines = []
    lines.append(f"# 新浪财经 · 昨日更新（{yesterday}）\n")

    if not results:
        lines.append("（昨日无更新或页面结构变化）")
    else:
        for dt, title, link in results:
            lines.append(f"- [{title}]({link})  {dt.strftime('%H:%M')}")

    lines.append(f"\n_生成时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}_")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✅ 抓取完成，共 {len(results)} 条，已写入 {OUT_FILE}")


if __name__ == "__main__":
    main()
