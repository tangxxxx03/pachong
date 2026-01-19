# -*- coding: utf-8 -*-
"""
人社部网站 - 新闻中心 - 地方动态 列表抓取
需求：
- 周二~周五：抓取“前一天”的信息
- 周一：抓取“上周五”的信息
- 其他日期：默认不抓（你也可以改成抓上一个工作日）

输出：
- 控制台打印
- 同时写出 json 文件：mohrss_local_news_YYYY-MM-DD.json
"""

import re
import json
import sys
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE = "https://www.mohrss.gov.cn"
LIST_URL = "https://www.mohrss.gov.cn/SYrlzyhshbzb/rdzt/gzdt/"  # 你截图这类列表页的上层目录一般长这样
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def compute_target_date(now: datetime) -> str | None:
    """
    返回目标日期字符串：YYYY-MM-DD
    规则：
    - 周一：回到上周五（-3天）
    - 周二~周五：回到昨天（-1天）
    - 周六/周日：返回 None（不抓）
    """
    wd = now.weekday()  # Mon=0 ... Sun=6
    if wd == 0:  # Monday
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if 1 <= wd <= 4:  # Tue-Fri
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def fetch_html(url: str) -> str:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    # 适度容错（政府站偶尔慢）
    r = s.get(url, timeout=20)
    r.raise_for_status()
    # requests 通常能自动识别编码；如果乱码，可强制：
    # r.encoding = "utf-8"
    return r.text


def parse_list(html: str, page_url: str) -> list[dict]:
    """
    解析列表页，提取：
    - title
    - url
    - date (YYYY-MM-DD)

    你的截图里：
    - 标题是 a 标签（如 “黑龙江：多维发力牢牢稳住就业基本盘”）
    - 日期在 span.organMenuTxtLink 里（如 2026-01-16）
    """
    soup = BeautifulSoup(html, "html.parser")

    items = []

    # 1) 优先按“日期 span”来定位（最贴合你截图）
    date_spans = soup.select("span.organMenuTxtLink")
    for sp in date_spans:
        date_text = _norm(sp.get_text())
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            continue

        # 日期附近通常会有标题链接（在同一条目容器里）
        # 向上找一个比较“像条目”的容器
        container = sp
        for _ in range(6):
            if container is None:
                break
            # 容器里能找到 <a href="...html"> 且文字不为空，就当成条目
            a = container.find("a", href=True)
            if a and _norm(a.get_text()):
                title = _norm(a.get_text())
                href = a["href"].strip()
                full_url = urljoin(page_url, href)
                items.append({"date": date_text, "title": title, "url": full_url})
                break
            container = container.parent

    # 2) 如果上面没抓到（页面结构变了），做一个兜底：抓所有带日期模式的条目
    if not items:
        # 找所有 a，然后在它父容器里找日期
        for a in soup.find_all("a", href=True):
            title = _norm(a.get_text())
            if not title:
                continue
            href = a["href"].strip()
            if ".html" not in href:
                continue

            parent = a
            found_date = None
            for _ in range(6):
                if parent is None:
                    break
                text = _norm(parent.get_text(" "))
                m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
                if m:
                    found_date = m.group(1)
                    break
                parent = parent.parent

            if found_date:
                items.append({"date": found_date, "title": title, "url": urljoin(page_url, href)})

    # 去重（同一条可能被扫描到两次）
    seen = set()
    uniq = []
    for it in items:
        key = (it["date"], it["title"], it["url"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    # 按日期倒序、标题排序（可选）
    uniq.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    return uniq


def main():
    now = datetime.now()
    target = compute_target_date(now)

    if not target:
        print("今天是周末（或你没安排抓取的日子），按规则不抓取。")
        return

    print(f"目标日期：{target}")

    # 抓列表页
    html = fetch_html(LIST_URL)
    items = parse_list(html, LIST_URL)

    # 过滤目标日期
    hit = [x for x in items if x.get("date") == target]

    print(f"列表页共解析到 {len(items)} 条，命中目标日期 {len(hit)} 条。")
    for i, it in enumerate(hit, 1):
        print(f"{i}. [{it['date']}] {it['title']}\n   {it['url']}")

    # 输出文件
    out = {
        "source": "mohrss_local_news",
        "list_url": LIST_URL,
        "target_date": target,
        "count": len(hit),
        "items": hit,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path = f"mohrss_local_news_{target}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n已写出：{out_path}")

    # 如果你希望“目标日必须有数据，否则视为异常”，可以取消注释：
    # if len(hit) == 0:
    #     sys.exit(2)


if __name__ == "__main__":
    main()
