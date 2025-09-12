# -*- coding: utf-8 -*-
"""
合并版（统一推送 & 连续编号）
- People.cn 站内搜索（最近N小时；多关键词顺序抓取，逐关键词合并去重）
- HR 多站点资讯（仅当天，默认剔除 people.com.cn，且与 People.cn 跨模块去重）
- both 模式默认“合并推送一个消息，序号连续”；如需分开发两条，加 --separate

示例：
  # 合并推送（默认）
  python hr_news_crawler.py both --keywords "外包,人力资源" --pages 2 --window-hours 24 --limit 20
  # 分别推送两条
  python hr_news_crawler.py both --separate --keywords "外包,人力资源" --pages 2 --window-hours 24
  # 单独模块
  python hr_news_crawler.py people --keywords "外包,人力资源" --pages 2 --window-hours 24
  python hr_news_crawler.py hr
"""

import os
import re
import time
import csv
import json
import argparse
import hmac
import hashlib
import base64
import urllib.parse
from urllib.parse import urljoin, urlparse, quote_plus
from collections import defaultdict
from datetime import datetime, timedelta

# 兼容 Py<3.9 的 zoneinfo
try:
    from zoneinfo import ZoneInfo  # Py3.9+
except Exception:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # pip install backports.zoneinfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import ssl

# ====================== DingTalk 统一配置 ======================
DEFAULT_DINGTALK_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send?"
    "access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
)
DEFAULT_DINGTALK_SECRET = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"

DINGTALK_BASE   = os.getenv("DINGTALK_BASE", DEFAULT_DINGTALK_WEBHOOK).strip()
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", DEFAULT_DINGTALK_SECRET).strip()
DINGTALK_KEYWORD = os.getenv("DINGTALK_KEYWORD", "").strip()

def _sign_webhook(base_webhook: str, secret: str) -> str:
    if not base_webhook:
        return ""
    if not secret:
        return base_webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in base_webhook else "?"
    return f"{base_webhook}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title: str, md_text: str) -> bool:
    webhook = _sign_webhook(DINGTALK_BASE, DINGTALK_SECRET)
    if not webhook:
        print("🔕 未配置钉钉 Webhook，跳过推送。")
        return False
    if DINGTALK_KEYWORD and (DINGTALK_KEYWORD not in title and DINGTALK_KEYWORD not in md_text):
        title = f"{DINGTALK_KEYWORD} | {title}"
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print("DingTalk resp:", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

# ====================== HTTP 会话（重试/旧TLS兼容） ======================
class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

def make_session():
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    retries = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    legacy = LegacyTLSAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", legacy)
    return s

# ====================== 通用工具 ======================
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u3000", " ").strip())

def strip_html(html: str) -> str:
    if not html:
        return ""
    return norm(BeautifulSoup(html, "html.parser").get_text(" "))

def zh_weekday(dt):
    return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

def parse_local_dt(s: str, tz: ZoneInfo) -> datetime:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=0, minute=0)
            return dt.replace(tzinfo=tz)
        except Exception:
            continue
    raise ValueError(f"无法解析时间：{s}")

def parse_keywords_arg(raw: str | None, fallback: str) -> list:
    src = raw if (raw and raw.strip()) else fallback
    parts = re.split(r"[,\s|，；;]+", src.strip())
    kws = [p.strip() for p in parts if p.strip()]
    out, seen = [], set()
    for k in kws:
        if k not in seen:
            out.append(k); seen.add(k)
    return out

def slugify_kw(kw: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fa5]+", "_", kw).strip("_") or "kw"

def days_between(start_dt: datetime, end_dt: datetime) -> set:
    days = set()
    d = start_dt.date()
    last = end_dt.date()
    for _ in range(8):  # 最多跨7天
        days.add(d.strftime("%Y-%m-%d"))
        if d == last:
            break
        d = d + timedelta(days=1)
    return days

# ====================== 模块A：People.cn（最近N小时） ======================
class PeopleSearch:
    API_URLS = [
        "http://search.people.cn/search-platform/front/search",
        "http://search.people.cn/api-search/front/search",
    ]

    def __init__(self, keyword="外包", max_pages=1, delay=120,
                 tz="Asia/Shanghai", start_ms: int | None = None, end_ms: int | None = None, page_limit=20):
        self.keyword = keyword
        self.max_pages = max_pages
        self.page_limit = max(1, min(50, int(page_limit)))
        self.tz = ZoneInfo(tz)

        if start_ms is None or end_ms is None:
            now = datetime.now(self.tz)
            self.start_ms = int((now - timedelta(hours=24)).timestamp() * 1000)
            self.end_ms = int(now.timestamp() * 1000)
        else:
            self.start_ms = int(start_ms); self.end_ms = int(end_ms)
        self.start_dt = datetime.fromtimestamp(self.start_ms / 1000, self.tz)
        self.end_dt = datetime.fromtimestamp(self.end_ms / 1000, self.tz)

        self.session = make_session()
        self._next_allowed_time = defaultdict(float)
        self._domain_delay = {"search.people.cn": delay, "www.people.com.cn": delay, "people.com.cn": delay}
        self.results, self._seen = [], set()

    def _throttle(self, host: str):
        delay = self._domain_delay.get(host, 0)
        now = time.time()
        next_at = self._next_allowed_time.get(host, 0.0)
        if delay > 0 and next_at > now:
            time.sleep(max(0.0, next_at - now))
        if delay > 0:
            self._next_allowed_time[host] = time.time() + delay

    def _post_with_throttle(self, url, **kwargs):
        self._throttle(urlparse(url).netloc)
        return self.session.post(url, **kwargs)

    def _get_with_throttle(self, url, **kwargs):
        self._throttle(urlparse(url).netloc)
        return self.session.get(url, **kwargs)

    def _search_api_page(self, api_url: str, page: int):
        payload = {
            "key": self.keyword, "page": page, "limit": self.page_limit,
            "hasTitle": True, "hasContent": True, "isFuzzy": True,
            "type": 0, "sortType": 2, "startTime": self.start_ms, "endTime": self.end_ms,
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "http://search.people.cn",
            "Referer": f"http://search.people.cn/s/?keyword={quote_plus(self.keyword)}&page={page}",
        }
        try:
            r = self._post_with_throttle(api_url, json=payload, headers=headers, timeout=25)
            if r.status_code != 200:
                return []
            j = r.json()
        except Exception:
            return []
        data = j.get("data") or j
        records = (data.get("records") or data.get("list") or data.get("items") or data.get("homePageRecords") or [])
        out = []
        for rec in records:
            title = strip_html(rec.get("title") or rec.get("showTitle") or "")
            url = (rec.get("url") or rec.get("articleUrl") or rec.get("pcUrl") or "").strip()
            ts = rec.get("displayTime") or rec.get("publishTime") or rec.get("pubTimeLong")
            if not (title and url and ts): continue
            ts = int(ts)
            if not (self.start_ms <= ts <= self.end_ms): continue
            dt_str = datetime.fromtimestamp(ts / 1000, self.tz).strftime("%Y-%m-%d %H:%M")
            digest = strip_html(rec.get("content") or rec.get("abs") or rec.get("summary") or "")
            source = norm(rec.get("belongsName") or rec.get("mediaName") or rec.get("siteName") or "人民网")
            out.append({"title": title, "url": url, "source": source, "date": dt_str[:10], "datetime": dt_str, "content": digest[:160]})
        return out

    def _fallback_html_page(self, page: int):
        url = f"https://search.people.cn/s/?keyword={quote_plus(self.keyword)}&page={page}"
        try:
            resp = self._get_with_throttle(url, timeout=25)
            resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code != 200: return []
            soup = BeautifulSoup(resp.text, "html.parser")
            scope = soup
            for sel in ["div.article", "div.content", "div.search", "div.main-container", "div.module-common"]:
                t = soup.select_one(sel)
                if t: scope = t; break
            nodes = []
            for sel in ["div.article li", "ul li", "li"]:
                nodes = scope.select(sel)
                if nodes: break
            days = days_between(self.start_dt, self.end_dt)
            out = []
            for li in nodes:
                if "page" in " ".join(li.get("class") or []): continue
                pub = li.select_one(".tip-pubtime")
                a = li.select_one("a[href]")
                if not pub or not a: continue
                m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", pub.get_text(" ", strip=True))
                if not m: continue
                d = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                if d not in days: continue
                title = norm(a.get_text()); href = (a.get("href") or "").strip()
                if not title or not href or href.startswith(("#", "javascript")): continue
                full_url = urljoin(url, href)
                abs_el = li.select_one(".abs")
                digest = norm(abs_el.get_text(" ", strip=True)) if abs_el else norm(li.get_text(" ", strip=True))
                out.append({"title": title, "url": full_url, "source": "人民网（搜索）", "date": d, "datetime": d + " 00:00", "content": digest[:160]})
            return out
        except Exception:
            return []

    def _push_if_new(self, item):
        key = item["url"]
        if key in self._seen: return False
        self._seen.add(key); self.results.append(item); return True

    def run(self):
        added_total = 0
        for page in range(1, self.max_pages + 1):
            page_items = []
            for api in self.API_URLS:
                page_items = self._search_api_page(api, page)
                if page_items: break
            if not page_items: page_items = self._fallback_html_page(page)
            for it in page_items:
                if self._push_if_new(it): added_total += 1
        print(f"[People.cn|{self.keyword}] 抓到 {added_total} 条。")
        return self.results

# ====================== 模块B：HR 多站点（仅当天，剔除人民网） ======================
HR_SAVE_FORMAT = os.getenv("HR_SAVE_FORMAT", "none").strip().lower()
HR_MAX_PER_SOURCE = int(os.getenv("HR_MAX_ITEMS", "10"))
HR_ONLY_TODAY = os.getenv("HR_ONLY_TODAY", "1").strip().lower() in ("1", "true", "yes", "y")
HR_TZ_STR = os.getenv("HR_TZ", "Asia/Shanghai").strip()
HR_REQUIRE_ALL = os.getenv("HR_REQUIRE_ALL", "0").strip().lower() in ("1","true","yes","y")
HR_KEYWORDS = [k.strip() for k in re.split(r"[,\s，；;|]+", os.getenv("HR_FILTER_KEYWORDS", "人力资源,外包")) if k.strip()]
EXCLUDE_DOMAINS = {"people.com.cn", "www.people.com.cn"}  # 统一剔除人民网域

def now_tz():
    return datetime.now(ZoneInfo(HR_TZ_STR))

DEFAULT_SELECTORS = [
    ".list li", ".news-list li", ".content-list li", ".box-list li",
    "ul.list li", "ul.news li", "ul li", "li"
]

def as_list(env_name: str, defaults: list[str]) -> list[str]:
    raw = os.getenv(env_name, "").strip()
    if not raw: return defaults
    return [u.strip() for u in raw.split(",") if u.strip()]

class HRNewsCrawler:
    def __init__(self):
        self.session = make_session()
        self.results, self._seen = [], set()

    def crawl_generic(self, source_name: str, base: str | None, list_urls: list[str], selectors=None):
        if not list_urls: return
        selectors = selectors or DEFAULT_SELECTORS
        total = 0
        for url in list_urls:
            if total >= HR_MAX_PER_SOURCE: break
            try:
                resp = self.session.get(url, timeout=(6.1, 20))
                resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
                if resp.status_code != 200: continue
                soup = BeautifulSoup(resp.text, "html.parser")
                items = []
                for css in selectors:
                    items = soup.select(css)
                    if items: break
                if not items: items = soup.select("a")
                for node in items:
                    if total >= HR_MAX_PER_SOURCE: break
                    a = node if node.name == "a" else node.find("a")
                    if not a: continue
                    title = norm(a.get_text())
                    if not title: continue
                    href = a.get("href") or ""
                    full_url = urljoin(base or url, href)

                    # 剔除人民网域
                    host = urlparse(full_url).netloc.lower()
                    if host in EXCLUDE_DOMAINS:
                        continue

                    date_text = self._find_date(node) or self._find_date(a)
                    if HR_ONLY_TODAY and (not date_text or not self._is_today(date_text)): continue

                    snippet = self._snippet(node)
                    if not self._hit_keywords(title, snippet): continue

                    item = {"title": title, "url": full_url, "source": source_name, "date": date_text, "content": snippet}
                    if self._push_if_new(item):
                        total += 1
            except Exception:
                continue

    # 站点列表（可用环境变量覆盖入口）
    def crawl_mohrss(self):
        urls = as_list("SRC_MOHRSS_URLS", [
            "https://www.mohrss.gov.cn/",
            "https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/gzdt/index.html",
            "https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/tzgg/index.html",
        ])
        self.crawl_generic("人社部", "https://www.mohrss.gov.cn", urls)

    def crawl_people(self):  # 仍保留入口，但被 EXCLUDE_DOMAINS 屏蔽
        urls = as_list("SRC_PEOPLE_URLS", ["http://www.people.com.cn/"])
        self.crawl_generic("人民网", "http://www.people.com.cn", urls)

    def crawl_gmw(self):
        urls = as_list("SRC_GMW_URLS", ["https://www.gmw.cn/"])
        self.crawl_generic("光明日报", "https://www.gmw.cn", urls)

    def crawl_beijing_hrss(self):
        urls = as_list("SRC_RSJ_BJ_URLS", [
            "https://rsj.beijing.gov.cn/xxgk/tzgg/",
            "https://rsj.beijing.gov.cn/xxgk/gzdt/",
            "https://rsj.beijing.gov.cn/xxgk/zcfg/",
        ])
        self.crawl_generic("北京人社局", "https://rsj.beijing.gov.cn", urls)

    def crawl_xinhua(self):
        urls = as_list("SRC_XINHUA_URLS", ["https://www.xinhuanet.com/"])
        self.crawl_generic("新华网", "https://www.xinhuanet.com", urls)

    def crawl_chrm(self):
        urls = as_list("SRC_CHRM_URLS", ["https://chrm.mohrss.gov.cn/"])
        self.crawl_generic("中国人力资源市场网", "https://chrm.mohrss.gov.cn", urls)

    def crawl_job_mohrss(self):
        urls = as_list("SRC_JOB_MOHRSS_URLS", ["http://job.mohrss.gov.cn/"])
        self.crawl_generic("中国公共招聘网", "http://job.mohrss.gov.cn", urls)

    def crawl_newjobs(self):
        urls = as_list("SRC_NEWJOBS_URLS", ["https://www.newjobs.com.cn/"])
        self.crawl_generic("中国国家人才网", "https://www.newjobs.com.cn", urls)

    def crawl_hrloo(self):
        urls = as_list("SRC_HRLOO_URLS", ["https://www.hrloo.com/"])
        self.crawl_generic("三茅人力资源网", "https://www.hrloo.com", urls)

    def crawl_hroot(self):
        urls = as_list("SRC_HROOT_URLS", ["https://www.hroot.com/"])
        self.crawl_generic("HRoot", "https://www.hroot.com", urls)

    def crawl_chinatax(self):
        urls = as_list("SRC_CHINATAX_URLS", ["https://www.chinatax.gov.cn/"])
        self.crawl_generic("国家税务总局", "https://www.chinatax.gov.cn", urls)

    def crawl_bjsfj(self):
        urls = as_list("SRC_BJ_SFJ_URLS", ["https://sfj.beijing.gov.cn/", "https://sfj.beijing.gov.cn/zwgk/"])
        self.crawl_generic("北京市司法局", "https://sfj.beijing.gov.cn", urls)

    def crawl_si_12333(self):
        urls = as_list("SRC_SI_12333_URLS", ["https://si.12333.gov.cn/"])
        self.crawl_generic("国家社会保险平台", "https://si.12333.gov.cn", urls)

    def crawl_chinahrm(self):
        urls = as_list("SRC_CHINAHRM_URLS", ["https://www.chinahrm.cn/"])
        self.crawl_generic("中人网·人力资源频道", "https://www.chinahrm.cn", urls)

    def crawl_newjobs_policy(self):
        urls = as_list("SRC_NEWJOBS_POLICY_URLS", ["https://www.newjobs.com.cn/"])
        self.crawl_generic("中国国家人才网·政策法规", "https://www.newjobs.com.cn", urls)

    def crawl_bj_hr_associations(self):
        urls = as_list("SRC_BJ_HR_ASSOC_URLS", [])
        if urls:
            self.crawl_generic("北京人力资源服务协会", None, urls)

    def crawl_stats(self):
        urls = as_list("SRC_STATS_URLS", ["https://www.stats.gov.cn/"])
        self.crawl_generic("国家统计局", "https://www.stats.gov.cn", urls)

    def get_today_news(self):
        fns = [
            self.crawl_beijing_hrss, self.crawl_mohrss, self.crawl_people, self.crawl_gmw, self.crawl_xinhua,
            self.crawl_chrm, self.crawl_job_mohrss, self.crawl_newjobs, self.crawl_hrloo, self.crawl_hroot,
            self.crawl_chinatax, self.crawl_bjsfj, self.crawl_si_12333, self.crawl_chinahrm,
            self.crawl_newjobs_policy, self.crawl_bj_hr_associations, self.crawl_stats,
        ]
        for fn in fns:
            try:
                fn(); time.sleep(0.6)
            except Exception:
                continue
        print(f"[HR多站点] 抓到 {len(self.results)} 条（已剔除人民网域）。")
        return self.results

    # 工具
    def _push_if_new(self, item: dict) -> bool:
        key = item.get("url") or f"{item.get('title','')}|{item.get('date','')}"
        if key in self._seen: return False
        self._seen.add(key); self.results.append(item); return True

    @staticmethod
    def _snippet(node) -> str:
        try:
            text = node.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            return (text[:100] + "...") if len(text) > 100 else text
        except Exception:
            return "内容获取中..."

    def _find_date(self, node) -> str:
        if not node: return ""
        raw = node.get_text(" ", strip=True)
        if re.search(r"(刚刚|分钟|小时前|今日|今天)", raw): return now_tz().strftime("%Y-%m-%d")
        m_rel = re.search(r"(\d+)\s*(分钟|小时)前", raw)
        if m_rel: return now_tz().strftime("%Y-%m-%d")
        m_today_hm = re.search(r"今天\s*\d{1,2}:\d{1,2}", raw)
        if m_today_hm: return now_tz().strftime("%Y-%m-%d")
        normtxt = raw.replace("年","-").replace("月","-").replace("日","-").replace("/", "-").replace(".", "-")
        m = re.search(r"(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})", normtxt)
        if m: return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m2 = re.search(r"\b(\d{1,2})-(\d{1,2})\b", normtxt)
        if m2: return f"{now_tz().year:04d}-{int(m2.group(1)):02d}-{int(m2.group(2)):02d}"
        return ""

    def _parse_date(self, s: str):
        if not s: return None
        s = s.strip().replace("年","-").replace("月","-").replace("日","-").replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d", "%y-%m-%d", "%Y-%m", "%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%m-%d": dt = dt.replace(year=now_tz().year)
                if fmt == "%y-%m-%d" and dt.year < 2000: dt = dt.replace(year=2000 + dt.year % 100)
                return dt.replace(tzinfo=ZoneInfo(HR_TZ_STR))
            except ValueError:
                continue
        return None

    def _is_today(self, date_str: str) -> bool:
        dt = self._parse_date(date_str)
        return bool(dt and dt.date() == now_tz().date())

    def _hit_keywords(self, title: str, content: str) -> bool:
        if not HR_KEYWORDS: return True
        hay = (title or "") + " " + (content or "")
        hay_low = hay.lower()
        kws_low = [k.lower() for k in HR_KEYWORDS]
        if HR_REQUIRE_ALL:
            return all(k in hay_low for k in kws_low)
        return any(k in hay_low for k in kws_low)

# ====================== 构建“合并推送 & 连续编号”的正文 ======================
def build_unified_markdown(tz_str: str, people_blocks: list[list[dict]], hr_items: list[dict], total_limit: int = 20):
    """people_blocks: [[items for kw1], [items for kw2], ...]；hr_items: HR聚合结果"""
    tz = ZoneInfo(tz_str)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    wd = zh_weekday(datetime.now(tz))

    # 合并顺序：People（按关键词顺序）→ HR
    merged = []
    for block in people_blocks:
        merged.extend(block)
    # 去重：按URL
    seen = set()
    uniq = []
    for it in merged + hr_items:
        url = it.get("url") or ""
        if url in seen:
            continue
        seen.add(url)
        uniq.append(it)

    # 截断总条数
    if total_limit and total_limit > 0:
        uniq = uniq[:total_limit]

    lines = [
        f"**日期：{today_str}（{wd}）**",
        "",
        f"**标题：早安资讯｜综合**",
        "",
        "**主要内容**",
    ]
    if not uniq:
        lines.append("> 暂无更新。")
        return "\n".join(lines)

    for i, it in enumerate(uniq, 1):
        # 在标题行后追加轻量来源（不增加层级）
        title_line = f"{i}. [{it['title']}]({it['url']})"
        if it.get("source"):
            title_line += f"　—　*{it['source']}*"
        lines.append(title_line)
        if it.get("content"):
            lines.append(f"> {it['content'][:120]}")
        lines.append("")
    return "\n".join(lines)

# ====================== 运行入口 ======================
def compute_people_window(args):
    tz = ZoneInfo(args.tz)
    if args.since or args.until:
        end_dt = parse_local_dt(args.until, tz) if args.until else datetime.now(tz)
        start_dt = parse_local_dt(args.since, tz) if args.since else end_dt - timedelta(hours=args.window_hours)
    else:
        end_dt = datetime.now(tz)
        start_dt = end_dt - timedelta(hours=args.window_hours)
    if start_dt >= end_dt:
        raise ValueError("开始时间必须早于结束时间")
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)

def run_people(args):
    start_ms, end_ms = compute_people_window(args)
    kws = parse_keywords_arg(args.keywords, args.keyword)
    people_blocks = []
    all_urls = set()
    for kw in kws:
        spider = PeopleSearch(
            keyword=kw, max_pages=args.pages, delay=args.delay, tz=args.tz,
            start_ms=start_ms, end_ms=end_ms, page_limit=args.page_size,
        )
        items = spider.run() or []
        # 去重（跨关键词）
        block = []
        for it in items:
            if it["url"] in all_urls:
                continue
            all_urls.add(it["url"])
            block.append(it)
        people_blocks.append(block)

        # 单独推送（仅 people 模式使用）
        if args.mode == "people":
            md = build_unified_markdown(args.tz, [block], [], args.limit)
            send_dingtalk_markdown(f"早安资讯｜{kw}", md)
    return people_blocks

def run_hr(args, people_seen_urls=None):
    crawler = HRNewsCrawler()
    items = crawler.get_today_news() or []
    # 跨模块去重（避免与 People.cn 重复）
    if people_seen_urls:
        items = [it for it in items if it.get("url") not in people_seen_urls]
    # 单独推送（仅 hr 模式使用）
    if args.mode == "hr":
        md = build_unified_markdown(args.tz, [], items, args.limit)
        send_dingtalk_markdown("早安资讯｜HR综合", md)
    return items

def run_both(args):
    # 先跑 People，各关键词块 + URL 集合
    people_blocks = run_people(args)
    seen_urls = set()
    for blk in people_blocks:
        for it in blk:
            if it.get("url"):
                seen_urls.add(it["url"])

    # 再跑 HR，并剔除与 People 重复
    hr_items = run_hr(args, seen_urls)

    if args.separate:
        # 分别推送
        for blk in people_blocks:
            if blk:
                md = build_unified_markdown(args.tz, [blk], [], args.limit)
                send_dingtalk_markdown("早安资讯｜人民网", md)
        if hr_items:
            md = build_unified_markdown(args.tz, [], hr_items, args.limit)
            send_dingtalk_markdown("早安资讯｜HR综合（非人民网）", md)
    else:
        # 合并推送，序号连续
        md = build_unified_markdown(args.tz, people_blocks, hr_items, args.limit)
        send_dingtalk_markdown("早安资讯｜综合", md)

def main():
    parser = argparse.ArgumentParser(description="People.cn + HR 多站合并推送（连续编号）")
    sub = parser.add_subparsers(dest="mode")  # 允许不填（见下）

    # People 子命令
    p = sub.add_parser("people", help="People.cn 站内搜索（最近N小时；多关键词顺序抓取）")
    p.add_argument("--keyword", default="外包", help="单个关键词（兼容旧参数）")
    p.add_argument("--keywords", default="外包,人力资源,", help="多个关键词，逗号/空格/竖线分隔")
    p.add_argument("--pages", type=int, default=1, help="每个关键词最多翻页数（默认1）")
    p.add_argument("--delay", type=int, default=120, help="同域请求间隔秒（默认120）")
    p.add_argument("--tz", default="Asia/Shanghai", help="时区（默认Asia/Shanghai）")
    p.add_argument("--window-hours", type=int, default=24, help="最近N小时（默认24）")
    p.add_argument("--since", default=None, help="开始时间，如 '2025-09-11 08:00'")
    p.add_argument("--until", default=None, help="结束时间，如 '2025-09-12 08:00'")
    p.add_argument("--page-size", type=int, default=20, help="每页条数（默认20，最大50）")
    p.add_argument("--limit", type=int, default=20, help="合并时展示总条数上限（默认20）")
    p.add_argument("--separate", action="store_true", help="both 模式下改为分别推送两条")

    # HR 子命令
    h = sub.add_parser("hr", help="HR 多站点资讯（当天，默认剔除人民网）")
    h.add_argument("--tz", default="Asia/Shanghai", help="时区（默认Asia/Shanghai）")
    h.add_argument("--limit", type=int, default=20, help="合并时展示总条数上限（默认20）")
    h.add_argument("--separate", action="store_true", help="both 模式下改为分别推送两条")

    # both 子命令
    b = sub.add_parser("both", help="两者都跑（默认合并推送一个消息，序号连续）")
    b.add_argument("--keyword", default="外包", help="单个关键词（兼容旧参数）")
    b.add_argument("--keywords", default="外包,人力资源,派遣", help="多个关键词，逗号/空格/竖线分隔")
    b.add_argument("--pages", type=int, default=1, help="每个关键词最多翻页数（默认1）")
    b.add_argument("--delay", type=int, default=120, help="同域请求间隔秒（默认120）")
    b.add_argument("--tz", default="Asia/Shanghai", help="时区（默认Asia/Shanghai）")
    b.add_argument("--window-hours", type=int, default=24, help="最近N小时（默认24）")
    b.add_argument("--since", default=None, help="开始时间")
    b.add_argument("--until", default=None, help="结束时间")
    b.add_argument("--page-size", type=int, default=20, help="每页条数（默认20，最大50）")
    b.add_argument("--limit", type=int, default=20, help="合并时展示总条数上限（默认20）")
    b.add_argument("--separate", action="store_true", help="改为分别推送两条")

    args = parser.parse_args()
    # 兼容：未填 mode 时默认 both
    if not getattr(args, "mode", None):
        args.mode = os.getenv("MODE", "both").lower()
        if args.mode not in {"people", "hr", "both"}:
            args.mode = "both"

    if args.mode == "people":
        run_people(args)
    elif args.mode == "hr":
        run_hr(args)
    else:
        run_both(args)

if __name__ == "__main__":
    main()
