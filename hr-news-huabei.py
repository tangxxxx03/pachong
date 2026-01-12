# -*- coding: utf-8 -*-

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from datetime import datetime, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

# ----------------------------------------
# 固定钉钉 Webhook & Secret（你给我的版本）
# ----------------------------------------
DINGTALK_BASE = "https://oapi.dingtalk.com/robot/send?access_token=00c49f5d9aab4b8c86d60ef9bc0a25d46d9669b1b1d94645671062c4b845dced"
DINGTALK_SECRET = "SEC2431e95f7bca3b419185a0fbd80530829c45c94977ba338022400433f064c6ad"

def _sign_webhook(base, secret):
    if not base: return ""
    if not secret: return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    sign = urllib.parse.quote_plus(
        base64.b64encode(hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest())
    )
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title, md):
    base = DINGTALK_BASE
    secret = DINGTALK_SECRET
    if not base:
        print("🔕 未配置 webhook，跳过推送。")
        return False
    try:
        r = requests.post(
            _sign_webhook(base, secret),
            json={"msgtype":"markdown","markdown":{"title":title,"text":md}},
            timeout=20
        )
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} code={r.status_code}")
        if not ok:
            print("resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language":"zh-CN,zh;q=0.9"
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

CN_TITLE_DATE = re.compile(r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]")
def date_from_bracket_title(text:str):
    m = CN_TITLE_DATE.search(text or "")
    if not m: return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except:
        return None

def looks_like_numbered(text: str) -> bool:
    # 1、xxx  1.xxx  （1）xxx  1）xxx  等
    return bool(re.match(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*\S+", text or ""))

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
def strip_leading_num(t: str) -> str:
    t = re.sub(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*", "", t)
    t = re.sub(r"^\s*[" + CIRCLED + r"]\s*", "", t)
    t = re.sub(r"^\s*[０-９]+\s*[、.．]\s*", "", t)
    return t.strip()

class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1

        t = (os.getenv("HR_TARGET_DATE") or "").strip()
        if t:
            try:
                y,m,d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y,m,d)
            except:
                print("⚠️ HR_TARGET_DATE 解析失败，使用今日。")
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()

        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.sources = [u.strip() for u in os.getenv(
            "SRC_HRLOO_URLS",
            "https://www.hrloo.com/,https://www.hrloo.com/news/hr"
        ).split(",") if u.strip()]

        print(f"[CFG] target_date={self.target_date} {zh_weekday(now_tz())}  sources={self.sources}")

    def crawl(self):
        for base in self.sources:
            if self._crawl_source(base):
                break

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

        # 通道 1：列表容器（老样式可能还在）
        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if items:
            for div in items:
                dts = (div.get("dwdata-time") or "").strip()
                if dts:
                    try:
                        pub_d = datetime.strptime(dts.split()[0], "%Y-%m-%d").date()
                        if pub_d != self.target_date:
                            continue
                    except:
                        pass

                a = div.find("a", href=True)
                if not a:
                    continue

                title_text = norm(a.get_text())
                if not self.daily_title_pat.search(title_text):
                    continue

                t2 = date_from_bracket_title(title_text)
                if t2 and t2 != self.target_date:
                    continue

                abs_url = urljoin(base, a["href"])
                if self._try_detail(abs_url):
                    return True

            print("[MISS] 容器通道未命中：", base)

        # 通道 2：兜底扫 links（/news/数字.html 且标题含三茅日报）
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href","")
            if not re.search(r"/news/\d+\.html$", href):
                continue
            text = norm(a.get_text())
            if not self.daily_title_pat.search(text):
                continue

            t2 = date_from_bracket_title(text)
            if t2 and t2 != self.target_date:
                continue

            links.append(urljoin(base, href))

        seen = set()
        for url in links:
            if url in seen:
                continue
            seen.add(url)
            if self._try_detail(url):
                return True

        print("[MISS] 本源未命中目标日期：", base)
        return False

    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)

        if not page_title or not self.daily_title_pat.search(page_title):
            return False

        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date:
            return False

        # 没括号日期时，用发布时间兜底校验
        if pub_dt and pub_dt.date() != self.target_date and not t3:
            return False

        if not titles:
            return False

        self.results.append({
            "title": page_title,
            "url": abs_url,
            "date": (pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else f"{self.target_date} 09:00"),
            "titles": titles
        })
        print(f"[HIT] {abs_url} -> {len(titles)} 条")
        return True

    def _extract_pub_time(self, soup: BeautifulSoup):
        cand = []
        for t in soup.select("time[datetime]"):
            cand.append(t.get("datetime",""))
        for m in soup.select("meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"):
            cand.append(m.get("content",""))
        for sel in [".time",".date",".pubtime",".publish-time",".post-time",".info","meta[itemprop='datePublished']"]:
            for x in soup.select(sel):
                if isinstance(x, Tag):
                    cand.append(x.get_text(" ", strip=True))

        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        def parse_one(s):
            m = pat.search(s or "")
            if not m:
                return None
            try:
                y,mo,d = int(m[1]),int(m[2]),int(m[3])
                hh = int(m[4]) if m[4] else 9
                mm = int(m[5]) if m[5] else 0
                return datetime(y,mo,d,hh,mm,tzinfo=_tz())
            except:
                return None

        dts = [dt for dt in map(parse_one, cand) if dt]
        if dts:
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    # ========== ✅ 新版要点提取：优先 h2.style-h2 ==========
    def _extract_h2_titles(self, root: Tag):
        """
        新版详情页的要点标题是：
        <h2 class="... style-h2">1、xxx</h2>
        """
        out = []
        for h2 in root.select("h2.style-h2, h2[class*='style-h2']"):
            text = norm(h2.get_text())
            if not text:
                continue
            text = strip_leading_num(text)
            # 常见：标题后面可能带括号补充说明，按你之前逻辑砍掉
            text = re.split(r"[（(]", text)[0].strip()
            if text and len(text) >= 4:
                out.append(text)

        # 去重保序
        seen, final = set(), []
        for t in out:
            if t in seen:
                continue
            seen.add(t)
            final.append(t)
        return final

    def _extract_strong_titles(self, root: Tag):
        keep = []
        for st in root.select("strong"):
            text = norm(st.get_text())
            if not text:
                continue
            if len(text) < 4:
                continue
            text = re.split(r"[（(]?(阅读|阅读量|浏览|来源)[:：]\s*\d+.*$", text)[0].strip()
            if not text:
                continue
            text = strip_leading_num(text)
            if text:
                keep.append(text)

        seen, out = set(), []
        for t in keep:
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    def _extract_numbered_titles(self, root: Tag):
        out = []
        for p in root.find_all(["p","h2","h3","div","span","li"]):
            text = norm(p.get_text())
            if looks_like_numbered(text):
                text = strip_leading_num(text)
                text = re.split(r"[（(]", text)[0].strip()
                if text and len(text) >= 4:
                    out.append(text)

        seen, final = set(), []
        for t in out:
            if t in seen:
                continue
            seen.add(t)
            final.append(t)
        return final

    def _pick_container(self, soup: BeautifulSoup):
        # 详情正文容器（按你截图的 class 做多候选）
        selectors = [
            ".content-con.fn-wenda-detail-infomation",
            ".fn-wenda-detail-infomation",
            ".content-con.hr-rich-text.fn-wenda-detail-infomation",
            ".hr-rich-text.fn-wenda-detail-infomation",
            ".fn-hr-rich-text.custom-style-warp",
            ".custom-style-warp",
            ".content-wrap-con",  # 兜底
        ]
        for sel in selectors:
            node = soup.select_one(sel)
            if node:
                return node
        return soup

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6, 20))
            if r.status_code != 200:
                print("[DetailFail]", url, r.status_code)
                return None, [], ""

            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # ✅ 标题：优先 h1（避免误拿到 h2 要点）
            h1 = soup.find("h1")
            if h1:
                page_title = norm(h1.get_text())
            else:
                title_tag = soup.find(["h1","h2"])
                page_title = norm(title_tag.get_text()) if title_tag else ""

            pub_dt = self._extract_pub_time(soup)

            container = self._pick_container(soup)

            # 清理无关区块（尽量别把正文砍掉）
            for sel in [
                ".other-wrap",
                ".txt",
                "a.prev.fn-dataStatistics-btn",
                "a.next.fn-dataStatistics-btn",
                ".footer",
                ".bottom",
            ]:
                for bad in container.select(sel):
                    bad.decompose()

            # ✅ 新版：先抓 h2.style-h2
            titles = self._extract_h2_titles(container)

            # 退回：strong
            if not titles:
                titles = self._extract_strong_titles(container)

            # 再退回：编号段落
            if not titles:
                titles = self._extract_numbered_titles(container)

            return pub_dt, titles, page_title

        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

def build_md(items):
    n = now_tz()
    out = [
        f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）**  ",
        "",
        "**标题：人资日报｜每日要点**  ",
        ""
    ]
    if not items:
        out.append("> 未发现当天的“三茅日报”。")
        return "\n".join(out)

    it = items[0]
    for idx, t in enumerate(it["titles"], 1):
        out.append(f"{idx}. {t}  ")
    out.append(f"[查看详细]({it['url']})  ")
    return "\n".join(out)

if __name__ == "__main__":
    print("执行 hr-news-huabei.py（当天一条 · 三重日期校验 · h2/strong/编号标题提取）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("人资日报｜每日要点", md)
