# -*- coding: utf-8 -*-
"""
People.cn 站内搜索测试（仅当天 + 可翻页 + 遵守 robots 限速）
- 关键词：默认“外包”，命令行 --keyword 可改
- 页数：默认 3 页，--pages 可改
- 仅当天：只保留今天的结果（按 Asia/Shanghai）
- 速率限制：search.people.cn / www.people.com.cn 120 秒/次（可用 --delay 覆盖）

依赖：requests, beautifulsoup4
用法示例：
  python people_search_today.py --keyword 外包 --pages 2 --delay 120 --save both
"""

import re
import time
import csv
import json
import argparse
from urllib.parse import urlencode, urljoin, urlparse
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session():
    s = requests.Session()
    s.trust_env = False  # 不继承环境代理
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/123.0.0.0 Safari/537.36")
    })
    retries = Retry(total=3, backoff_factor=0.6,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=["GET"])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    s.mount("http://", adapter); s.mount("https://", adapter)
    return s


class PeopleSearch:
    def __init__(self, keyword="外包", max_pages=3, delay=120, tz="Asia/Shanghai"):
        self.keyword = keyword
        self.max_pages = max_pages
        self.tz = ZoneInfo(tz)
        self.today = datetime.now(self.tz).strftime("%Y-%m-%d")
        self.session = make_session()
        # per-domain throttle（遵守 robots）
        self._next_allowed_time = defaultdict(float)
        self._domain_delay = {
            "search.people.cn": delay,
            "www.people.com.cn": delay,
        }
        self.results = []
        self._seen = set()

    def _get_with_throttle(self, url, timeout=20):
        host = urlparse(url).netloc
        delay = self._domain_delay.get(host, 0)
        now = time.time()
        if delay > 0 and self._next_allowed_time[host] > now:
            time.sleep(self._next_allowed_time[host] - now)
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
        self._seen.add(key); self.results.append(item); return True

    def _extract_date(self, text: str) -> str:
        # 先找 YYYY-MM-DD HH:MM:SS
        m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\s+\d{2}:\d{2}:\d{2}", text)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # 再退回 YYYY-MM-DD
        m2 = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m2:
            return f"{int(m2.group(1)):04d}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
        return ""

    def run(self):
        print(f"开始抓取：关键词='{self.keyword}'，仅当天={self.today}，最多 {self.max_pages} 页，延迟={self._domain_delay['search.people.cn']}s/次")
        added_total = 0
        for page in range(1, self.max_pages + 1):
            url = self._build_url(page)
            try:
                resp = self._get_with_throttle(url, timeout=25)
                resp.encoding = resp.apparent_encoding or "utf-8"
                if resp.status_code != 200:
                    print(f"⚠️ 第{page}页访问失败 {resp.status_code}: {url}")
                    break

                soup = BeautifulSoup(resp.text, "html.parser")

                # 取结果块（尽量宽松，适配 SSR 结构）
                candidates = []
                for css in [
                    "div.content div[class*='news']",
                    "div.content div[class*='result']",
                    "div.content div",
                    "div.search div",
                    "div.article div"
                ]:
                    candidates = soup.select(css)
                    if candidates:
                        break
                if not candidates:
                    candidates = soup.select("div")

                added_page = 0
                for node in candidates:
                    a = node.find("a", href=True)
                    if not a:
                        continue
                    title = re.sub(r"\s+", " ", a.get_text(strip=True))
                    if not title:
                        continue
                    href = a["href"].strip()
                    full_url = urljoin(url, href)

                    raw = node.get_text(" ", strip=True)
                    d = self._extract_date(raw)
                    if d != self.today:
                        continue

                    p = node.find("p")
                    digest = ""
                    if p:
                        digest = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
                    if not digest:
                        digest = re.sub(r"\s+", " ", raw)[:160]

                    item = {
                        "title": title,
                        "url": full_url,
                        "source": "人民网（搜索）",
                        "date": d,
                        "content": digest
                    }
                    if self._push_if_new(item):
                        added_page += 1
                        print(f" + {title} | {d}")

                added_total += added_page
                # 如果第一页就一个当天都没，后面通常更旧，直接停
                if page == 1 and added_page == 0:
                    print("第一页未找到当天结果，提前结束。")
                    break

            except Exception as e:
                print(f"⚠️ 抓取异常 page={page}: {e}")
                break

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
                w = csv.DictWriter(f, fieldnames=["title","url","source","date","content"])
                w.writeheader(); w.writerows(self.results)
            out.append(fn); print("CSV:", fn)
        if fmt in ("json", "both"):
            fn = f"people_search_{ts}.json"
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            out.append(fn); print("JSON:", fn)
        return out


def main():
    ap = argparse.ArgumentParser(description="People.cn 搜索：仅当天 + 翻页")
    ap.add_argument("--keyword", default="外包", help="搜索关键词（默认：外包）")
    ap.add_argument("--pages", type=int, default=3, help="最多翻页数（默认：3）")
    ap.add_argument("--delay", type=int, default=120, help="同域请求间隔秒（默认：120，遵守 robots）")
    ap.add_argument("--tz", default="Asia/Shanghai", help="时区（默认：Asia/Shanghai）")
    ap.add_argument("--save", default="both", choices=["csv","json","both","none"], help="保存格式（默认：both）")
    args = ap.parse_args()

    spider = PeopleSearch(keyword=args.keyword, max_pages=args.pages, delay=args.delay, tz=args.tz)
    spider.run()
    if args.save != "none":
        spider.save(args.save)

if __name__ == "__main__":
    main()
