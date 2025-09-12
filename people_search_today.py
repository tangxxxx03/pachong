# -*- coding: utf-8 -*-
"""
People.cn 站内搜索（最近N小时 + 翻页）→ 自动推送钉钉（加签）
- 直连接口：http://search.people.cn/search-platform/front/search（POST）
- 通过 startTime / endTime 定义“滚动时间窗”（默认：最近24小时，Asia/Shanghai）
- 新增参数：--window-hours / --since / --until（三者任选其一，优先级：since/until > window-hours）
- 接口优先；失败降级到 HTML 解析（放宽到窗口内的自然日）
- 同域节流（默认 120s）+ 重试；保留 CSV/JSON 存档与 Markdown 推送
用法示例：
  # 最近24小时
  python people_search_today.py --keyword 外包 --pages 1 --window-hours 24
  # 指定绝对时间（本地时区 Asia/Shanghai）
  python people_search_today.py --keyword 派遣 --since "2025-09-11 08:00" --until "2025-09-12 08:00"
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
from urllib.parse import urlencode, urljoin, urlparse, quote_plus
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
    s.trust_env = False  # 不继承 runner 的代理
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


DATE_PATTERNS = [
    r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\s+\d{2}:\d{2}:\d{2}",
    r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})",
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日",
]


def find_date_in_text(text: str) -> str:
    t = (text or "").replace("\u3000", " ")
    for pat in DATE_PATTERNS:
        m = re.search(pat, t)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def parse_local_dt(s: str, tz: ZoneInfo) -> datetime:
    """容错解析本地时间字符串到 tz-aware datetime。支持:
       'YYYY-MM-DD HH:MM', 'YYYY-MM-DDTHH:MM', 'YYYY-MM-DD'（默认00:00）"""
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


class PeopleSearch:
    API_URLS = [
        "http://search.people.cn/search-platform/front/search",
        "http://search.people.cn/api-search/front/search",
    ]

    def __init__(self, keyword="外包", max_pages=1, delay=120,
                 tz="Asia/Shanghai", start_ms: int = None, end_ms: int = None):
        self.keyword = keyword
        self.max_pages = max_pages
        self.tz = ZoneInfo(tz)

        # —— 时间窗（毫秒）——
        if start_ms is None or end_ms is None:
            now = datetime.now(self.tz)
            self.start_ms = int((now - timedelta(hours=24)).timestamp() * 1000)
            self.end_ms = int(now.timestamp() * 1000)
        else:
            self.start_ms = int(start_ms)
            self.end_ms = int(end_ms)
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
            "limit": 20,
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
        records = (
            data.get("records")
            or data.get("list")
            or data.get("items")
            or data.get("homePageRecords")
            or []
        )
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

            # 时间窗覆盖到的自然日集合（最多跨两天）
            days = {self.start_dt.strftime("%Y-%m-%d"), self.end_dt.strftime("%Y-%m-%d")}

            out = []
            for li in nodes:
                classes = " ".join(li.get("class") or [])
                if "page" in classes:
                    continue
                pub = li.select_one(".tip-pubtime")
                a = li.select_one("a[href]")
                if not pub or not a:
                    continue
                d = find_date_in_text(pub.get_text(" ", strip=True))
                if d not in days:
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
                    "content": digest[:160]
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
        print(
            f"开始抓取（接口优先）：关键词='{self.keyword}'，"
            f"时间窗=[{self.start_dt.strftime('%Y-%m-%d %H:%M')} ~ {self.end_dt.strftime('%Y-%m-%d %H:%M')}]，"
            f"最多 {self.max_pages} 页（people 需延迟）"
        )
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

        print(f"完成：共抓到 {added_total} 条窗口内结果。")
        return self.results

    def save(self, fmt="both"):
        if not self.results:
            print("无结果可保存。")
            return []
        ts = self.end_dt.strftime("%Y%m%d_%H%M%S")
        out = []
        if fmt in ("csv", "both"):
            fn = f"people_search_{ts}.csv"
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["title", "url", "source", "date", "datetime", "content"])
                w.writeheader()
                w.writerows(self.results)
            out.append(fn)
            print("CSV:", fn)
        if fmt in ("json", "both"):
            fn = f"people_search_{ts}.json"
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            out.append(fn)
            print("JSON:", fn)
        return out

    def to_markdown(self, limit=12):
        if not self.results:
            return (
                f"### 人民网搜索（最近N小时）\n"
                f"**关键词：{self.keyword}**\n"
                f"**时间窗：{self.start_dt.strftime('%Y-%m-%d %H:%M')} ~ {self.end_dt.strftime('%Y-%m-%d %H:%M')} ({self.end_dt.tzinfo.key})**\n"
                f"> 窗口内未找到符合条件的结果。"
            )
        lines = [
            "### 人民网搜索（最近N小时）",
            f"**关键词：{self.keyword}**",
            f"**时间窗：{self.start_dt.strftime('%Y-%m-%d %H:%M')} ~ {self.end_dt.strftime('%Y-%m-%d %H:%M')} ({self.end_dt.tzinfo.key})**",
            "",
            "#### 结果",
        ]
        for i, it in enumerate(self.results[:limit], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
            lines.append(f"> ⏱️ {it.get('datetime', it['date'])} | 🏛️ {it['source']}")
            if it.get("content"):
                lines.append(f"> {it['content'][:120]}")
            lines.append("")
        return "\n".join(lines)


def compute_window_args(args):
    tz = ZoneInfo(args.tz)
    # 优先 since/until
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
    ap = argparse.ArgumentParser(description="People.cn 搜索：最近N小时 + 翻页 → 钉钉推送（接口优先）")
    ap.add_argument("--keyword", default="外包", help="搜索关键词（默认：外包）")
    ap.add_argument("--pages", type=int, default=1, help="最多翻页数（默认：1）")
    ap.add_argument("--delay", type=int, default=120, help="同域请求间隔秒（默认：120，遵守 robots）")
    ap.add_argument("--tz", default="Asia/Shanghai", help="时区（默认：Asia/Shanghai）")
    ap.add_argument("--save", default="both", choices=["csv", "json", "both", "none"], help="保存格式（默认：both）")
    # 新增：时间窗参数
    ap.add_argument("--window-hours", type=int, default=24, help="最近N小时（默认：24）")
    ap.add_argument("--since", default=None, help="开始时间，如 '2025-09-11 08:00'")
    ap.add_argument("--until", default=None, help="结束时间，如 '2025-09-12 08:00'")
    args = ap.parse_args()

    start_ms, end_ms, tz = compute_window_args(args)

    spider = PeopleSearch(
        keyword=args.keyword,
        max_pages=args.pages,
        delay=args.delay,
        tz=args.tz,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    spider.run()
    if args.save != "none":
        spider.save(args.save)

    md = spider.to_markdown()
    ok = send_dingtalk_markdown(f"人民网搜索（{args.keyword}）最近{args.window_hours}小时结果", md)
    print("钉钉推送：", "成功 ✅" if ok else "失败 ❌")


if __name__ == "__main__":
    main()
