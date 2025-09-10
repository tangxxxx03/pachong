# -*- coding: utf-8 -*-
"""
HR 资讯自动抓取（仅当天） +（可选）钉钉推送（加签）
- 覆盖站点（均做真实抓取，支持自定义 URL 列表）
- 新增：关键词过滤（默认：人力资源, 外包；匹配标题+摘要）
  * HR_FILTER_KEYWORDS 可覆盖
  * HR_REQUIRE_ALL=1 时要求全部关键词命中；默认任意命中即可
"""

import os
import re
import csv
import json
import time
import hmac
import base64
import hashlib
import urllib.parse
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo  # Python 3.9+

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ====================== 环境变量配置 ======================

SAVE_FORMAT = os.getenv("HR_SAVE_FORMAT", "both").strip().lower()  # csv/json/both
MAX_PER_SOURCE = int(os.getenv("HR_MAX_ITEMS", "10"))
ONLY_TODAY = os.getenv("HR_ONLY_TODAY", "1").strip().lower() in ("1", "true", "yes", "y")
TZ_STR = os.getenv("HR_TZ", "Asia/Shanghai").strip()
HTTP_PROXY = os.getenv("HTTP_PROXY", "").strip()
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "").strip()

# 关键词过滤（新增）
def _parse_keywords(s: str) -> list[str]:
    parts = re.split(r"[,\s，；;|]+", s or "")
    return [p.strip() for p in parts if p.strip()]

HR_REQUIRE_ALL = os.getenv("HR_REQUIRE_ALL", "0").strip().lower() in ("1","true","yes","y")
KEYWORDS = _parse_keywords(os.getenv("HR_FILTER_KEYWORDS", "人力资源,外包"))

# 钉钉（已硬编码，不再依赖环境变量注入 webhook/secret）
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
DINGTALK_SECRET  = "SEC6a9573022b3e890e062c9cb867f9e53d06e3a36ac593e2da06a41522f2448225"
DINGTALK_KEYWORD = os.getenv("DINGTALK_KEYWORD_HR", "").strip()  # 如机器人开了“关键词”，可在这里写；否则留空

def now_tz():
    return datetime.now(ZoneInfo(TZ_STR))

# ====================== HTTP 会话（重试/超时） ======================

def make_session():
    s = requests.Session()
    s.trust_env = False  # 避免继承 Runner 的 http_proxy/https_proxy 等
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    s.headers.update(headers)

    retries = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=12)
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    proxies = {}
    if HTTP_PROXY:
        proxies["http"] = HTTP_PROXY
    if HTTPS_PROXY:
        proxies["https"] = HTTPS_PROXY
    if proxies:
        s.proxies.update(proxies)

    return s

# ====================== 钉钉发送（加签） ======================

def _sign_webhook(base_webhook: str, secret: str) -> str:
    if not secret:
        return base_webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title: str, md_text: str) -> bool:
    if not DINGTALK_WEBHOOK or not DINGTALK_SECRET:
        print("🔕 未配置钉钉 WEBHOOK/SECRET，跳过推送。")
        return False

    webhook = _sign_webhook(DINGTALK_WEBHOOK, DINGTALK_SECRET)
    if DINGTALK_KEYWORD and (DINGTALK_KEYWORD not in title and DINGTALK_KEYWORD not in md_text):
        title = f"{DINGTALK_KEYWORD} | {title}"

    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        print("HR DingTalk resp:", r.status_code, r.text[:300])
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
        return ok
    except Exception as e:
        print("❌ 钉钉请求异常：", e)
        return False

# ====================== 抓取类 ======================

DEFAULT_SELECTORS = [
    ".list li", ".news-list li", ".content-list li", ".box-list li",
    "ul.list li", "ul.news li", "ul li", "li"
]

def as_list(env_name: str, defaults: list[str]) -> list[str]:
    """从环境变量取 URL 列表（逗号分隔），否则用默认列表"""
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return defaults
    return [u.strip() for u in raw.split(",") if u.strip()]

class HRNewsCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self._seen = set()

    # ------------ 通用抓取器 ------------
    def crawl_generic(self, source_name: str, base: str | None, list_urls: list[str], selectors=None):
        if not list_urls:
            print(f"ℹ️ {source_name}: 未配置入口 URL，跳过。")
            return
        selectors = selectors or DEFAULT_SELECTORS
        total = 0
        for url in list_urls:
            if total >= MAX_PER_SOURCE:
                break
            try:
                resp = self.session.get(url, timeout=15)
                resp.encoding = resp.apparent_encoding or "utf-8"
                if resp.status_code != 200:
                    print(f"⚠️ {source_name} 访问失败 {resp.status_code}: {url}")
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")

                items = []
                for css in selectors:
                    items = soup.select(css)
                    if items:
                        break
                if not items:
                    items = soup.select("a")  # 兜底

                for node in items:
                    if total >= MAX_PER_SOURCE:
                        break
                    a = node if node.name == "a" else node.find("a")
                    if not a:
                        continue

                    title = self._norm(a.get_text())
                    if not title:
                        continue

                    href = a.get("href") or ""
                    full_url = urljoin(base or url, href)

                    # 日期
                    date_text = self._find_date(node) or self._find_date(a)
                    if not date_text:
                        if ONLY_TODAY:
                            continue
                        continue

                    if ONLY_TODAY and (not self._is_today(date_text)):
                        continue

                    snippet = self._snippet(node)

                    # 关键词过滤（标题+摘要）
                    if KEYWORDS and (not self._hit_keywords(title, snippet)):
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

            except Exception as e:
                print(f"⚠️ {source_name} 抓取错误 {url}: {e}")

    # ------------ 站点适配（可用环境变量覆盖入口） ------------
    def crawl_mohrss(self):
        urls = as_list("SRC_MOHRSS_URLS", [
            "https://www.mohrss.gov.cn/",
            "https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/gzdt/index.html",
            "https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/tzgg/index.html",
        ])
        self.crawl_generic("人社部", "https://www.mohrss.gov.cn", urls)

    def crawl_people(self):
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
        urls = as_list("SRC_BJ_SFJ_URLS", ["https://sfj.beijing.gov.cn/"])
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
            self.crawl_generic("北京人力资源服务协会（含行业协会）", None, urls)
        else:
            print("ℹ️ 北京 HR 协会未配置 URL（SRC_BJ_HR_ASSOC_URLS），已跳过。")

    def crawl_stats(self):
        urls = as_list("SRC_STATS_URLS", ["https://www.stats.gov.cn/"])
        self.crawl_generic("国家统计局", "https://www.stats.gov.cn", urls)

    # ------------ 主流程 ------------
    def get_today_news(self):
        print("开始抓取人力资源相关资讯（仅当天，关键词过滤={}，模式={}）...".format(
            "、".join(KEYWORDS) if KEYWORDS else "（未设置）",
            "全部命中" if HR_REQUIRE_ALL else "任意命中"
        ))
        fns = [
            self.crawl_beijing_hrss,
            self.crawl_mohrss,
            self.crawl_people,
            self.crawl_gmw,
            self.crawl_xinhua,
            self.crawl_chrm,
            self.crawl_job_mohrss,
            self.crawl_newjobs,
            self.crawl_hrloo,
            self.crawl_hroot,
            self.crawl_chinatax,
            self.crawl_bjsfj,
            self.crawl_si_12333,
            self.crawl_chinahrm,
            self.crawl_newjobs_policy,
            self.crawl_bj_hr_associations,
            self.crawl_stats,
        ]
        for fn in fns:
            try:
                fn()
                time.sleep(0.8)
            except Exception as e:
                print(f"抓取来源时出错: {e}")
        return self.results

    # ------------ 工具方法 ------------
    def _push_if_new(self, item: dict) -> bool:
        key = item.get("url") or f"{item.get('title','')}|{item.get('date','')}"
        if key in self._seen:
            return False
        self._seen.add(key)
        self.results.append(item)
        return True

    @staticmethod
    def _norm(s: str) -> str:
        if not s:
            return ""
        return re.sub(r"\s+", " ", s.replace("\u3000", " ")).strip()

    @staticmethod
    def _snippet(node) -> str:
        try:
            text = node.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            return (text[:100] + "...") if len(text) > 100 else text
        except Exception:
            return "内容获取中..."

    def _find_date(self, node) -> str:
        if not node:
            return ""
        raw = node.get_text(" ", strip=True)
        raw = raw.replace("年", "-").replace("月", "-").replace("日", "-")
        raw = raw.replace("/", "-").replace(".", "-")

        t = None
        for sel in ["time", ".time", ".date", "span.time", "span.date", "em.time", "em.date", "p.time", "p.date"]:
            sub = node.select_one(sel) if hasattr(node, "select_one") else None
            if sub:
                t = sub.get("datetime") or sub.get_text(strip=True)
                if t:
                    break
        if t:
            raw = t.replace("年", "-").replace("月", "-").replace("日", "-").replace("/", "-").replace(".", "-")

        m = re.search(r"(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})", raw)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        m2 = re.search(r"\b(\d{1,2})-(\d{1,2})\b", raw)
        if m2:
            y = now_tz().year
            return f"{y:04d}-{int(m2.group(1)):02d}-{int(m2.group(2)):02d}"
        return ""

    def _parse_date(self, s: str):
        if not s:
            return None
        s = s.strip().replace("年", "-").replace("月", "-").replace("日", "-").replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d", "%y-%m-%d", "%Y-%m", "%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%m-%d":
                    dt = dt.replace(year=now_tz().year)
                if fmt == "%y-%m-%d" and dt.year < 2000:
                    dt = dt.replace(year=2000 + dt.year % 100)
                return dt.replace(tzinfo=ZoneInfo(TZ_STR))
            except ValueError:
                continue
        return None

    def _is_today(self, date_str: str) -> bool:
        dt = self._parse_date(date_str)
        if not dt:
            return False
        return dt.date() == now_tz().date()

    def _hit_keywords(self, title: str, content: str) -> bool:
        """标题+摘要 命中关键词"""
        if not KEYWORDS:
            return True
        hay = (title or "") + " " + (content or "")
        # 不区分大小写
        hay_low = hay.lower()
        kws_low = [k.lower() for k in KEYWORDS]
        if HR_REQUIRE_ALL:
            return all(k in hay_low for k in kws_low)
        return any(k in hay_low for k in kws_low)

    # ------------ 输出 ------------
    def save_results(self):
        if not self.results:
            print("没有找到“当天”的相关资讯")
            return None, None
        ts = now_tz().strftime("%Y%m%d_%H%M%S")
        csvf = jsonf = None

        if SAVE_FORMAT in ("csv", "both"):
            csvf = f"hr_news_{ts}.csv"
            with open(csvf, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["title", "url", "source", "date", "content"])
                w.writeheader()
                w.writerows(self.results)
            print(f"✅ CSV 已保存：{csvf}")

        if SAVE_FORMAT in ("json", "both"):
            jsonf = f"hr_news_{ts}.json"
            with open(jsonf, "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            print(f"✅ JSON 已保存：{jsonf}")

        return csvf, jsonf

    def to_markdown(self):
        if not self.results:
            return "今天未抓到符合条件的人社类资讯。"
        lines = [
            "### 🧩 人力资源资讯每日汇总（仅当天）",
            f"**汇总时间：{now_tz().strftime('%Y年%m月%d日 %H:%M')}（{TZ_STR}）**",
            f"**今日资讯：{len(self.results)} 条**",
            "",
            "🗞️ **资讯详情**"
        ]
        for i, it in enumerate(self.results[:12], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
            lines.append(f"> 📅 {it['date']}　|　🏛️ {it['source']}")
            if it.get("content"):
                lines.append(f"> {it['content'][:120]}")
            lines.append("")
        lines.append("💡 今日人力资源资讯已为您整理完毕")
        return "\n".join(lines)

    def display_results(self):
        if not self.results:
            print("没有找到“当天”的人力资源相关资讯")
            return
        print(f"\n找到 {len(self.results)} 条“当天”资讯:\n" + "-" * 100)
        for i, it in enumerate(self.results, 1):
            print(f"{i}. {it['title']}")
            print(f"   来源: {it['source']} | 日期: {it['date']}")
            print(f"   链接: {it['url']}")
            print(f"   内容: {it['content']}")
            print("-" * 100)

# ====================== 程序入口 ======================

def main():
    print("人力资源资讯自动抓取工具（仅当天）")
    print("=" * 50)
    crawler = HRNewsCrawler()

    # 抓取
    crawler.get_today_news()

    # 打印展示
    crawler.display_results()

    # 保存
    crawler.save_results()

    # 推送钉钉
    md = crawler.to_markdown()
    ok = send_dingtalk_markdown("人力资源资讯（当天）", md)
    print("钉钉推送：", "成功 ✅" if ok else "未推送/失败 ❌")

if __name__ == "__main__":
    main()
