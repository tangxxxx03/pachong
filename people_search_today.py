# -*- coding: utf-8 -*-
"""
People.cn 站内搜索（最近N小时；顺序抓取多关键词）→ 钉钉推送
* 正文极简：仅包含【日期】【标题】【主要内容】
* 直连官方搜索接口（POST），失败自动回退 HTML 解析
* 每个关键词单独保存、单独推送
用法示例：
  python people_seq_simple.py --keywords "外包,人力资源,派遣" --pages 2 --window-hours 24
"""

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

# ====== 钉钉（硬编码）======
DINGTALK_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send?"
    "access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
)
DINGTALK_SECRET = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"


def _sign_webhook(base_webhook: str, secret: str) -> str:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"


def send_dingtalk_markdown(title: str, md_text: str) -> bool:
    try:
        webhook = _sign_webhook(DINGTALK_WEBHOOK, DINGTALK_SECRET)
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
        r = requests.post(webhook, json=payload, timeout=20)
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print("DingTalk resp:", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False


# ====== HTTP 会话 ======
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
    })
    retries = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# ====== 工具 ======
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def strip_html(html: str) -> str:
    if not html:
        return ""
    return norm(BeautifulSoup(html, "html.parser").get_text(" "))


def zh_weekday(dt):
    return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]


def parse_local_dt(s: str, tz: ZoneInfo) -> datetime:
    """'YYYY-MM-DD HH:MM' / 'YYYY-MM-DDTHH:MM' / 'YYYY-MM-DD'"""
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


def parse_keywords_arg(args) -> list:
    """--keywords 或 --keyword，分隔符：逗号/空格/竖线；去重保序"""
    if args.keywords:
        parts = re.split(r"[,\s|]+", args.keywords.strip())
        kws = [p for p in (x.strip() for x in parts) if p]
        return list(dict.fromkeys(kws))
    return [args.keyword]


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


# ====== 核心抓取类 ======
class PeopleSearch:
    API_URLS = [
        "http://search.people.cn/search-platform/front/search",
        "http://search.people.cn/api-search/front/search",
    ]

    def __init__(self, keyword="外包", max_pages=1, delay=120,
                 tz="Asia/Shanghai", start_ms: int = None, end_ms: int = None, page_limit=20):
        self.keyword = keyword
        self.max_pages = max_pages
        self.page_limit = max(1, min(50, int(page_limit)))
        self.tz = ZoneInfo(tz)

        # —— 时间窗（毫秒）——
        if start_ms is None or end_ms is None:
            now = datetime.now(self.tz)
            self.start_ms = int((now - timedelta(hours=24)).timestamp() * 1000)
            self.end_ms = int(now.timestamp() * 1000)
        else:
            self.start_ms = int(start_ms); self.end_ms = int(end_ms)
        self.start_dt = datetime.fromtimestamp(self.start_ms / 1000, self.tz)
        self.end_dt = datetime.fromtimestamp(self.end_ms / 1000, self.tz)

        self.session = make_session()
        # 按域名节流（遵守 robots）
        self._next_allowed_time = defaultdict(float)
        self._domain_delay = {
            "search.people.cn": delay,
            "www.people.com.cn": delay,
            "people.com.cn": delay,
        }
        self.results = []
        self._seen = set()

    # —— 节流 GET/POST —— #
    def _throttle(self, host: str):
        delay = self._domain_delay.get(host, 0)
        now = time.time()
        next_at = self._next_allowed_time.get(host, 0.0)
        if delay > 0 and next_at > now:
            time.sleep(max(0.0, next_at - now))
        if delay > 0:
            self._next_allowed_time[host] = time.time() + delay

    def _post_with_throttle(self, url, **kwargs):
        host = urlparse(url).netloc
        self._throttle(host)
        return self.session.post(url, **kwargs)

    def _get_with_throttle(self, url, **kwargs):
        host = urlparse(url).netloc
        self._throttle(host)
        return self.session.get(url, **kwargs)

    # —— 通过接口抓取一页 —— #
    def _search_api_page(self, api_url: str, page: int):
        payload = {
            "key": self.keyword,
            "page": page,
            "limit": self.page_limit,
            "hasTitle": True,
            "hasContent": True,
            "isFuzzy": True,
            "type": 0,         # 0=全部
            "sortType": 2,     # 2=时间降序
            "startTime": self.start_ms,
            "endTime": self.end_ms,
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "http://search.people.cn",
            "Referer": f"http://search.people.cn/s/?keyword={quote_plus(self.keyword)}&page={page}",
        }
        try:
            r = self._post_with_throttle(api_url, json=payload, headers=headers, timeout=25)
            if r.status_code != 200:
                print(f"  API {api_url} 第{page}页 HTTP {r.status_code}")
                return []
            j = r.json()
        except Exception as e:
            print(f"  API {api_url} 第{page}页解析异常：{e}")
            return []

        data = j.get("data") or j
        records = (data.get("records") or data.get("list") or data.get("items") or data.get("homePageRecords") or [])
        out = []
        for rec in records:
            title = strip_html(rec.get("title") or rec.get("showTitle") or "")
            url = (rec.get("url") or rec.get("articleUrl") or rec.get("pcUrl") or "").strip()
            ts = rec.get("displayTime") or rec.get("publishTime") or rec.get("pubTimeLong")
            if not (title and url and ts):
                continue
            ts = int(ts)
            if not (self.start_ms <= ts <= self.end_ms):
                continue
            dt_str = datetime.fromtimestamp(ts / 1000, self.tz).strftime("%Y-%m-%d %H:%M")
            digest = strip_html(rec.get("content") or rec.get("abs") or rec.get("summary") or "")
            source = norm(rec.get("belongsName") or rec.get("mediaName") or rec.get("siteName") or "人民网")
            out.append({
                "title": title,
                "url": url,
                "source": source,
                "date": dt_str[:10],
                "datetime": dt_str,
                "content": digest[:160],
            })
        return out

    # —— HTML 兜底（窗口内自然日）—— #
    def _fallback_html_page(self, page: int):
        url = f"https://search.people.cn/s/?keyword={quote_plus(self.keyword)}&page={page}"
        try:
            resp = self._get_with_throttle(url, timeout=25)
            resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")

            root = None
            for sel in ["div.article", "div.content", "div.search", "div.main-container", "div.module-common"]:
                root = soup.select_one(sel)
                if root:
                    break
            scope = root or soup

            nodes = []
            for sel in ["div.article li", "ul li", "li"]:
                nodes = scope.select(sel)
                if nodes:
                    break

            days = days_between(self.start_dt, self.end_dt)

            out = []
            for li in nodes:
                classes = " ".join(li.get("class") or [])
                if "page" in classes:
                    continue
                pub = li.select_one(".tip-pubtime")
                a = li.select_one("a[href]")
                if not pub or not a:
                    continue
                d = None
                if pub:
                    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", pub.get_text(" ", strip=True))
                    if m:
                        y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        d = f"{y:04d}-{mo:02d}-{da:02d}"
                if not d or d not in days:
                    continue
                title = norm(a.get_text())
                href = (a.get("href") or "").strip()
                if not title or not href or href.startswith(("#", "javascript")):
                    continue
                full_url = urljoin(url, href)
                abs_el = li.select_one(".abs")
                if abs_el:
                    digest = norm(abs_el.get_text(" ", strip=True))
                else:
                    p = li.find("p")
                    digest = norm(p.get_text(" ", strip=True)) if p else norm(li.get_text(" ", strip=True))
                out.append({
                    "title": title,
                    "url": full_url,
                    "source": "人民网（搜索）",
                    "date": d,
                    "datetime": d + " 00:00",
                    "content": digest[:160],
                })
            return out
        except Exception as e:
            print(f"  HTML 兜底异常 page={page}: {e}")
            return []

    def _push_if_new(self, item):
        key = item["url"]
        if key in self._seen:
            return False
        self._seen.add(key)
        self.results.append(item)
        return True

    def run(self):
        rng = f"{self.start_dt.strftime('%Y-%m-%d %H:%M')} ~ {self.end_dt.strftime('%Y-%m-%d %H:%M')}"
        print(f"\n=== 关键词：{self.keyword} | 时间窗：[{rng}] | 最多 {self.max_pages} 页 ===")
        added_total = 0

        for page in range(1, self.max_pages + 1):
            added_page = 0
            page_items = []
            for api in self.API_URLS:
                items = self._search_api_page(api, page)
                if items:
                    page_items = items
                    break
            if not page_items:
                page_items = self._fallback_html_page(page)

            for it in page_items:
                if self._push_if_new(it):
                    added_page += 1
                    print(f" + {it['title']} | {it.get('datetime', it['date'])}")

            added_total += added_page
            print(f"第{page}页：命中 {added_page} 条。")

        print(f"完成：[{self.keyword}] 共抓到 {added_total} 条窗口内结果。")
        return self.results

    # —— 极简早安版正文（只含 日期 / 标题 / 主要内容）—— #
    def to_markdown(self, limit=12):
        today_str = self.end_dt.strftime("%Y-%m-%d")
        wd = zh_weekday(self.end_dt)

        lines = [
            f"**日期：{today_str}（{wd}）**",
            "",
            f"**标题：早安资讯｜人民网｜{self.keyword}**",
            "",
            "**主要内容**",
        ]
        if not self.results:
            lines.append("> 暂无更新。")
            return "\n".join(lines)

        for i, it in enumerate(self.results[:limit], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
            if it.get("content"):
                lines.append(f"> {it['content'][:120]}")
            lines.append("")
        return "\n".join(lines)

    # —— 可选保存 —— #
    def save(self, fmt="none"):
        if not self.results or fmt == "none":
            return []
        ts = self.end_dt.strftime("%Y%m%d_%H%M%S")
        kwslug = slugify_kw(self.keyword)
        out = []
        if fmt in ("csv", "both"):
            fn = f"people_search_{kwslug}_{ts}.csv"
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["title", "url", "source", "date", "datetime", "content"])
                w.writeheader()
                w.writerows(self.results)
            out.append(fn); print("CSV:", fn)
        if fmt in ("json", "both"):
            fn = f"people_search_{kwslug}_{ts}.json"
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            out.append(fn); print("JSON:", fn)
        return out


# ====== 参数计算 & 主流程 ======
def compute_window_args(args):
    tz = ZoneInfo(args.tz)
    # since/until > window-hours
    if args.since or args.until:
        end_dt = parse_local_dt(args.until, tz) if args.until else datetime.now(tz)
        start_dt = parse_local_dt(args.since, tz) if args.since else end_dt - timedelta(hours=args.window_hours)
    else:
        end_dt = datetime.now(tz)
        start_dt = end_dt - timedelta(hours=args.window_hours)
    if start_dt >= end_dt:
        raise ValueError("开始时间必须早于结束时间")
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000), tz


def main():
    ap = argparse.ArgumentParser(description="People.cn 搜索：最近N小时；顺序抓取多关键词 → 极简早安版推送")
    ap.add_argument("--keyword", default="外包", help="单个关键词（兼容旧参数）")
    ap.add_argument("--keywords", default=None, help="多个关键词，逗号/空格/竖线分隔，如：外包,人力资源,派遣")
    ap.add_argument("--pages", type=int, default=1, help="每个关键词最多翻页数（默认：1）")
    ap.add_argument("--delay", type=int, default=120, help="同域请求间隔秒（默认：120）")
    ap.add_argument("--tz", default="Asia/Shanghai", help="时区（默认：Asia/Shanghai）")
    ap.add_argument("--save", default="none", choices=["csv", "json", "both", "none"], help="是否本地保存（默认：none）")
    ap.add_argument("--window-hours", type=int, default=24, help="最近N小时（默认：24）")
    ap.add_argument("--since", default=None, help="开始时间，如 '2025-09-11 08:00'")
    ap.add_argument("--until", default=None, help="结束时间，如 '2025-09-12 08:00'")
    ap.add_argument("--page-size", type=int, default=20, help="每页条数（默认：20，最大：50）")
    ap.add_argument("--limit", type=int, default=12, help="正文列表最多显示条数（默认：12）")
    args = ap.parse_args()

    start_ms, end_ms, tz = compute_window_args(args)
    kws = parse_keywords_arg(args)

    for kw in kws:
        spider = PeopleSearch(
            keyword=kw,
            max_pages=args.pages,
            delay=args.delay,
            tz=args.tz,
            start_ms=start_ms,
            end_ms=end_ms,
            page_limit=args.page_size,
        )
        spider.run()
        if args.save != "none":
            spider.save(args.save)

        md = spider.to_markdown(limit=args.limit)
        title = f"早安资讯｜{kw}"
        ok = send_dingtalk_markdown(title, md)
        print(f"钉钉推送[{kw}]：", "成功 ✅" if ok else "失败 ❌")


if __name__ == "__main__":
    main()
