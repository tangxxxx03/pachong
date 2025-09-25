# -*- coding: utf-8 -*-
"""
hr_news_auto_range.py
抓取两个固定来源（无需关键词）：
  1) 人社部新闻/动态栏目：部内要闻、人才新闻（人社新闻）与地方动态（可扩展）
  2) 中国公共招聘网：资讯列表首页（主列表，右侧有日期）

时间策略（已按需固定）：
  - 恒定抓“昨天” 00:00~23:59（Asia/Shanghai）

规则（更严格）：
  - 仅接收“列表页能直接取到的日期”项；无日期一律丢弃
  - 公共招聘网只解析主列表 li（右侧 YYYY-MM-DD）
  - 人社部支持 table / ul.li / 新闻频道常见的 div 行块结构

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

# —— 人社部栏目（可按需增删）——
MOHRSS_DFDT = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/"          # 地方动态
MOHRSS_RSXW = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/rsxw/"          # 人社新闻（人才新闻/人社新闻）
MOHRSS_BNYW = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/buneiyaowen/"   # 部内要闻（你截图的这类）
MOHRSS_SECTIONS = [MOHRSS_BNYW, MOHRSS_RSXW, MOHRSS_DFDT]

# —— 公共招聘网 ——（主列表）
JOB_ZXSS = "http://job.mohrss.gov.cn/zxss/index.jhtml"

# —— 强制“必须有日期” —— 
REQUIRE_DATE_MOHRSS = True
REQUIRE_DATE_JOB = True

# —— 钉钉（A 变量名；可用 Secrets 覆盖）——
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
    s.trust_env = False  # 避免 runner 代理干扰
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
DATE_PATS = [
    r"(20\d{2})[^\d](\d{1,2})[^\d](\d{1,2})\s+(\d{1,2}):(\d{1,2})",  # 2025-09-16 08:30
    r"(20\d{2})[^\d](\d{1,2})[^\d](\d{1,2})",                        # 2025-09-16
    r"^(\d{4})-(\d{1,2})-(\d{1,2})$",                                # 严格 YYYY-MM-DD
]

def parse_dt(text: str) -> Optional[datetime]:
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip())
    # 严格 YYYY-MM-DD 优先
    m0 = re.search(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m0:
        y, mo, d = map(int, m0.groups())
        return datetime(y, mo, d, 12, 0, tzinfo=TZ)
    for pat in DATE_PATS:
        m = re.search(pat, t)
        if not m:
            continue
        if len(m.groups()) == 5:
            y, mo, d, hh, mm = map(int, m.groups())
            return datetime(y, mo, d, hh, mm, tzinfo=TZ)
        if len(m.groups()) == 3:
            y, mo, d = map(int, m.groups())
            return datetime(y, mo, d, 12, 0, tzinfo=TZ)
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

        # (A) 新闻频道常见的 div 行块结构：标题 a，同行/右侧 span 含 YYYY-MM-DD
        blocks = soup.select(
            "div.serviceMainList, div.serviceMainListTxt, "
            "div.organGeneralNewListTxtConTime, div.serviceMainListCon, "
            "div.list, div.listCon"
        )
        for blk in blocks:
            a = blk.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            if not title:
                continue
            url = urljoin(self.BASE, a["href"].strip())

            neighbor_txt = " ".join(el.get_text(" ", strip=True) for el in [blk, blk.parent] if el)
            m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", neighbor_txt)
            dt = None
            if m:
                y, mo, d = map(int, m.groups())
                dt = datetime(y, mo, d, 12, 0, tzinfo=TZ)

            if REQUIRE_DATE_MOHRSS and not dt:
                continue
            items.append(Item(title=title, url=url, dt=dt, content="", source="人社部·新闻/动态"))

        if items:
            return items

        # (B) table 行结构
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
            items.append(Item(title=title, url=url, dt=dt, content="", source="人社部·列表(table)"))

        if items:
            return items

        # (C) ul/li 结构
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
            items.append(Item(title=title, url=url, dt=dt, content="", source="人社部·列表(ul)"))

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
        """
        只解析主列表：div.zp-listnavbox ul li
        结构：<a href>标题</a><span class="floatright font12 gray">YYYY-MM-DD</span>
        必须命中日期；不再去详情页补时间；过滤非 mohrss 域名。
        """
        soup = BeautifulSoup(html, "html.parser")
        items: List[Item] = []

        lis = soup.select("div.zp-listnavbox ul li")
        if not lis:
            # 宽松兜底：任何 li 里含右侧日期 span 的
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
        # 列表页多为 YYYY-MM-DD；若没有时分，显示日期即可
        dt_str = it.dt.strftime("%Y-%m-%d %H:%M") if (it.dt and (it.dt.hour or it.dt.minute)) else it.dt.strftime("%Y-%m-%d")
        line = f"{i}. [{it.title}]({it.url})　—　*{it.source}*　`{dt_str}`"
        lines.append(line)
        lines.append("")
    return "\n".join(lines)

# ===================== 主流程 =====================
def main():
    ap = argparse.ArgumentParser(description="人社部新闻/动态 + 公共招聘网资讯（固定栏目，昨日 & 必须有日期）→ 钉钉推送")
    ap.add_argument("--pages", type=int, default=int(os.getenv("PAGES", "2")), help="每站翻页数（默认2）")
    ap.add_argument("--delay", type=float, default=float(os.getenv("DELAY", "0.8")), help="请求间隔秒（默认0.8）")
    ap.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "50")), help="展示上限（默认50）")
    ap.add_argument("--date", default=os.getenv("DATE", ""), help="指定日期（yesterday/2025-09-17）；为空启用 --auto-range")
    ap.add_argument("--auto-range", default=os.getenv("AUTO_RANGE", "true").lower()=="true",
                    action="store_true", help="启用自动范围（默认开，恒定昨天）")
    ap.add_argument("--window-hours", type=int, default=int(os.getenv("WINDOW_HOURS", "48")),
                    help="当不启用自动范围时使用的滚动窗口小时数（默认48）")
    ap.add_argument("--no-push", action="store_true", help="只打印不推送钉钉")
    args = ap.parse_args()

    session = make_session()

    # 人社部
    mohrss_items: List[Item] = []
    for url in MOHRSS_SECTIONS:
        mohr = MohrssList(session, url, delay=args.delay)
        mohrss_items.extend(mohr.run(args.pages))

    # 公共招聘网
    job = JobZxss(session, delay=args.delay)
    job_items = job.run(args.pages)

    all_items = dedup_by_url(mohrss_items + job_items)

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

    # 只保留命中时间窗口的项
    all_items = filter_by_range(all_items, start, end)

    # 排序 + 截断
    all_items.sort(key=lambda x: x.dt, reverse=True)
    show = all_items[:args.limit] if args.limit and args.limit > 0 else all_items

    print(f"✅ 原始抓取 {len(mohrss_items)+len(job_items)} 条；去重后 {len(all_items)} 条；展示 {len(show)} 条。")
    md = build_markdown(show, title_prefix)
    print("\n--- Markdown Preview ---\n")
    print(md)

    # 落盘（便于 artifacts 下载）
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
