# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报净化版（稳健版）
- 仅抓取“三茅日报”
- 提取正文内编号标题（1、2、3、…）
- 自动识别并剔除广告/提示信息（手机、境外、APP、审核等）
- 输出 Markdown，可推送钉钉
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ========= 时区/时间 =========
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def within_24h(dt): return (now_tz() - dt).total_seconds() <= 36 * 3600 if dt else False  # 放宽到 36h
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
        print("🔕 未配置 DINGTALK_BASE，跳过推送。")
        return False
    try:
        r = requests.post(
            _sign_webhook(base, secret),
            json={"msgtype": "markdown", "markdown": {"title": title, "text": md}},
            timeout=20
        )
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} code={r.status_code}")
        if not ok:
            print("resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

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
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9"})
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

# ========= 爬虫主体 =========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = int(os.getenv("HR_MAX_ITEMS", "15") or "15")
        self.detail_timeout = (6, 20)
        self.detail_sleep = 0.6
        self.only_today = os.getenv("HR_ONLY_TODAY", "0") == "1"
        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.sources = [u.strip() for u in os.getenv("SRC_HRLOO_URLS", "https://www.hrloo.com/").split(",") if u.strip()]

    def crawl(self):
        for base in self.sources:
            self._crawl_source(base)

    def _crawl_source(self, base):
        r = self.session.get(base, timeout=20)
        if r.status_code != 200:
            print("首页请求失败：", base, r.status_code)
            return
        soup = BeautifulSoup(r.text, "html.parser")

        links = []
        for a in soup.select("a[href*='/news/']"):
            href, text = a.get("href", ""), norm(a.get_text())
            if re.search(r"/news/\d+\.html$", href) and self.daily_title_pat.search(text):
                links.append(urljoin(base, href))

        # 兜底：如果首页没找到“日報”关键词，也先收集新闻详情链接，交给详情页判断
        if not links:
            links = [urljoin(base, a.get("href"))
                     for a in soup.select("a[href*='/news/']")
                     if re.search(r"/news/\d+\.html$", a.get("href", ""))]

        seen = set()
        for url in links:
            if url in seen:
                continue
            seen.add(url)
            pub_dt, titles, main = self._fetch_detail_clean(url)
            if not main:
                continue
            if not self.daily_title_pat.search(main):
                continue
            if self.only_today and not same_day(pub_dt):
                continue
            if not self.only_today and not within_24h(pub_dt):
                continue
            if not titles:
                continue
            self.results.append({
                "title": main,
                "url": url,
                "date": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "",
                "titles": titles
            })
            print(f"[OK] {url} -> {len(titles)} 条")
            if len(self.results) >= self.max_items:
                break
            time.sleep(self.detail_sleep)

    # —— 稳健发布时间提取 —— #
    def _extract_pub_time(self, soup):
        cand_texts = []

        # <time datetime="...">
        for t in soup.select("time[datetime]"):
            cand_texts.append(t.get("datetime", ""))

        # 常见类名
        for sel in [".time", ".date", ".pubtime", ".post-time", ".publish-time"]:
            for x in soup.select(sel):
                cand_texts.append(x.get_text(" ", strip=True))

        # meta
        for m in soup.select("meta[property='article:published_time'], meta[name='pubdate'], meta[name='publishdate']"):
            cand_texts.append(m.get("content", ""))

        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")

        def parse_one(s):
            m = pat.search(s or "")
            if not m:
                return None
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
            hh = int(m[4]) if m[4] else 9
            mm = int(m[5]) if m[5] else 0
            try:
                return datetime(y, mo, d, hh, mm, tzinfo=_tz())
            except:
                return None

        cand_dt = [parse_one(txt) for txt in cand_texts]
        cand_dt = [dt for dt in cand_dt if dt is not None]

        if not cand_dt:
            all_dt = []
            for m in pat.finditer(soup.get_text(" ")):
                try:
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                    hh = int(m[4]) if m[4] else 9
                    mm = int(m[5]) if m[5] else 0
                    all_dt.append(datetime(y, mo, d, hh, mm, tzinfo=_tz()))
                except:
                    pass
            cand_dt = all_dt

        if cand_dt:
            now = now_tz()
            past = [dt for dt in cand_dt if dt <= now]
            if past:
                return min(past, key=lambda dt: (now - dt))
            return min(cand_dt, key=lambda dt: (dt - now))

        # 兜底：当天 09:00
        n = now_tz()
        return datetime(n.year, n.month, n.day, 9, 0, tzinfo=_tz())

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=self.detail_timeout)
            if r.status_code != 200:
                print("[DetailFail]", url, r.status_code)
                return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            title_tag = soup.find(["h1", "h2"])
            page_title = norm(title_tag.get_text()) if title_tag else ""
            pub_dt = self._extract_pub_time(soup)

            container = soup.select_one(
                "article, .article, .article-content, .detail-content, .news-content, .content, .post-content"
            ) or soup

            titles = self._extract_daily_item_titles(container)
            return pub_dt, titles, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    # —— 只保留真正新闻标题，剔除广告提示 —— #
    def _extract_daily_item_titles(self, root):
        ad_words = [
            "手机", "境外", "短信", "验证码", "审核", "粉丝", "入群", "账号", "APP", "登录",
            "推广", "广告", "创建申请", "协议", "关注", "申诉", "下载", "网盘", "失信", "封号"
        ]
        by_num = {}
        for t in root.find_all(["h2", "h3", "h4", "strong", "b", "p", "li", "span", "div"]):
            raw = (t.get_text() or "").strip()
            if not raw:
                continue
            m = re.match(r"^\s*[（(]?\s*(\d{1,2})\s*[)）]?\s*[、.．]?\s*(.+)$", raw)
            if not m:
                continue
            num, txt = int(m.group(1)), m.group(2).strip()
            if num >= 10 or txt.startswith("日，") or txt.startswith("日 "):
                continue
            if any(w in txt for w in ad_words):
                continue
            title = re.split(r"[（\(]{1}", txt)[0].strip()
            if not (4 <= len(title) <= 80):  # 放宽到 80
                continue
            zh_ratio = len(re.findall(r"[\u4e00-\u9fa5]", title)) / max(len(title), 1)
            if zh_ratio < 0.3:
                continue
            by_num.setdefault(num, title)

        seq = []
        n = 1
        while n in by_num:
            seq.append(by_num[n])
            n += 1
            if n > 20:
                break
        return seq[:10]

# ========= Markdown 输出 =========
def build_md(items):
    n = now_tz()
    out = [
        f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）**  ",
        "",
        "**标题：人资早报｜每日要点**  ",
        ""
    ]
    if not items:
        out.append("> 未发现新的“三茅日报”。")
        return "\n".join(out)

    date_yesterday = (now_tz() - timedelta(days=1)).strftime("%Y-%m-%d")  # 固定展示“昨天”
    for it in items:
        for j, t in enumerate(it["titles"], 1):
            out.append(f"{j}. {t}  ")
        out.append(f"[查看详细]({it['url']}) （{date_yesterday}）  ")
        out.append("")
    return "\n".join(out)

# ========= 主入口 =========
if __name__ == "__main__":
    print("执行 hr_news_crawler_daily_clean_adfree.py（广告过滤·稳健版）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("人资早报｜每日要点", md)
