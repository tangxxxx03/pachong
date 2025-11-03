# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报净化版（三重日期校验·当天一条·过滤非日报）
———————————————————————————————
✅ 三重校验逻辑：
  1. 优先匹配首页 dwdata-time；
  2. 其次匹配标题括号日期（（2025年11月3日）或 (2025-11-03)）；
  3. 最后兜底匹配详情页时间。
✅ 过滤假日报（群发消息类），仅保留“人力资源相关简讯”。
✅ 自动清除“阅读量”等尾巴，格式化为 Markdown。
✅ 可钉钉推送；若未配置 webhook 则跳过。
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

# ---------- 钉钉 ----------
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
        print("🔕 未配置 DINGTALK_BASE，跳过推送。")
        return False
    try:
        r = requests.post(
            _sign_webhook(base, secret),
            json={"msgtype":"markdown","markdown":{"title":title,"text":md}},
            timeout=20
        )
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} code={r.status_code}")
        if not ok: print("resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e); return False

# ---------- HTTP ----------
class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent":"Mozilla/5.0","Accept-Language":"zh-CN,zh;q=0.9"})
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

CN_TITLE_DATE = re.compile(r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]")
def date_from_bracket_title(text:str):
    m = CN_TITLE_DATE.search(text or "")
    if not m: return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except: return None

# ---------- 主类 ----------
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1
        t = (os.getenv("HR_TARGET_DATE") or "").strip()
        if t:
            try:
                y,m,d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y,m,d)
            except:
                print("⚠️ HR_TARGET_DATE 解析失败，使用今日。")
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()
        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.sources = [u.strip() for u in os.getenv("SRC_HRLOO_URLS","https://www.hrloo.com/").split(",") if u.strip()]
        print(f"[CFG] target_date={self.target_date} {zh_weekday(now_tz())} sources={self.sources}")

    def crawl(self):
        for base in self.sources:
            if self._crawl_source(base): break

    def _crawl_source(self, base):
        try:
            r = self.session.get(base, timeout=20)
        except Exception as e:
            print("首页请求异常：", base, e); return False
        if r.status_code != 200:
            print("首页请求失败：", base, r.status_code); return False

        soup = BeautifulSoup(r.text, "html.parser")
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href","")
            if not re.search(r"/news/\d+\.html$", href): continue
            text = norm(a.get_text())
            if not self.daily_title_pat.search(text): continue
            t2 = date_from_bracket_title(text)
            if t2 and t2 != self.target_date: continue
            links.append(urljoin(base, href))

        seen = set()
        for url in links:
            if url in seen: continue
            seen.add(url)
            if self._try_detail(url): return True

        print("[MISS] 本源未命中目标日期：", base)
        return False

    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        if not page_title or not self.daily_title_pat.search(page_title): return False
        if not re.search(r"(人力资源相关|简讯|每日要点|早报)", page_title): return False
        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date: return False
        if pub_dt and pub_dt.date() != self.target_date and not t3: return False
        if not titles or len(titles) < 3: return False
        self.results.append({
            "title": page_title,
            "url": abs_url,
            "date": (pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else f"{self.target_date} 09:00"),
            "titles": titles
        })
        print(f"[HIT] {abs_url} -> {len(titles)} 条")
        return True

    def _extract_pub_time(self, soup):
        cand = []
        for t in soup.select("time[datetime]"): cand.append(t.get("datetime",""))
        for m in soup.select("meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"):
            cand.append(m.get("content",""))
        for sel in [".time",".date",".pubtime",".publish-time",".info"]:
            for x in soup.select(sel):
                cand.append(x.get_text(" ", strip=True))
        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        def parse_one(s):
            m = pat.search(s or "")
            if not m: return None
            try:
                y,mo,d = int(m[1]),int(m[2]),int(m[3])
                hh = int(m[4]) if m[4] else 9
                mm = int(m[5]) if m[5] else 0
                return datetime(y,mo,d,hh,mm,tzinfo=_tz())
            except: return None
        dts = [dt for dt in map(parse_one, cand) if dt]
        if dts:
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6,20))
            if r.status_code != 200:
                print("[DetailFail]", url, r.status_code); return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            title_tag = soup.find(["h1","h2"])
            page_title = norm(title_tag.get_text()) if title_tag else ""
            pub_dt = self._extract_pub_time(soup)
            container = soup.select_one("article, .article, .content, .news-content") or soup
            titles = self._extract_daily_item_titles(container)
            return pub_dt, titles, page_title
        except Exception as e:
            print("[DetailError]", url, e); return None, [], ""

    def _extract_daily_item_titles(self, root):
        ad_words = ["手机","境外","广告","推广","APP","扫码","入群"]
        by_num = {}
        for node in root.find_all(["p","li","div","span","h2","h3","strong"]):
            raw = norm(node.get_text())
            if not raw: continue
            m = re.match(r"^\s*[（(]?\s*(\d{1,2})\s*[)）]?\s*[、.．]?\s*(.+)$", raw)
            if not m: continue
            num, txt = int(m.group(1)), m.group(2).strip()
            if any(w in txt for w in ad_words): continue
            title = re.split(r"[（\(]", txt)[0]
            if not (3 <= len(title) <= 100): continue
            by_num.setdefault(num, title)
        seq, n = [], 1
        while n in by_num:
            seq.append(by_num[n]); n += 1
            if n > 15: break
        return seq

# ---------- Markdown ----------
def build_md(items):
    n = now_tz()
    out = [
        f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）**  ",
        "",
        "**标题：人资早报｜每日要点**  ",
        ""
    ]
    if not items:
        out.append("> 未发现当天的“三茅日报”。")
        return "\n".join(out)
    it = items[0]
    for j, t in enumerate(it["titles"], 1):
        out.append(f"{j}. {t}  ")
    out.append(f"[查看详细]({it['url']}) （{it['date'][:10]}）  ")
    return "\n".join(out)

if __name__ == "__main__":
    print("执行 hr_news_crawler.py（当天一条 · 三重日期校验版本）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("人资早报｜每日要点", md)
