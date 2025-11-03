# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报净化版（当天一条·强标题抓取）
—————————————————————————————————————
✅ 三重校验逻辑（主页/标题括号/详情时间）
✅ 仅抓“当天”的“三茅日报”一条
✅ 仅保留正文主体里每条新闻的 <strong> 标题行（你标注要保留的那行）
✅ 兼容：有编号/无编号 的两种渲染（以 strong 为准，fallback 数字前缀）
✅ 过滤：相关阅读（.other-wrap）、免责声明（.txt）、上一篇/下一篇（.prev/.next btn）等噪声
✅ 输出 Markdown；可选钉钉 Markdown 推送（未配 webhook 则跳过）
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from datetime import datetime, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ========== 时区 ==========
try:
    from zoneinfo import ZoneInfo  # py3.9+
except:
    from backports.zoneinfo import ZoneInfo  # py<3.9

def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

# ========== 钉钉 ==========
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
            json={"msgtype":"markdown","markdown":{"title":title,"text":md}},
            timeout=20
        )
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} code={r.status_code}")
        if not ok: print("resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e); return False

# ========== 会话 ==========
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
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language":"zh-CN,zh;q=0.9"
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

# ========== 工具 ==========
CN_TITLE_DATE = re.compile(r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]")
def date_from_bracket_title(text:str):
    m = CN_TITLE_DATE.search(text or "")
    if not m: return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except: return None

def looks_like_numbered(text: str) -> bool:
    # 形如 "1. xxx"、"1、xxx"、"（1）xxx" 等
    return bool(re.match(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*\S+", text or ""))

# ========== 主类 ==========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1  # 命中即停（只保留当天的一条日报）

        # 目标日期：环境变量指定或今日
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

        self.daily_title_pat = re.compile(r"三茅日[报報]")  # 只认三茅日报
        # 同时支持首页和“人力资源/HR 快讯页”
        self.sources = [u.strip() for u in os.getenv("SRC_HRLOO_URLS","https://www.hrloo.com/,https://www.hrloo.com/news/hr").split(",") if u.strip()]
        print(f"[CFG] target_date={self.target_date} {zh_weekday(now_tz())}  sources={self.sources}")

    # —— 外部入口
    def crawl(self):
        for base in self.sources:
            if self._crawl_source(base): break  # 命中即停

    # —— 主页扫描
    def _crawl_source(self, base):
        try:
            r = self.session.get(base, timeout=20)
        except Exception as e:
            print("首页请求异常：", base, e); return False
        if r.status_code != 200:
            print("首页请求失败：", base, r.status_code); return False

        soup = BeautifulSoup(r.text, "html.parser")

        # 优先容器（带 dwdata-time 的左栏列表）
        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if items:
            for div in items:
                # ① 容器上的 dwdata-time
                dts = (div.get("dwdata-time") or "").strip()
                if dts:
                    try:
                        pub_d = datetime.strptime(dts.split()[0], "%Y-%m-%d").date()
                        if pub_d != self.target_date:
                            continue
                    except: pass
                # 标题与 url
                a = div.find("a", href=True)
                if not a: continue
                title_text = norm(a.get_text())
                if not self.daily_title_pat.search(title_text):
                    continue
                # ② 标题括号日期
                t2 = date_from_bracket_title(title_text)
                if t2 and t2 != self.target_date:
                    continue
                # ③ 详情页复核
                abs_url = urljoin(base, a["href"])
                if self._try_detail(abs_url): return True
            print("[MISS] 容器通道未命中：", base)

        # 备用：/news/123456.html 的链接扫描
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href","")
            if not re.search(r"/news/\d+\.html$", href): continue
            text = norm(a.get_text())
            if not self.daily_title_pat.search(text): continue
            t2 = date_from_bracket_title(text)
            if t2 and t2 != self.target_date: continue
            links.append(urljoin(base, href))

        seen = set()
        for url in links:
            if url in seen: continue
            seen.add(url)
            if self._try_detail(url): return True

        print("[MISS] 本源未命中目标日期：", base)
        return False

    # —— 详情页复核 + 提取
    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        # 只认“三茅日报”
        if not page_title or not self.daily_title_pat.search(page_title): return False
        # 标题括号日期
        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date: return False
        # 详情页发布时间（若没括号日期，则用它做第三道校验）
        if pub_dt and pub_dt.date() != self.target_date and not t3: return False
        if not titles: return False

        self.results.append({
            "title": page_title,
            "url": abs_url,
            "date": (pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else f"{self.target_date} 09:00"),
            "titles": titles
        })
        print(f"[HIT] {abs_url} -> {len(titles)} 条")
        return True

    # —— 提取详情页时间
    def _extract_pub_time(self, soup: BeautifulSoup):
        cand = []
        # meta/time 混合尝试
        for t in soup.select("time[datetime]"): cand.append(t.get("datetime",""))
        for m in soup.select("meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"):
            cand.append(m.get("content",""))
        for sel in [".time",".date",".pubtime",".publish-time",".post-time",".info","meta[itemprop='datePublished']"]:
            for x in soup.select(sel):
                if isinstance(x, Tag):
                    cand.append(x.get_text(" ", strip=True))
        # 中文/数字日期解析
        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        def parse_one(s):
            m = pat.search(s or "")
            if not m: return None
            try:
                y,mo,d = int(m[1]),int(m[2]),int(m[3])
                hh = int(m[4]) if m[4] else 9
                mm = int(m[5]) if m[5] else 0
                return datetime(y,mo,d,hh,mm,tzinfo=_tz())
            except: return None
        dts = [dt for dt in map(parse_one, cand) if dt]
        if dts:
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    # —— 详情页抓取 + 正文净化（只保留 strong 标题）
    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6, 20))
            if r.status_code != 200:
                print("[DetailFail]", url, r.status_code); return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # 页标题
            title_tag = soup.find(["h1","h2"])
            page_title = norm(title_tag.get_text()) if title_tag else ""

            # 发布时间（第三道校验用）
            pub_dt = self._extract_pub_time(soup)

            # 主体容器（你圈出的那个）
            container = soup.select_one(
                ".content-con.hr-rich-text.fn-wenda-detail-infomation.fn-hr-rich-text.custom-style-w"
            ) or soup

            # 过滤明显不需要的板块（相关阅读/免责声明/上一篇-下一篇等）
            for sel in [
                ".other-wrap",           # 相关阅读区
                ".txt",                  # 免责声明“注：文中内容…”
                "a.prev.fn-dataStatistics-btn",  # 上一篇
                "a.next.fn-dataStatistics-btn",  # 下一篇
                ".footer",
                ".bottom",
            ]:
                for bad in container.select(sel):
                    bad.decompose()

            titles = self._extract_strong_titles(container)
            # fallback：极端情况下，页面没有 strong，尝试数字编号行
            if not titles:
                titles = self._extract_numbered_titles(container)

            return pub_dt, titles, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    def _extract_strong_titles(self, root: Tag):
        """
        只保留正文里每条新闻的 <strong> 标题文本。
        - 常见结构：<p><strong>标题</strong></p>  或  <strong>标题</strong>
        - 忽略空/过短/广告样文本
        """
        keep = []
        # 新闻标题往往在 <h2> 区块内部的 <p>/<strong> 里，这里宽松匹配
        for st in root.select("strong"):
            text = norm(st.get_text())
            if not text: continue
            # 消噪：忽略无意义/过短/非新闻句
            if len(text) < 4:  # 比如“目录”等
                continue
            # 忽略“阅读量/来源”等尾巴（保险再切一次）
            text = re.split(r"[（(]?(阅读|阅读量|浏览|来源)[:：]\s*\d+.*$", text)[0].strip()
            if not text: continue
            keep.append(text)

        # 去重并保序
        seen, out = set(), []
        for t in keep:
            if t in seen: continue
            seen.add(t)
            out.append(t)
        return out

    def _extract_numbered_titles(self, root: Tag):
        """
        兜底：若页面没有 <strong>，取带编号的标题行（1.  xx / 1、xx / （1）xx）
        """
        out = []
        for p in root.find_all(["p","h2","h3","div","span","li"]):
            text = norm(p.get_text())
            if looks_like_numbered(text):
                # 把编号去掉只留标题
                text = re.sub(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*", "", text)
                # 到“（”前截断，去掉可能的补充括号
                text = re.split(r"[（(]", text)[0].strip()
                if text and len(text) >= 4:
                    out.append(text)
        # 去重并保序
        seen, final = set(), []
        for t in out:
            if t in seen: continue
            seen.add(t); final.append(t)
        return final

# ========== Markdown ==========
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

    it = items[0]  # 当天仅一条
    for idx, t in enumerate(it["titles"], 1):
        out.append(f"{idx}. {t}  ")
    out.append(f"[查看详细]({it['url']})  ")
    return "\n".join(out)

# ========== 主入口 ==========
if __name__ == "__main__":
    print("执行 hr_news_crawler.py（当天一条 · 三重日期校验 · strong 标题提取）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("人资日报｜每日要点", md)
