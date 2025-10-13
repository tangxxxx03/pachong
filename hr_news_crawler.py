# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）专抓版 · 仅提取小标题
- 仅抓取 HRLoo，支持当天过滤、关键词过滤；
- 进入详情页只提取分节标题（如“1、xxx / 2、xxx”），不抓正文、不抓摘要；
- 兼容钉钉推送。
"""

import os
import re
import time
import hmac
import base64
import hashlib
import urllib.parse
import requests
from bs4 import BeautifulSoup, Comment
from urllib.parse import urljoin
from datetime import datetime
import ssl
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo


# ========= 基础工具 =========
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def now_tz(): return datetime.now(ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai")))


# ========= 钉钉 =========
def _sign_webhook(base, secret):
    if not base or "REPLACE_ME" in base:
        return ""
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    h = hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title, md):
    base = os.getenv("DINGTALK_BASEA")
    secret = os.getenv("DINGTALK_SECRETA")
    webhook = _sign_webhook(base, secret)
    if not webhook:
        print("🔕 未配置钉钉 Webhook，跳过推送。")
        return False
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md}}
    r = requests.post(webhook, json=payload, timeout=20)
    ok = (r.status_code == 200 and r.json().get("errcode") == 0)
    print("DingTalk:", ok)
    return ok


# ========= 网络请求 =========
class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kw)

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    })
    retry = Retry(total=3, backoff_factor=0.8, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=retry))
    return s


# ========= 主爬虫 =========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = int(os.getenv("HR_MAX_ITEMS", "10"))
        self.only_today = os.getenv("HR_ONLY_TODAY", "1") in ("1","true")
        self.detail_timeout = (6, 20)
        self.detail_sleep = 0.6

    def crawl_hrloo(self):
        url = "https://www.hrloo.com/"
        self._crawl_list("三茅人力资源网", url)

    def _crawl_list(self, source, base_url):
        print("开始抓取三茅列表...")
        r = self.session.get(base_url, timeout=20)
        if r.status_code != 200: return
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.select("a[href*='/news/']")
        added = set()

        for a in links:
            href = a.get("href")
            if not href or href in added: continue
            full = urljoin(base_url, href)
            if not re.search(r"/news/\d+\.html", full): continue
            added.add(full)
            title = norm(a.get_text())
            subs = self._fetch_titles(full)
            if not subs: continue
            self.results.append({"title": title, "url": full, "source": source, "subtitles": subs})
            if len(self.results) >= self.max_items: break
            time.sleep(self.detail_sleep)

    def _fetch_titles(self, url):
        """仅抓详情页小标题 strong / h2 / h3 / span.bjh-p"""
        try:
            r = self.session.get(url, timeout=self.detail_timeout)
            if r.status_code != 200: return []
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # 清除无用节点
            for t in soup(["script","style","footer","header","nav","iframe"]): t.decompose()
            for c in soup.find_all(string=lambda t:isinstance(t, Comment)): c.extract()

            # 小标题识别
            subs = []
            for tag in soup.find_all(["strong","h2","h3","span"], class_=lambda c: c in (None, "bjh-p")):
                txt = norm(tag.get_text())
                if re.match(r"^\d+\s*[、.．]\s*.+", txt) and txt not in subs:
                    subs.append(txt)
            return subs
        except Exception as e:
            print("detail error", e)
            return []


# ========= Markdown 输出 =========
def build_md(items):
    now = now_tz()
    out = [f"**日期：{now.strftime('%Y-%m-%d')}（{zh_weekday(now)}）**", "", "**标题：早安资讯｜人力资源每日资讯推送**", "", "**主要内容**"]
    if not items:
        out.append("> 暂无更新。")
        return "\n".join(out)

    for i, it in enumerate(items, 1):
        out.append(f"{i}. [{it['title']}]({it['url']})　—　*{it['source']}*")
        for st in it["subtitles"]:
            out.append(f"> 🟦 {st}")
        out.append("")
    return "\n".join(out)


# ========= 主程序 =========
if __name__ == "__main__":
    print("执行 hr_news_crawler.py")
    crawler = HRLooCrawler()
    crawler.crawl_hrloo()
    md = build_md(crawler.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("早安资讯｜三茅小标题抓取", md)
