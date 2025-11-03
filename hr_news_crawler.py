# -*- coding: utf-8 -*-
"""
HRLoo（三茅人力资源网）· 三茅日报净化版（三重日期校验·当天一条）
————————————————————————————————————————————
特性：
1）仅抓“当天”的《三茅日报》一条（命中即停）；
2）智能提取正文要点——同时兼容“带编号/不带编号”的小节标题；
3）强力净化：剔除“日期/阅读量（content-desc）”“上一篇/下一篇”
   “相关资讯/底部推荐区（other-wrap/active）”“统计按钮（fn-dataStatistics-btn）”
   等一切无关内容；
4）输出 Markdown，可对接钉钉（未配置则跳过推送）。

环境变量（可选）：
- HR_TARGET_DATE=YYYY-MM-DD   指定目标日期（不设则用今日，Asia/Shanghai）
- SRC_HRLOO_URLS              源站列表，逗号分隔，默认 https://www.hrloo.com/
- DINGTALK_BASE / DINGTALK_SECRET  钉钉机器人

"""

import os, re, time, hmac, ssl, base64, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ========== 时区 ==========
try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

def _tz(): return ZoneInfo("Asia/Shanghai")
def now_tz(): return datetime.now(_tz())
def zh_weekday(dt): return ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
def norm(s): return re.sub(r"\s+", " ", (s or "").strip())

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
    s.headers.update({"User-Agent":"Mozilla/5.0", "Accept-Language":"zh-CN,zh;q=0.9"})
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500,502,503,504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s

# ========== 工具 ==========
CN_TITLE_DATE = re.compile(r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]")

def date_from_bracket_title(text: str):
    m = CN_TITLE_DATE.search(text or "")
    if not m: return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except:
        return None

# 需要强制剔除的父级 class（命中任一即跳过）
BLOCK_CONTAINER_CLASSES = {
    # 顶部：日期/阅读量等
    "content-desc",
    # 底部推荐/相关资讯区块
    "other-wrap", "active", "other", "other-wrap-con",
    # 上/下一篇导航等
    "prevnext", "prevnext-wrap",
    # 标签/热词区
    "tags-layout", "tag-layout", "hot-layout",
    # 统计按钮/阅读统计
    "fn-dataStatistics-btn", "fn-dataStatistics",
    # 右侧栏与页脚干扰（兜底）
    "aside", "sidebar", "copyright", "footer"
}

# 命中文本关键词就剔除（多一层兜底）
DROP_TEXT_PAT = re.compile(
    r"(阅读量|阅读\b|上一篇|下一篇|相关资讯|热门|热榜|责任编辑|来源[:：]|免责声明|版权|本文内容|标签)",
    re.I
)

def has_block_ancestor(node):
    """向上检查父级是否带有需剔除的 class。"""
    p = node.parent
    while p and getattr(p, "attrs", None) is not None:
        classes = set((p.get("class") or []))
        if classes & BLOCK_CONTAINER_CLASSES:
            return True
        p = p.parent
    return False

# ========== 爬虫 ==========
class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1  # 当天仅留一条

        # 目标日期：环境变量或今日
        t = (os.getenv("HR_TARGET_DATE") or "").strip()
        if t:
            try:
                y, m, d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y, m, d)
            except:
                print("⚠️ HR_TARGET_DATE 解析失败，使用今日。")
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()

        self.daily_title_pat = re.compile(r"三茅日[报報]")
        # 顶部频道页 + HR 专栏页同时兜住
        default_src = "https://www.hrloo.com/"
        add_src = "https://www.hrloo.com/news/hr"
        env_src = os.getenv("SRC_HRLOO_URLS", f"{default_src},{add_src}")
        self.sources = [u.strip() for u in env_src.split(",") if u.strip()]
        print(f"[CFG] target_date={self.target_date} {zh_weekday(now_tz())}  sources={self.sources}")

    def crawl(self):
        for base in self.sources:
            if self._crawl_source(base):
                break  # 命中即停

    def _crawl_source(self, base):
        try:
            r = self.session.get(base, timeout=20)
        except Exception as e:
            print("首页请求异常：", base, e); return False
        if r.status_code != 200:
            print("首页请求失败：", base, r.status_code); return False

        soup = BeautifulSoup(r.text, "html.parser")

        # 1) 主列表容器：dwdata-time + 标题括号日期 双重匹配
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
                if not a: continue
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

        # 2) 备用：全页链接扫描
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href", "")
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
            if url in seen: continue
            seen.add(url)
            if self._try_detail(url):
                return True

        print("[MISS] 本源未命中目标日期：", base)
        return False

    # 详情复核 + 清洗
    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        if not page_title or not self.daily_title_pat.search(page_title):
            return False
        # 标题括号日期再校验
        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date:
            return False
        # 若未在标题中给出日期，则用详情页发布时间兜底
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

    def _extract_pub_time(self, soup):
        cand = []
        for t in soup.select("time[datetime]"):
            cand.append(t.get("datetime", ""))
        for m in soup.select("meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"):
            cand.append(m.get("content", ""))
        for sel in [".time", ".date", ".pubtime", ".post-time", ".publish-time", ".info"]:
            for x in soup.select(sel):
                cand.append(x.get_text(" ", strip=True))

        pat = re.compile(r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?")
        def parse_one(s):
            m = pat.search(s or "")
            if not m: return None
            try:
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
                hh = int(m[4]) if m[4] else 9
                mm = int(m[5]) if m[5] else 0
                return datetime(y, mo, d, hh, mm, tzinfo=_tz())
            except:
                return None

        dts = [dt for dt in map(parse_one, cand) if dt]
        if dts:
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6, 20))
            if r.status_code != 200:
                print("[DetailFail]", url, r.status_code); return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")

            title_tag = soup.find(["h1", "h2"])
            page_title = norm(title_tag.get_text()) if title_tag else ""

            pub_dt = self._extract_pub_time(soup)

            # 主要正文容器（多选一兜底）
            container = (
                soup.select_one("article, .article, .detail-content, .news-content, .content, .post-content")
                or soup.select_one(".hr-rich-text, .content-wrap, .content-con")
                or soup
            )

            titles = self._extract_daily_item_titles(container)
            return pub_dt, titles, page_title
        except Exception as e:
            print("[DetailError]", url, e); return None, [], ""

    # —— 抽取“每日要点”标题（兼容带编号/不带编号），并做强力净化 —— #
    def _extract_daily_item_titles(self, root):
        by_num = {}   # 1,2,3… -> 文本
        plain = []    # 非编号小节（按出现顺序）

        # 1）优先：识别带编号的行（（1）1. 1、 ① 之类）
        num_pat = re.compile(r"^\s*[（(]?\s*(\d{1,2})\s*[)）]?\s*[、.．)]?\s*(.+)$")

        # 2）候选元素：标题常见标签 + 段落
        for node in root.find_all(["h2", "h3", "h4", "strong", "b", "p", "li", "div", "span"]):
            # 父级黑名单过滤
            if has_block_ancestor(node):
                continue

            raw = (node.get_text(" ", strip=True) or "").strip()
            if not raw:
                continue

            # 文本黑名单兜底
            if DROP_TEXT_PAT.search(raw):
                continue

            # 过滤过短/全数字/疑似时间与阅读
            if len(raw) < 4:
                continue
            if re.fullmatch(r"\d+\s*(阅读|次)?", raw):
                continue
            if re.fullmatch(r"\d{2}-\d{2}-\d{2}.*", raw):
                continue

            # 解析编号
            m = num_pat.match(raw)
            if m:
                try:
                    num = int(m.group(1))
                    txt = m.group(2).strip()
                    # 去掉括号内备注（尽量保留标题的主体）
                    txt = re.split(r"[（(]", txt)[0].strip()
                    if 3 <= len(txt) <= 80:
                        by_num.setdefault(num, txt)
                    continue
                except:
                    pass

            # 非编号标题：尽量捕捉“主题式句子”
            # 经验：中文比例过低/过长/包含句末句号的正文段，跳过
            zh_ratio = len(re.findall(r"[\u4e00-\u9fa5]", raw)) / max(len(raw), 1)
            if zh_ratio < 0.35:
                continue
            # 典型标题往往不以句号结尾
            if raw.endswith(("。", ".", "！", "？")) and len(raw) > 18:
                continue

            # 长度合理的非编号短标题
            if 4 <= len(raw) <= 60:
                plain.append(raw)

        # 3）组装顺序：优先编号 1..N，缺少编号时再填充非编号
        seq = []
        n = 1
        while n in by_num and len(seq) < 15:
            seq.append(by_num[n])
            n += 1
        if len(seq) < 3 and plain:  # 如果这个日更没有编号，用非编号备份
            # 去重保持顺序
            seen = set()
            for t in plain:
                if t in seen: continue
                seen.add(t)
                seq.append(t)
                if len(seq) >= 15: break

        return seq

# ========== Markdown ==========
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
    return "\n".join(out)

# ========== 主入口 ==========
if __name__ == "__main__":
    print("执行 hr_news_crawler.py（当天一条 · 三重日期校验版）")
    c = HRLooCrawler()
    c.crawl()
    md = build_md(c.results)
    print("\n===== Markdown Preview =====\n")
    print(md)
    send_dingtalk_markdown("人资早报｜每日要点", md)
