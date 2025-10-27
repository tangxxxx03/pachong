# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）爬虫 · 精简要点版（24小时 + 关键词白名单 + 强力去噪 + 合并去重）
输出只保留：对象/金额-补贴-标准/条件-资格/材料-所需/流程-步骤/时间-期限-截至/渠道-入口-平台/范围-地域/依据-政策-文件/咨询-电话（可选）
"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ====== 时区 ======
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

# ========= 小工具 =========
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def now_tz(): return datetime.now(ZoneInfo("Asia/Shanghai"))
def within_24h(dt): return (now_tz() - dt).total_seconds() <= 86400 if dt else False

# ========= 钉钉 =========
def _sign_webhook(base, secret):
    if not base: return ""
    if not secret: return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    h = hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"

def _mask(v: str, head=6, tail=6):
    if not v: return ""
    if len(v) <= head + tail: return v
    return v[:head] + "..." + v[-tail:]

def send_dingtalk_markdown(title, md):
    base = os.getenv("DINGTALK_BASE")      # 必填：无加签也可
    secret = os.getenv("DINGTALK_SECRET")  # 选填：开启“加签”才需要
    if not base:
        print("🔕 未配置 DINGTALK_BASE，跳过推送。")
        return False
    webhook = _sign_webhook(base, secret)
    try:
        r = requests.post(
            webhook,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": md}},
            timeout=20,
        )
        ok = (r.status_code == 200 and r.json().get("errcode") == 0)
        print(f"DingTalk push={ok} base={_mask(base)} http={r.status_code}")
        if not ok: print("DingTalk resp:", r.text[:300])
        return ok
    except Exception as e:
        print("DingTalk error:", e)
        return False

# ========= 网络 =========
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

# ========= 主爬虫 =========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 15
        self.detail_timeout = (6, 20)
        self.detail_sleep = 0.6

        # —— 主题匹配：与“人资政策/用工管理/社保/工资”等强关联 —— #
        self.keywords = [k.strip() for k in os.getenv(
            "HR_FILTER_KEYWORDS",
            "人力资源, 社保, 养老, 医保, 工伤, 生育, 失业, 缴费, 用工, 劳动, 招聘, 工资, 薪酬, 补贴, 就业, 参保, 保障, 户籍, 灵活就业"
        ).split(",") if k.strip()]

        # —— 强力去噪：广告/营销/扫码/注册/验证码/抽奖/APP 等 —— #
        self.noise_words = [
            "手机", "短信", "验证码", "诈骗", "举报", "运营商", "黑名单", "安全",
            "客服", "充值", "密码", "封号", "信号", "注销", "注册", "账号",
            "广告", "下载", "扫码", "二维码", "关注", "转发", "抽奖", "福利",
            "直播", "视频", "评论", "点赞", "私信", "礼包", "优惠券"
        ]

        # —— 要点白名单：只保留这些“有用信息” —— #
        self.keep_words = [
            "对象","适用","范围","城市","地区","地域","户籍","年龄","身份","条件","资格",
            "金额","补贴","标准","比例","上限","下限","额度","享受","待遇",
            "材料","证明","所需","提交","准备","清单",
            "流程","步骤","方式","渠道","入口","平台","办理","申领","申请","登记","注册",
            "时间","期限","截至","起止","截至","截至时间","执行时间",
            "依据","政策","文件","通知","条款","解读",
            "咨询","电话","窗口","地点","地址"
        ]

    # —— 对外入口 —— #
    def crawl(self):
        base = "https://www.hrloo.com/"
        r = self.session.get(base, timeout=20)
        if r.status_code != 200:
            print("首页请求失败", r.status_code); return
        soup = BeautifulSoup(r.text, "html.parser")

        # 主页中形如 /news/123456.html 的新闻链接
        links = [urljoin(base, a.get("href"))
                 for a in soup.select("a[href*='/news/']")
                 if re.search(r"/news/\d+\.html$", a.get("href",""))]

        seen = set()
        for url in links:
            if url in seen: continue
            seen.add(url)
            pub_dt, subtitles, main_title = self._fetch_detail_clean(url)
            if not pub_dt or not within_24h(pub_dt): continue
            if not subtitles: continue
            if not self._match_keywords(main_title, subtitles): continue

            self.results.append({
                "title": main_title or url,
                "url": url,
                "date": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "titles": subtitles
            })
            print(f"[OK] {url} {pub_dt} 要点{len(subtitles)}个")
            if len(self.results) >= self.max_items: break
            time.sleep(self.detail_sleep)

    # —— 明细页抽取 + 清洗 —— #
    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=self.detail_timeout)
            if r.status_code != 200: return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            # 主标题
            h = soup.find(["h1","h2"])
            page_title = norm(h.get_text()) if h else ""

            # 发布时间
            pub_dt = self._extract_pub_time(soup)

            # 先抓“编号段落”（1. / 1、 / 一、 之类）
            raw = []
            for t in soup.find_all(["strong","h2","h3","span","p","li"]):
                text = norm(t.get_text())
                if not text: continue
                # 允许的编号样式
                if not re.match(r"^([（(]?\d+[)）]|[一二三四五六七八九十]+、|\d+\s*[、.．])\s*.+", text):
                    continue
                raw.append(text)

            # 统一清洗 & 过滤
            clean = self._clean_subtitles(raw)
            return pub_dt, clean, page_title
        except Exception as e:
            print("[DetailError]", url, e)
            return None, [], ""

    def _extract_pub_time(self, soup):
        tz = ZoneInfo("Asia/Shanghai")
        txt = soup.get_text(" ")
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?", txt)
        if not m: return None
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        hh = int(m[4]) if m[4] else 9
        mm = int(m[5]) if m[5] else 0
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=tz)
        except: return None

    # —— 只保留“有用信息”的清洗器 —— #
    def _clean_subtitles(self, items):
        out, seen = [], set()
        for t in items:
            # 去编号：1. / 1、 / （1） / 一、 等
            t = re.sub(r"^([（(]?\d+[)）]|[一二三四五六七八九十]+、|\d+\s*[、.．])\s*", "", t)
            t = norm(t)

            # 长度阈值
            if len(t) < 6 or len(t) > 50:  # 过短/过长不要
                continue

            # 中文占比（过滤英文/数字噪声）
            zh_ratio = len(re.findall(r"[\u4e00-\u9fa5]", t)) / max(len(t),1)
            if zh_ratio < 0.5:
                continue

            # 噪声词过滤（营销/扫码/抽奖等）
            if any(w in t for w in self.noise_words):
                continue

            # “要点白名单”过滤（只留有用信息）
            if not any(k in t for k in self.keep_words):
                continue

            # 归一化同义词：金额/补贴/标准；条件/资格；材料/证明/所需；流程/步骤/办理/申请……
            t = re.sub(r"(金额|补贴|标准|额度|比例)", "金额/标准", t)
            t = re.sub(r"(条件|资格)", "申领条件", t)
            t = re.sub(r"(材料|证明|所需)", "所需材料", t)
            t = re.sub(r"(流程|步骤|办理|申请|申领|登记|渠道|入口|平台)", "办理流程/入口", t)
            t = re.sub(r"(时间|期限|截至|起止)", "办理时间", t)
            t = re.sub(r"(对象|适用|范围|城市|地区|地域|户籍|身份|年龄)", "适用对象/范围", t)
            t = re.sub(r"(依据|政策|文件|通知|条款)", "政策依据", t)

            # 去末尾无信息标点
            t = re.sub(r"[，。；、,.]+$", "", t)

            # 去重（按小写和去空格）
            key = t.lower().replace(" ", "")
            if key in seen: continue
            seen.add(key)
            out.append(t)

            if len(out) >= 8:  # 每篇最多 8 条要点
                break
        return out

    def _match_keywords(self, title, subtitles):
        hay = (title or "") + " " + " ".join(subtitles)
        return any(kw in hay for kw in self.keywords)

# ========= Markdown 输出 =========
def build_md(items):
    now = now_tz()
    out = []
    out.append(f"**日期：{now.strftime('%Y-%m-%d')}（{zh_weekday(now)}）**  ")
    out.append("")
    out.append("**标题：早安资讯｜人力资源每日资讯推送**  ")
    out.append("")
    out.append("**主要内容**  ")
    out.append("")

    if not items:
        out.append("> 24小时内无符合关键词的内容。")
        return "\n".join(out)

    for i, it in enumerate(items, 1):
        out.append(f"{i}. [{it['title']}]({it['url']}) （{it['date']}）  ")
        for s in it['titles']:
            out.append(f"> 🟦 {s}  ")
        out.append("")  # 间隔一行
    return "\n".join(out)

# ========= 主入口 =========
if __name__ == "__main__":
    print("执行 hr_news_crawler_minimal.py（仅保留要点）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("早安资讯｜人力资源每日资讯推送", md)
