# -*- coding: utf-8 -*-
"""
People.cn 站内搜索（仅当天 + 翻页）→ 自动推送钉钉（加签）
- 按 <li> 解析，直接读取 .tip-pubtime 日期，命中率更高
- 仅保留“今天”的结果（Asia/Shanghai）
- 遵守 robots（默认：search.people.cn / www.people.com.cn 120s/次）
- 运行完成后将结果以 Markdown 推送到钉钉（已硬编码 webhook/secret）

用法（建议先跑 1 页验证）：
  python people_search_today.py --keyword 外包 --pages 1 --delay 120
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
from urllib.parse import urlencode, urljoin, urlparse
from collections import defaultdict
from datetime import datetime

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
    s.trust_env = False  # 不继承 runner 的代理，避免莫名其妙的 407/超时
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    })
    retries = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# ====== 工具 ======
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


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


class PeopleSearch:
    def __init__(self, keyword="外包", max_pages=1, delay=120, tz="Asia/Shanghai"):
        self.keyword = keyword
        self.max_pages = max_pages
        self.tz = ZoneInfo(tz)
        self.today = datetime.now(self.tz).strftime("%Y-%m-%d")
        self.session = make_session()
        # 按域名节流（遵守 robots）
        self._next_allowed_time = defaultdict(float)
        self._domain_delay = {
            "search.people.cn": delay,
            "www.people.com.cn": delay,
        }
        self.results = []
        self._seen = set()

    def _get_with_throttle(self, url, timeout=25):
        host = urlparse(url).netloc
        delay = self._domain_delay.get(host, 0)
        now = time.time()
        next_at = self._next_allowed_time.get(host, 0.0)
        if delay > 0 and next_at > now:
            time.sleep(max(0.0, next_at - now))
        resp = self.session.get(url, timeout=timeout)
        if delay > 0:
            self._next_allowed_time[host] = time.time() + delay
        return resp

    def _build_url(self, page: int) -> str:
        base = "https://search.people.cn/s/"
        qs = {"keyword": self.keyword, "page": page}
        return base + "?" + urlencode(qs, doseq=True)

    def _push_if_new(self, item):
        key = item["url"]
        if key in self._seen:
            return False
        self._seen.add(key)
        self.results.append(item)
        return True

    def run(self):
        print(
            f"开始抓取：关键词='{self.keyword}'，仅当天={self.today}，"
            f"最多 {self.max_pages} 页（people 需延迟）"
        )
        added_total = 0

        for page in range(1, self.max_pages + 1):
            url = self._build_url(page)
            try:
                resp = self._get_with_throttle(url, timeout=25)
                resp.encoding = resp.apparent_encoding or "utf-8"
                if resp.status_code != 200:
                    print(f"⚠️ 第{page}页访问失败 {resp.status_code}: {url}")
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                # 锁定结果区域（页面是 SSR + Nuxt，通常这些容器里有列表）
                root = None
                for sel in ["div.article", "div.content", "div.search", "div.main-container", "div.module-common"]:
                    root = soup.select_one(sel)
                    if root:
                        break
                scope = root or soup

                # 按“条目 li”解析，要求具备 .tip-pubtime 日期
                items = []
                for sel in ["div.article li", "ul li", "li"]:
                    items = scope.select(sel)
                    if items:
                        break

                added_page = 0
                for li in items:
                    # 跳过分页/导航类 li
                    classes = " ".join(li.get("class") or [])
                    if "page" in classes:
                        continue

                    pub = li.select_one(".tip-pubtime")
                    a = li.select_one("a[href]")
                    if not pub or not a:
                        continue

                    d = find_date_in_text(pub.get_text(" ", strip=True))
                    if d != self.today:
                        continue  # 仅当天

                    title = norm(a.get_text())
                    if not title:
                        continue
                    href = (a.get("href") or "").strip()
                    if not href or href.startswith("#") or href.startswith("javascript"):
                        continue
                    full_url = urljoin(url, href)

                    # 摘要优先 .abs，其次第一个 <p>，最后 li 文本
                    abs_el = li.select_one(".abs")
                    if abs_el:
                        digest = norm(abs_el.get_text(" ", strip=True))
                    else:
                        p = li.find("p")
                        digest = norm(p.get_text(" ", strip=True)) if p else norm(li.get_text(" ", strip=True))
                    digest = digest[:160]

                    item = {
                        "title": title,
                        "url": full_url,
                        "source": "人民网（搜索）",
                        "date": d,
                        "content": digest,
                    }
                    if self._push_if_new(item):
                        added_page += 1
                        print(f" + {title} | {d}")

                added_total += added_page
                print(f"第{page}页：当天命中 {added_page} 条。")

            except Exception as e:
                print(f"⚠️ 抓取异常 page={page}: {e}")
                continue

        print(f"完成：共抓到 {added_total} 条当天结果。")
        return self.results

    def save(self, fmt="both"):
        if not self.results:
            print("无结果可保存。")
            return []
        ts = datetime.now(self.tz).strftime("%Y%m%d_%H%M%S")
        out = []
        if fmt in ("csv", "both"):
            fn = f"people_search_{ts}.csv"
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["title", "url", "source", "date", "content"])
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
                f"### 人民网搜索（仅当天）\n"
                f"**关键词：{self.keyword}**\n"
                f"**时间：{self.today}**\n"
                f"> 今天未找到符合条件的结果。"
            )
        lines = [
            "### 人民网搜索（仅当天）",
            f"**关键词：{self.keyword}**",
            f"**时间：{self.today}**",
            "",
            "#### 结果",
        ]
        for i, it in enumerate(self.results[:limit], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
            lines.append(f"> 📅 {it['date']} | 🏛️ {it['source']}")
            if it.get("content"):
                lines.append(f"> {it['content'][:120]}")
            lines.append("")
        return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="People.cn 搜索：仅当天 + 翻页 → 钉钉推送")
    ap.add_argument("--keyword", default="外包", help="搜索关键词（默认：外包）")
    ap.add_argument("--pages", type=int, default=1, help="最多翻页数（默认：1）")
    ap.add_argument("--delay", type=int, default=120, help="同域请求间隔秒（默认：120，遵守 robots）")
    ap.add_argument("--tz", default="Asia/Shanghai", help="时区（默认：Asia/Shanghai）")
    ap.add_argument("--save", default="both", choices=["csv", "json", "both", "none"], help="保存格式（默认：both）")
    args = ap.parse_args()

    spider = PeopleSearch(keyword=args.keyword, max_pages=args.pages, delay=args.delay, tz=args.tz)
    spider.run()
    if args.save != "none":
        spider.save(args.save)

    md = spider.to_markdown()
    ok = send_dingtalk_markdown(f"人民网搜索（{args.keyword}）当天结果", md)
    print("钉钉推送：", "成功 ✅" if ok else "失败 ❌")


if __name__ == "__main__":
    main()
