# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报专抓版
功能：
  1) 仅抓取标题包含「三茅日报/三茅日報」的新闻；
  2) 从正文中抽取“1、… 2、…”等编号条目的【标题】；
  3) 仅保留当天（HR_ONLY_TODAY=1）或近24h（默认）；
  4) 生成 Markdown，并推送到钉钉机器人（可选）。

环境变量（可选）：
  HR_TZ=Asia/Shanghai
  HR_ONLY_TODAY=1/0   # 1=只要当天；0=近24小时（默认）
  HR_MAX_ITEMS=15
  SRC_HRLOO_URLS=https://www.hrloo.com/   # 可扩展多个，以逗号分隔
  DINGTALK_BASE / DINGTALK_SECRET         # 标准变量名
  DINGTALK_BASEA / DINGTALK_SECRETA       # 兼容你的 Actions 配置
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ====== 时区与时间 ======
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo  # 仅旧版 Python 需要

def tz():
    return ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai"))

def now_tz():
    return datetime.now(tz())

def norm(s): 
    return re.sub(r"\s+", " ", (s or "").strip())

def zh_weekday(dt):
    return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

def within_24h(dt):
    if not dt: return False
    return (now_tz() - dt).total_seconds() <= 86400

def same_day(dt):
    if not dt: return False
    n = now_tz().date()
    return dt.astimezone(tz()).date() == n

# ====== 钉钉推送 ======
def _sign_webhook(base, secret):
    if not base: return ""
    if not secret: return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    h = hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def _mask(v: str, head=6, tail=6):
    if not v: return ""
    if len(v) <= head + tail: return v
    return v[:head] + "..." + v[-tail:]

def send_dingtalk_markdown(title, md):
    # 兼容两种变量名
    base = os.getenv("DINGTALK_BASE") or os.getenv("DINGTALK_BASEA")
    secret = os.getenv("DINGTALK_SECRET") or os.getenv("DINGTALK_SECRETA")
    if not base:
        print("🔕 未配置 DINGTALK_BASE/BASEA，跳过推送。")
        return False
    webhook = _sign_webhook(base, secret)
    try:
        r = requests.post(
            webhook,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": md}},
            timeout=20,
        )
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} base={_mask(base)} http={r.status_code}")
        if not ok:
            print("DingTalk resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

# ====== 网络会话 ======
class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

# ====== 爬虫主体 ======
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = int(os.getenv("HR_MAX_ITEMS", "15") or "15")
        self.detail_timeout = (6, 20)
        self.detail_sleep = 0.6

        # 标题必须包含“三茅日报/三茅日報”
        self.daily_title_pat = re.compile(r"三茅日[报報]")

        # 抓取入口（可逗号分隔多个）
        src = os.getenv("SRC_HRLOO_URLS", "https://www.hrloo.com/").strip()
        self.sources = [u.strip() for u in src.split(",") if u.strip()]

        # 时间策略：当天 or 近24h
        self.only_today = (os.getenv("HR_ONLY_TODAY", "0") == "1")

    def crawl(self):
        for base in self.sources:
            try:
                self._crawl_source(base)
            except Exception as e:
                print(f"[SourceError] {base} -> {e}")

    def _crawl_source(self, base):
        r = self.session.get(base, timeout=20)
        if r.status_code != 200:
            print("首页请求失败", base, r.status_code); 
            return
        soup = BeautifulSoup(r.text, "html.parser")

        # 优先：a 文本里就包含“三茅日报”
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href", "")
            text = norm(a.get_text())
            if not re.search(r"/news/\d+\.html$", href):
                continue
            if self.daily_title_pat.search(text or ""):
                links.append(urljoin(base, href))

        # 兜底：如果首页 a 文本没有明确包含，退回到全部 news 链接，去详情页二次判定
        if not links:
            links = [urljoin(base, a.get("href"))
                     for a in soup.select("a[href*='/news/']")
                     if re.search(r"/news/\d+\.html$", a.get("href",""))]

        seen = set()
        for url in links:
            if url in seen:
                continue
            seen.add(url)

            pub_dt, item_titles, main_title = self._fetch_detail_clean(url)

            # 必须是“三茅日报”
            if not main_title or not self.daily_title_pat.search(main_title):
                continue

            # 时间过滤
            if self.only_today:
                if not same_day(pub_dt):
                    continue
            else:
                if not within_24h(pub_dt):
                    continue

            if not item_titles:
                continue

            self.results.append({
                "title": norm(main_title),
                "url": url,
                "date": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "",
                "titles": item_titles
            })
            print(f"[OK] {url} {pub_dt} 条目{len(item_titles)}个")
            if len(self.results) >= self.max_items:
                break
            time.sleep(self.detail_sleep)

    # —— 明细页抽取（日报标题 + 发布时间 + 条目标题列表） —— #
    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=self.detail_timeout)
            if r.status_code != 200:
                return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # 主标题
            h = soup.find(["h1","h2"])
            page_title = norm(h.get_text()) if h else ""

            # 发布时间
            pub_dt = self._extract_pub_time(soup)

            # 抽“编号条目”的标题
            item_titles = self._extract_daily_item_titles(soup)

            return pub_dt, item_titles, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    def _extract_pub_time(self, soup):
        txt = soup.get_text(" ")
        # 匹配：2025-10-29 08:53 或 2025年10月29日 08:53
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?", txt)
        if not m: 
            return None
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        hh = int(m[4]) if m[4] else 9
        mm = int(m[5]) if m[5] else 0
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=tz())
        except:
            return None

    # —— 从日报正文提取“1、… 2、…”的条目标题 —— #
    def _extract_daily_item_titles(self, soup):
        items = []
        # 常见容器标签里找文本
        for t in soup.find_all(["h2","h3","h4","strong","b","p","li","span","div"]):
            raw = (t.get_text() or "").strip()
            if not raw:
                continue
            # 允许的编号样式：1、xxx / 1. xxx / 1．xxx / （1）xxx / (1) xxx
            m = re.match(r"^\s*(?:（?\(?\s*\d+\s*\)?）?)\s*[、.．]?\s*(.+)", raw)
            if not m:
                continue
            title = m.group(1).strip()

            # 切掉可能跟着的解释/冒号后缀等，只留“标题短语”
            title = re.split(r"[：:。]|（|（图|（详见|—|--|-{2,}", title)[0].strip()

            # 过滤噪声与异常长度
            if not (4 <= len(title) <= 60):
                continue
            # 中文占比（避免纯数字/链接噪声）
            zh_ratio = len(re.findall(r"[\u4e00-\u9fa5]", title)) / max(len(title), 1)
            if zh_ratio < 0.3:
                continue

            items.append(title)

        # 去重保序
        seen = set()
        uniq = []
        for x in items:
            k = x.replace(" ", "").lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(x)

        # 限制数量
        return uniq[:15]

# ====== Markdown 输出 ======
def build_md(items):
    n = now_tz()
    out = []
    out.append(f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）**  ")
    out.append("")
    out.append("**标题：每日资讯｜人力资源相关资讯**  ")
    out.append("")
    if not items:
        out.append("> 指定时间范围内未发现新的“三茅日报”。")
        return "\n".join(out)

    for i, it in enumerate(items, 1):
        out.append(f"{i}. [{it['title']}]({it['url']}) （{it['date']}）  ")
        for j, t in enumerate(it['titles'], 1):
            out.append(f"> {j}. {t}  ")
        out.append("")
    return "\n".join(out)

# ====== 主入口 ======
if __name__ == "__main__":
    print("执行 hr_news_crawler_daily_only.py（只抓“三茅日报”条目标题）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("每日资讯｜人力资源相关资讯", md)
