# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）爬虫 · 仅提取小标题（24小时内）
- 自动识别发布时间，仅抓取24小时内发布的新闻
- 从每篇文章中提取分节标题（1、2、3…），不抓正文
- 可选钉钉推送
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup, Comment
from urllib.parse import urljoin
from datetime import datetime, timedelta
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo


# ========= 基础工具 =========
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def now_tz(): return datetime.now(ZoneInfo("Asia/Shanghai"))
def within_24h(dt): return (now_tz() - dt).total_seconds() <= 86400 if dt else False


# ========= 钉钉 =========
def _sign_webhook(base, secret):
    if not base or not secret: return ""
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    h = hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title, md):
    base = os.getenv("DINGTALK_BASEA") or os.getenv("DINGTALK_BASE")
    secret = os.getenv("DINGTALK_SECRETA") or os.getenv("DINGTALK_SECRET")
    if not base or "REPLACE_ME" in base:
        print("🔕 未配置钉钉 Webhook，跳过推送。")
        return False
    webhook = _sign_webhook(base, secret)
    r = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"title": title, "text": md}}, timeout=20)
    ok = (r.status_code == 200 and r.json().get("errcode") == 0)
    print("DingTalk:", ok)
    return ok


# ========= 网络请求 =========
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s


# ========= 主爬虫 =========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 15
        self.detail_timeout = (6, 20)
        self.detail_sleep = 0.6

    def crawl(self):
        base = "https://www.hrloo.com/"
        r = self.session.get(base, timeout=20)
        if r.status_code != 200:
            print("首页请求失败", r.status_code)
            return
        soup = BeautifulSoup(r.text, "html.parser")
        links = [urljoin(base, a.get("href")) for a in soup.select("a[href*='/news/']") if re.search(r"/news/\d+\.html$", a.get("href",""))]
        seen = set()
        for url in links:
            if url in seen: continue
            seen.add(url)
            pub_dt, titles, main_title = self._fetch_detail_24h_titles(url)
            if not pub_dt or not within_24h(pub_dt): continue
            if not titles: continue
            self.results.append({"title": main_title or url, "url": url, "date": pub_dt.strftime("%Y-%m-%d %H:%M"), "titles": titles})
            print(f"[OK] {url} {pub_dt} 小标题{len(titles)}个")
            if len(self.results) >= self.max_items: break
            time.sleep(self.detail_sleep)

    def _fetch_detail_24h_titles(self, url):
        try:
            r = self.session.get(url, timeout=self.detail_timeout)
            if r.status_code != 200: return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # 标题
            h = soup.find(["h1", "h2"])
            page_title = norm(h.get_text()) if h else ""

            # 发布时间
            pub_dt = self._extract_pub_time(soup)

            # 小标题 strong/h2/h3/span.bjh-p
            titles = []
            for t in soup.find_all(["strong","h2","h3","span","p"]):
                text = norm(t.get_text())
                if re.match(r"^\d+\s*[、.．]\s*.+", text) and text not in titles:
                    titles.append(text)
            return pub_dt, titles, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    def _extract_pub_time(self, soup):
        tz = ZoneInfo("Asia/Shanghai")
        txt = soup.get_text(" ")
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?", txt)
        if not m: return None
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        hh = int(m[4]) if m[4] else 9
        mm = int(m[5]) if m[5] else 0
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=tz)
        except: return None


# ========= 输出 =========
def build_md(items):
    now = now_tz()
    out = [f"**日期：{now.strftime('%Y-%m-%d')}（{zh_weekday(now)}）**", "", "**标题：早安资讯｜人力资源24小时内新闻**", "", "**主要内容**"]
    if not items:
        out.append("> 24小时内无内容。")
        return "\n".join(out)
    for i, it in enumerate(items, 1):
        out.append(f"{i}. [{it['title']}]({it['url']}) （{it['date']}）")
        for s in it['titles']:
            out.append(f"> 🟦 {s}")
        out.append("")
    return "\n".join(out)


# ========= 主入口 =========
if __name__ == "__main__":
    print("执行 hr_news_crawler.py（24小时内小标题版）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("早安资讯｜三茅24小时新闻", md)
