# -*- coding: utf-8 -*-
"""
hr_search_24h_dingtalk.py  — 昨天专抓版
新增：
  - --date 参数：支持 yesterday / YYYY-MM-DD
  - 仅昨天模式：过滤时间范围为 [昨天00:00, 昨天23:59:59]
  - 不传 --date 时，保持原有 --window-hours 行为（向后兼容）
"""

import re
import os
import time
import hmac
import base64
import hashlib
import argparse
from dataclasses import dataclass
from typing import List, Tuple, Optional
from urllib.parse import urljoin, urlencode, urlparse, quote
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo  # Py3.9+
except Exception:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup


# ===================== 全局配置 =====================
TZ = ZoneInfo("Asia/Shanghai")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/123.0.0.0 Safari/537.36")

# —— 默认钉钉（可被环境变量覆盖）
DEFAULT_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send?"
    "access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
)
DEFAULT_SECRET = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"


def _first_env(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k, "").strip()
        if v:
            return v
    return default


DINGTALK_WEBHOOK = _first_env("DINGTALK_WEBHOOK", "DINGTALK_BASE", "WEBHOOK", default=DEFAULT_WEBHOOK)
DINGTALK_SECRET  = _first_env("DINGTALK_SECRET",  "SECRET",        default=DEFAULT_SECRET)


def _mask_tail(s: str, keep: int = 6) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


# ===================== HTTP 工具 =====================
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    retries = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.trust_env = False
    return s


# ===================== DingTalk 推送 =====================
def _sign_webhook(base_webhook: str, secret: str) -> str:
    if not base_webhook:
        return ""
    if not secret:
        return base_webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = quote(base64.b64encode(hmac_code))
    sep = "&" if "?" in base_webhook else "?"
    return f"{base_webhook}{sep}timestamp={ts}&sign={sign}"


def send_dingtalk_markdown(title: str, md_text: str) -> bool:
    webhook = _sign_webhook(DINGTALK_WEBHOOK, DINGTALK_SECRET)
    if not webhook:
        print("🔕 未配置钉钉 Webhook，跳过推送。")
        return False
    try:
        host = urlparse(webhook).netloc
        print(f"[DingTalk] host={host}  token~{_mask_tail(DINGTALK_WEBHOOK)}  secret~{_mask_tail(DINGTALK_SECRET)}")
    except Exception:
        pass

    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
        print("DingTalk resp:", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False


# ===================== 解析与时间过滤 =====================
DATE_PATS = [
    r"(20\d{2})[^\d](\d{1,2})[^\d](\d{1,2})\s+(\d{1,2}):(\d{1,2})",  # 2025-09-16 08:30
    r"(20\d{2})[^\d](\d{1,2})[^\d](\d{1,2})",                        # 2025-09-16
    r"(\d{1,2})[^\d](\d{1,2})\s+(\d{1,2}):(\d{1,2})",               # 09-16 08:30
]

def parse_dt(text: str) -> Optional[datetime]:
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip())
    # 明确日期匹配
    for pat in DATE_PATS:
        m = re.search(pat, t)
        if not m:
            continue
        if len(m.groups()) == 5:
            y, mo, d, hh, mm = map(int, m.groups())
            return datetime(y, mo, d, hh, mm, tzinfo=TZ)
        if len(m.groups()) == 3:
            y, mo, d = map(int, m.groups())
            # 只有日期时设为中午12:00，避免被24h窗口误杀
            return datetime(y, mo, d, 12, 0, tzinfo=TZ)
        if len(m.groups()) == 4:
            mo, d, hh, mm = map(int, m.groups())
            y = datetime.now(TZ).year
            return datetime(y, mo, d, hh, mm, tzinfo=TZ)
    # 相对时间
    if re.search(r"(刚刚|分钟|小时前|今天|今日)", t):
        return datetime.now(TZ)
    return None


def within_last_hours(dt: Optional[datetime], hours: int) -> bool:
    if not dt:
        return False
    now = datetime.now(TZ)
    return (now - timedelta(hours=hours)) <= dt <= now


def day_range(date_str: str) -> Tuple[datetime, datetime]:
    """返回某天在本地时区的 [start, end]"""
    if date_str.lower() == "yesterday":
        base = datetime.now(TZ).date() - timedelta(days=1)
    else:
        base = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime(base.year, base.month, base.day, 0, 0, 0, tzinfo=TZ)
    end   = datetime(base.year, base.month, base.day, 23, 59, 59, tzinfo=TZ)
    return start, end


# ===================== 数据结构 =====================
@dataclass
class Item:
    title: str
    url: str
    dt: Optional[datetime]
    content: str
    source: str


# ===================== 站点 1：mohrss.gov.cn/hsearch =====================
class MohrssHSearch:
    BASE = "https://www.mohrss.gov.cn"
    PATH = "/hsearch/"

    def __init__(self, session: requests.Session, q: str, delay: float = 1.0):
        self.session = session
        self.q = q
        self.delay = delay

    def _fetch_page(self, page: int) -> str:
        params = {"searchword": self.q}
        if page > 1:
            params["page"] = page
        url = self.BASE + self.PATH + "?" + urlencode(params)
        r = self.session.get(url, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(self.delay)
        return r.text

    def parse_list(self, html: str) -> Tuple[List[Item], Optional[str]]:
        soup = BeautifulSoup(html, "html.parser")
        nodes = []
        for sel in ["ul.search-list li", "div.search-list li", "div.list li", "ul li", "div.result", "div.row"]:
            tmp = soup.select(sel)
            if tmp:
                nodes = tmp
                break
        if not nodes:
            nodes = soup.select("a")  # 兜底

        items: List[Item] = []
        for node in nodes:
            a = node if node.name == "a" else node.find("a")
            if not a or not a.get("href"):
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href").strip()
            url = urljoin(self.BASE, href)

            # 内容/摘要
            abs_el = None
            for sel in [".summary", ".abs", ".intro", "p"]:
                abs_el = node.select_one(sel)
                if abs_el:
                    break
            content = abs_el.get_text(" ", strip=True) if abs_el else ""

            # 时间
            ttxt = ""
            for sel in [".date", ".time", ".pubtime", ".f-date", ".info time", ".post-time"]:
                sub = node.select_one(sel)
                if sub:
                    ttxt = sub.get_text(" ", strip=True)
                    break
            if not ttxt:
                ttxt = node.get_text(" ", strip=True)
            dt = parse_dt(ttxt)

            items.append(Item(title=title, url=url, dt=dt, content=content, source="人社部站内搜索"))

        # 下一页
        next_link = None
        for a in soup.select("a"):
            txt = a.get_text(strip=True)
            if txt in ("下一页", "下页", "›", ">") or a.get("rel") == ["next"]:
                href = a.get("href") or ""
                if href and href != "javascript:;" and not href.startswith("#"):
                    next_link = urljoin(self.BASE, href)
                    break

        return items, next_link

    def run(self, max_pages: int) -> List[Item]:
        all_items: List[Item] = []
        next_url = None
        for p in range(1, max_pages + 1):
            if p == 1 or not next_url:
                html = self._fetch_page(p)
            else:
                r = self.session.get(next_url, timeout=20)
                r.encoding = r.apparent_encoding or "utf-8"
                time.sleep(self.delay)
                html = r.text
            items, next_url = self.parse_list(html)
            if not items and p == 1:
                break
            all_items.extend(items)
            if not next_url:
                break
        return all_items


# ===================== 站点 2：job.mohrss.gov.cn/zxss =====================
class JobMohrssSearch:
    BASE = "http://job.mohrss.gov.cn"
    PATH = "/zxss/index.jhtml"

    def __init__(self, session: requests.Session, q: str, delay: float = 1.0):
        self.session = session
        self.q = q
        self.delay = delay

    def _fetch_page(self, page: int, last_next: Optional[str]) -> str:
        if last_next:
            url = last_next
        else:
            params = {"textfield": self.q}
            if page > 1:
                params["pageNo"] = page
            url = self.BASE + self.PATH + "?" + urlencode(params)
        r = self.session.get(url, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(self.delay)
        return r.text

    def parse_list(self, html: str) -> Tuple[List[Item], Optional[str]]:
        soup = BeautifulSoup(html, "html.parser")
        nodes = []
        for sel in [
            ".list li", ".news-list li", ".content-list li", ".box-list li",
            "ul.list li", "ul.news li", "ul li", "li"
        ]:
            tmp = soup.select(sel)
            if tmp:
                nodes = tmp
                break
        if not nodes:
            nodes = soup.select("a")

        items: List[Item] = []
        for node in nodes:
            a = node if node.name == "a" else node.find("a")
            if not a or not a.get("href"):
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href").strip()
            url = urljoin(self.BASE, href)

            # 过滤非本站
            host = urlparse(url).netloc.lower()
            if not host.endswith("mohrss.gov.cn"):
                continue

            # 摘要
            abs_el = None
            for sel in [".summary", ".abs", ".intro", "p"]:
                abs_el = node.select_one(sel)
                if abs_el:
                    break
            content = abs_el.get_text(" ", strip=True) if abs_el else ""

            # 时间
            ttxt = ""
            for sel in [".date", ".time", ".pubtime", ".f-date", ".info time", ".post-time", "em", "span"]:
                sub = node.select_one(sel)
                if sub:
                    maybe = sub.get_text(" ", strip=True)
                    if re.search(r"\d{2,4}[^\d]\d{1,2}[^\d]\d{1,2}", maybe) or re.search(r"(刚刚|分钟|小时前|今天|今日)", maybe):
                        ttxt = maybe
                        break
            if not ttxt:
                ttxt = node.get_text(" ", strip=True)
            dt = parse_dt(ttxt)

            items.append(Item(title=title, url=url, dt=dt, content=content, source="公共招聘网搜索"))

        # 下一页
        next_link = None
        for a in soup.select("a"):
            txt = a.get_text(strip=True)
            if txt in ("下一页", "下页", "›", ">") or a.get("rel") == ["next"]:
                href = a.get("href") or ""
                if href and href != "javascript:;" and not href.startswith("#"):
                    next_link = urljoin(self.BASE, href)
                    break

        return items, next_link

    def run(self, max_pages: int) -> List[Item]:
        all_items: List[Item] = []
        next_url = None
        for p in range(1, max_pages + 1):
            html = self._fetch_page(p, last_next=next_url)
            items, next_url = self.parse_list(html)
            if not items and p == 1:
                break
            all_items.extend(items)
            if not next_url:
                break
        return all_items


# ===================== 汇总、过滤、输出 =====================
def dedup_by_url(items: List[Item]) -> List[Item]:
    seen = set()
    out: List[Item] = []
    for it in items:
        if it.url and it.url not in seen:
            seen.add(it.url)
            out.append(it)
    return out


def filter_by_range(items: List[Item], start: datetime, end: datetime) -> List[Item]:
    out: List[Item] = []
    for it in items:
        if it.dt and start <= it.dt <= end:
            out.append(it)
    return out


def build_markdown(items: List[Item], keyword: str, title_prefix: str) -> str:
    now_dt = datetime.now(TZ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now_dt.weekday()]
    lines = [
        f"**日期：{now_dt.strftime('%Y-%m-%d')}（{wd}）**",
        "",
        f"**标题：{title_prefix}｜人社部 & 公共招聘网搜索｜{keyword}**",
        "",
        "**主要内容**",
    ]
    if not items:
        lines.append("> 暂无更新。")
        return "\n".join(lines)

    for i, it in enumerate(items, 1):
        dt_str = it.dt.strftime("%Y-%m-%d %H:%M") if it.dt else ""
        title_line = f"{i}. [{it.title}]({it.url})"
        if it.source:
            title_line += f"　—　*{it.source}*"
        if dt_str:
            title_line += f"　`{dt_str}`"
        lines.append(title_line)
        if it.content:
            snippet = re.sub(r"\s+", " ", it.content).strip()[:120]
            lines.append(f"> {snippet}")
        lines.append("")
    return "\n".join(lines)


# ===================== 主流程 =====================
def main():
    ap = argparse.ArgumentParser(description="人社部 & 公共招聘网 站内搜索 → 钉钉推送")
    ap.add_argument("--q", default=os.getenv("QUERY", "人力资源"), help="搜索关键词（默认：人力资源；也可用环境变量 QUERY 覆盖）")
    ap.add_argument("--pages", type=int, default=int(os.getenv("PAGES", "2")), help="每站最多翻页数（默认2，可用 env PAGES）")
    ap.add_argument("--window-hours", type=int, default=int(os.getenv("WINDOW_HOURS", "24")), help="滚动窗口小时数（默认24）")
    ap.add_argument("--delay", type=float, default=float(os.getenv("DELAY", "1.0")), help="每次请求间隔秒（默认1.0）")
    ap.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "20")), help="正文最多展示条数（默认20）")
    ap.add_argument("--date", default=os.getenv("DATE", "yesterday"),
                    help="抓取指定日期（yesterday 或 YYYY-MM-DD）。若为空则使用 --window-hours 滚动窗口")
    ap.add_argument("--no-push", action="store_true", help="只打印不推送钉钉")
    args = ap.parse_args()

    session = make_session()

    # 站点抓取
    a = MohrssHSearch(session, args.q, delay=args.delay).run(max_pages=args.pages)
    b = JobMohrssSearch(session, args.q, delay=args.delay).run(max_pages=args.pages)
    all_items = dedup_by_url(a + b)

    title_prefix = "早安资讯"
    # 时间过滤：优先使用 --date（默认 yesterday），否则回退 --window-hours
    if args.date:
        start, end = day_range(args.date)
        all_items = filter_by_range(all_items, start, end)
        title_prefix = f"{args.date} 专题"
    else:
        all_items = [it for it in all_items if within_last_hours(it.dt, args.window_hours)]

    # 排序+截断
    all_items.sort(key=lambda x: x.dt or datetime(1970, 1, 1, tzinfo=TZ), reverse=True)
    show = all_items[:args.limit] if args.limit and args.limit > 0 else all_items

    print(f"✅ 合计候选 {len(a)+len(b)} 条；筛选后 {len(all_items)} 条；展示 {len(show)} 条。")

    md = build_markdown(show, args.q, title_prefix)
    print("\n--- Markdown Preview ---\n")
    print(md)

    if not args.no_push:
        ok = send_dingtalk_markdown(f"{title_prefix}｜部网&招聘网搜索｜{args.q}", md)
        print("钉钉推送：", "成功 ✅" if ok else "失败/未推送 ❌")


if __name__ == "__main__":
    main()
