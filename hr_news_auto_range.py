# -*- coding: utf-8 -*-
"""
hr_news_auto_range.py  （完整版 · 修复“抓不到”）

关键改动：
1) parse_dt_smart：兼容 YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD / YYYY年MM月DD日 /
   MM-DD / MM/DD / MM.DD / M月D日（可带 HH:MM）；无年份 → 结合 ref_date.year 补全年份，
   若补今年后落在未来则回退一年（解决跨年列表）。
2) “昨日兜底”：若仍未解析到日期且处于“昨日专辑”模式（传入 ref_date=昨日），
   临时赋值为“昨日 12:00”，避免被时间窗口全部刷掉。
3) 选择器做了轻量兜底；其余逻辑保持不变。

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
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
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
def parse_dt_smart(text: str, *, tz=TZ, ref_date=None) -> Optional[datetime]:
    """
    兼容：
      - YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
      - YYYY年MM月DD日
      - MM-DD / MM/DD / MM.DD
      - M月D日 / MM月DD日
      - 可选 HH:MM
    无年份 → 用 ref_date.year（若落在未来 → 回退一年）
    """
    if not text:
        return None
    s = re.sub(r"\s+", " ", text.strip())

    # 1) 带年（- / . / /）
    m = re.search(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) and m.group(5) else (12, 0)
        return datetime(y, mo, d, hh, mm, tzinfo=tz)

    # 2) 带年（中文：YYYY年MM月DD日）
    m = re.search(r"^(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s+(\d{1,2}):(\d{1,2}))?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) and m.group(5) else (12, 0)
        return datetime(y, mo, d, hh, mm, tzinfo=tz)

    # 3) 只有月日（- / . / /）
    m = re.search(r"^(\d{1,2})[-/.](\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?$", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        hh, mm = (int(m.group(3)), int(m.group(4))) if m.group(3) and m.group(4) else (12, 0)
        base = datetime.now(tz).date() if ref_date is None else ref_date
        y = base.year
        cand = datetime(y, mo, d, hh, mm, tzinfo=tz)
        if cand.date() > base:  # 跨年回退
            cand = datetime(y - 1, mo, d, hh, mm, tzinfo=tz)
        return cand

    # 4) 只有月日（中文：M月D日）
    m = re.search(r"^(\d{1,2})月(\d{1,2})日(?:\s+(\d{1,2}):(\d{1,2}))?$", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        hh, mm = (int(m.group(3)), int(m.group(4))) if m.group(3) and m.group(4) else (12, 0)
        base = datetime.now(tz).date() if ref_date is None else ref_date
        y = base.year
        cand = datetime(y, mo, d, hh, mm, tzinfo=tz)
        if cand.date() > base:
            cand = datetime(y - 1, mo, d, hh, mm, tzinfo=tz)
        return cand

    return None

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

    def __init__(self, session: requests.Session, list_url: str, delay: float = 1.0, ref_date=None):
        self.session = session
        self.list_url = list_url
        self.delay = delay
        self.ref_date = ref_date  # date

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

        # —— 常见容器：div.serviceMainListTxtCon ——
        cards = soup.select("div.serviceMainListTxtCon")
        for card in cards:
            a = card.select_one(".serviceMainListTxtLink a[href]") or card.select_one("a[href]")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            url = urljoin(self.BASE, a["href"].strip())

            # 日期：尝试多个位置 + 文本兜底
            date_el = (card.select_one(".organMenuTxtLink")
                       or card.select_one(".organGeneralNewListTxtConTime")
                       or card.select_one(".time") or card.select_one(".date"))
            dt_txt = date_el.get_text(" ", strip=True) if date_el else ""
            if not dt_txt:
                # 从整段文本里兜底抓一个“带年或月日”的片段
                m_any = re.search(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})|(\d{1,2}[-/.]\d{1,2})|(\d{1,2}月\d{1,2}日)", card.get_text(" ", strip=True))
                if m_any:
                    dt_txt = m_any.group(0)

            # —— 解析 + 昨日兜底 ——
            dt = parse_dt_smart(dt_txt, ref_date=self.ref_date)
            if not dt and self.ref_date is not None:
                try:
                    dt = datetime(self.ref_date.year, self.ref_date.month, self.ref_date.day, 12, 0, tzinfo=TZ)
                except Exception:
                    dt = None
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

            dt = parse_dt_smart(dt_txt, ref_date=self.ref_date)
            if not dt and self.ref_date is not None:
                try:
                    dt = datetime(self.ref_date.year, self.ref_date.month, self.ref_date.day, 12, 0, tzinfo=TZ)
                except Exception:
                    dt = None
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

            dt = parse_dt_smart(dt_txt, ref_date=self.ref_date)
            if not dt and self.ref_date is not None:
                try:
                    dt = datetime(self.ref_date.year, self.ref_date.month, self.ref_date.day, 12, 0, tzinfo=TZ)
                except Exception:
                    dt = None
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

    def __init__(self, session: requests.Session, delay: float = 1.0, ref_date=None):
        self.session = session
        self.delay = delay
        self.ref_date = ref_date  # date

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

            dt = parse_dt_smart(dt_txt, ref_date=self.ref_date)
            if not dt and self.ref_date is not None:
                try:
                    dt = datetime(self.ref_date.year, self.ref_date.month, self.ref_date.day, 12, 0, tzinfo=TZ)
                except Exception:
                    dt = None
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
        if it.dt:
            dt_str = it.dt.strftime("%Y-%m-%d %H:%M") if (it.dt.hour or it.dt.minute) else it.dt.strftime("%Y-%m-%d")
        else:
            dt_str = ""
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

    # 时间范围：优先 --date；否则昨日；否则滚动窗口
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

    # 解析时参考的日期（用于“只有月日”的情况补全年份更稳定）
    ref_date = start.date()

    # 人社部
    mohrss_items: List[Item] = []
    for url in MOHRSS_SECTIONS:
        mohr = MohrssList(session, url, delay=args.delay, ref_date=ref_date)
        got = mohr.run(args.pages)
        if DEBUG:
            print(f"[DEBUG] MOHRSS: {url} → parsed {len(got)} items (before filter)")
            for x in got[:5]:
                print(f"        · {(x.dt.strftime('%Y-%m-%d') if x.dt else 'NO-DATE')} | {x.title[:60]}")
        mohrss_items.extend(got)

    # 公共招聘网
    job = JobZxss(session, delay=args.delay, ref_date=ref_date)
    job_items = job.run(args.pages)
    if DEBUG:
        print(f"[DEBUG] JOB.ZXSS → parsed {len(job_items)} items (before filter)")
        for x in job_items[:5]:
            print(f"        · {(x.dt.strftime('%Y-%m-%d') if x.dt else 'NO-DATE')} | {x.title[:60]}")

    all_items_raw = mohrss_items + job_items
    print(f"✅ 原始抓取 {len(all_items_raw)} 条（未去重/未过滤）")

    # 去重 + 命中时间窗口
    all_items = dedup_by_url(all_items_raw)
    kept = filter_by_range(all_items, start, end)
    if DEBUG:
        print(f"[DEBUG] Time window: {start.strftime('%Y-%m-%d %H:%M:%S')} ~ {end.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"[DEBUG] After time-filter: {len(kept)} items")

    # 排序 + 截断
    kept.sort(key=lambda x: (x.dt or datetime(1970,1,1, tzinfo=TZ)), reverse=True)
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
