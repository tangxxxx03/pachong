# -*- coding: utf-8 -*-
"""
三茅人资日报 + 财富中文网·商业频道
—— 合并版爬虫 + AI 摘要 + 钉钉推送 V11 (修复 AI 导入)
"""

import os
import re
import time
import csv
import hmac
import ssl
import base64
import hashlib
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup, Tag
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# --- 🎯 核心修正：导入 OpenAI 客户端 ---
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    print("⚠️ 警告：缺少 'openai' 库。请运行 pip install openai 安装。")
    HAS_OPENAI = False
# ----------------------------------------

try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo

# ===================== 通用工具 (保持不变) =====================

def _tz():
    return ZoneInfo("Asia/Shanghai")

def now_tz():
    return datetime.now(_tz())

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def zh_weekday(dt):
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]

def _sign_webhook(base, secret):
    """
    钉钉签名，兼容“base 不带参数 / 已带 ?access_token=”两种情况。
    """
    if not base:
        return ""
    if not secret:
        return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    sign = urllib.parse.quote_plus(
        base64.b64encode(hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest())
    )
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"


class LegacyTLSAdapter(HTTPAdapter):
    """
    为一些老站点兼容 TLS
    """
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def make_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s


# ===================== 一、三茅 · HRLoo 三茅日报爬虫 (保持不变) =====================

# (此处省略三茅爬虫 HRLooCrawler 类的完整定义，因为它在您提供的代码中是完整的且与本次修正无关)


# ===================== 二、财富中文网 · 商业频道爬虫 + AI 摘要 =====================

FC_BASE = "https://www.fortunechina.com"
FC_LIST_URL_BASE = "https://www.fortunechina.com/shangye/"
FC_MAX_PAGES = 1
FC_MAX_RETRY = 3

FC_OUTPUT_CSV = "fortunechina_articles_with_ai_title.csv"
FC_OUTPUT_MD = "fortunechina_articles_with_ai_title.md"


def get_target_date() -> str:
    """
    决定财富中文网要抓取的目标日期
    """
    env_date = os.getenv("TARGET_DATE", "2025-12-07").strip() # 默认值修正为 2025-12-07
    if env_date:
        return env_date

    tz_cn = timezone(timedelta(hours=8))
    yesterday_cn = (datetime.now(tz_cn) - timedelta(days=1)).strftime("%Y-%m-%d")
    return yesterday_cn


FC_TARGET_DATE = get_target_date()

FC_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}

# --- 🎯 核心修正：AI 客户端初始化 ---

AI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-lTg1L3LAYY1rGfWH21QgK7bkCoe4SIQZJIYiW0c9W2Gg4Zlq").strip() # 从环境变量读取，或使用你的 Key
AI_API_BASE = os.getenv("AI_API_BASE") # 优先使用环境变量中的 base url
AI_MODEL = os.getenv("AI_MODEL", "gpt-3.5-turbo") # 默认模型

if HAS_OPENAI and AI_API_KEY:
    try:
        # 使用环境变量中的 BASE_URL
        AI_CLIENT = OpenAI(
            api_key=AI_API_KEY,
            base_url=AI_API_BASE if AI_API_BASE else None 
        )
        print(f"[AI CFG] 成功初始化 AI 客户端。模型: {AI_MODEL}")
    except Exception as e:
        print(f"[AI CFG] ⚠️ AI 客户端初始化失败: {e}")
        HAS_OPENAI = False
else:
    AI_CLIENT = None
# -------------------------------------


def get_ai_summary(content: str, fallback_title: str = "") -> str:
    """
    使用 AI 客户端生成一句话摘要。
    """
    if not HAS_OPENAI or not AI_CLIENT:
        print("  ⚠️ AI 功能未初始化或未配置 API Key，跳过摘要。")
        return fallback_title or "（未配置 AI 摘要）"

    if not content or len(content) < 50:
        return fallback_title or "内容过短，无需摘要"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    # 使用 AI_CHAT_URL，需要确保 BASE URL 是正确的 OpenAI 兼容地址。
    # 我们这里使用客户端自带的 chat.completions.create 即可，更安全
    
    print("  🤖 正在调用 AI 生成摘要...")

    try:
        resp = AI_CLIENT.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "你是一个严谨的商务分析师，负责将长篇新闻快速提炼。请将新闻正文提炼概括为**一句完整的陈述句总结**，用于内部沟通，要求客观、信息完整、不超过50个字。",
                },
                {"role": "user", "content": content[:2000]},
            ],
            max_tokens=150,
            temperature=0.3,
        )

        summary = resp.choices[0].message.content.strip()
        summary = summary.splitlines()[0].strip()
        print(f"  ✨ AI 摘要：{summary}")
        return summary or (fallback_title or "（AI 摘要为空）")

    except Exception as e:
        print(f"  ⚠️ AI 调用失败：{e}")
        return fallback_title or f"[AI 调用失败: {e}]"


# (fc_fetch_list 和 fc_fetch_article_content 函数，以及后续的保存和推送逻辑，保持不变)

def fc_fetch_article_content(item: dict):
    # ... (原有逻辑，仅在最后调用 get_ai_summary)
    # ... (省略网络请求、抓取正文 content 的逻辑)

    # 最终成功抓取 content 后:
    if item["content"] and "获取失败" not in item["content"]:
        item["ai_summary"] = get_ai_summary(item["content"], item["title"])

# (其他函数省略)

def main():
    print("=== 执行合并爬虫：三茅 + 财富中文网 ===")

    # 1. 三茅日报
    # ... (省略三茅抓取逻辑)

    # 2. 财富中文网
    print("\n>>> [步骤2] 抓取财富中文网 · 商业频道 + AI 摘要")
    fc_articles = run_fortune_crawler()

    # 3. 合并 Markdown
    # ... (省略合并 Markdown 逻辑)

    # 4. 发送钉钉
    # ... (省略发送钉钉逻辑)

# (if __name__ == "__main__": main() )
