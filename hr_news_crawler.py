# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报净化版（当天一条 · 三重日期校验 · 命中即停）
- 优先：首页 dwdata-time 精确匹配目标日期
- 其次：标题括号日期（（YYYY年M月D日）或 (YYYY-MM-DD)）匹配
- 兜底：详情页时间提取匹配
- 自动去“阅读量”等尾巴，仅输出当天一条；可钉钉推送
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ===== 时区/时间 =====
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo
def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]

# ===== 钉钉 =====
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

# ===== 会话 =====
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

# ===== 工具：从“括号标题”提取日期 =====
CN_TITLE_DATE = re.compile(r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]")
def date_from_bracket_title(text:str):
    m = CN_TITLE_DATE.search(text or "")
    if not m: return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except: return None

# ===== 主体 =====
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1  # 命中即停

        # 目标日期：env 指定，否则今天
        t = (os.getenv("HR_TARGET_DATE") or "").strip()
        if t:
            try:
                y,m,d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y,m,d)
            except:
                print("⚠️ HR_TARGET_DATE 解析失败，改用今天。")
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()

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
            print("首页请求异常：", base, e); return False
        if r.status_code != 200:
            print("首页请求失败：", base, r.status_code); return False

        soup = BeautifulSoup(r.text, "html.parser")

        # A. 优先：官方列表容器（有些情况下不会下发给非浏览器）
        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if items:
            for div in items:
                # ① 首页 data-time 强筛
                dts = (div.get("dwdata-time") or "").strip()
                if dts:
                    try:
                        pub_d = datetime.strptime(dts.split()[0], "%Y-%m-%d").date()
                        if pub_d != self.target_date:
                            continue
                    except: pass
                # ② 标题括号日期次筛
                a = div.find("a", href=True)
                if not a: continue
                title_text = norm(a.get_text())
                if not self.daily_title_pat.search(title_text):  # 必须是“三茅日报”
                    continue
                t2 = date_from_bracket_title(title_text)
                if t2 and t2 != self.target_date:
                    continue

                # 详情复核
                abs_url = urljoin(base, a["href"])
                if self._try_detail(abs_url):
                    return True
            print("[MISS] 容器通道未命中：", base)

        # B. 备用：遍历所有新闻链接 + 标题括号日期强筛
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href",""); 
            if not re.search(r"/news/\d+\.html$", href): 
                continue
            text = norm(a.get_text())
            if not self.daily_title_pat.search(text):
                continue
            t2 = date_from_bracket_title(text)
            if t2 and t2 != self.target_date:
                continue
            links.append(urljoin(base, href))

        # 去重 & 逐个复核
        seen = set()
        for url in links:
            if url in seen: continue
            seen.add(url)
            if self._try_detail(url):
                return True

        print("[MISS] 本源未命中目标日期：", base)
        return False

    # 进入详情并最终判定
    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        if not page_title or not self.daily_title_pat.search(page_title):
            return False

        # 先用标题括号日期判定（很多详情页时间写在标题里）
        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date:
            return False

        # 再用正文时间做兜底
        if pub_dt and pub_dt.date() != self.target_date:
            # 如果标题没日期或不确定，再严格用正文时间
            if not t3:
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

    # 详情页时间（兜底）
    def _extract_pub_time(self, soup):
        # 多源尝试：time/meta/常见类名/纯文本
        cand = []
        for t in soup.select("time[datetime]"):
            cand.append(t.get("datetime",""))
        for m in soup.select("meta[property='article:published_time'], meta[name='pubdate'], meta[name='publishdate']"):
            cand.append(m.get("content",""))
        for sel in [".time",".date",".pubtime",".post-time",".publish-time",".info"]:
            for x in soup.select(sel):
                cand.append(x.get_text(" ", strip=True))

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

        dts = [parse_one(x) for x in cand if x]
        dts = [dt for dt in dts if dt]
        if dts: 
            # 取最接近现在且不在未来
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))

        # 彻底没有就返回 None，让上游用标题日期判定
        return None

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6,20))
            if r.status_code != 200:
                print("[DetailFail]", url, r.status_code); return None, [], ""
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
            print("[DetailError]", url, e); return None, [], ""

    # 只保留“1/2/3/…”编号标题，去掉广告/阅读量尾巴
    def _extract_daily_item_titles(self, root):
        ad_words = ["手机","境外","短信","验证码","审核","粉丝","入群","账号","APP","登录","推广","广告","创建申请","协议","关注","申诉","下载","网盘","失信","封号"]
        def strip_views(title:str)->str:
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
            zh_ratio = len(re.findall(r"[\u4e00-\u9fa5]", title)) / max(len(title),1)
            if zh_ratio < 0.3: continue
            by_num.setdefault(num, title)

        seq, n = [], 1
        while n in by_num:
            seq.append(by_num[n]); n += 1
            if n > 20: break
        return seq[:10]

# ===== Markdown =====
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

# ===== 入口 =====
if __name__ == "__main__":
    print("执行 hr_news_crawler_daily_clean_adfree.py（三重日期校验·当天一条）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("人资早报｜每日要点", md)
