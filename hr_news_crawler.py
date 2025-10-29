# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报专抓 · 条目聚合净化版

功能：
  1) 仅抓取标题包含「三茅日报/三茅日報」的新闻；
  2) 在正文容器中提取编号条目标题（1、2、3、…），按编号聚合，避免被“28日，据报道…”误判；
  3) 修复“：”后的标题被截断问题，只去括注；
  4) 排除右栏/站务噪声；输出 Markdown，可推送钉钉。

环境变量（可选）：
  HR_ONLY_TODAY=1           # 仅当天（否则近24小时）
  HR_MAX_ITEMS=15
  SRC_HRLOO_URLS=https://www.hrloo.com/   # 可逗号分隔多个入口页
  DINGTALK_BASE / DINGTALK_SECRET
  DINGTALK_BASEA / DINGTALK_SECRETA
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ========= 基本工具 =========
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo  # 旧版 Python 兼容

def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def within_24h(dt): return (now_tz() - dt).total_seconds() <= 86400 if dt else False
def same_day(dt): return bool(dt) and dt.astimezone(_tz()).date() == now_tz().date()

# ========= 钉钉推送 =========
def _sign_webhook(base, secret):
    if not base: return ""
    if not secret: return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title, md):
    base = os.getenv("DINGTALK_BASE") or os.getenv("DINGTALK_BASEA")
    secret = os.getenv("DINGTALK_SECRET") or os.getenv("DINGTALK_SECRETA")
    if not base:
        print("🔕 未配置 DINGTALK_BASE/BASEA，跳过推送。"); return False
    try:
        r = requests.post(_sign_webhook(base, secret),
                          json={"msgtype":"markdown","markdown":{"title":title,"text":md}}, timeout=20)
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} code={r.status_code}")
        if not ok: print("resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e); return False

# ========= 网络会话 =========
class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0 Safari/537.36",
                      "Accept-Language":"zh-CN,zh;q=0.9,en;q=0.8"})
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

# ========= 主爬虫 =========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = int(os.getenv("HR_MAX_ITEMS","15") or "15")
        self.detail_timeout = (6, 20)
        self.detail_sleep = 0.6
        self.only_today = (os.getenv("HR_ONLY_TODAY","0") == "1")
        self.daily_title_pat = re.compile(r"三茅日[报報]")

        src = os.getenv("SRC_HRLOO_URLS","https://www.hrloo.com/").strip()
        self.sources = [u.strip() for u in src.split(",") if u.strip()]

    def crawl(self):
        for base in self.sources:
            try:
                self._crawl_source(base)
            except Exception as e:
                print(f"[SourceError] {base} -> {e}")

    def _crawl_source(self, base):
        r = self.session.get(base, timeout=20)
        if r.status_code != 200:
            print("首页请求失败", base, r.status_code); return
        soup = BeautifulSoup(r.text, "html.parser")

        # 优先：a 文本包含“三茅日报”
        links = []
        for a in soup.select("a[href*='/news/']"):
            href, text = a.get("href",""), norm(a.get_text())
            if re.search(r"/news/\d+\.html$", href) and self.daily_title_pat.search(text):
                links.append(urljoin(base, href))

        # 兜底：抓全部 news 链接，进详情再判定
        if not links:
            links = [urljoin(base, a.get("href"))
                     for a in soup.select("a[href*='/news/']")
                     if re.search(r"/news/\d+\.html$", a.get("href",""))]

        seen = set()
        for url in links:
            if url in seen: continue
            seen.add(url)

            pub_dt, item_titles, main_title = self._fetch_detail_clean(url)
            if not main_title or not self.daily_title_pat.search(main_title): continue

            # 时间过滤
            if self.only_today:
                if not same_day(pub_dt): continue
            else:
                if not within_24h(pub_dt): continue

            if not item_titles: continue

            self.results.append({
                "title": norm(main_title),
                "url": url,
                "date": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "",
                "titles": item_titles
            })
            print(f"[OK] {url} -> {len(item_titles)} 条")
            if len(self.results) >= self.max_items: break
            time.sleep(self.detail_sleep)

    # —— 明细页抽取（正文容器 + 条目聚合） —— #
    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=self.detail_timeout)
            if r.status_code != 200: return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # 标题 & 发布时间
            h = soup.find(["h1","h2"])
            page_title = norm(h.get_text()) if h else ""
            pub_dt = self._extract_pub_time(soup)

            # 仅在正文容器内抽取（命中其一即可）
            container = soup.select_one(
                "article, .article, .article-content, .detail-content, .news-content, .content, .post-content"
            ) or soup

            item_titles = self._extract_daily_item_titles(container)
            return pub_dt, item_titles, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    def _extract_pub_time(self, soup):
        txt = soup.get_text(" ")
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?", txt)
        if not m: return None
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        hh = int(m[4]) if m[4] else 9
        mm = int(m[5]) if m[5] else 0
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=_tz())
        except:
            return None

    # —— 核心：提取并聚合“1、2、3、…”的条目标题 —— #
    def _extract_daily_item_titles(self, root):
        # 站务/右栏噪声黑词
        blacklist = ["粉丝","入群","申诉","短信","验证码","举报","审核","发布协议",
                     "账号","登录","APP","创建申请","广告","下载","推广","礼包","网盘"]

        # 1) 收集候选：只接受 1..9 的编号；过滤“28日，据报道…”这类日期型误判
        by_num = {}  # num -> 首次出现的标题
        for t in root.find_all(["h2","h3","h4","strong","b","p","li","span","div"]):
            raw = (t.get_text() or "").strip()
            if not raw:
                continue
            m = re.match(r"^\s*[（(]?\s*(\d{1,2})\s*[)）]?\s*[、.．]?\s*(.+)$", raw)
            if not m:
                continue

            num = int(m.group(1))
            txt = m.group(2).strip()

            # —— 日期型误判：如“28日，”“28日 ”，直接跳过
            if num >= 10 or txt.startswith("日，") or txt.startswith("日 "):
                continue

            # 标题保留冒号后的主体，只去掉后续括注/尾部说明
            title = re.split(r"[（\(]{1}", txt)[0].strip()

            # 基础过滤
            if not (1 <= num <= 9):    # 日报通常 1~5/1~9
                continue
            if not (4 <= len(title) <= 60):
                continue
            zh_ratio = len(re.findall(r"[\u4e00-\u9fa5]", title)) / max(len(title), 1)
            if zh_ratio < 0.3:
                continue
            if any(k in title for k in blacklist):
                continue

            by_num.setdefault(num, title)  # 同一编号只取第一次

        # 2) 从 1 开始按编号聚合，遇缺位即停止（保证连续 1→2→…）
        seq = []
        n = 1
        while n in by_num:
            seq.append(by_num[n])
            n += 1
            if n > 20:
                break
        return seq[:15]

# ========= Markdown 输出 =========
def build_md(items):
    n = now_tz()
    out = []
    out.append(f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）**  ")
    out.append("")
    out.append("**标题：「人资早报｜每日要点」**  ")
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

# ========= 主入口 =========
if __name__ == "__main__":
    print("执行 hr_news_crawler_daily_clean.py（只抓三茅日报 · 条目聚合净化版）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("「人资早报｜每日要点」", md)
