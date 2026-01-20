# -*- coding: utf-8 -*-
"""
每日简报（钉钉友好最终版）
- 🏢 企业新闻：三茅日报要点（当天） + 新浪财经（周一抓上周五，其他工作日抓昨天）合并输出，统一连续编号
- 🧩 地方政策：人社部-地方动态（周一抓上周五，周二~周五抓昨天；周末不抓）

展示要求（按你最新要求）：
1) 不要底部“查看详细”
2) 每条后面都要一个 👉 [详情](url)（蓝字可点）
3) 标题不做整段超链接（避免花眼），只让“详情”蓝字可点
4) 企业新闻里：先三茅要点，再财经；编号统一连续
5) 地方政策单独一块，单独编号从 1 开始

钉钉环境变量（Secrets）：
- SHIYANQUNWEBHOOK
- SHIYANQUNSECRET

可选环境变量：
- HR_TZ=Asia/Shanghai
- OUT_FILE=daily_all.md
- RUN_HRLOO=1/0
- RUN_SINA=1/0
- RUN_MOHRSS=1/0

- SRC_HRLOO_URLS=...（默认 hrloo 首页+频道）
- HR_TARGET_DATE=YYYY-MM-DD（默认当天）

- SINA_TARGET_DATE=YYYY-MM-DD（可覆盖财经抓取日）
- SINA_MAX_PAGES=5
- SINA_SLEEP_SEC=0.8
- SINA_MAX_ITEMS=15

- MOHRSS_LIST_URL=...（默认人社部地方动态列表页）
"""

import os
import re
import time
import ssl
import hmac
import base64
import hashlib
import urllib.parse
from datetime import datetime, timedelta, date
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup, Tag
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


# ===================== 通用 =====================
TZ = ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai"))

def now_cn() -> datetime:
    return datetime.now(TZ)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def truncate_text(s: str, max_len: int = 70) -> str:
    s = norm(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"

def safe_md_text(s: str) -> str:
    # 防止标题里出现 [] 影响 markdown
    return (s or "").replace("[", "【").replace("]", "】")

def parse_ymd(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        y, m, d = map(int, re.split(r"[-/\.]", s))
        return date(y, m, d)
    except Exception:
        return None

def target_prev_workday(today: date) -> date:
    """
    周一：抓上周五（today - 3）
    周二~周五：抓昨天（today - 1）
    周末：不运行（由 main 控制）
    """
    if today.weekday() == 0:
        return today - timedelta(days=3)
    return today - timedelta(days=1)

def md_item_with_detail(i: int, title: str, url: str) -> str:
    """
    每条输出： 1. 标题  👉 [详情](url)
    """
    title = safe_md_text(truncate_text(title, 70))
    return f"{i}. {title}  👉 [详情]({url})"


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

def dingtalk_signed_url(webhook_or_token: str, secret: str) -> str:
    """
    兼容：SHIYANQUNWEBHOOK 既可以传整条 webhook，也可以只传 access_token
    """
    raw = (webhook_or_token or "").strip()
    token = extract_access_token(raw)
    if not token:
        raise RuntimeError("SHIYANQUNWEBHOOK 为空（可填整条 webhook 或 access_token）")

    ts = str(int(time.time() * 1000))
    to_sign = f"{ts}\n{secret}"
    sign = urllib.parse.quote_plus(
        base64.b64encode(
            hmac.new(secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha256).digest()
        )
    )
    return f"https://oapi.dingtalk.com/robot/send?access_token={token}&timestamp={ts}&sign={sign}"

def dingtalk_send_markdown(title: str, markdown_text: str) -> dict:
    webhook = (os.getenv("SHIYANQUNWEBHOOK") or "").strip()
    secret = (os.getenv("SHIYANQUNSECRET") or "").strip()
    if not webhook or not secret:
        raise RuntimeError("缺少 SHIYANQUNWEBHOOK 或 SHIYANQUNSECRET")

    url = dingtalk_signed_url(webhook, secret)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown_text}}
    r = requests.post(url, json=payload, timeout=25)
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
    override = parse_ymd(os.getenv("SINA_TARGET_DATE"))
    today = now_cn().date()
    target = override or target_prev_workday(today)

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


# ===================== 人力资讯：HRLoo（三茅） =====================
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
SECTION_BLACKLIST = {"AI最前沿", "热点速递", "行业观察", "最新动态"}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"

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

def strip_leading_num(t: str) -> str:
    t = re.sub(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*", "", t)
    t = re.sub(r"^\s*[" + CIRCLED + r"]\s*", "", t)
    t = re.sub(r"^\s*[０-９]+\s*[、.．]\s*", "", t)
    return t.strip()

class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []

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
        _, titles, page_title = self._fetch_detail_clean(abs_url)
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
            "titles": titles
        })
        return True

    def _extract_h2_titles(self, root: Tag):
        out = []
        for h2 in root.select("h2.style-h2, h2[class*='style-h2']"):
            text = norm(h2.get_text())
            if not text:
                continue
            text = strip_leading_num(text)
            text = re.split(r"[（(]", text)[0].strip()
            if not text:
                continue
            if text in SECTION_BLACKLIST:
                continue
            if len(text) >= 4:
                out.append(text)

        seen, final = set(), []
        for t in out:
            if t not in seen:
                seen.add(t)
                final.append(t)
        return final

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

            container = self._pick_container(soup)
            for sel in [".other-wrap", ".txt", ".footer", ".bottom"]:
                for bad in container.select(sel):
                    bad.decompose()

            titles = self._extract_h2_titles(container)
            if not titles:
                titles = self._extract_numbered_titles(container)

            return None, titles, page_title
        except Exception:
            return None, [], ""

def crawl_hrloo():
    c = HRLooCrawler()
    c.crawl()
    if not c.results:
        return None, []
    it = c.results[0]
    return it, it.get("titles", [])


# ===================== 地方政策：人社部-地方动态（Playwright） =====================
MOHRSS_DEFAULT_LIST_URL = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/index.html"
RE_DATE_DASH = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
RE_DATE_CN = re.compile(r"\b(20\d{2})年(\d{1,2})月(\d{1,2})日\b")

def normalize_date_text(text: str) -> str | None:
    if not text:
        return None
    s = norm(text)

    m1 = RE_DATE_DASH.search(s)
    if m1:
        return m1.group(1)

    m2 = RE_DATE_CN.search(s)
    if m2:
        y = m2.group(1)
        mo = int(m2.group(2))
        d = int(m2.group(3))
        return f"{y}-{mo:02d}-{d:02d}"
    return None

def fetch_rendered_html(url: str, retries: int = 2) -> str:
    last_html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )

        for _ in range(retries + 1):
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page.set_extra_http_headers({"Accept-Language": "zh-CN,zh;q=0.9"})

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_function(
                        "document.body && /20\\d{2}-\\d{2}-\\d{2}/.test(document.body.innerText)",
                        timeout=12000
                    )
                except Exception:
                    page.wait_for_timeout(1500)

                html = page.content()
                last_html = html

                if len(html or "") < 5000:
                    page.close()
                    time.sleep(1.2)
                    continue

                page.close()
                browser.close()
                return html

            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                time.sleep(1.2)

        browser.close()
        return last_html

def parse_list_robust(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for node in soup.find_all(string=True):
        dt = normalize_date_text(str(node))
        if not dt:
            continue

        container = node.parent
        for _ in range(12):
            if not container:
                break
            a = container.find("a", href=True)
            if a and norm(a.get_text()):
                href = a["href"].strip()
                if ".html" in href:
                    items.append({
                        "date": dt,
                        "title": norm(a.get_text()),
                        "url": urljoin(page_url, href)
                    })
                    break
            container = container.parent

    seen, uniq = set(), []
    for it in items:
        key = (it["date"], it["title"], it["url"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    uniq.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    return uniq

def crawl_mohrss_target_day():
    today = now_cn().date()
    target = target_prev_workday(today)
    list_url = (os.getenv("MOHRSS_LIST_URL") or MOHRSS_DEFAULT_LIST_URL).strip()

    html = fetch_rendered_html(list_url, retries=2)
    items = parse_list_robust(html, list_url)
    hit = [x for x in items if x["date"] == target.strftime("%Y-%m-%d")]
    return target, list_url, hit


# ===================== Markdown 组装（最终样式） =====================
def build_enterprise_block(run_hrloo: bool, run_sina: bool) -> str:
    lines = ["## 🏢 财经新闻"]
    idx = 1

    # 先三茅要点
    if run_hrloo:
        hr_item, hr_titles = crawl_hrloo()
        if hr_item and hr_titles:
            for t in hr_titles:
                # 三茅要点详情统一跳到当天三茅日报文章页（同一个 url）
                lines.append(md_item_with_detail(idx, t, hr_item["url"]))
                idx += 1
        else:
            lines.append("（未发现当天的三茅日报）")

    # 再新浪财经
    if run_sina:
        _, sina_items = crawl_sina_target_day()
        if sina_items:
            for _, title, link in sina_items:
                lines.append(md_item_with_detail(idx, title, link))
                idx += 1
        else:
            lines.append("（新浪财经无更新或页面结构变化）")

    return "\n".join(lines).strip()

def build_policy_block(run_mohrss: bool) -> str:
    lines = ["## 🧩 地方动态"]
    if not run_mohrss:
        lines.append("（本次未启用）")
        return "\n".join(lines).strip()

    # 周末不抓
    wd = now_cn().weekday()
    if wd >= 5:
        lines.append("（周末不抓取）")
        return "\n".join(lines).strip()

    _, _, hit = crawl_mohrss_target_day()
    if not hit:
        lines.append("（无更新或本次未命中）")
        return "\n".join(lines).strip()

    for i, it in enumerate(hit, 1):
        lines.append(md_item_with_detail(i, it["title"], it["url"]))

    return "\n".join(lines).strip()

def build_markdown(enterprise_block: str, policy_block: str) -> str:
    mmdd = now_cn().strftime("%m-%d")
    md = [f"## 📌 {mmdd} 每日简报", ""]
    md.append(enterprise_block or "## 🏢 财经新闻\n（本次未生成）")
    md.append("\n---\n")
    md.append(policy_block or "## 🧩 地方动态\n（本次未生成）")
    return "\n".join(md).strip() + "\n"


def main():
    # 周末不运行（你规则里周六/周日不抓）
    wd = now_cn().weekday()
    if wd >= 5:
        print("[INFO] 周末不运行")
        return

    run_hrloo = (os.getenv("RUN_HRLOO", "1").strip() != "0")
    run_sina = (os.getenv("RUN_SINA", "1").strip() != "0")
    run_mohrss = (os.getenv("RUN_MOHRSS", "1").strip() != "0")

    enterprise_block = build_enterprise_block(run_hrloo, run_sina)
    policy_block = build_policy_block(run_mohrss)

    md = build_markdown(enterprise_block, policy_block)

    out_file = os.getenv("OUT_FILE", "daily_all.md")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(md)

    title = f"{now_cn().strftime('%m-%d')} 每日简报"
    resp = dingtalk_send_markdown(title, md)
    print("✅ DingTalk OK:", resp)
    print("✅ wrote:", out_file)


if __name__ == "__main__":
    main()
