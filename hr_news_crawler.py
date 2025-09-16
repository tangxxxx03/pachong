# -*- coding: utf-8 -*-
"""
HR 多站点资讯（不爬人民网）
- 仅抓取 HR 站点，当天信息；统一 Markdown 推送钉钉；支持总条数上限、关键词过滤
- 已彻底删除 People.cn 抓取，并继续对 people.com.cn 域名做域级剔除
- 兼容：both 模式 == hr 模式（保留旧参数但不再使用）
"""

import os
import re
import time
import argparse
import hmac
import hashlib
import base64
import urllib.parse
from urllib.parse import urljoin, urlparse
from datetime import datetime, timedelta
from collections import defaultdict

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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

def now_tz():
    tz = ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai").strip())
    return datetime.now(tz)

# ====================== HR 多站点（仅当天，剔除人民网） ======================
HR_MAX_PER_SOURCE = int(os.getenv("HR_MAX_ITEMS", "10"))
HR_ONLY_TODAY = os.getenv("HR_ONLY_TODAY", "1").strip().lower() in ("1", "true", "yes", "y")
HR_TZ_STR = os.getenv("HR_TZ", "Asia/Shanghai").strip()
HR_REQUIRE_ALL = os.getenv("HR_REQUIRE_ALL", "0").strip().lower() in ("1","true","yes","y")
# 默认仅“人力资源”
HR_KEYWORDS = [k.strip() for k in re.split(r"[,\s，；;|]+", os.getenv("HR_FILTER_KEYWORDS", "人力资源")) if k.strip()]
# 统一剔除人民网域
EXCLUDE_DOMAINS = {"people.com.cn", "www.people.com.cn"}

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

                    # 域级剔除：人民网
                    host = urlparse(full_url).netloc.lower()
                    if host in EXCLUDE_DOMAINS:
                        continue

                    date_text = self._find_date(node) or self._find_date(a)
                    if HR_ONLY_TODAY and (not date_text or not self._is_today(date_text)): 
                        continue

                    snippet = self._snippet(node)
                    if not self._hit_keywords(title, snippet): 
                        continue

                    item = {"title": title, "url": full_url, "source": source_name, "date": date_text, "content": snippet}
                    if self._push_if_new(item):
                        total += 1
            except Exception:
                continue

    # —— 站点列表（可用环境变量覆盖入口）——
    def crawl_mohrss(self):
        urls = as_list("SRC_MOHRSS_URLS", [
            "https://www.mohrss.gov.cn/",
            "https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/gzdt/index.html",
            "https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/tzgg/index.html",
        ])
        self.crawl_generic("人社部", "https://www.mohrss.gov.cn", urls)

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
        # ⚠️ 已剔除人民网，不再调用其入口
        fns = [
            self.crawl_beijing_hrss, self.crawl_mohrss, self.crawl_gmw, self.crawl_xinhua,
            self.crawl_chrm, self.crawl_job_mohrss, self.crawl_newjobs, self.crawl_hrloo, self.crawl_hroot,
            self.crawl_chinatax, self.crawl_bjsfj, self.crawl_si_12333, self.crawl_chinahrm,
            self.crawl_newjobs_policy, self.crawl_bj_hr_associations, self.crawl_stats,
        ]
        for fn in fns:
            try:
                fn(); time.sleep(0.6)
            except Exception:
                continue
        print(f"[HR多站点] 抓到 {len(self.results)} 条（已完全不爬人民网，且域名级剔除 people.com.cn）。")
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

# ====================== 构建推送正文（仅 HR） ======================
def build_markdown_hr(tz_str: str, hr_items: list[dict], total_limit: int = 20):
    tz = ZoneInfo(tz_str)
    now_dt = datetime.now(tz)
    today_str = now_dt.strftime("%Y-%m-%d")
    wd = zh_weekday(now_dt)

    items = hr_items[:total_limit] if (total_limit and total_limit > 0) else hr_items

    lines = [
        f"**日期：{today_str}（{wd}）**",
        "",
        f"**标题：早安资讯｜HR综合**",
        "",
        "**主要内容**",
    ]
    if not items:
        lines.append("> 暂无更新。")
        return "\n".join(lines)

    for i, it in enumerate(items, 1):
        title_line = f"{i}. [{it['title']}]({it['url']})"
        if it.get("source"):
            title_line += f"　—　*{it['source']}*"
        lines.append(title_line)
        if it.get("content"):
            lines.append(f"> {it['content'][:120]}")
        lines.append("")
    return "\n".join(lines)

# ====================== 运行入口 ======================
def run_hr(args):
    crawler = HRNewsCrawler()
    items = crawler.get_today_news() or []
    md = build_markdown_hr(args.tz, items, args.limit)
    send_dingtalk_markdown("早安资讯｜HR综合（不含人民网）", md)
    return items

def run_both(args):
    # 兼容旧口令：both == hr
    return run_hr(args)

def main():
    parser = argparse.ArgumentParser(description="HR 多站合并推送（不爬人民网）")
    sub = parser.add_subparsers(dest="mode")

    # hr 子命令
    h = sub.add_parser("hr", help="HR 多站点资讯（当天，剔除 people.com.cn 域）")
    h.add_argument("--tz", default="Asia/Shanghai", help="时区（默认Asia/Shanghai）")
    h.add_argument("--limit", type=int, default=20, help="展示总条数上限（默认20）")

    # both 子命令（为兼容旧用法而保留，但行为与 hr 相同）
    b = sub.add_parser("both", help="兼容旧命令；行为等同于 hr（不爬人民网）")
    b.add_argument("--tz", default="Asia/Shanghai", help="时区（默认Asia/Shanghai）")
    b.add_argument("--limit", type=int, default=20, help="展示总条数上限（默认20）")
    # 下面这些旧参数会被忽略（保留仅为避免报错）
    b.add_argument("--keyword", default="人力资源")
    b.add_argument("--keywords", default="人力资源")
    b.add_argument("--pages", type=int, default=1)
    b.add_argument("--delay", type=int, default=120)
    b.add_argument("--window-hours", type=int, default=24)
    b.add_argument("--since", default=None)
    b.add_argument("--until", default=None)
    b.add_argument("--page-size", type=int, default=20)
    b.add_argument("--separate", action="store_true")

    args = parser.parse_args()
    if not getattr(args, "mode", None):
        args.mode = os.getenv("MODE", "hr").lower()
        if args.mode not in {"hr", "both"}:
            args.mode = "hr"

    if args.mode == "hr":
        run_hr(args)
    else:
        run_both(args)

if __name__ == "__main__":
    main()
