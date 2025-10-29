# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报专抓 + 条目净化版
功能：
  ✅ 仅抓取标题包含“三茅日报”的新闻；
  ✅ 从正文中提取编号标题（1、2、3、…）；
  ✅ 自动排除右栏公告与杂讯；
  ✅ 修复“：”截断问题，完整提取新闻标题；
  ✅ 输出 Markdown，可推送钉钉。

环境变量：
  HR_ONLY_TODAY=1   # 仅当天，否则为近24小时
  HR_MAX_ITEMS=15
  SRC_HRLOO_URLS=https://www.hrloo.com/
  DINGTALK_BASE / DINGTALK_SECRET 或 DINGTALK_BASEA / DINGTALK_SECRETA
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ========= 基本工具 =========
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

def tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(tz())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())

def within_24h(dt): return (now_tz() - dt).total_seconds() <= 86400 if dt else False
def same_day(dt): return dt and dt.date() == now_tz().date()

# ========= 钉钉推送 =========
def _sign_webhook(base, secret):
    if not base: return ""
    if not secret: return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    h = hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title, md):
    base = os.getenv("DINGTALK_BASE") or os.getenv("DINGTALK_BASEA")
    secret = os.getenv("DINGTALK_SECRET") or os.getenv("DINGTALK_SECRETA")
    if not base:
        print("🔕 未配置钉钉地址，跳过推送。"); return False
    webhook = _sign_webhook(base, secret)
    try:
        r = requests.post(webhook, json={"msgtype": "markdown","markdown":{"title":title,"text":md}}, timeout=20)
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} code={r.status_code}")
        if not ok: print("resp:", r.text[:200])
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

# ========= 主爬虫 =========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = int(os.getenv("HR_MAX_ITEMS","15"))
        self.detail_timeout = (6,20)
        self.detail_sleep = 0.6
        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.only_today = os.getenv("HR_ONLY_TODAY","0") == "1"
        self.sources = [u.strip() for u in os.getenv("SRC_HRLOO_URLS","https://www.hrloo.com/").split(",") if u.strip()]

    def crawl(self):
        for base in self.sources:
            self._crawl_source(base)

    def _crawl_source(self, base):
        r = self.session.get(base, timeout=20)
        if r.status_code != 200:
            print("首页访问失败", r.status_code); return
        soup = BeautifulSoup(r.text,"html.parser")
        links = []
        for a in soup.select("a[href*='/news/']"):
            href, text = a.get("href",""), norm(a.get_text())
            if re.search(r"/news/\d+\.html$", href) and self.daily_title_pat.search(text):
                links.append(urljoin(base, href))
        if not links:
            links = [urljoin(base,a.get("href")) for a in soup.select("a[href*='/news/']") if re.search(r"/news/\d+\.html$",a.get("href",""))]

        seen=set()
        for url in links:
            if url in seen: continue
            seen.add(url)
            pub_dt, titles, main = self._fetch_detail_clean(url)
            if not main or not self.daily_title_pat.search(main): continue
            if self.only_today and not same_day(pub_dt): continue
            if not self.only_today and not within_24h(pub_dt): continue
            if not titles: continue
            self.results.append({"title":main,"url":url,"date":pub_dt.strftime("%Y-%m-%d %H:%M"),"titles":titles})
            print(f"[OK] {url} -> {len(titles)} 条")
            if len(self.results)>=self.max_items: break
            time.sleep(self.detail_sleep)

    def _extract_pub_time(self,soup):
        txt = soup.get_text(" ")
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?",txt)
        if not m: return None
        y,mo,d = int(m[1]),int(m[2]),int(m[3])
        hh = int(m[4]) if m[4] else 9; mm = int(m[5]) if m[5] else 0
        try: return datetime(y,mo,d,hh,mm,tzinfo=tz())
        except: return None

    def _fetch_detail_clean(self,url):
        try:
            r=self.session.get(url,timeout=self.detail_timeout)
            if r.status_code!=200: return None,[], ""
            r.encoding=r.apparent_encoding or "utf-8"
            soup=BeautifulSoup(r.text,"html.parser")
            title_tag=soup.find(["h1","h2"])
            page_title=norm(title_tag.get_text()) if title_tag else ""
            pub_dt=self._extract_pub_time(soup)
            titles=self._extract_daily_item_titles(soup)
            return pub_dt,titles,page_title
        except Exception as e:
            print("[DetailError]",url,e)
            return None,[], ""

    # —— 核心：提取日报条目标题（已修复第5条截断问题 + 自动去掉无关信息）—— #
    def _extract_daily_item_titles(self,soup):
        blacklist=["粉丝","入群","申诉","短信","验证码","举报","审核","发布协议",
                   "账号","登录","APP","创建申请","广告","下载","推广","礼包"]
        items=[]
        for t in soup.find_all(["h2","h3","h4","strong","b","p","li","span","div"]):
            raw=(t.get_text() or "").strip()
            if not raw: continue
            m=re.match(r"^\s*[（(]?\s*(\d{1,2})\s*[)）]?\s*[、.．]?\s*(.+)$",raw)
            if not m: continue
            num=int(m.group(1)); title=m.group(2).strip()
            # 不截断“：”内容，仅去括注/多余说明
            title=re.split(r"[（\(]{1}",title)[0].strip()
            if not (4<=len(title)<=60): continue
            if len(re.findall(r"[\u4e00-\u9fa5]",title))/max(len(title),1)<0.3: continue
            if any(b in title for b in blacklist): continue
            items.append((num,title))
        if not items: return []
        start=next((i for i,(n,_) in enumerate(items) if n==1),0)
        seq=[]; seen=set(); expect=items[start][0]
        for n,t in items[start:]:
            if n!=expect: break
            if t not in seen:
                seq.append(t); seen.add(t)
            expect+=1
            if expect>20: break
        return seq[:15]

# ========= Markdown 输出 =========
def build_md(items):
    n=now_tz()
    out=[f"**日期：{n.strftime('%Y-%m-%d')}（{zh_weekday(n)}）**  ","","**标题：三茅日报｜条目速览**  ",""]
    if not items:
        out.append("> 未发现新的“三茅日报”。"); return "\n".join(out)
    for i,it in enumerate(items,1):
        out.append(f"{i}. [{it['title']}]({it['url']}) （{it['date']}）  ")
        for j,t in enumerate(it['titles'],1):
            out.append(f"> {j}. {t}  ")
        out.append("")
    return "\n".join(out)

# ========= 主入口 =========
if __name__=="__main__":
    print("执行 hr_news_crawler_daily_clean.py（只抓三茅日报条目标题）")
    c=HRLooCrawler()
    c.crawl()
    md=build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("三茅日报｜条目速览",md)
