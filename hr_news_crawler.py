# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）专抓版
- 仅抓取 HRLoo，当天信息；统一 Markdown 推送钉钉；支持总条数上限、关键词过滤
- 继承原逻辑：只抓当天（可用 HR_ONLY_TODAY 关闭）、关键词命中、旧 TLS 兼容、重试等
- 环境变量兼容：
    DINGTALK_BASE / DINGTALK_SECRET / DINGTALK_KEYWORD
    HR_FILTER_KEYWORDS（默认：人力资源，逗号/空格分隔）
    HR_REQUIRE_ALL（默认0，设为1表示需要命中全部关键词）
    HR_ONLY_TODAY（默认1，当天）
    HR_TZ（默认 Asia/Shanghai）
    HR_MAX_ITEMS（默认10，单站最大抓取条数）
    SRC_HRLOO_URLS（默认：https://www.hrloo.com/）
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
from datetime import datetime
import ssl

# 兼容 Py<3.9 的 zoneinfo
try:
    from zoneinfo import ZoneInfo  # Py3.9+
except Exception:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # pip install backports.zoneinfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ====================== DingTalk 统一配置 ======================
DEFAULT_DINGTALK_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send?access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
)
DEFAULT_DINGTALK_SECRET = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"

DINGTALK_BASE   = os.getenv("DINGTALK_BASEA", DEFAULT_DINGTALK_WEBHOOK).strip()
DINGTALK_SECRET = os.getenv("DINGTALK_SECRETA", DEFAULT_DINGTALK_SECRET).strip()
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
    import re as _re
    return _re.sub(r"\s+", " ", (s or "").replace("\u3000", " ").strip())

def zh_weekday(dt):
    return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

def now_tz():
    tz = ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai").strip())
    return datetime.now(tz)

# ====================== 运行参数 & 过滤配置 ======================
HR_MAX_PER_SOURCE = int(os.getenv("HR_MAX_ITEMS", "10"))
HR_ONLY_TODAY = os.getenv("HR_ONLY_TODAY", "1").strip().lower() in ("1", "true", "yes", "y")
HR_TZ_STR = os.getenv("HR_TZ", "Asia/Shanghai").strip()
HR_REQUIRE_ALL = os.getenv("HR_REQUIRE_ALL", "0").strip().lower() in ("1","true","yes","y")
HR_KEYWORDS = [k.strip() for k in re.split(r"[,\s，；;|]+", os.getenv("HR_FILTER_KEYWORDS", "人力资源")) if k.strip()]

DEFAULT_SELECTORS = [
    ".list li", ".news-list li", ".content-list li", ".box-list li",
    "ul.list li", "ul.news li", "ul li", "li"
]

def as_list(env_name: str, defaults: list[str]) -> list[str]:
    raw = os.getenv(env_name, "").strip()
    if not raw: return defaults
    return [u.strip() for u in raw.split(",") if u.strip()]

# ====================== HRLoo 专抓 ======================
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results, self._seen = [], set()

    def crawl_hrloo(self):
        urls = as_list("SRC_HRLOO_URLS", ["https://www.hrloo.com/"])
        self._crawl_generic("三茅人力资源网", "https://www.hrloo.com", urls)

    # 通用抓取：只用于 HRLoo
    def _crawl_generic(self, source_name: str, base: str | None, list_urls: list[str], selectors=None):
        if not list_urls: return
        selectors = selectors or DEFAULT_SELECTORS
        total = 0
        for url in list_urls:
            if total >= HR_MAX_PER_SOURCE: break
            try:
                resp = self.session.get(url, timeout=(6.1, 20))
                resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
                if resp.status_code != 200: 
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                items = []
                for css in selectors:
                    items = soup.select(css)
                    if items: break
                if not items: 
                    items = soup.select("a")
                for node in items:
                    if total >= HR_MAX_PER_SOURCE: break
                    a = node if getattr(node, "name", None) == "a" else node.find("a")
                    if not a: 
                        continue
                    title = norm(a.get_text())
                    if not title: 
                        continue
                    href = a.get("href") or ""
                    full_url = urljoin(base or url, href)

                    # 解析日期（只识别“今天/刚刚/xx分钟前/标准日期格式”等）
                    date_text = self._find_date(node) or self._find_date(a)
                    if HR_ONLY_TODAY and (not date_text or not self._is_today(date_text)): 
                        continue

                    snippet = self._snippet(node)
                    if not self._hit_keywords(title, snippet): 
                        continue

                    item = {
                        "title": title, 
                        "url": full_url, 
                        "source": source_name, 
                        "date": date_text, 
                        "content": snippet
                    }
                    if self._push_if_new(item):
                        total += 1
            except Exception:
                continue

    # 工具函数
    def _push_if_new(self, item: dict) -> bool:
        key = item.get("url") or f"{item.get('title','')}|{item.get('date','')}"
        if key in self._seen: 
            return False
        self._seen.add(key)
        self.results.append(item)
        return True

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
        if re.search(r"(刚刚|分钟|小时前|今日|今天)", raw): 
            return now_tz().strftime("%Y-%m-%d")
        m_rel = re.search(r"(\d+)\s*(分钟|小时)前", raw)
        if m_rel: 
            return now_tz().strftime("%Y-%m-%d")
        m_today_hm = re.search(r"今天\s*\d{1,2}:\d{1,2}", raw)
        if m_today_hm: 
            return now_tz().strftime("%Y-%m-%d")
        normtxt = raw.replace("年","-").replace("月","-").replace("日","-").replace("/", "-").replace(".", "-")
        m = re.search(r"(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})", normtxt)
        if m: 
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m2 = re.search(r"\b(\d{1,2})-(\d{1,2})\b", normtxt)
        if m2: 
            return f"{now_tz().year:04d}-{int(m2.group(1)):02d}-{int(m2.group(2)):02d}"
        return ""

    def _parse_date(self, s: str):
        if not s: return None
        s = s.strip().replace("年","-").replace("月","-").replace("日","-").replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d", "%y-%m-%d", "%Y-%m", "%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%m-%d": 
                    dt = dt.replace(year=now_tz().year)
                if fmt == "%y-%m-%d" and dt.year < 2000: 
                    dt = dt.replace(year=2000 + dt.year % 100)
                return dt.replace(tzinfo=ZoneInfo(HR_TZ_STR))
            except ValueError:
                continue
        return None

    def _is_today(self, date_str: str) -> bool:
        dt = self._parse_date(date_str)
        return bool(dt and dt.date() == now_tz().date())

    def _hit_keywords(self, title: str, content: str) -> bool:
        if not HR_KEYWORDS: 
            return True
        hay = (title or "") + " " + (content or "")
        hay_low = hay.lower()
        kws_low = [k.lower() for k in HR_KEYWORDS]
        if HR_REQUIRE_ALL:
            return all(k in hay_low for k in kws_low)
        return any(k in hay_low for k in kws_low)

# ====================== 构建推送正文（仅 HRLoo） ======================
def build_markdown(hr_items: list[dict], tz_str: str, total_limit: int = 20):
    tz = ZoneInfo(tz_str)
    now_dt = datetime.now(tz)
    today_str = now_dt.strftime("%Y-%m-%d")
    wd = zh_weekday(now_dt)

    items = hr_items[:total_limit] if (total_limit and total_limit > 0) else hr_items

    lines = [
        f"**日期：{today_str}（{wd}）**",
        "",
        f"**标题：早安资讯｜人力资源每日资讯推送**",
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
def main():
    parser = argparse.ArgumentParser(description="人力资源每日资讯推送")
    parser.add_argument("--tz", default=os.getenv("HR_TZ", "Asia/Shanghai"), help="时区（默认Asia/Shanghai）")
    parser.add_argument("--limit", type=int, default=20, help="展示总条数上限（默认20）")
    parser.add_argument("--no-push", action="store_true", help="只打印不推送钉钉")
    args = parser.parse_args()

    crawler = HRLooCrawler()
    crawler.crawl_hrloo()
    items = crawler.results or []
    print(f"[HRLoo] 抓到 {len(items)} 条。")

    md = build_markdown(items, args.tz, args.limit)
    print("\n===== Markdown Preview =====\n")
    print(md)

    if not args.no_push:
        send_dingtalk_markdown("早安资讯｜人力资源资讯推送", md)

if __name__ == "__main__":
    main()
