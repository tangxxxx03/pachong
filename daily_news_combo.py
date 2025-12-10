# -*- coding: utf-8 -*-
"""
三茅网「三茅日报」 + 财富中文网「商业频道」合并爬虫 V12 (修复链接与乱码)
—————————————————————————
功能：
1）从三茅网抓取当天的「三茅日报」并抽取要点标题
2）从财富中文网 商业频道（PC 版 /shangye/）抓取最新若干条文章
3）把两个来源合成一条 Markdown 消息推送到钉钉

环境变量（推荐在 GitHub Actions Secrets 里配置）：
  # 钉钉群机器人（可多个，用逗号分隔）
  DINGTALK_BASES   = https://oapi.dingtalk.com/robot/send?access_token=xxx,https://...
  DINGTALK_SECRETS = xxx,yyy

  # 三茅目标日期（可选，不设则默认“今天”）
  HR_TARGET_DATE   = 2025-12-04

  # 三茅抓取入口（可选，一般不用改）
  SRC_HRLOO_URLS   = https://www.hrloo.com/,https://www.hrloo.com/news/hr

  # 财富抓取篇数（可选，默认 5）
  FORTUNE_MAX_ITEMS = 5
"""

import os
import re
import ssl
import time
import hmac
import base64
import hashlib
import urllib.parse
from datetime import datetime, date
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup, Tag
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ================== 通用小工具 ==================

try:
    from zoneinfo import ZoneInfo
except:  # Python<3.9
    from backports.zoneinfo import ZoneInfo


def _tz():
    return ZoneInfo("Asia/Shanghai")


def now_tz():
    return datetime.now(_tz())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def zh_weekday(dt: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]

# --- 🎯 核心修复：URL 编码与清洗 ---
def safe_url(url: str) -> str:
    """
    对 URL 进行清洗和编码，防止 Markdown 解析错误或 404。
    只对路径部分编码，保留 :// 等符号。
    """
    if not url: return ""
    # 先去除首尾空白
    url = url.strip()
    # 简单编码，safe 字符不编码
    return quote(url, safe=":/?&amp;=#%")
# -----------------------------------

# ================== DingTalk 推送 ==================

def _sign_webhook(base: str, secret: str) -> str:
    """钉钉自带加签逻辑"""
    if not base:
        return ""
    if not secret:
        return base

    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    sign = urllib.parse.quote_plus(
        base64.b64encode(hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest())
    )
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"


def send_dingtalk_markdown_all(title: str, text: str) -> None:
    """
    同时往多个群推送
    """
    bases = (os.getenv("DINGTALK_BASES") or "").split(",")
    secrets = (os.getenv("DINGTALK_SECRETS") or "").split(",")

    bases = [b.strip() for b in bases if b.strip()]
    secrets = [s.strip() for s in secrets if s.strip()]

    if not bases:
        print("🔕 未配置 DINGTALK_BASES，跳过推送。")
        return

    for i, base in enumerate(bases):
        secret = secrets[i] if i < len(secrets) else ""
        try:
            url = _sign_webhook(base, secret)
            resp = requests.post(
                url,
                json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
                timeout=20,
            )
            ok = resp.status_code == 200 and resp.json().get("errcode") == 0
            print(f"[DingTalk #{i}] push={ok} code={resp.status_code}")
            if not ok:
                print("  resp:", resp.text[:300])
        except Exception as e:
            print(f"[DingTalk #{i}] error:", e)


# ================== HTTP Session（支持旧 TLS） ==================

class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s


# ================== 三茅网：人资日报 ==================
# (保持原有逻辑不变，只在生成 URL 时调用 safe_url)

CN_TITLE_DATE = re.compile(r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]")

def date_from_bracket_title(text: str):
    m = CN_TITLE_DATE.search(text or "")
    if not m: return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except Exception: return None

def looks_like_numbered(text: str) -> bool:
    return bool(re.match(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*\S+", text or ""))

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
def strip_leading_num(t: str) -> str:
    t = re.sub(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*", "", t)
    t = re.sub(r"^\s*[" + CIRCLED + r"]\s*", "", t)
    t = re.sub(r"^\s*[０-９]+\s*[、.．]\s*", "", t)
    return t.strip()

class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        t = (os.getenv("HR_TARGET_DATE") or "").strip()
        if t:
            try:
                y, m, d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y, m, d)
            except Exception:
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()
        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.sources = [u.strip() for u in os.getenv("SRC_HRLOO_URLS", "https://www.hrloo.com/,https://www.hrloo.com/news/hr").split(",") if u.strip()]

    def run(self):
        for base in self.sources:
            if self._crawl_source(base): break

    def _crawl_source(self, base: str) -> bool:
        try: r = self.session.get(base, timeout=20)
        except Exception: return False
        if r.status_code != 200: return False
        soup = BeautifulSoup(r.text, "html.parser")
        
        # 1) dwxfd-list
        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if items:
            for div in items:
                dts = (div.get("dwdata-time") or "").strip()
                if dts:
                    try:
                        pub_d = datetime.strptime(dts.split()[0], "%Y-%m-%d").date()
                        if pub_d != self.target_date: continue
                    except Exception: pass
                a = div.find("a", href=True)
                if not a: continue
                title_text = norm(a.get_text())
                if not self.daily_title_pat.search(title_text): continue
                t2 = date_from_bracket_title(title_text)
                if t2 and t2 != self.target_date: continue
                abs_url = urljoin(base, a["href"])
                if self._try_detail(abs_url): return True

        # 2) /news/
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href", "")
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
        return False

    def _try_detail(self, abs_url: str) -> bool:
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        if not page_title or not self.daily_title_pat.search(page_title): return False
        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date: return False
        if pub_dt and pub_dt.date() != self.target_date and not t3: return False
        if not titles: return False
        self.results.append({
            "title": page_title,
            "url": safe_url(abs_url), # 使用 safe_url 清洗链接
            "date": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else f"{self.target_date} 09:00",
            "titles": titles,
        })
        return True

    def _extract_pub_time(self, soup: BeautifulSoup):
        cand = []
        for t in soup.select("time[datetime]"): cand.append(t.get("datetime", ""))
        for m in soup.select("meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"): cand.append(m.get("content", ""))
        for sel in [".time", ".date", ".pubtime", ".publish-time", ".post-time", ".info", "meta[itemprop='datePublished']"]:
            for x in soup.select(sel):
                if isinstance(x, Tag): cand.append(x.get_text(" ", strip=True))
        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        dts = []
        for s in cand:
            m = pat.search(s or "")
            if m:
                try: dts.append(datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]) if m[4] else 9, int(m[5]) if m[5] else 0, tzinfo=_tz()))
                except Exception: pass
        if dts:
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    def _fetch_detail_clean(self, url: str):
        try:
            r = self.session.get(url, timeout=(6, 20))
            if r.status_code != 200: return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            title_tag = soup.find(["h1", "h2"])
            page_title = norm(title_tag.get_text()) if title_tag else ""
            pub_dt = self._extract_pub_time(soup)
            container = soup.select_one(".content-con.hr-rich-text.fn-wenda-detail-infomation.fn-hr-rich-text.custom-style-w") or soup
            for sel in [".other-wrap", ".txt", "a.prev.fn-dataStatistics-btn", "a.next.fn-dataStatistics-btn", ".footer", ".bottom"]:
                for bad in container.select(sel): bad.decompose()
            titles = self._extract_strong_titles(container)
            if not titles: titles = self._extract_numbered_titles(container)
            return pub_dt, titles, page_title
        except Exception: return None, [], ""

    def _extract_strong_titles(self, root: Tag):
        keep = []
        for st in root.select("strong"):
            text = norm(st.get_text())
            if not text or len(text) < 4: continue
            text = re.split(r"[（(]?(阅读|阅读量|浏览|来源)[:：]\s*\d+.*$", text)[0].strip()
            if not text: continue
            text = strip_leading_num(text)
            if text: keep.append(text)
        seen, out = set(), []
        for t in keep:
            if t in seen: continue
            seen.add(t)
            out.append(t)
        return out

    def _extract_numbered_titles(self, root: Tag):
        out = []
        for p in root.find_all(["p", "h2", "h3", "div", "span", "li"]):
            text = norm(p.get_text())
            if looks_like_numbered(text):
                text = strip_leading_num(text)
                text = re.split(r"[（(]", text)[0].strip()
                if text and len(text) >= 4: out.append(text)
        seen, final = set(), []
        for t in out:
            if t in seen: continue
            seen.add(t)
            final.append(t)
        return final

def build_hrloo_md_block(crawler: HRLooCrawler) -> str:
    if not crawler.results: return "> 今天未发现三茅网发布“三茅日报”。\n"
    it = crawler.results[0]
    out = [f"**三茅日报 · {it['date']}** \n"]
    for idx, t in enumerate(it["titles"], 1): out.append(f"{idx}. {t}  ")
    out.append(f"[👉 查看原文]({it['url']})  ")
    return "\n".join(out) + "\n"


# ================== 财富中文网 商业频道 ==================

BASE_FORTUNE = "https://www.fortunechina.com"
# 列表页 URL，用于正确拼接相对路径
LIST_URL_FORTUNE = "https://www.fortunechina.com/shangye/"

class FortuneChinaCrawler:
    def __init__(self, max_items: int = 5):
        self.session = make_session()
        self.max_items = max_items

    def fetch_list_page(self, page: int = 1):
        url = f"{BASE_FORTUNE}/shangye/" if page == 1 else f"{BASE_FORTUNE}/shangye/?page={page}"
        print(f"[Fortune] 请求列表页：{url}")
        try:
            r = self.session.get(url, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print("[Fortune] 列表页异常：", e)
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        items = []

        for li in soup.select("ul.news-list li.news-item"):
            h2 = li.find("h2")
            a = li.find("a", href=True)
            date_div = li.find("div", class_="date")

            if not (h2 and a): continue

            href = a["href"].strip()
            # 简单校验
            if not re.search(r"content_\d+\.htm", href): continue

            title = norm(h2.get_text())
            
            # --- 🎯 核心修复：URL 拼接 ---
            # 使用列表页 LIST_URL_FORTUNE 作为基准，解决相对路径 404 问题
            full_url = urljoin(LIST_URL_FORTUNE, href)
            # ---------------------------

            pub_date = norm(date_div.get_text()) if date_div else ""

            items.append({
                "title": title,
                "url": safe_url(full_url), # 清洗 URL
                "date": pub_date,
            })

        print(f"[Fortune] 抓到 {len(items)} 条。")
        return items

    def run(self):
        items = self.fetch_list_page(page=1)
        return items[: self.max_items]


def build_fortune_md_block(items) -> str:
    if not items: return "> 财富中文网商业频道暂无抓取到内容。\n"
    out = ["**财富中文网 · 商业频道精选** ", ""]
    for i, it in enumerate(items, 1):
        # 钉钉链接格式：[标题](链接)
        out.append(f"{i}. [{it['title']}]({it['url']})  （{it['date']}）")
    return "\n".join(out) + "\n"


# ================== 主流程：合并推送 ==================

def build_final_markdown(hr_md: str, fortune_md: str) -> str:
    n = now_tz()
    head = f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）** \n\n"
    head += "**人资 & 商业情报 · 每日简报** \n\n"
    parts = [
        "### 一、HR 人资热点（来自三茅网）",
        "",
        hr_md,
        "",
        "### 二、商业财经热点（来自财富中文网）",
        "",
        fortune_md,
    ]
    return head + "\n".join(parts)


def main():
    print("=== 合并爬虫开始执行（三茅 + 财富中文网）V12 ===")

    # 1) 三茅日报
    hr = HRLooCrawler()
    hr.run()
    hr_block = build_hrloo_md_block(hr)

    # 2) 财富商业频道
    max_items = int(os.getenv("FORTUNE_MAX_ITEMS") or 5)
    fortune_crawler = FortuneChinaCrawler(max_items=max_items)
    fortune_items = fortune_crawler.run()
    fortune_block = build_fortune_md_block(fortune_items)

    # 3) 拼 Markdown & 推钉钉
    md = build_final_markdown(hr_block, fortune_block)
    print("\n===== Markdown Preview =====\n")
    print(md)

    send_dingtalk_markdown_all("人资 & 商业简报", md)


if __name__ == "__main__":
    main()
