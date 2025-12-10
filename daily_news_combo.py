# -*- coding: utf-8 -*-
"""
三茅网 + 财富中文网 合并爬虫 V16 (防幻觉增强版)
—————————————————————————
更新重点：
1. 优化 System Prompt：加入“严格基于原文”、“禁止编造”等指令。
2. 降低 Temperature：设为 0.1，强制 AI 保持客观，减少发散。
3. 输入增强：同时投喂标题和正文，辅助 AI 理解上下文。
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

# --- 🎯 1. AI 依赖检查 ---
AI_DEBUG_MSG = "" 
try:
    from openai import OpenAI
    HAS_OPENAI_LIB = True
except ImportError:
    print("⚠️ 警告：缺少 openai 库，请在 yml 文件中运行 pip install openai")
    HAS_OPENAI_LIB = False
    AI_DEBUG_MSG = "(AI库缺失)"
# -----------------------

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


# ================== AI 总结模块 (防幻觉核心修改) ==================

AI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.siliconflow.cn/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "Qwen/Qwen2.5-7B-Instruct")

# 初始化 AI 客户端
AI_CLIENT = None
if HAS_OPENAI_LIB:
    if AI_API_KEY:
        try:
            AI_CLIENT = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE)
        except Exception as e:
            print(f"[AI Init Error] {e}")
            AI_DEBUG_MSG = f"(AI配置错误)"
    else:
        AI_DEBUG_MSG = "(AI Key缺失)"
elif not AI_DEBUG_MSG:
    AI_DEBUG_MSG = "(AI库缺失)"

def get_ai_summary(content: str, title: str = "") -> str:
    """调用 AI 生成一句话摘要 (防幻觉版)"""
    if not AI_CLIENT:
        return f"{title} {AI_DEBUG_MSG}"

    if not content or len(content) < 50:
        return title

    print(f"  🤖 正在 AI 总结: {title[:10]}...")
    
    # --- 🛡️ 防幻觉 Prompt 设计 ---
    system_prompt = (
        "你是一个严谨、客观的新闻编辑助手。你的任务是根据提供的文章正文生成摘要。\n"
        "遵守以下绝对规则：\n"
        "1. **严格基于原文**：摘要内容必须完全来源于提供的正文，严禁引入原文未提及的外部知识，严禁编造事实。\n"
        "2. **一句话总结**：生成一句流畅的中文陈述句，概括文章核心事件或观点。\n"
        "3. **客观中立**：去除原文中的营销性、夸张性词汇，保持客观冷静的语调。\n"
        "4. **长度限制**：控制在 60 字以内。\n"
        "5. **兜底机制**：如果正文内容空洞或无法提取有效信息，请直接返回“原标题”。"
    )
    
    user_prompt = f"原标题：{title}\n\n文章正文：\n{content[:2500]}" # 稍微增加一点截取长度
    # ---------------------------

    try:
        resp = AI_CLIENT.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=100,
            temperature=0.1 # ❄️ 核心修改：降温到 0.1，让 AI 变得极其保守，不乱发挥
        )
        summary = resp.choices[0].message.content.strip()
        # 清理符号
        summary = summary.replace('"', '').replace("'", "").replace("\n", " ")
        
        # 简单校验：如果 AI 复读了“原标题”三个字，就用真的原标题
        if "原标题" in summary and len(summary) < 10:
            return title
            
        print(f"  ✨ 摘要成功: {summary[:20]}...")
        return summary
    except Exception as e:
        print(f"  ⚠️ AI 调用失败: {e}")
        return f"{title} (AI调用失败)"


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
        except Exception as e:
            print(f"[HR Error] {base}: {e}")
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
                    "date": str(self.target_date),
                    "titles": titles
                })
                return True
        except: pass
        return False

def build_hr_md(crawler):
    if not crawler.results: return "> 今日未抓取到三茅日报。\n"
    it = crawler.results[0]
    md = [f"**三茅日报 · {it['date']}** \n"]
    for i, t in enumerate(it['titles'], 1):
        md.append(f"{i}. {t}")
    md.append(f"\n[👉 查看原文]({it['url']})\n")
    return "\n".join(md)


# ================== 财富中文网爬虫 ==================

BASE_FORTUNE = "https://www.fortunechina.com"
LIST_URL = "https://www.fortunechina.com/shangye/"

class FortuneCrawler:
    def __init__(self, max_items=5):
        self.session = make_session()
        self.max_items = max_items
        self.items = []

    def run(self):
        print(f"[Fortune] 开始抓取列表 (Max: {self.max_items})...")
        try:
            r = self.session.get(LIST_URL, timeout=15)
            r.encoding = "utf-8" 
            soup = BeautifulSoup(r.text, "html.parser")
            
            cnt = 0
            for li in soup.select("ul.news-list li.news-item"):
                if cnt >= self.max_items: break
                
                h2 = li.find("h2")
                a = li.find("a", href=True)
                date_div = li.find("div", class_="date")
                
                if not (h2 and a): continue
                
                href = a["href"].strip()
                if "content_" not in href: continue 
                
                title = norm(h2.get_text())
                pub_date = norm(date_div.get_text()) if date_div else ""
                full_url = urljoin(LIST_URL, href)
                
                content = self._fetch_content(full_url)
                ai_summary = get_ai_summary(content, title)
                
                self.items.append({
                    "title": title,
                    "summary": ai_summary, 
                    "url": safe_url(full_url),
                    "date": pub_date
                })
                cnt += 1
                
        except Exception as e:
            print(f"[Fortune Error] {e}")

    def _fetch_content(self, url):
        """抓取正文用于 AI 总结"""
        try:
            r = self.session.get(url, timeout=10)
            r.encoding = "utf-8" 
            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.select_one("div.article-mod div.word-text-con") or \
                        soup.select_one("div.article-content")
            
            if container:
                return norm(container.get_text())
        except:
            pass
        return ""

def build_fortune_md(crawler):
    if not crawler.items: return "> 财富中文网暂无内容。\n"
    md = ["**财富中文网 · 商业精选 (AI 摘要)** \n"]
    for i, it in enumerate(crawler.items, 1):
        display_text = it['summary']
        md.append(f"{i}. [{display_text}]({it['url']})")
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
    print("=== 启动合并爬虫 V16 (防幻觉版) ===")
    
    # 1. 三茅
    hr = HRLooCrawler()
    hr.run()
    hr_md = build_hr_md(hr)
    
    # 2. 财富
    fc = FortuneCrawler(max_items=int(os.getenv("FORTUNE_MAX_ITEMS") or 5))
    fc.run()
    fc_md = build_fortune_md(fc)
    
    # 3. 合并
    final_md = (
        f"**人资 & 商业早报 ({now_tz().strftime('%Y-%m-%d')})** \n\n"
        "### 一、HR 热点 (三茅网)\n"
        f"{hr_md}\n"
        "### 二、商业热点 (财富中文网)\n"
        f"{fc_md}"
    )
    
    print("\n--- Markdown 预览 ---\n")
    print(final_md)
    
    send_dingtalk("人资&商业早报", final_md)

if __name__ == "__main__":
    main()
