# -*- coding: utf-8 -*-
"""
每日早报（钉钉友好版）
- 👥 人力资讯：HRLoo 三茅日报要点（抓当天；保留“查看详细”可点击）
- 🏢 企业新闻：新浪财经 上市公司研究院（周一抓上周五；其他工作日抓昨天；标题+可点击链接）

展示要求（你强调的）：
1) 不显示“抓取日期”
2) 不显示“AI最前沿”等栏目标题（只要 numbered 要点）
3) 链接必须可点击：每条新闻用“两行写法”（钉钉最稳）

环境变量（GitHub Actions / Secrets）：
- DINGTALK_TOKEN   ：可填整条 webhook 或 access_token
- DINGTALK_SECRET  ：机器人加签 secret（必须开启加签）

可选环境变量：
- RUN_HRLOO=1/0
- RUN_SINA=1/0
- OUT_FILE=daily_report.md

- HR_TARGET_DATE=YYYY-MM-DD（默认当天；你说三茅抓当天）
- SRC_HRLOO_URLS=...（默认 hrloo 首页+频道）

- SINA_TARGET_DATE=YYYY-MM-DD（可覆盖企业新闻抓取日）
- SINA_MAX_PAGES=5
- SINA_SLEEP_SEC=0.8
- SINA_MAX_ITEMS=15
"""

import os
import re
import time
import ssl
import hmac
import base64
import hashlib
import urllib.parse
import requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from datetime import datetime, timedelta, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


# ===================== 通用 =====================
def now_cn() -> datetime:
    return datetime.now(TZ)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def truncate_text(s: str, max_len: int = 60) -> str:
    """钉钉一行太长容易被截断/点不开，主动截短更稳。"""
    s = norm(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"

def parse_ymd(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        y, m, d = map(int, re.split(r"[-/\.]", s))
        return date(y, m, d)
    except Exception:
        return None

def target_date_sina(today: date) -> date:
    """
    你的规则：新浪财经
    - 周一：抓上周五（today - 3）
    - 其他工作日：抓昨天（today - 1）
    说明：工作流只在周一到周五运行，所以不需要考虑周末运行的情况。
    """
    if today.weekday() == 0:  # 周一
        return today - timedelta(days=3)
    return today - timedelta(days=1)


# ===================== 钉钉（加签） =====================
def extract_access_token(token_or_webhook: str) -> str:
    s = (token_or_webhook or "").strip()
    if not s:
        return ""
    if "access_token=" in s:
        u = urllib.parse.urlparse(s)
        q = urllib.parse.parse_qs(u.query)
        return (q.get("access_token") or [""])[0].strip()
    return s

def dingtalk_signed_url(access_token: str, secret: str) -> str:
    ts = str(int(time.time() * 1000))
    to_sign = f"{ts}\n{secret}"
    sign = urllib.parse.quote_plus(
        base64.b64encode(
            hmac.new(secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha256).digest()
        )
    )
    return f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={ts}&sign={sign}"

def dingtalk_send_markdown(title: str, markdown_text: str) -> dict:
    raw = (os.getenv("DINGTALK_TOKEN") or "").strip()
    secret = (os.getenv("DINGTALK_SECRET") or "").strip()
    token = extract_access_token(raw)

    if not token:
        raise RuntimeError("缺少 DINGTALK_TOKEN（可填整条 webhook 或 access_token）")
    if not secret:
        raise RuntimeError("缺少 DINGTALK_SECRET（请确认机器人已开启“加签”）")

    url = dingtalk_signed_url(token, secret)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown_text}}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if str(data.get("errcode")) != "0":
        raise RuntimeError(f"钉钉发送失败：{data}")
    return data


# ===================== 企业新闻：新浪财经 =====================
SINA_START_URL = "https://finance.sina.com.cn/roll/c/221431.shtml"
SINA_MAX_PAGES = int(os.getenv("SINA_MAX_PAGES", "5"))
SINA_SLEEP_SEC = float(os.getenv("SINA_SLEEP_SEC", "0.8"))
SINA_MAX_ITEMS = int(os.getenv("SINA_MAX_ITEMS", "15"))
SINA_DATE_RE = re.compile(r"\((\d{2})月(\d{2})日\s*(\d{2}):(\d{2})\)")

def sina_get_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text

def sina_parse_datetime(text: str):
    m = SINA_DATE_RE.search(text or "")
    if not m:
        return None
    month, day, hh, mm = map(int, m.groups())
    now = now_cn()
    year = now.year
    if now.month == 1 and month == 12:
        year -= 1
    try:
        return datetime(year, month, day, hh, mm, tzinfo=TZ)
    except Exception:
        return None

def sina_find_next_page(soup: BeautifulSoup):
    a = soup.find("a", string=lambda s: s and "下一页" in s)
    if a and a.get("href"):
        return urljoin(SINA_START_URL, a["href"])
    return None

def sina_pick_best_link(li: Tag):
    """
    li 里可能多个 <a>，优先选最像正文页的链接：
    - .shtml 或 /doc- 或 /article/
    """
    links = []
    for a in li.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        abs_url = urljoin(SINA_START_URL, href)
        text = a.get_text(strip=True)
        links.append((abs_url, text))
    if not links:
        return None, None

    def score(u: str):
        s = 0
        if ".shtml" in u: s += 10
        if "/doc-" in u: s += 8
        if "/article/" in u: s += 6
        if "finance.sina.com.cn" in u: s += 2
        return s

    links.sort(key=lambda x: score(x[0]), reverse=True)
    return links[0][0], links[0][1]

def crawl_sina_target_day():
    # 允许环境变量覆盖
    override = parse_ymd(os.getenv("SINA_TARGET_DATE"))
    today = now_cn().date()
    target = override or target_date_sina(today)

    seen_link = set()
    seen_tt = set()
    results = []

    url = SINA_START_URL
    hit = False

    for _ in range(1, SINA_MAX_PAGES + 1):
        html = sina_get_html(url)
        soup = BeautifulSoup(html, "html.parser")

        container = soup.select_one("div.listBlk")
        if not container:
            break
        lis = container.find_all("li")
        if not lis:
            break

        for li in lis:
            text_all = li.get_text(" ", strip=True)
            dt = sina_parse_datetime(text_all)
            if not dt or dt.date() != target:
                continue

            link, anchor_text = sina_pick_best_link(li)
            if not link:
                continue

            a0 = li.find("a")
            title = (a0.get_text(strip=True) if a0 else "") or (anchor_text or "")
            title = norm(title)
            if not title:
                continue

            k1 = link
            k2 = (title, dt.strftime("%Y-%m-%d %H:%M"))
            if k1 in seen_link or k2 in seen_tt:
                continue

            seen_link.add(k1)
            seen_tt.add(k2)
            results.append((dt, title, link))
            hit = True

        # 早停：已经命中目标日，且本页时间都早于目标日
        if hit:
            dts = [sina_parse_datetime(li.get_text(" ", strip=True)) for li in lis]
            dts = [d for d in dts if d]
            if dts and all(d.date() < target for d in dts):
                break

        next_url = sina_find_next_page(soup)
        if not next_url:
            break
        url = next_url
        time.sleep(SINA_SLEEP_SEC)

    results.sort(key=lambda x: x[0], reverse=True)
    return target, results[:SINA_MAX_ITEMS]

def md_enterprise_news(target_day: date, results):
    lines = []
    lines.append("## 🏢 企业新闻")

    if not results:
        lines.append("（无更新或页面结构变化）")
        return "\n".join(lines)

    # 钉钉稳定写法：每条两行（标题一行 + 链接一行）
    for dt, title, link in results:
        short = truncate_text(title, 50)
        lines.append(f"- {short}")
        lines.append(f"  👉 [打开详情]({link})")

    return "\n".join(lines)


# ===================== 人力资讯：HRLoo =====================
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9"
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

CN_TITLE_DATE = re.compile(r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]")

def date_from_bracket_title(text: str):
    m = CN_TITLE_DATE.search(text or "")
    if not m:
        return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except Exception:
        return None

def looks_like_numbered(text: str) -> bool:
    return bool(re.match(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*\S+", text or ""))

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"

def strip_leading_num(t: str) -> str:
    t = re.sub(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*", "", t)
    t = re.sub(r"^\s*[" + CIRCLED + r"]\s*", "", t)
    t = re.sub(r"^\s*[０-９]+\s*[、.．]\s*", "", t)
    return t.strip()

# 过滤掉栏目标题（你指出的“AI最前沿”等）
SECTION_BLACKLIST = {"AI最前沿", "热点速递", "行业观察", "最新动态"}

class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []

        # ✅ 你说三茅日报抓当天：默认今天（也允许环境变量覆盖）
        override = parse_ymd(os.getenv("HR_TARGET_DATE"))
        self.target_date = override or now_cn().date()

        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.sources = [u.strip() for u in os.getenv(
            "SRC_HRLOO_URLS",
            "https://www.hrloo.com/,https://www.hrloo.com/news/hr"
        ).split(",") if u.strip()]

    def crawl(self):
        for base in self.sources:
            if self._crawl_source(base):
                break

    def _crawl_source(self, base):
        try:
            r = self.session.get(base, timeout=20)
        except Exception:
            return False
        if r.status_code != 200:
            return False

        soup = BeautifulSoup(r.text, "html.parser")

        # 通道1：列表容器
        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if items:
            for div in items:
                a = div.find("a", href=True)
                if not a:
                    continue
                title_text = norm(a.get_text())
                if not self.daily_title_pat.search(title_text):
                    continue

                t2 = date_from_bracket_title(title_text)
                if t2 and t2 != self.target_date:
                    continue

                abs_url = urljoin(base, a["href"])
                if self._try_detail(abs_url):
                    return True

        # 通道2：兜底扫 links
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href", "")
            if not re.search(r"/news/\d+\.html$", href):
                continue
            text = norm(a.get_text())
            if not self.daily_title_pat.search(text):
                continue
            t2 = date_from_bracket_title(text)
            if t2 and t2 != self.target_date:
                continue
            links.append(urljoin(base, href))

        seen = set()
        for u in links:
            if u in seen:
                continue
            seen.add(u)
            if self._try_detail(u):
                return True
        return False

    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        if not page_title or not self.daily_title_pat.search(page_title):
            return False

        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date:
            return False
        if not titles:
            return False

        self.results.append({
            "title": page_title,
            "url": abs_url,
            "date": (pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else f"{self.target_date} 09:00"),
            "titles": titles
        })
        return True

    def _extract_pub_time(self, soup: BeautifulSoup):
        cand = []
        for t in soup.select("time[datetime]"):
            cand.append(t.get("datetime", ""))
        for m in soup.select("meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"):
            cand.append(m.get("content", ""))
        for sel in [".time", ".date", ".pubtime", ".publish-time", ".post-time", ".info", "meta[itemprop='datePublished']"]:
            for x in soup.select(sel):
                if isinstance(x, Tag):
                    cand.append(x.get_text(" ", strip=True))

        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        def parse_one(s):
            m = pat.search(s or "")
            if not m:
                return None
            try:
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
                hh = int(m[4]) if m[4] else 9
                mm = int(m[5]) if m[5] else 0
                return datetime(y, mo, d, hh, mm, tzinfo=TZ)
            except Exception:
                return None

        dts = [dt for dt in map(parse_one, cand) if dt]
        if dts:
            now = now_cn()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    def _extract_h2_titles(self, root: Tag):
        """
        ✅ 只提取 numbered 要点，并过滤“AI最前沿”等栏目标题
        """
        out = []
        for h2 in root.select("h2.style-h2, h2[class*='style-h2']"):
            text = norm(h2.get_text())
            if not text:
                continue

            # 去编号/去括号
            text = strip_leading_num(text)
            text = re.split(r"[（(]", text)[0].strip()
            if not text:
                continue

            # ❌ 过滤栏目标题
            if text in SECTION_BLACKLIST:
                continue

            # ✅ 只保留更像“要点”的内容：至少4字
            if len(text) >= 4:
                out.append(text)

        seen, final = set(), []
        for t in out:
            if t not in seen:
                seen.add(t)
                final.append(t)
        return final

    def _extract_strong_titles(self, root: Tag):
        keep = []
        for st in root.select("strong"):
            text = norm(st.get_text())
            if not text or len(text) < 4:
                continue
            text = strip_leading_num(text)
            text = re.split(r"[（(]", text)[0].strip()
            if text and text not in SECTION_BLACKLIST:
                keep.append(text)
        seen, out = set(), []
        for t in keep:
            if t not in seen:
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
                if text and len(text) >= 4 and text not in SECTION_BLACKLIST:
                    out.append(text)
        seen, final = set(), []
        for t in out:
            if t not in seen:
                seen.add(t)
                final.append(t)
        return final

    def _pick_container(self, soup: BeautifulSoup):
        selectors = [
            ".content-con.fn-wenda-detail-infomation",
            ".fn-wenda-detail-infomation",
            ".content-con.hr-rich-text.fn-wenda-detail-infomation",
            ".hr-rich-text.fn-wenda-detail-infomation",
            ".fn-hr-rich-text.custom-style-warp",
            ".custom-style-warp",
            ".content-wrap-con",
        ]
        for sel in selectors:
            node = soup.select_one(sel)
            if node:
                return node
        return soup

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6, 20))
            if r.status_code != 200:
                return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            h1 = soup.find("h1")
            page_title = norm(h1.get_text()) if h1 else ""
            if not page_title:
                title_tag = soup.find(["h1", "h2"])
                page_title = norm(title_tag.get_text()) if title_tag else ""

            pub_dt = self._extract_pub_time(soup)
            container = self._pick_container(soup)

            for sel in [".other-wrap", ".txt", ".footer", ".bottom"]:
                for bad in container.select(sel):
                    bad.decompose()

            # ✅ 优先 h2（并过滤栏目）
            titles = self._extract_h2_titles(container)

            # 退回：strong
            if not titles:
                titles = self._extract_strong_titles(container)

            # 再退回：编号段落
            if not titles:
                titles = self._extract_numbered_titles(container)

            return pub_dt, titles, page_title
        except Exception:
            return None, [], ""

def crawl_hrloo():
    c = HRLooCrawler()
    c.crawl()
    if not c.results:
        return None, []
    it = c.results[0]
    return it, it.get("titles", [])

def md_hr_info(item, titles):
    lines = []
    lines.append("## 👥 人力资讯")

    if not item or not titles:
        lines.append("（未发现当天的“三茅日报”）")
        return "\n".join(lines)

    for idx, t in enumerate(titles, 1):
        lines.append(f"{idx}. {truncate_text(t, 55)}")

    # ✅ “查看详细”单独一行，钉钉稳定可点
    lines.append(f"\n👉 [查看详细]({item['url']})")
    return "\n".join(lines)


# ===================== 汇总 Markdown（你要的标题风格） =====================
def build_markdown(hr_block: str, enterprise_block: str):
    today_mmdd = now_cn().strftime("%m-%d")
    md = [f"## 📌 {today_mmdd} 每日早报", ""]
    md.append(hr_block or "## 👥 人力资讯\n（本次未生成）")
    md.append("\n---\n")
    md.append(enterprise_block or "## 🏢 企业新闻\n（本次未生成）")
    return "\n".join(md).strip() + "\n"


def main():
    run_hrloo = (os.getenv("RUN_HRLOO", "1").strip() != "0")
    run_sina = (os.getenv("RUN_SINA", "1").strip() != "0")

    hr_block = ""
    enterprise_block = ""

    # 👥 三茅日报：抓当天
    if run_hrloo:
        hr_item, hr_titles = crawl_hrloo()
        hr_block = md_hr_info(hr_item, hr_titles)

    # 🏢 新浪财经：周一抓上周五，其他工作日抓昨天
    if run_sina:
        target_day, sina_items = crawl_sina_target_day()
        enterprise_block = md_enterprise_news(target_day, sina_items)

    md = build_markdown(hr_block, enterprise_block)

    out_file = os.getenv("OUT_FILE", "daily_report.md")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(md)

    title = f"{now_cn().strftime('%m-%d')} 每日早报"
    resp = dingtalk_send_markdown(title, md)
    print("✅ DingTalk OK:", resp)
    print("✅ wrote:", out_file)


if __name__ == "__main__":
    main()
