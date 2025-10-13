# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）专抓版 · 仅提取小标题 · 仅抓“昨天”的文章
- 进入每篇 /news/xxxx.html 详情页，只抽取分节标题（1、2、3…），不抓正文/摘要
- 自动识别发布时间，**仅保留“昨天”的文章**（以 Asia/Shanghai 为准）
- 支持钉钉 Markdown 推送（读取 DINGTALK_BASEA / DINGTALK_SECRETA 或 DINGTALK_BASE / DINGTALK_SECRET）
"""

import os
import re
import time
import hmac
import ssl
import base64
import hashlib
import urllib.parse
import requests
from bs4 import BeautifulSoup, Comment
from urllib.parse import urljoin
from datetime import datetime, timedelta
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


# =============== 基础工具 ===============
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def zh_weekday(dt: datetime) -> str:
    return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

def now_tz() -> datetime:
    return datetime.now(ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai")))

def is_yesterday(dt: datetime) -> bool:
    if not dt:
        return False
    y = (now_tz() - timedelta(days=1)).date()
    return dt.date() == y


# =============== 钉钉 ===============
def _sign_webhook(base: str, secret: str) -> str:
    if not base or not secret:
        return ""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title: str, md_text: str) -> bool:
    base = (os.getenv("DINGTALK_BASEA") or os.getenv("DINGTALK_BASE") or "").strip()
    secret = (os.getenv("DINGTALK_SECRETA") or os.getenv("DINGTALK_SECRET") or "").strip()
    webhook = _sign_webhook(base, secret)
    if not webhook:
        print("🔕 未配置钉钉 Webhook，跳过推送。")
        return False
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    r = requests.post(webhook, json=payload, timeout=20)
    ok = (r.status_code == 200 and r.json().get("errcode") == 0)
    print("DingTalk resp:", r.status_code, r.text[:200])
    return ok


# =============== 网络请求 ===============
class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    retries = Retry(total=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", LegacyTLSAdapter(max_retries=retries))
    return s


# =============== 爬虫 ===============
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = int(os.getenv("HR_MAX_ITEMS", "15"))
        self.detail_timeout = (6.0, 20.0)
        self.detail_sleep = float(os.getenv("HR_DETAIL_SLEEP", "0.6"))

    def crawl(self):
        base = "https://www.hrloo.com/"
        self._crawl_list(base)

    def _crawl_list(self, base_url: str):
        print("[List] 抓取首页：", base_url)
        r = self.session.get(base_url, timeout=20)
        if r.status_code != 200:
            print("列表请求失败：", r.status_code)
            return
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")

        # 抓取所有可能的新闻链接
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href") or ""
            if re.search(r"/news/\d+\.html$", href):
                links.append(urljoin(base_url, href))

        seen = set()
        for url in links:
            if url in seen:
                continue
            seen.add(url)
            # 详情抓取：只要“昨天”的文章 + 提取小标题
            pub_dt, subtitles, title = self._fetch_detail_yesterday_and_titles(url)
            if not pub_dt:
                continue
            if not is_yesterday(pub_dt):
                continue
            if not subtitles:
                continue

            self.results.append({
                "title": title or url,
                "url": url,
                "source": "三茅人力资源网",
                "date": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "subtitles": subtitles,
            })
            print(f"[Keep] {url} {pub_dt} 小标题{len(subtitles)}条")
            if len(self.results) >= self.max_items:
                break
            time.sleep(self.detail_sleep)

    # —— 详情页：解析发布时间（datetime）+ 只提取分节小标题（不抓正文）
    def _fetch_detail_yesterday_and_titles(self, url: str):
        try:
            r = self.session.get(url, timeout=self.detail_timeout)
            if r.status_code != 200:
                return None, [], ""
            r.encoding = r.apparent_encoding or r.encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # 清理无用节点
            for tag in soup(["script","style","noscript","iframe","footer","header","nav","form"]):
                tag.decompose()
            for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
                c.extract()

            # 标题（页面主标题）
            title_tag = soup.find(["h1","h2"], limit=1)
            page_title = norm(title_tag.get_text()) if title_tag else ""

            # 发布时间：正文 meta/信息区、或全页文本中提取
            pub_dt = self._extract_pub_datetime(soup)

            # 小标题：strong/h2/h3/span.bjh-p，匹配形如“1、xx / 1.xx / 1．xx”
            subtitles = []
            candidates_parent = self._find_content_container(soup)
            for tag in candidates_parent.find_all(["strong","h2","h3","span","p"]):
                # 限定 span 类名（根据你截图）也可能叫 bjh-p
                if tag.name == "span":
                    cls = " ".join((tag.get("class") or []))
                    if cls and "bjh-p" not in cls:
                        # 不是正文小标题类，跳过（仍允许 strong/h2/h3/p）
                        pass
                text = norm(tag.get_text())
                if re.match(r"^\d+\s*[、.．]\s*.+", text):
                    if text not in subtitles:
                        subtitles.append(text)

            return pub_dt, subtitles, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    def _find_content_container(self, soup: BeautifulSoup):
        # 优先正文容器
        for css in [
            ".article-content", ".news-content", ".content", ".article_box",
            ".neirong", ".main-content", ".entry-content", ".post-content", "#article", "#content"
        ]:
            node = soup.select_one(css)
            if node and norm(node.get_text()):
                return node
        return soup

    def _extract_pub_datetime(self, soup: BeautifulSoup) -> datetime | None:
        """
        从详情页提取发布时间；优先在“时间/作者/阅读”等信息区查找；
        兜底在全页文本里用正则匹配。
        """
        tz = ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai"))
        text_candidates = []

        # 信息区常见选择器
        for css in [".meta", ".info", ".news-info", ".article-info", ".time", ".date", ".post-meta"]:
            node = soup.select_one(css)
            if node:
                text_candidates.append(node.get_text(" "))

        # 标题附近的兄弟节点
        h = soup.find(["h1","h2"])
        if h and h.parent:
            text_candidates.append(h.parent.get_text(" "))

        # 全页兜底
        text_candidates.append(soup.get_text(" "))

        # 正则模式（年-月-日 可带时间；年/月/日；中文日期）
        patterns = [
            r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?",
        ]

        for raw in text_candidates:
            raw = norm(raw)
            for pat in patterns:
                m = re.search(pat, raw)
                if m:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    hh = int(m.group(4)) if m.group(4) else 9
                    mm = int(m.group(5)) if m.group(5) else 0
                    try:
                        return datetime(y, mo, d, hh, mm, tzinfo=tz)
                    except ValueError:
                        continue
        return None


# =============== Markdown 输出（只显示小标题） ===============
def build_markdown(items: list[dict]) -> str:
    now = now_tz()
    lines = [
        f"**日期：{now.strftime('%Y-%m-%d')}（{zh_weekday(now)}）**",
        "",
        "**标题：早安资讯｜人力资源每日资讯推送（仅昨日）**",
        "",
        "**主要内容**",
    ]
    if not items:
        lines.append("> 昨日无匹配内容。")
        return "\n".join(lines)

    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['title'] or it['url']}]({it['url']})　—　*{it['source']}*（{it['date']}）")
        for st in it.get("subtitles", []):
            lines.append(f"> 🟦 {st}")
        lines.append("")
    return "\n".join(lines)


# =============== 入口 ===============
if __name__ == "__main__":
    print("执行 hr_news_crawler.py（仅抓昨天＆只摘小标题）")
    crawler = HRLooCrawler()
    crawler.crawl()
    md = build_markdown(crawler.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("早安资讯｜三茅昨日小标题", md)
