# -*- coding: utf-8 -*-
"""
mohrss_search_24h.py
针对 https://www.mohrss.gov.cn/hsearch/ 的站内搜索抓取（仅保留最近 N 小时，默认 24h）

用法示例：
  python mohrss_search_24h.py --q "人力资源" --pages 3 --window-hours 24 --save both

说明：
- 脚本会请求 /hsearch/ 页面（GET），并尝试解析列表条目中的标题/链接/时间/摘要。
- 只保留发布时间在当前 Asia/Shanghai 时区内最近 window-hours 小时的条目。
- 若站点结构变化，解析器尽量通过多种选择器和正则宽容匹配。
"""

import re
import time
import json
import csv
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Python3.9+
from urllib.parse import urljoin, urlencode
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

BASE = "https://www.mohrss.gov.cn"
SEARCH_PATH = "/hsearch/"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/123.0.0.0 Safari/537.36")

DATE_PATTERNS = [
    r"(20\d{2})[^\d](\d{1,2})[^\d](\d{1,2})\s*(\d{1,2}):(\d{1,2})",  # 2025-09-16 08:30 or 2025/09/16 08:30
    r"(20\d{2})[^\d](\d{1,2})[^\d](\d{1,2})",                         # 2025-09-16
    r"(\d{1,2})[^\d](\d{1,2})\s*(\d{1,2}):(\d{1,2})",                # 09-16 08:30
]

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    retries = Retry(total=3, backoff_factor=0.6, status_forcelist=(429,500,502,503,504), allowed_methods=frozenset(["GET","POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

def parse_date_text(txt, tz):
    """尝试从文本解析出带时区的 datetime；若失败返回 None"""
    if not txt:
        return None
    t = txt.strip()
    # 先常见格式尝试
    for pat in DATE_PATTERNS:
        m = re.search(pat, t)
        if m:
            if len(m.groups()) == 5:
                y, mo, d, hh, mm = m.groups()
                try:
                    return datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=tz)
                except Exception:
                    continue
            if len(m.groups()) == 3:
                y, mo, d = m.groups()
                try:
                    return datetime(int(y), int(mo), int(d), 0, 0, tzinfo=tz)
                except Exception:
                    continue
            if len(m.groups()) == 4:
                mo, d, hh, mm = m.groups()
                try:
                    now = datetime.now(tz)
                    return datetime(now.year, int(mo), int(d), int(hh), int(mm), tzinfo=tz)
                except Exception:
                    continue
    # 常见相对时间
    if re.search(r"刚刚|分钟|小时前|今天|今日", t):
        return datetime.now(tz)
    # 兜底：返回 None
    return None

def extract_items_from_search_html(html, base_url, tz):
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 多策略：常见结果区选择器
    candidates = []
    # 常见列表 li
    for sel in ["ul.search-list li", "div.search-list li", "div.list li", "ul li", "div.result", "div.row"]:
        found = soup.select(sel)
        if found:
            candidates = found
            break
    if not candidates:
        # 兜底：所有 a 元素
        candidates = soup.select("a")

    for node in candidates:
        # 找第一个链接
        a = node if node.name == "a" else node.find("a")
        if not a or not a.get("href"):
            continue
        title = a.get_text(" ", strip=True)
        href = a.get("href").strip()
        full_url = urljoin(base_url, href)
        # 摘要优先 .summary/.abs，其次段落文本
        abs_el = node.select_one(".summary, .abs, .intro, p")
        snippet = abs_el.get_text(" ", strip=True) if abs_el else ""
        # 尝试提取时间：优先 .date/.time/.pubtime 元素，其次整节点文本
        ttxt = ""
        for sel in [".date", ".time", ".pubtime", ".f-date", ".info time", ".post-time"]:
            sub = node.select_one(sel)
            if sub:
                ttxt = sub.get_text(" ", strip=True)
                break
        if not ttxt:
            # 可能时间和标题在同一行
            ttxt = node.get_text(" ", strip=True)

        dt = parse_date_text(ttxt, tz)
        items.append({
            "title": title,
            "url": full_url,
            "datetime": dt.isoformat() if dt else "",
            "content": snippet,
            "raw_time_text": ttxt,
        })
    return items

def crawl_search(session, q, page=1, page_size=10, delay=1.0):
    """
    请求示例 URL:
      https://www.mohrss.gov.cn/hsearch/?searchword=人力资源&page=1
    注意：具体参数名（page）若不生效，可通过观察或在浏览器中查看真正的 next link。
    """
    params = {"searchword": q}
    # 一些站可能使用 page 参数
    if page and page > 1:
        params["page"] = page
    url = BASE + SEARCH_PATH + "?" + urlencode(params)
    r = session.get(url, timeout=20)
    time.sleep(delay)
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def filter_recent(items, window_hours, tz):
    now = datetime.now(tz)
    start = now - timedelta(hours=window_hours)
    out = []
    for it in items:
        dt = None
        if it.get("datetime"):
            try:
                dt = datetime.fromisoformat(it["datetime"])
            except Exception:
                dt = None
        if not dt:
            # try parse raw_time_text again lightly (already attempted)
            dt = parse_date_text(it.get("raw_time_text",""), tz)
        if dt and start <= dt <= now:
            it["datetime_obj"] = dt
            out.append(it)
    return out

def save_results(results, prefix="mohrss", fmt="both"):
    ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    files = []
    if fmt in ("csv","both"):
        fn = f"{prefix}_{ts}.csv"
        with open(fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["title","url","datetime","content"])
            w.writeheader()
            for r in results:
                w.writerow({"title": r.get("title",""), "url": r.get("url",""), "datetime": r.get("datetime",""), "content": r.get("content","")})
        files.append(fn)
    if fmt in ("json","both"):
        fn = f"{prefix}_{ts}.json"
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        files.append(fn)
    return files

def to_markdown(results, tz):
    now_dt = datetime.now(tz)
    header = f"**日期：{now_dt.strftime('%Y-%m-%d')}（{['周一','周二','周三','周四','周五','周六','周日'][now_dt.weekday()]}）**\n\n**标题：早安资讯｜人社部搜索（最近 {len(results)} 条）**\n\n**主要内容**\n"
    lines = [header]
    for i, it in enumerate(results, 1):
        dt = it.get("datetime_obj") or it.get("datetime") or ""
        if isinstance(dt, datetime):
            dt = dt.strftime("%Y-%m-%d %H:%M")
        lines.append(f"{i}. [{it['title']}]({it['url']})　—　`{dt}`")
        if it.get("content"):
            lines.append(f"> {it['content'][:120]}")
        lines.append("")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser(description="抓取 mohrss.gov.cn/hsearch/（仅最近N小时）")
    ap.add_argument("--q", required=True, help="搜索关键词（例如 人力资源）")
    ap.add_argument("--pages", type=int, default=1, help="最多翻页数（默认1）")
    ap.add_argument("--page-size", type=int, default=10, help="每页估算条数（仅用于解析时参考）")
    ap.add_argument("--window-hours", type=int, default=24, help="最近多少小时（默认24）")
    ap.add_argument("--delay", type=float, default=1.0, help="每次请求延时（秒）")
    ap.add_argument("--save", choices=["csv","json","both","none"], default="none", help="是否保存结果")
    args = ap.parse_args()

    tz = ZoneInfo("Asia/Shanghai")
    session = make_session()
    all_items = []

    for p in range(1, args.pages + 1):
        try:
            html = crawl_search(session, args.q, page=p, page_size=args.page_size, delay=args.delay)
            items = extract_items_from_search_html(html, BASE, tz)
            if not items:
                # 如果第一页就没内容，提前退出
                if p == 1:
                    print("未解析到搜索结果，请检查关键词或搜索页面结构。")
                break
            all_items.extend(items)
        except Exception as e:
            print("请求或解析第 %d 页异常: %s" % (p, e))
            continue

    recent = filter_recent(all_items, args.window_hours, tz)
    # 去重（按 URL）
    seen = set(); uniq = []
    for it in recent:
        u = it.get("url")
        if u and u not in seen:
            seen.add(u); uniq.append(it)

    print(f"解析到 {len(all_items)} 条候选，窗口内（最近 {args.window_hours} 小时）命中 {len(uniq)} 条。")
    if args.save != "none":
        files = save_results(uniq, prefix="mohrss_search", fmt=args.save)
        print("Saved:", files)

    md = to_markdown(uniq, tz)
    print("\n\n--- MarkDown Preview ---\n")
    print(md)

if __name__ == "__main__":
    main()
