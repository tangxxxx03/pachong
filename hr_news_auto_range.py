# -*- coding: utf-8 -*-
"""
hr_news_auto_range.py

◆ 固定来源（无关键词）：
  1) 人社部新闻/动态：部内要闻（buneiyaowen）/ 人社新闻（rsxw）/ 地方动态（dfdt）
     - 逐条以列表容器抓取：标题 + 链接 + 日期（YYYY-MM-DD）
     - 只接受“列表页可见日期”，无日期丢弃
  2) 中国公共招聘网：资讯首页主列表（右侧日期）

◆ 时间策略：
  - 默认“抓昨天（Asia/Shanghai）” 00:00~23:59:59
  - 也支持 --date 指定（yesterday / YYYY-MM-DD）
  - 关闭 --auto-range 时可用 --window-hours 滚动窗口

◆ 输出：
  - Markdown 到 stdout 与 hr_news.md
  - 可选钉钉 Markdown 推送（DINGTALK_WEBHOOKA / DINGTALK_SECRETA）

依赖：
  pip install requests beautifulsoup4 urllib3
"""

import os, re, time, hmac, base64, hashlib, argparse
from dataclasses import dataclass
from typing import List, Tuple, Optional
from urllib.parse import urljoin, urlparse, quote
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
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

# —— 人社部栏目 ——
MOHRSS_BNYW = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/buneiyaowen/"  # 部内要闻
MOHRSS_RSXW = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/rsxw/"        # 人社新闻
MOHRSS_DFDT = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/"        # 地方动态
MOHRSS_SECTIONS = [MOHRSS_BNYW, MOHRSS_RSXW, MOHRSS_DFDT]

# —— 公共招聘网（主列表） ——
JOB_ZXSS = "http://job.mohrss.gov.cn/zxss/index.jhtml"

# —— 强制：必须有日期（列表页可见） ——
REQUIRE_DATE_MOHRSS = True
REQUIRE_DATE_JOB = True

# —— Debug 输出开关 ——
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes", "on")

# —— 钉钉（A 版变量名；可被环境覆盖） ——
DEFAULT_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send?"
    "access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
)
DEFAULT_SECRET = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"

def _first_env(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k, "")
        if v and v.strip():
            return v.strip()
    return default

DINGTALK_WEBHOOK = _first_env("DINGTALK_WEBHOOKA", "DINGTALK_BASEA", "WEBHOOKA", default=DEFAULT_WEBHOOK)
DINGTALK_SECRET  = _first_env("DINGTALK_SECRETA",  "SECRETA",        default=DEFAULT_SECRET)

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
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
        print("DingTalk resp:", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

# ===================== 时间解析 =====================
def parse_dt(text: str) -> Optional[datetime]:
    """仅解析 YYYY-MM-DD 或 YYYY-MM-DD HH:MM；否则返回 None。"""
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip())
    m = re.search(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?$", t)
    if not m:
        return None
    y, mo, d, hh, mm = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    y, mo, d = int(y), int(mo), int(d)
    if hh and mm:
        return datetime(y, mo, d, int(hh), int(mm), tzinfo=TZ)
    return datetime(y, mo, d, 12, 0, tzinfo=TZ)

def day_range(date_str: str) -> Tuple[datetime, datetime]:
    if date_str.lower() == "yesterday":
        base = datetime.now(TZ).date() - timedelta(days=1)
    else:
        base = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime(base.year, base.month, base.day, 0, 0, 0, tzinfo=TZ)
    end   = datetime(base.year, base.month, base.day, 23, 59, 59, tzinfo=TZ)
    return start, end

def auto_range() -> Tuple[datetime, datetime, str]:
    start, end = day_range("yesterday")
    return start, end, "昨日专辑"

# ===================== 数据结构 =====================
@dataclass
class Item:
    title: str
    url: str
    dt: Optional[datetime]
    content: str
    source: str

# ===================== 站点 1：人社部 =====================
class MohrssList:
    BASE = "https://www.mohrss.gov.cn"

    def __init__(self, session: requests.Session, list_url: str, delay: float = 1.0):
        self.session = session
        self.list_url = list_url
        self.delay = delay

    def _fetch(self, url: str) -> str:
        r = self.session.get(url, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(self.delay)
        return r.text

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.list_url
        tail = "" if self.list_url.endswith("/") else "/"
        return urljoin(self.list_url, f"{tail}index_{page}.html")

    def parse_list(self, html: str) -> List[Item]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[Item] = []

        # —— 精准容器：一条新闻在 div.serviceMainListTxtCon 中 ——
        cards = soup.select("div.serviceMainListTxtCon")
        for card in cards:
            a = card.select_one(".serviceMainListTxtLink a[href]")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            url = urljoin(self.BASE, a["href"].strip())

            # 日期：优先两个固定位置，其次容器内任意 YYYY-MM-DD
            date_el = card.select_one(".organMenuTxtLink") or card.select_one(".organGeneralNewListTxtConTime")
            dt_txt = date_el.get_text(" ", strip=True) if date_el else ""
            if not dt_txt:
                m_any = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", card.get_text(" ", strip=True))
                dt_txt = m_any.group(0) if m_any else ""

            dt = parse_dt(dt_txt)
            if REQUIRE_DATE_MOHRSS and not dt:
                continue

            items.append(Item(title=title, url=url, dt=dt, content="", source="人社部"))

        if items:
            return items

        # —— 兜底：table 结构 ——
        rows = soup.select("table tr")
        for tr in rows:
            a = tr.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            url = urljoin(self.BASE, a["href"].strip())
            tds = tr.find_all("td")
            dt_txt = tds[-1].get_text(" ", strip=True) if len(tds) >= 2 else tr.get_text(" ", strip=True)
            dt = parse_dt(dt_txt)
            if REQUIRE_DATE_MOHRSS and not dt:
                continue
            items.append(Item(title=title, url=url, dt=dt, content="", source="人社部(table)"))

        if items:
            return items

        # —— 兜底：ul/li 结构 ——
        lis = soup.select("ul li")
        for li in lis:
            a = li.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            url = urljoin(self.BASE, a["href"].strip())
            dt_txt = ""
            for sel in ["span", "em", ".date", ".time"]:
                node = li.select_one(sel)
                if node:
                    dt_txt = node.get_text(" ", strip=True); break
            if not dt_txt:
                dt_txt = li.get_text(" ", strip=True)
            dt = parse_dt(dt_txt)
            if REQUIRE_DATE_MOHRSS and not dt:
                continue
            items.append(Item(title=title, url=url, dt=dt, content="", source="人社部(ul)"))

        return items

    def run(self, pages: int) -> List[Item]:
        all_items: List[Item] = []
        for p in range(1, pages + 1):
            url = self._page_url(p)
            html = self._fetch(url)
            part = self.parse_list(html)
            if not part and p == 1:
                break
            all_items.extend(part)
        return all_items

# ===================== 站点 2：公共招聘网（主列表+右侧日期） =====================
class JobZxss:
    BASE = "http://job.mohrss.gov.cn"
    LIST = JOB_ZXSS

    def __init__(self, session: requests.Session, delay: float = 1.0):
        self.session = session
        self.delay = delay

    def _fetch(self, url: str) -> str:
        r = self.session.get(url, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(self.delay)
        return r.text

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.LIST
        sep = "&" if "?" in self.LIST else "?"
        return self.LIST + f"{sep}pageNo={page}"

    def parse_list(self, html: str) -> List[Item]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[Item] = []

        lis = soup.select("div.zp-listnavbox ul li")
        if not lis:
            lis = [li for li in soup.select("ul li")
                   if li.find("span", class_=re.compile(r"floatright.*gray"))]

        for li in lis:
            a = li.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            url = urljoin(self.BASE, a["href"].strip())
            host = urlparse(url).netloc.lower()
            if not host.endswith("mohrss.gov.cn"):
                continue

            span = li.find("span", class_=re.compile(r"floatright.*gray"))
            dt_txt = span.get_text(" ", strip=True) if span else ""
            dt = parse_dt(dt_txt)
            if REQUIRE_DATE_JOB and not dt:
                continue

            items.append(Item(title=title, url=url, dt=dt, content="", source="公共招聘网·资讯"))
        return items

    def run(self, pages: int) -> List[Item]:
        all_items: List[Item] = []
        for p in range(1, pages + 1):
            url = self._page_url(p)
            html = self._fetch(url)
            part = self.parse_list(html)
            if not part and p == 1:
                break
            all_items.extend(part)
        return all_items

# ===================== 汇总/过滤/输出 =====================
def dedup_by_url(items: List[Item]) -> List[Item]:
    seen = set(); out: List[Item] = []
    for it in items:
        if it.url and it.url not in seen:
            seen.add(it.url)
            out.append(it)
    return out

def filter_by_range(items: List[Item], start: datetime, end: datetime) -> List[Item]:
    return [it for it in items if it.dt and (start <= it.dt <= end)]

def build_markdown(items: List[Item], title_prefix: str) -> str:
    now_dt = datetime.now(TZ)
    wd = ["周一","周二","周三","周四","周五","周六","周日"][now_dt.weekday()]
    lines = [
        f"**日期：{now_dt.strftime('%Y-%m-%d')}（{wd}）**",
        "",
        f"**标题：{title_prefix}｜人社部 & 公共招聘网（固定栏目）**",
        "",
        "**主要内容**",
    ]
    if not items:
        lines.append("> 暂无更新。")
        return "\n".join(lines)
    for i, it in enumerate(items, 1):
        dt_str = it.dt.strftime("%Y-%m-%d %H:%M") if (it.dt and (it.dt.hour or it.dt.minute)) else it.dt.strftime("%Y-%m-%d")
        lines.append(f"{i}. [{it.title}]({it.url})　—　*{it.source}*　`{dt_str}`")
        lines.append("")
    return "\n".join(lines)

# ===================== 主流程 =====================
def main():
    ap = argparse.ArgumentParser(description="人社部新闻/动态 + 公共招聘网（昨日 & 列表页有日期）→ 钉钉推送")
    ap.add_argument("--pages", type=int, default=int(os.getenv("PAGES", "2")), help="每站翻页数（默认2）")
    ap.add_argument("--delay", type=float, default=float(os.getenv("DELAY", "0.8")), help="请求间隔秒（默认0.8）")
    ap.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "50")), help="展示上限（默认50）")
    ap.add_argument("--date", default=os.getenv("DATE", ""), help="指定日期（yesterday/2025-09-24）；为空启用 --auto-range")
    ap.add_argument("--auto-range", default=os.getenv("AUTO_RANGE", "true").lower()=="true",
                    action="store_true", help="启用自动范围（默认‘昨天’）")
    ap.add_argument("--window-hours", type=int, default=int(os.getenv("WINDOW_HOURS", "48")),
                    help="当不启用自动范围时使用滚动窗口小时数（默认48）")
    ap.add_argument("--no-push", action="store_true", help="只打印不推送钉钉")
    args = ap.parse_args()

    session = make_session()

    # 人社部
    mohrss_items: List[Item] = []
    for url in MOHRSS_SECTIONS:
        mohr = MohrssList(session, url, delay=args.delay)
        got = mohr.run(args.pages)
        if DEBUG:
            print(f"[DEBUG] MOHRSS: {url} → parsed {len(got)} items (before filter)")
            for x in got[:5]:
                print(f"        · {(x.dt.strftime('%Y-%m-%d') if x.dt else 'NO-DATE')} | {x.title[:50]}")
        mohrss_items.extend(got)

    # 公共招聘网
    job = JobZxss(session, delay=args.delay)
    job_items = job.run(args.pages)
    if DEBUG:
        print(f"[DEBUG] JOB.ZXSS → parsed {len(job_items)} items (before filter)")
        for x in job_items[:5]:
            print(f"        · {(x.dt.strftime('%Y-%m-%d') if x.dt else 'NO-DATE')} | {x.title[:50]}")

    all_items_raw = mohrss_items + job_items
    print(f"✅ 原始抓取 {len(all_items_raw)} 条（未去重/未过滤）")

    # 时间范围
    if args.date:
        start, end = day_range(args.date)
        title_prefix = f"{args.date} 专题"
    elif args.auto_range:
        start, end, title_prefix = auto_range()
    else:
        now = datetime.now(TZ)
        start = now - timedelta(hours=args.window_hours)
        end = now
        title_prefix = f"近{args.window_hours}小时"

    # 去重 + 只保留命中时间窗口
    all_items = dedup_by_url(all_items_raw)
    kept = filter_by_range(all_items, start, end)
    if DEBUG:
        print(f"[DEBUG] Time window: {start.strftime('%Y-%m-%d %H:%M:%S')} ~ {end.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"[DEBUG] After time-filter: {len(kept)} items")

    # 排序 + 截断
    kept.sort(key=lambda x: x.dt, reverse=True)
    show = kept[:args.limit] if args.limit and args.limit > 0 else kept

    md = build_markdown(show, title_prefix)
    print("\n--- Markdown Preview ---\n")
    print(md)

    # 落盘
    try:
        with open("hr_news.md", "w", encoding="utf-8") as f:
            f.write(md)
    except Exception as e:
        print("write md error:", e)

    if not args.no_push:
        ok = send_dingtalk_markdown(f"{title_prefix}｜人社部&公共招聘网（固定栏目）", md)
        print("钉钉推送：", "成功 ✅" if ok else "失败/未推送 ❌")

if __name__ == "__main__":
    main()
