# -*- coding: utf-8 -*-
"""
People.cn 站内搜索（仅当天 + 翻页）→ 自动推送钉钉（加签）

- 关键词默认：外包（--keyword 可改）
- 翻页默认：1 页（--pages 可改；遵守 robots 建议 120s/次，翻多页会慢）
- 仅当天（Asia/Shanghai）
- 发送钉钉：硬编码 webhook/secret（你提供的那组）

依赖：requests, beautifulsoup4, urllib3
用法：
  python people_search_today.py --keyword 外包 --pages 1 --delay 120
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
import hmac, hashlib, base64, urllib.parse

# ============ 钉钉机器人（已硬编码） ============
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
DINGTALK_SECRET  = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"

def _sign_webhook(base_webhook: str, secret: str) -> str:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title: str, md_text: str) -> bool:
    try:
        webhook = _sign_webhook(DINGTALK_WEBHOOK, DINGTALK_SECRET)
        payload = {"msgtype":"markdown","markdown":{"title":title,"text":md_text}}
        r = requests.post(webhook, json=payload, timeout=20)
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print("DingTalk resp:", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

# ============ HTTP 会话 ============
def make_session():
    s = requests.Session()
    s.trust_env = False
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
    def __init__(self, keyword="外包", max_pages=1, delay=120, tz="Asia/Shanghai"):
        self.keyword = keyword
        self.max_pages = max_pages
        self.tz = ZoneInfo(tz)
        self.today = datetime.now(self.tz).strftime("%Y-%m-%d")
        self.session = make_session()
        # people 域名按 robots 节流
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

    @staticmethod
    def _extract_date(text: str) -> str:
        m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\s+\d{2}:\d{2}:\d{2}", text)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m2 = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m2:
            return f"{int(m2.group(1)):04d}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
        return ""

    def run(self):
        print(f"开始抓取：关键词='{self.keyword}'，仅当天={self.today}，最多 {self.max_pages} 页")
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

                candidates = []
                for css in [
                    "div.content div[class*='news']",
                    "div.content div[class*='result']",
                    "div.content div",
                    "div.search div",
                    "div.article div"
                ]:
                    candidates = soup.select(css)
                    if candidates: break
                if not candidates:
                    candidates = soup.select("div")

                added_page = 0
                for node in candidates:
                    a = node.find("a", href=True)
                    if not a: continue
                    title = re.sub(r"\s+", " ", a.get_text(strip=True))
                    if not title: continue
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
            print("无结果可保存。"); return []
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

    def to_markdown(self, limit=12):
        if not self.results:
            return f"### 人民网搜索（仅当天）\n**关键词：{self.keyword}**\n**时间：{self.today}**\n> 今天未找到符合条件的结果。"
        lines = [
            f"### 人民网搜索（仅当天）",
            f"**关键词：{self.keyword}**",
            f"**时间：{self.today}**",
            "",
            "#### 结果"
        ]
        for i, it in enumerate(self.results[:limit], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
            lines.append(f"> 📅 {it['date']} | 🏛️ {it['source']}")
            if it.get("content"): lines.append(f"> {it['content'][:120]}")
            lines.append("")
        return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser(description="People.cn 搜索：仅当天 + 翻页 → 钉钉推送")
    ap.add_argument("--keyword", default="外包", help="搜索关键词（默认：外包）")
    ap.add_argument("--pages", type=int, default=1, help="最多翻页数（默认：1）")
    ap.add_argument("--delay", type=int, default=120, help="同域请求间隔秒（默认：120，遵守 robots）")
    ap.add_argument("--tz", default="Asia/Shanghai", help="时区（默认：Asia/Shanghai）")
    ap.add_argument("--save", default="both", choices=["csv","json","both","none"], help="保存格式（默认：both）")
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
