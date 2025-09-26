# -*- coding: utf-8 -*-
"""
hr_news_detail_first.py  —— 深入爬取 · 详情页优先（稳定避开列表日期坑）

功能：
- 人社部固定栏目：部内要闻(buneiyaowen)、人社新闻(rsxw)、地方动态(dfdt)、会议活动(hyhd)
- 列表页只取链接；真实发布时间从详情页解析（正文/时间节点/meta/URL/Last-Modified 多重兜底）
- 时间过滤：支持 --date=yesterday 或指定 YYYY-MM-DD；默认滚动窗口 --window-hours（48）
- 钉钉 Markdown 推送（A版 env：DINGTALK_WEBHOOKA / DINGTALK_SECRETA）
"""

import os, re, time, hmac, base64, hashlib, argparse, email.utils
from dataclasses import dataclass
from typing import List, Tuple, Optional, Iterable
from urllib.parse import urljoin, urlparse, quote
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ===================== 全局配置 =====================
TZ = ZoneInfo("Asia/Shanghai")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Connection": "close",
}
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes", "on")

# —— 人社部栏目 —— 
MOHRSS_BASE = "https://www.mohrss.gov.cn"
MOHRSS_SECTIONS = {
    "部内要闻": f"{MOHRSS_BASE}/SYrlzyhshbzb/dongtaixinwen/buneiyaowen/",
    "人社新闻": f"{MOHRSS_BASE}/SYrlzyhshbzb/dongtaixinwen/rsxw/",
    "地方动态": f"{MOHRSS_BASE}/SYrlzyhshbzb/dongtaixinwen/dfdt/",
    "会议活动": f"{MOHRSS_BASE}/SYrlzyhshbzb/dongtaixinwen/hyhd/",
}

# —— 钉钉（A 版变量名） —— 
DEFAULT_WEBHOOK = ("https://oapi.dingtalk.com/robot/send?"
                   "access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49")
DEFAULT_SECRET = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"

def _first_env(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k, "")
        if v and v.strip():
            return v.strip()
    return default

DINGTALK_WEBHOOK = _first_env("DINGTALK_WEBHOOKA", "DINGTALK_BASEA", "WEBHOOKA", default=DEFAULT_WEBHOOK)
DINGTALK_SECRET  = _first_env("DINGTALK_SECRETA",  "SECRETA",        default=DEFAULT_SECRET)

# ===================== HTTP 会话 =====================
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.trust_env = False
    return s

# ===================== 钉钉推送 =====================
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
        r = requests.post(webhook, json={"msgtype":"markdown","markdown":{"title":title,"text":md_text}}, timeout=20)
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
        if DEBUG:
            print("DingTalk resp:", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

# ===================== 时间工具 =====================
DATE_PATTS = [
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*(\d{1,2}):(\d{1,2})",
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})",
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})",
    r"(\d{4})年(\d{1,2})月(\d{1,2})日",
]
MONTHDAY_PATTS = [
    r"(\d{1,2})[-/.](\d{1,2})",
    r"(\d{1,2})月(\d{1,2})日",
]

def build_dt(y:int, m:int, d:int, hh:int=12, mm:int=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=TZ)

def parse_any_datetime(text: str, *, ref: Optional[datetime]=None) -> Optional[datetime]:
    if not text:
        return None
    s = re.sub(r"\s+", " ", text.strip())
    for p in DATE_PATTS:
        m = re.search(p, s)
        if m:
            g = [int(x) for x in m.groups() if x]
            if len(g) == 5:
                y, mo, d, hh, mm = g
                return build_dt(y, mo, d, hh, mm)
            elif len(g) == 3:
                y, mo, d = g
                return build_dt(y, mo, d)
    base = (ref or datetime.now(TZ))
    for p in MONTHDAY_PATTS:
        m = re.search(p, s)
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
            cand = build_dt(base.year, mo, d)
            if cand > base:
                cand = build_dt(base.year - 1, mo, d)
            return cand
    return None

def day_range(date_str: str) -> Tuple[datetime, datetime]:
    if date_str.lower() == "yesterday":
        base = datetime.now(TZ).date() - timedelta(days=1)
    else:
        base = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (datetime(base.year, base.month, base.day, 0,0,0, tzinfo=TZ),
            datetime(base.year, base.month, base.day, 23,59,59, tzinfo=TZ))

# ===================== 数据结构 =====================
@dataclass
class Item:
    title: str
    url: str
    dt: Optional[datetime]
    source: str

# ===================== 解析器（详情优先） =====================
def discover_links_from_mohrss_list(html: str, base_page_url: str) -> Iterable[Tuple[str,str]]:
    """从列表页抽取文章链接；注意用【当前列表页URL】做 urljoin 的基准"""
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.serviceMainListTabCon, div.serviceMainListTxtCon")
    if not blocks:
        blocks = soup.select("ul li, table tr")
    seen = set()
    for b in blocks:
        a = b.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        if href.startswith("javascript:") or href.startswith("#"):
            continue
        # —— 修正点：以“当前页 URL”为基准拼接相对路径 —— #
        url = urljoin(base_page_url, href)
        if url in seen:
            continue
        seen.add(url)
        title = a.get_text(" ", strip=True)
        if title:
            yield title, url

def extract_publish_dt_from_detail(html: str, url: str, last_modified_header: str = "") -> Optional[datetime]:
    soup = BeautifulSoup(html, "html.parser")
    text_blocks = []
    cand_nodes = soup.select("time, .time, .date, .pubtime, .publish-time, .source, .info, .article-info, .xxgk-info")
    for n in cand_nodes:
        text_blocks.append(n.get_text(" ", strip=True))
    for sel in [
        "meta[name='PubDate']", "meta[name='publishdate']",
        "meta[property='article:published_time']", "meta[name='releaseDate']",
        "meta[name='weibo: article:create_at']"
    ]:
        m = soup.select_one(sel)
        if m and m.get("content"):
            text_blocks.append(m["content"])
    header = soup.select_one("h1, .title, .articleTitle")
    if header:
        text_blocks.append(header.get_text(" ", strip=True))
    body = soup.select_one("article, .article, .TRS_Editor, .content, #content")
    if body:
        text_blocks.append(body.get_text(" ", strip=True)[:500])
    text_blocks.append(url.replace("/", " ").replace("_", " "))
    if last_modified_header:
        text_blocks.append(last_modified_header)
    big = " | ".join([t for t in text_blocks if t])
    return parse_any_datetime(big, ref=datetime.now(TZ))

def fetch_list_and_details(session: requests.Session, list_url: str, pages: int, site: str) -> List[Item]:
    items: List[Item] = []
    for p in range(1, pages + 1):
        url = list_url if p == 1 else urljoin(list_url, f"index_{p}.html")
        r = session.get(url, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
        if DEBUG: print(f"[DEBUG] list {site} p{p} len={len(html)}")

        # —— 修正点：把“当前列表页 URL (url)”传进去当基准 —— #
        for title, link in discover_links_from_mohrss_list(html, url):
            try:
                rr = session.get(link, timeout=20, headers={"Referer": url})
                rr.encoding = rr.apparent_encoding or "utf-8"
                lm = rr.headers.get("Last-Modified") or rr.headers.get("last-modified") or ""
                dt = extract_publish_dt_from_detail(rr.text, link, lm)
                if not dt and lm:
                    try:
                        dt_parsed = email.utils.parsedate_to_datetime(lm)
                        if dt_parsed and dt_parsed.tzinfo is not None:
                            dt = dt_parsed.astimezone(TZ)
                        elif dt_parsed:
                            dt = dt_parsed.replace(tzinfo=TZ)
                    except Exception:
                        dt = None
                items.append(Item(title=title, url=link, dt=dt, source=site))
                time.sleep(0.4)
            except Exception as e:
                if DEBUG: print("[DEBUG] detail error:", e)
                continue
    return items

# ===================== 汇总/过滤/输出 =====================
def dedup(items: List[Item]) -> List[Item]:
    seen = set(); out=[]
    for it in items:
        k = it.url.split("#")[0]
        if k in seen: 
            continue
        seen.add(k)
        out.append(it)
    return out

def filter_by_time(items: List[Item], start: datetime, end: datetime, allow_nodate: bool=False) -> List[Item]:
    kept=[]
    for it in items:
        if it.dt:
            if start <= it.dt <= end:
                kept.append(it)
        elif allow_nodate:
            kept.append(it)
    return kept

def build_markdown(items: List[Item], title_prefix: str) -> str:
    now_dt = datetime.now(TZ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now_dt.weekday()]
    lines = [
        f"**日期：{now_dt.strftime('%Y-%m-%d')}（{wd}）**",
        "",
        f"**标题：{title_prefix}｜人社部（固定栏目）**",
        "",
        "**主要内容**",
    ]
    if not items:
        lines.append("> 暂无更新。")
        return "\n".join(lines)
    for i, it in enumerate(items, 1):
        ds = it.dt.strftime("%Y-%m-%d %H:%M") if it.dt else "（时间未知）"
        lines.append(f"{i}. [{it.title}]({it.url})  —  *{it.source}*  `{ds}`")
        lines.append("")
    return "\n".join(lines)

# ===================== CLI =====================
def main():
    ap = argparse.ArgumentParser(description="人社部（详情优先解析发布时间）→ 钉钉推送")
    ap.add_argument("--pages", type=int, default=int(os.getenv("PAGES","2")))
    ap.add_argument("--delay", type=float, default=float(os.getenv("DELAY","0.6")))
    ap.add_argument("--limit", type=int, default=int(os.getenv("LIMIT","50")))
    ap.add_argument("--date", default=os.getenv("DATE",""), help="yesterday / YYYY-MM-DD")
    ap.add_argument("--auto-range", default=os.getenv("AUTO_RANGE","").lower()=="true", action="store_true")
    ap.add_argument("--window-hours", type=int, default=int(os.getenv("WINDOW_HOURS","48")))
    ap.add_argument("--allow-nodate", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    session = make_session()

    if args.date:
        start, end = day_range(args.date); title_prefix = f"{args.date} 专题"
    elif args.auto_range:
        start, end = day_range("yesterday"); title_prefix = "昨日专辑"
    else:
        now = datetime.now(TZ); start, end = (now - timedelta(hours=args.window_hours)), now
        title_prefix = f"近{args.window_hours}小时"

    all_items: List[Item] = []
    for name, url in MOHRSS_SECTIONS.items():
        try:
            got = fetch_list_and_details(session, url, args.pages, site=name)
            if DEBUG: print(f"[DEBUG] {name} got {len(got)}")
            all_items.extend(got)
            time.sleep(args.delay)
        except Exception as e:
            print(f"[WARN] 抓取 {name} 出错：{e}")

    all_items = dedup(all_items)
    kept = filter_by_time(all_items, start, end, allow_nodate=args.allow_nodate)
    kept.sort(key=lambda x: (x.dt or datetime(1970,1,1, tzinfo=TZ)), reverse=True)
    if args.limit > 0:
        kept = kept[:args.limit]

    md = build_markdown(kept, title_prefix)
    print("\n--- Markdown Preview ---\n"); print(md)

    try:
        with open("hr_news.md", "w", encoding="utf-8") as f:
            f.write(md)
    except Exception as e:
        print("write md error:", e)

    if not args.no_push:
        ok = send_dingtalk_markdown(f"{title_prefix}｜人社部（固定栏目）", md)
        print("钉钉推送：", "成功 ✅" if ok else "失败/未推送 ❌")

if __name__ == "__main__":
    main()
