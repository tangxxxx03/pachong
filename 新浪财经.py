# -*- coding: utf-8 -*-
"""
抓取新浪财经某列表页的“前一天”文章：仅标题 + 链接（Markdown 可点击）
- 适配你截图里的结构：ul.listcontent.list_009 > li
- 自动翻页（最多 MAX_PAGES 页）
- 只保留“昨天”的条目（上海时区）
- 输出到：
  1) stdout（Actions 日志可见）
  2) output/sina_yesterday.md（可作为 artifacts 或提交）
"""

import os
import re
import sys
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

DATE_RE = re.compile(r"\((\d{2})月(\d{2})日\s*(\d{2}):(\d{2})\)")

def now_tz():
    return datetime.now(TZ)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def safe_get(url: str, timeout=15, retries=3, sleep=1.0) -> str:
    headers = {
        "User-Agent": os.getenv(
            "UA",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "close",
    }
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            # 新浪页面一般是 gbk/utf-8 混用，requests 会猜；猜错就用 apparent_encoding
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(sleep)
    raise RuntimeError(f"GET failed: {url} ; err={last_err}")

def parse_item_datetime(li_text: str) -> datetime | None:
    """
    从 li 的文本里解析出 (01月13日 15:27) 这种时间
    由于页面只给“月日”，需要补年份（处理跨年：1月抓到12月 -> 去年）
    """
    m = DATE_RE.search(li_text)
    if not m:
        return None
    month, day, hh, mm = map(int, m.groups())
    now = now_tz()
    year = now.year
    # 跨年兜底：如果当前是1月，抓到12月 -> 认为是去年
    if now.month == 1 and month == 12:
        year = now.year - 1
    try:
        return datetime(year, month, day, hh, mm, tzinfo=TZ)
    except Exception:
        return None

def extract_list_items(html: str, base_url: str):
    """
    返回 [(title, href, dt), ...]
    """
    soup = BeautifulSoup(html, "html.parser")

    # 你截图：ul.listcontent.list_009
    ul = soup.select_one("ul.listcontent.list_009")
    if not ul:
        # 兜底：有些页面 class 可能略不同
        ul = soup.select_one("ul.listcontent") or soup.select_one("ul.list_009")

    if not ul:
        return []

    results = []
    for li in ul.select("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        title = norm(a.get_text())
        if not title:
            continue
        href = urljoin(base_url, a["href"].strip())
        li_text = norm(li.get_text(" ", strip=True))
        dt = parse_item_datetime(li_text)
        results.append((title, href, dt))
    return results

def find_next_page(html: str, base_url: str) -> str | None:
    """
    找“下一页”链接（你截图里有：下一页）
    """
    soup = BeautifulSoup(html, "html.parser")

    # 先按文字找
    a = soup.find("a", string=lambda s: s and "下一页" in s)
    if a and a.get("href"):
        return urljoin(base_url, a["href"].strip())

    # 兜底：有些分页是 button-like 或者 class 标记
    a2 = soup.select_one("a.next, a.page-next, a[rel='next']")
    if a2 and a2.get("href"):
        return urljoin(base_url, a2["href"].strip())

    return None

def md_escape(text: str) -> str:
    # Markdown 链接里一般不用特别转义，但方括号要处理一下更稳
    return text.replace("[", "【").replace("]", "】")

def build_markdown(items, title="新浪财经｜昨日更新"):
    lines = []
    lines.append(f"# {title}")
    lines.append("")
    if not items:
        lines.append("（昨日无匹配内容或页面结构变化）")
        return "\n".join(lines) + "\n"

    # 去重：同标题同链接算一条
    seen = set()
    for t, u, dt in items:
        key = hashlib.md5((t + "||" + u).encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        t2 = md_escape(t)
        ts = dt.strftime("%Y-%m-%d %H:%M") if dt else ""
        lines.append(f"- [{t2}]({u}){('  ' + ts) if ts else ''}")

    lines.append("")
    lines.append(f"_Generated at {now_tz().strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)_")
    return "\n".join(lines) + "\n"

def main():
    start_url = os.getenv("START_URL", "").strip()
    if not start_url:
        print("ERROR: 环境变量 START_URL 不能为空（填你的列表页 URL）", file=sys.stderr)
        sys.exit(2)

    max_pages = int(os.getenv("MAX_PAGES", "5"))
    only_yesterday = os.getenv("ONLY_YESTERDAY", "1").strip() != "0"
    sleep_sec = float(os.getenv("SLEEP_SEC", "0.6"))

    now = now_tz()
    yesterday = (now - timedelta(days=1)).date()

    collected = []
    url = start_url
    base_url = start_url

    found_any_yesterday = False

    for page_idx in range(1, max_pages + 1):
        html = safe_get(url)
        items = extract_list_items(html, base_url=base_url)

        # 如果列表里没有 dt，也先收集（但只抓昨天时会过滤掉）
        for title, href, dt in items:
            if only_yesterday:
                if dt and dt.date() == yesterday:
                    collected.append((title, href, dt))
                    found_any_yesterday = True
                else:
                    # dt 缺失就跳过（因为你要“前一天”的）
                    pass
            else:
                collected.append((title, href, dt))

        # 早停策略：如果已经找到昨天内容，并且这一页所有 dt 都 < yesterday，就停止翻页
        if only_yesterday and found_any_yesterday:
            dts = [dt for _, _, dt in items if dt is not None]
            if dts and all(d.date() < yesterday for d in dts):
                break

        next_url = find_next_page(html, base_url=base_url)
        if not next_url:
            break

        url = next_url
        time.sleep(sleep_sec)

    # 排序：按时间倒序（dt 为空放后面）
    collected.sort(key=lambda x: (x[2] is None, x[2] or datetime.min.replace(tzinfo=TZ)), reverse=False)
    # 上面 reverse=False + key 逻辑其实是让 dt 有值的在前、时间小的在前；我们更想新的在前：
    collected.sort(key=lambda x: (x[2] is None, x[2] or datetime.min.replace(tzinfo=TZ)), reverse=True)

    md = build_markdown(collected, title=f"新浪财经｜昨日({yesterday.strftime('%Y-%m-%d')})标题索引")

    # 输出到 stdout
    print(md)

    # 写文件
    out_dir = os.getenv("OUT_DIR", "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "sina_yesterday.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"[OK] wrote: {out_path}", file=sys.stderr)

if __name__ == "__main__":
    main()
