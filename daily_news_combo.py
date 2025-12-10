# -*- coding: utf-8 -*-
"""
三茅网 + 财富中文网 合并爬虫 V23 (清单早报风)
—————————————————————————
核心更新：
1. 彻底去标签化：不再强行定义“HR”或“商业”，回归来源属性。
2. 视觉优化：Emoji 引导，链接行内化（更省空间）。
"""

import os
import re
import ssl
import time
import hmac
import base64
import hashlib
import urllib.parse
from datetime import datetime, date
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup, Tag
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# --- AI 依赖 ---
try:
    from openai import OpenAI
    HAS_OPENAI_LIB = True
except ImportError:
    HAS_OPENAI_LIB = False

try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo


# ================== 基础工具 ==================

def _tz():
    return ZoneInfo("Asia/Shanghai")

def now_tz():
    return datetime.now(_tz())

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def zh_weekday(dt: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]

def safe_url(url: str) -> str:
    if not url: return ""
    return quote(url.strip(), safe=":/?&amp;=#%")


# ================== AI 总结模块 ==================

AI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.siliconflow.cn/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "Qwen/Qwen2.5-7B-Instruct")

AI_CLIENT = None
if HAS_OPENAI_LIB and AI_API_KEY:
    try:
        AI_CLIENT = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE)
    except: pass

def get_ai_summary(content: str, title: str = "") -> str:
    """30字极限总结"""
    if not AI_CLIENT: return title 
    if not content or len(content) < 50: return title

    print(f"  🤖 正在 AI 总结: {title[:10]}...")
    
    system_prompt = (
        "你是一个字字千金的快讯编辑。请将新闻压缩为一句**30字以内**的极简短语。\n"
        "规则：1.字数锁死30字内。2.去废话，直接说结论。3.禁止任何标签前缀。"
    )
    user_prompt = f"标题：{title}\n正文：{content[:2000]}"

    try:
        resp = AI_CLIENT.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=60, 
            temperature=0.3 
        )
        summary = resp.choices[0].message.content.strip()
        summary = summary.replace('"', '').replace("'", "").replace("\n", " ")
        summary = re.sub(r"^(摘要|结论|核心|背景)[/:]\s*", "", summary)
        
        if "原标题" in summary and len(summary) < 10: return title
        print(f"  ✨ 摘要成功: {summary[:20]}...")
        return summary
    except:
        return title


# ================== HTTP Session ==================

class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s


# ================== 三茅日报爬虫 ==================

class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.target_date = now_tz().date()
        t = os.getenv("HR_TARGET_DATE", "")
        if t:
            try:
                y, m, d = map(int, t.split("-"))
                self.target_date = date(y, m, d)
            except: pass
        self.sources = ["https://www.hrloo.com/", "https://www.hrloo.com/news/hr"]
        self.daily_title_pat = re.compile(r"三茅日[报報]")

    def run(self):
        for base in self.sources:
            if self._crawl_source(base): break

    def _crawl_source(self, base):
        try:
            r = self.session.get(base, timeout=15)
            r.encoding = "utf-8"
            if r.status_code != 200: return False
            soup = BeautifulSoup(r.text, "html.parser")
            
            items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
            if items:
                for div in items:
                    dts = (div.get("dwdata-time") or "").strip()
                    if dts and str(self.target_date) not in dts: continue
                    a = div.find("a", href=True)
                    if not a: continue
                    if self._check_and_fetch(base, a): return True

            for a in soup.select("a[href*='/news/']"):
                if self._check_and_fetch(base, a): return True
        except: pass
        return False

    def _check_and_fetch(self, base, a):
        text = norm(a.get_text())
        href = a["href"]
        if not self.daily_title_pat.search(text): return False
        abs_url = urljoin(base, href)
        return self._fetch_detail(abs_url)

    def _fetch_detail(self, url):
        try:
            r = self.session.get(url, timeout=15)
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.select_one(".content-con.hr-rich-text") or soup
            titles = []
            for st in container.select("strong"):
                t = norm(st.get_text())
                t = re.sub(r"^\d+[.、]\s*", "", t)
                if len(t) > 5 and "阅读" not in t:
                    titles.append(t)
            if not titles:
                for p in container.select("p"):
                    t = norm(p.get_text())
                    if re.match(r"^\d+[.、]", t) and len(t) > 5:
                        titles.append(re.sub(r"^\d+[.、]\s*", "", t))
            titles = list(dict.fromkeys(titles))

            if titles:
                self.results.append({
                    "title": "三茅日报",
                    "url": safe_url(url),
                    "titles": titles
                })
                return True
        except: pass
        return False

def build_hr_md(crawler):
    if not crawler.results: return "> 今日三茅暂无更新。\n"
    it = crawler.results[0]
    # 中性标题，不强调“HR”
    md = [f"**📰 三茅资讯**"]
    for i, t in enumerate(it['titles'], 1):
        # 链接缩小成一个小图标放在句尾，显得更整洁
        md.append(f"{i}. {t} [🔗]({it['url']})")
    return "\n".join(md) + "\n"


# ================== 财富中文网爬虫 ==================

BASE_FORTUNE = "https://www.fortunechina.com"
LIST_URL = "https://www.fortunechina.com/shangye/"

class FortuneCrawler:
    def __init__(self, max_items=5):
        self.session = make_session()
        self.max_items = max_items
        self.items = []

    def run(self):
        print(f"[Fortune] 开始抓取...")
        try:
            r = self.session.get(LIST_URL, timeout=15)
            r.encoding = "utf-8" 
            soup = BeautifulSoup(r.text, "html.parser")
            
            cnt = 0
            for li in soup.select("ul.news-list li.news-item"):
                if cnt >= self.max_items: break
                
                h2 = li.find("h2")
                a = li.find("a", href=True)
                
                if not (h2 and a): continue
                
                href = a["href"].strip()
                if "content_" not in href: continue 
                
                title = norm(h2.get_text())
                full_url = urljoin(LIST_URL, href)
                
                content = self._fetch_content(full_url)
                ai_summary = get_ai_summary(content, title)
                
                self.items.append({
                    "title": title,
                    "summary": ai_summary, 
                    "url": safe_url(full_url)
                })
                cnt += 1
                
        except Exception as e:
            print(f"[Fortune Error] {e}")

    def _fetch_content(self, url):
        try:
            r = self.session.get(url, timeout=10)
            r.encoding = "utf-8" 
            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.select_one("div.article-mod div.word-text-con") or \
                        soup.select_one("div.article-content")
            
            if container: return norm(container.get_text())
        except: pass
        return ""

def build_fortune_md(crawler):
    if not crawler.items: return "> 今日财富暂无更新。\n"
    # 中性标题，不强调“商业热点”
    md = ["**🚀 财富商业**"]
    for i, it in enumerate(crawler.items, 1):
        display_text = it['summary']
        # 链接放在句尾
        md.append(f"{i}. {display_text} [🔗]({it['url']})")
    return "\n".join(md) + "\n"


# ================== 推送与入口 ==================

def send_dingtalk(title, text):
    bases = (os.getenv("DINGTALK_BASES") or "").split(",")
    secrets = (os.getenv("DINGTALK_SECRETS") or "").split(",")
    
    if not bases or not bases[0]:
        print("🔕 未配置 DINGTALK_BASES")
        return

    for i, base in enumerate(bases):
        base = base.strip()
        if not base: continue
        secret = secrets[i].strip() if i < len(secrets) else ""
        
        if secret:
            ts = str(round(time.time() * 1000))
            s = f"{ts}\n{secret}".encode("utf-8")
            sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest()))
            url = f"{base}&timestamp={ts}&sign={sign}"
        else:
            url = base
            
        try:
            requests.post(url, json={
                "msgtype": "markdown",
                "markdown": {"title": title, "text": text}
            }, timeout=10)
            print(f"✅ 推送成功: 机器人 {i+1}")
        except Exception as e:
            print(f"❌ 推送失败: {e}")

def main():
    print("=== 启动合并爬虫 V23 (清单早报版) ===")
    
    # 1. 三茅
    hr = HRLooCrawler()
    hr.run()
    hr_md = build_hr_md(hr)
    
    # 2. 财富
    fc = FortuneCrawler(max_items=int(os.getenv("FORTUNE_MAX_ITEMS") or 5))
    fc.run()
    fc_md = build_fortune_md(fc)
    
    # 3. 合并 - 极简标题
    final_md = (
        f"**📅 {now_tz().strftime('%m-%d')} 每日早报** \n\n"
        f"{hr_md}\n"
        f"{fc_md}"
    )
    
    print("\n--- Markdown 预览 ---\n")
    print(final_md)
    
    send_dingtalk("每日早报", final_md)

if __name__ == "__main__":
    main()
