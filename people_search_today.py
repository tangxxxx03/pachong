# -*- coding: utf-8 -*-
"""
hr_search_24h_dingtalk.py  — 稳健版
改动亮点：
  1) --q 不再 required，默认 "人力资源"（再也不会因忘传参数退出）
  2) 钉钉 webhook/secret 支持多别名环境变量优先：
       DINGTALK_WEBHOOK / DINGTALK_BASE / WEBHOOK
       DINGTALK_SECRET  / SECRET
     → 解决“改了却还发到旧群”的常见问题
  3) 运行时打印掩码信息（host + token/secret 末尾6位），方便你确认推送目标（不泄露明文）
  4) 爬取、时间解析、分页逻辑与原版一致，增加少量兼容与注释
用法示例：
  python hr_search_24h_dingtalk.py --q "人力资源" --pages 3 --window-hours 24 --limit 20
  # 只打印不推送
  python hr_search_24h_dingtalk.py --no-push
依赖：
  pip install requests beautifulsoup4
  # 若 Python < 3.9：pip install backports.zoneinfo
"""

import re
import os
import time
import hmac
import json
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
    """取第一个非空环境变量值"""
    for k in keys:
        v = os.getenv(k, "").strip()
        if v:
            return v
    return default


DINGTALK_WEBHOOK = _first_env("DINGTALK_WEBHOOK", "DINGTALK_BASE", "WEBHOOK", default=DEFAULT_WEBHOOK)
DINGTALK_SECRET  = _first_env("DINGTALK_SECRET",  "SECRET",        default=DEFAULT_SECRET)


def _mask_tail(s: str, keep: int = 6) -> str:
    """掩码显示，仅保留末尾 keep 位"""
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
    s.trust_env = False  # 忽略系统代理，保证可控
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

    # 运行时提示（掩码），避免“发到旧群”
    try:
        host = urlparse(webhook).netloc
        print(f"[DingTalk] host={host}  token~{_mask_tail(DINGTALK_WEBHOOK, 6)}  secret~{_mask_tail(DINGTALK_SECRET, 6)}")
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
    r"(20\d{2})[^\d](\d{1,2})[^\d](\d{1,2})\s+(\d{1,2}):(\d{1,2})",  # 2025-09-16 08:30 / 2025/09/16 08:30
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
            return datetime(y, mo, d, 0, 0, tzinfo=TZ)
        if len(m.groups()) == 4:
            mo, d, hh, mm = map(int, m.groups())
            y = datetime.now(TZ).year
            return datetime(y, mo, d, hh, mm, tzinfo=TZ)
    # 相对时间（粗略兜底）
    if re.search(r"(刚刚|分钟|小时前|今天|今日)", t):
        return datetime.now(TZ)
    return None


def within_last_hours(dt: Optional[datetime], hours: int) -> bool:
    if not dt:
        return False
    now = datetime.now(TZ)
    return (now - timedelta(hours=hours)) <= dt <= now


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
            params["page"] = page  # 若站点用其他分页名也能兼容“下一页”抓取
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
        # 优先使用“下一页”链接（适应站点真实分页参数），否则回退 textfield + pageNo
        if last_next:
            url = last_next
        else:
            params = {"textfield": self.q}
            if page > 1:
                params["pageNo"] = page  # 常见分页名
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
            nodes = soup.select("a")  # 兜底

        items: List[Item] = []
        for node in nodes:
            a = node if node.name == "a" else node.find("a")
            if not a or not a.get("href"):
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href").strip()
            url = urljoin(self.BASE, href)

            # 过滤非本站、或无意义链接
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


def filter_24h(items: List[Item], window_hours: int) -> List[Item]:
    return [it for it in items if within_last_hours(it.dt, window_hours)]


def build_markdown(items: List[Item], keyword: str) -> str:
    now_dt = datetime.now(TZ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now_dt.weekday()]
    lines = [
        f"**日期：{now_dt.strftime('%Y-%m-%d')}（{wd}）**",
        "",
        f"**标题：早安资讯｜人社部 & 公共招聘网搜索｜{keyword}**",
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
    ap = argparse.ArgumentParser(description="人社部 & 公共招聘网 站内搜索（仅最近N小时）→ 钉钉推送")
    ap.add_argument("--q", default=os.getenv("QUERY", "人力资源"), help="搜索关键词（默认：人力资源；也可用环境变量 QUERY 覆盖）")
    ap.add_argument("--pages", type=int, default=int(os.getenv("PAGES", "2")), help="每站最多翻页数（默认2，可用 env PAGES）")
    ap.add_argument("--window-hours", type=int, default=int(os.getenv("WINDOW_HOURS", "24")), help="最近N小时（默认24，可用 env WINDOW_HOURS）")
    ap.add_argument("--delay", type=float, default=float(os.getenv("DELAY", "1.0")), help="每次请求间隔秒（默认1.0，可用 env DELAY）")
    ap.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "20")), help="正文最多展示条数（默认20，可用 env LIMIT）")
    ap.add_argument("--no-push", action="store_true", help="只打印不推送钉钉")
    args = ap.parse_args()

    session = make_session()

    # 站点 1
    hsearch = MohrssHSearch(session, args.q, delay=args.delay)
    a = hsearch.run(max_pages=args.pages)

    # 站点 2
    jsearch = JobMohrssSearch(session, args.q, delay=args.delay)
    b = jsearch.run(max_pages=args.pages)

    all_items = dedup_by_url(a + b)

    # 仅最近 N 小时
    all_items = filter_24h(all_items, args.window_hours)

    # 按时间降序（解析不到时间的排最后）
    all_items.sort(key=lambda x: x.dt or datetime(1970, 1, 1, tzinfo=TZ), reverse=True)

    # 截断展示条数
    show = all_items[:args.limit] if args.limit and args.limit > 0 else all_items

    print(f"✅ 合计候选 {len(a)+len(b)} 条；窗口内（最近 {args.window_hours}h）命中 {len(all_items)} 条；展示 {len(show)} 条。")

    md = build_markdown(show, args.q)
    print("\n--- Markdown Preview ---\n")
    print(md)

    if not args.no_push:
        ok = send_dingtalk_markdown(f"早安资讯｜部网&招聘网搜索｜{args.q}", md)
        print("钉钉推送：", "成功 ✅" if ok else "失败/未推送 ❌")


if __name__ == "__main__":
    main()
