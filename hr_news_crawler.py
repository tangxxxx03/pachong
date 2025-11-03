# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报净化版（只取当天）
- 首页精准提取 dwdata-time 发布时间，只抓当天三茅日报
- 严格日期匹配（默认今天，或 HR_TARGET_DATE 指定）
- 自动去除“阅读量”等尾巴
- 命中即停
- 输出 Markdown，可推送钉钉
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ========= 时区与时间 =========
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())

# ========= 钉钉推送 =========
def _sign_webhook(base, secret):
    if not base: return ""
    if not secret: return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title, md):
    base = os.getenv("DINGTALK_BASE") or os.getenv("DINGTALK_BASEA")
    secret = os.getenv("DINGTALK_SECRET") or os.getenv("DINGTALK_SECRETA")
    if not base:
        print("🔕 未配置 DINGTALK_BASE，跳过推送。")
        return False
    try:
        r = requests.post(
            _sign_webhook(base, secret),
            json={"msgtype": "markdown", "markdown": {"title": title, "text": md}},
            timeout=20
        )
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} code={r.status_code}")
        if not ok: print("resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e); return False

# ========= 网络会话 =========
class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent":"Mozilla/5.0","Accept-Language":"zh-CN,zh;q=0.9"})
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

# ========= 主体 =========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1  # 命中即停

        # 默认目标日期：今天
        target = (os.getenv("HR_TARGET_DATE") or "").strip()
        if target:
            try:
                y, m, d = map(int, re.split(r"[-/\.]", target))
                self.target_date = date(y, m, d)
            except:
                print("⚠️ HR_TARGET_DATE 无法解析，使用今天。")
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()

        self.cn_target = f"（{self.target_date.year}年{self.target_date.month}月{self.target_date.day}日）"
        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.sources = [u.strip() for u in os.getenv("SRC_HRLOO_URLS","https://www.hrloo.com/").split(",") if u.strip()]

        print(f"[CFG] target_date={self.target_date} {zh_weekday(now_tz())} sources={self.sources}")

    def crawl(self):
        for base in self.sources:
            if self._crawl_source(base): break  # 命中即停

    def _crawl_source(self, base):
        try:
            r = self.session.get(base, timeout=20)
        except Exception as e:
            print("首页请求异常：", base, e)
            return False
        if r.status_code != 200:
            print("首页请求失败：", base, r.status_code)
            return False

        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if not items:
            print("[WARN] 未找到 dwxfd-list-items，使用备用方案。")
            items = soup.select("a[href*='/news/']")

        for div in items:
            data_time = div.get("dwdata-time") or ""
            if data_time:
                try:
                    pub_date = datetime.strptime(data_time.split()[0], "%Y-%m-%d").date()
                    if pub_date != self.target_date:
                        continue
                except:
                    pass

            a = div.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            abs_url = urljoin(base, href)
            text = norm(a.get_text())

            if not self.daily_title_pat.search(text):
                continue

            pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
            if not pub_dt or pub_dt.date() != self.target_date:
                continue

            self.results.append({
                "title": page_title,
                "url": abs_url,
                "date": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "titles": titles
            })
            print(f"[HIT] {abs_url} -> {len(titles)} 条 @ {pub_dt}")
            return True  # 命中即停

        print("[MISS] 本源未命中目标日期：", base)
        return False

    def _extract_pub_time(self, soup):
        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        for node in soup.select("meta, time, span, div"):
            txt = (node.get("datetime") or node.get("content") or node.get_text() or "").strip()
            m = pat.search(txt)
            if m:
                try:
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                    hh = int(m[4]) if m[4] else 9
                    mm = int(m[5]) if m[5] else 0
                    return datetime(y, mo, d, hh, mm, tzinfo=_tz())
                except:
                    pass
        return datetime.combine(now_tz().date(), datetime.min.time()).replace(tzinfo=_tz())

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6,20))
            if r.status_code != 200:
                print("[DetailFail]", url, r.status_code)
                return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            title_tag = soup.find(["h1","h2"])
            page_title = norm(title_tag.get_text()) if title_tag else ""
            pub_dt = self._extract_pub_time(soup)
            container = soup.select_one(
                "article, .article, .article-content, .detail-content, .news-content, .content, .post-content"
            ) or soup
            titles = self._extract_daily_item_titles(container)
            return pub_dt, titles, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    def _extract_daily_item_titles(self, root):
        ad_words = ["手机","境外","短信","验证码","审核","粉丝","入群","账号","APP","登录",
                    "推广","广告","创建申请","协议","关注","申诉","下载","网盘","失信","封号"]

        def strip_views(title: str) -> str:
            t = title
            t = re.sub(r"(?:^|[\s·|｜:-])\s*\d+(?:\.\d+)?\s*(?:k|K|万)?\s*(?:次)?阅读\s*$", "", t)
            t = re.sub(r"(?:阅读量)\s*[:：]?\s*\d+(?:\.\d+)?\s*(?:k|K|万)?\s*$", "", t)
            t = re.sub(r"\s*阅读\s*$", "", t)
            return t.strip(" 、，,.;。；|-—~… ")

        by_num = {}
        for node in root.find_all(["h2","h3","h4","strong","b","p","li","span","div"]):
            raw = (node.get_text() or "").strip()
            if not raw: continue
            m = re.match(r"^\s*[（(]?\s*(\d{1,2})\s*[)）]?\s*[、.．]?\s*(.+)$", raw)
            if not m: continue

            num, txt = int(m.group(1)), m.group(2).strip()
            if num >= 10 or txt.startswith("日，") or txt.startswith("日 "): continue
            if any(w in txt for w in ad_words): continue
            title = re.split(r"[（\(]{1}", txt)[0].strip()
            title = strip_views(title)
            if not (4 <= len(title) <= 80): continue
            zh_ratio = len(re.findall(r"[\u4e00-\u9fa5]", title)) / max(len(title), 1)
            if zh_ratio < 0.3: continue
            by_num.setdefault(num, title)

        seq, n = [], 1
        while n in by_num:
            seq.append(by_num[n]); n += 1
            if n > 20: break
        return seq[:10]

# ========= Markdown 输出 =========
def build_md(items):
    n = now_tz()
    out = [
        f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）**  ",
        "",
        "**标题：人资早报｜每日要点**  ",
        ""
    ]
    if not items:
        out.append("> 未发现当天的“三茅日报”。")
        return "\n".join(out)

    it = items[0]
    for j, t in enumerate(it["titles"], 1):
        out.append(f"{j}. {t}  ")
    out.append(f"[查看详细]({it['url']}) （{it['date'][:10]}）  ")
    out.append("")
    return "\n".join(out)

# ========= 主入口 =========
if __name__ == "__main__":
    print("执行 hr_news_crawler_daily_clean_adfree.py（当天三茅日报·精准版）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("人资早报｜每日要点", md)
