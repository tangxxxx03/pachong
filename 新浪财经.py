# -*- coding: utf-8 -*-
"""
融合版推送（分两块）：
A) 人力资讯：HRLoo 三茅日报要点（h2/strong/编号三路提取）
B) 企业新闻：新浪财经 - 上市公司研究院：抓取【前一天】标题+链接（去重）

合并成一条 Markdown，钉钉机器人一次性推送（不刷屏、不重复）

GitHub Actions / 环境变量：
- DINGTALK_TOKEN   （可填整条 webhook 或 access_token）
- DINGTALK_SECRET  （加签 secret）

可选：
- RUN_SINA=1/0      是否跑企业新闻（默认 1）
- RUN_HRLOO=1/0     是否跑人力资讯（默认 1）

- HR_TARGET_DATE=YYYY-MM-DD  HRLoo 目标日期（默认今天）
- SRC_HRLOO_URLS=...         HRLoo 来源（逗号分隔）

输出文件：
- OUT_FILE=report.md（默认 daily_report.md）
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


# ===================== 通用：时区 & 文本 =====================
TZ = ZoneInfo("Asia/Shanghai")

def now_tz():
    return datetime.now(TZ)

def _tz():
    return TZ

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def zh_weekday(dt):
    return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]


# ===================== 通用：钉钉（加签） =====================
def extract_access_token(token_or_webhook: str) -> str:
    """
    支持两种：
    1) 仅 token: "xxxx"
    2) 整条 webhook: "https://oapi.dingtalk.com/robot/send?access_token=xxxx"
    """
    s = (token_or_webhook or "").strip()
    if not s:
        return ""
    if "access_token=" in s:
        try:
            if s.startswith("http"):
                u = urllib.parse.urlparse(s)
                q = urllib.parse.parse_qs(u.query)
                return (q.get("access_token") or [""])[0].strip()
            part = s.split("access_token=", 1)[1]
            return part.split("&", 1)[0].strip()
        except Exception:
            return ""
    return s

def dingtalk_signed_url(access_token: str, secret: str) -> str:
    ts = str(int(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={ts}&sign={sign}"

def dingtalk_send_markdown(title: str, markdown_text: str) -> dict:
    raw = (os.getenv("DINGTALK_TOKEN") or "").strip()
    secret = (os.getenv("DINGTALK_SECRET") or "").strip()
    token = extract_access_token(raw)

    if not token:
        raise RuntimeError("缺少 DINGTALK_TOKEN（可填整条 webhook 或 access_token）")
    if not secret:
        raise RuntimeError("缺少 DINGTALK_SECRET（请确认机器人已开启“加签”）")
    if len(token) < 10:
        raise RuntimeError(f"DINGTALK_TOKEN 解析后过短（len={len(token)}），疑似配置错误")

    url = dingtalk_signed_url(token, secret)
    payload = {"msgtype":"markdown","markdown":{"title":title,"text":markdown_text}}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if str(data.get("errcode")) != "0":
        raise RuntimeError(f"钉钉发送失败：{data}")
    return data


# ===================== A) 企业新闻：新浪财经昨日标题索引 =====================
SINA_START_URL = "https://finance.sina.com.cn/roll/c/221431.shtml"
SINA_MAX_PAGES = int(os.getenv("SINA_MAX_PAGES", os.getenv("MAX_PAGES", "5")))
SINA_SLEEP_SEC = float(os.getenv("SINA_SLEEP_SEC", os.getenv("SLEEP_SEC", "0.8")))
SINA_DATE_RE = re.compile(r"\((\d{2})月(\d{2})日\s*(\d{2}):(\d{2})\)")

def sina_get_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language":"zh-CN,zh;q=0.9",
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
    now = now_tz()
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

def crawl_sina_yesterday():
    yesterday = (now_tz() - timedelta(days=1)).date()

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
            if not dt or dt.date() != yesterday:
                continue

            link, anchor_text = sina_pick_best_link(li)
            if not link:
                continue

            a0 = li.find("a")
            title = (a0.get_text(strip=True) if a0 else "") or (anchor_text or "")
            title = title.strip()
            if not title:
                continue

            k1 = link
            k2 = (title, dt.strftime("%Y-%m-%d %H:%M"))

            if k1 in seen_link:
                continue
            if k2 in seen_tt:
                continue

            seen_link.add(k1)
            seen_tt.add(k2)
            results.append((dt, title, link))
            hit = True

        if hit:
            dts = [sina_parse_datetime(li.get_text(" ", strip=True)) for li in lis]
            dts = [d for d in dts if d]
            if dts and all(d.date() < yesterday for d in dts):
                break

        next_url = sina_find_next_page(soup)
        if not next_url:
            break
        url = next_url
        time.sleep(SINA_SLEEP_SEC)

    results.sort(key=lambda x: x[0], reverse=True)
    return yesterday, results


def md_enterprise_news(yesterday, results):
    lines = [f"### 🏢 企业新闻｜新浪财经（{yesterday}）"]
    if not results:
        lines.append("（昨日无更新或页面结构变化）")
    else:
        for dt, title, link in results:
            lines.append(f"- [{title}]({link})  `{dt.strftime('%H:%M')}`")
    return "\n".join(lines)


# ===================== B) 人力资讯：HRLoo 三茅日报要点 =====================
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
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language":"zh-CN,zh;q=0.9"
    })
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
    except:
        return None

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

        t = (os.getenv("HR_TARGET_DATE") or "").strip():
        if t:
            try:
                y,m,d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y,m,d)
            except:
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()

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

        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if items:
            for div in items:
                dts = (div.get("dwdata-time") or "").strip()
                if dts:
                    try:
                        pub_d = datetime.strptime(dts.split()[0], "%Y-%m-%d").date()
                        if pub_d != self.target_date:
                            continue
                    except:
                        pass

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

        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href","")
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
        for url in links:
            if url in seen:
                continue
            seen.add(url)
            if self._try_detail(url):
                return True

        return False

    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        if not page_title or not self.daily_title_pat.search(page_title):
            return False

        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date:
            return False
        if pub_dt and pub_dt.date() != self.target_date and not t3:
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
            cand.append(t.get("datetime",""))
        for m in soup.select("meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"):
            cand.append(m.get("content",""))
        for sel in [".time",".date",".pubtime",".publish-time",".post-time",".info","meta[itemprop='datePublished']"]:
            for x in soup.select(sel):
                if isinstance(x, Tag):
                    cand.append(x.get_text(" ", strip=True))

        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        def parse_one(s):
            m = pat.search(s or "")
            if not m:
                return None
            try:
                y,mo,d = int(m[1]),int(m[2]),int(m[3])
                hh = int(m[4]) if m[4] else 9
                mm = int(m[5]) if m[5] else 0
                return datetime(y,mo,d,hh,mm,tzinfo=_tz())
            except:
                return None

        dts = [dt for dt in map(parse_one, cand) if dt]
        if dts:
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    def _extract_h2_titles(self, root: Tag):
        out = []
        for h2 in root.select("h2.style-h2, h2[class*='style-h2']"):
            text = norm(h2.get_text())
            if not text:
                continue
            text = strip_leading_num(text)
            text = re.split(r"[（(]", text)[0].strip()
            if text and len(text) >= 4:
                out.append(text)
        seen, final = set(), []
        for t in out:
            if t in seen:
                continue
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
            if text:
                keep.append(text)
        seen, out = set(), []
        for t in keep:
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    def _extract_numbered_titles(self, root: Tag):
        out = []
        for p in root.find_all(["p","h2","h3","div","span","li"]):
            text = norm(p.get_text())
            if looks_like_numbered(text):
                text = strip_leading_num(text)
                text = re.split(r"[（(]", text)[0].strip()
                if text and len(text) >= 4:
                    out.append(text)
        seen, final = set(), []
        for t in out:
            if t in seen:
                continue
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
                title_tag = soup.find(["h1","h2"])
                page_title = norm(title_tag.get_text()) if title_tag else ""

            pub_dt = self._extract_pub_time(soup)
            container = self._pick_container(soup)

            for sel in [".other-wrap",".txt",".footer",".bottom"]:
                for bad in container.select(sel):
                    bad.decompose()

            titles = self._extract_h2_titles(container)
            if not titles:
                titles = self._extract_strong_titles(container)
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
    target = (os.getenv("HR_TARGET_DATE") or "").strip()
    if not target:
        target = now_tz().strftime("%Y-%m-%d")

    lines = [f"### 👥 人力资讯｜三茅日报要点（{target}）"]
    if not item or not titles:
        lines.append("> 未发现当天的“三茅日报”。")
        return "\n".join(lines)

    for idx, t in enumerate(titles, 1):
        lines.append(f"{idx}. {t}")
    lines.append(f"[查看详细]({item['url']})")
    return "\n".join(lines)


# ===================== 合并推送（两大分区） =====================
def build_final_markdown(hr_block: str, enterprise_block: str):
    n = now_tz()
    header = [
        f"## 📌 每日推送",
        f"> 生成时间：{n.strftime('%Y-%m-%d %H:%M:%S')}（{zh_weekday(n)}，Asia/Shanghai）",
        ""
    ]

    body = []
    # 人力资讯在前
    body.append("## 👥 人力资讯")
    body.append(hr_block or "（本次未生成）")
    body.append("")
    body.append("---")
    body.append("")

    # 企业新闻在后
    body.append("## 🏢 企业新闻")
    body.append(enterprise_block or "（本次未生成）")

    return "\n".join(header + body).strip() + "\n"


def main():
    run_sina = (os.getenv("RUN_SINA", "1").strip() != "0")
    run_hrloo = (os.getenv("RUN_HRLOO", "1").strip() != "0")

    hr_block = ""
    enterprise_block = ""

    if run_hrloo:
        hr_item, hr_titles = crawl_hrloo()
        hr_block = md_hr_info(hr_item, hr_titles)

    if run_sina:
        y, sina_items = crawl_sina_yesterday()
        enterprise_block = md_enterprise_news(y, sina_items)

    md = build_final_markdown(hr_block, enterprise_block)

    out_file = os.getenv("OUT_FILE", "daily_report.md")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(md)

    title = f"每日推送 {now_tz().strftime('%Y-%m-%d')}"
    resp = dingtalk_send_markdown(title, md)
    print("✅ DingTalk OK:", resp)
    print(f"✅ wrote: {out_file}")


if __name__ == "__main__":
    main()
