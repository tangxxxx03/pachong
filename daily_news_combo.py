# -*- coding: utf-8 -*-
"""
财富中文网 · 商业新闻爬虫 + AI 摘要 + 钉钉推送
支持：
- Fortune China 商业频道抓取
- AI 摘要（SiliconFlow / OpenAI 兼容 API）
- 安全检查（防脑补、保数字、去标题党）
- 钉钉多机器人推送（加签）
"""

import os, re, time, hmac, hashlib, base64, json
import requests
from urllib.parse import urljoin, quote_plus
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# 禁用代理避免 407
for _k in ("http_proxy","https_proxy","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","all_proxy"):
    os.environ.pop(_k, None)

# 会话重试
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
_SESSION = requests.Session()
_SESSION.mount("http://", HTTPAdapter(max_retries=Retry(total=3)))
_SESSION.mount("https://", HTTPAdapter(max_retries=Retry(total=3)))


# ============================
#        日期配置
# ============================
def get_target_date() -> str:
    """从环境变量读取日期，否则默认取北京时间昨天"""
    target_date = os.getenv("TARGET_DATE", "").strip()
    if target_date:
        return target_date

    today = datetime.utcnow() + timedelta(hours=8)
    yday = today - timedelta(days=1)
    return yday.strftime("%Y-%m-%d")


# ============================
#   AI 生成摘要（SiliconFlow）
# ============================
AI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.siliconflow.cn/v1").rstrip("/")
AI_CHAT_URL = f"{AI_API_BASE}/chat/completions"
AI_MODEL = os.getenv("AI_MODEL", "Qwen/Qwen2.5-14B-Instruct")


def _need_fallback(summary: str, title: str, content: str) -> bool:
    if not summary:
        return True

    s = summary.strip()
    if len(s) < 6 or len(s) > 40:
        return True

    nums_title = re.findall(r"\d+", title or "")
    if nums_title:
        if not any(n in s for n in nums_title):
            return True

    risky_words = ["竞争对手", "对手", "首次", "史上", "爆款", "重磅"]
    snippet = (content[:500] or "") + (title or "")

    for w in risky_words:
        if w in s and w not in snippet:
            return True

    return False


def get_ai_summary(content: str, fallback_title: str = "") -> str:
    if not content or len(content) < 30:
        return fallback_title or "内容过短，无需摘要"

    if not AI_API_KEY:
        return fallback_title or "（未配置 OPENAI_API_KEY）"

    system_prompt = (
        "你是中文商业新闻编辑，请为新闻生成【一句话摘要】。\n"
        "必须严格遵守：禁止脑补，不得添加原文未出现的信息；\n"
        "不得使用“竞争对手、首次、史上、重磅、爆款”等推断性词汇；\n"
        "摘要需保留关键数字与主体，长度≤25字，客观中性。"
    )

    user_content = f"请基于以下新闻生成一句摘要：\n\n{content[:2000]}"

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 120,
        "temperature": 0.2,
    }

    try:
        resp = requests.post(AI_CHAT_URL, json=payload, timeout=30)
        resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"].strip().splitlines()[0]
    except Exception:
        return fallback_title or "（AI 摘要失败）"

    if _need_fallback(summary, fallback_title, content):
        return fallback_title or summary or "（AI 摘要不可靠）"

    return summary


# ============================
#      解析财富中文网文章
# ============================
def fetch_article(url: str) -> dict:
    resp = _SESSION.get(url, timeout=10)
    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.select_one("h1").get_text(strip=True) if soup.select_one("h1") else ""
    time_tag = soup.select_one(".source-date")
    pub_time = time_tag.get_text(strip=True) if time_tag else ""

    paragraphs = soup.select(".article-entry p")
    content = "\n".join(p.get_text(strip=True) for p in paragraphs)

    return {
        "url": url,
        "title": title,
        "time": pub_time,
        "content": content,
    }


# ============================
#        抓取新闻列表
# ============================
LIST_URL = "https://www.fortunechina.com/business/c/{date}.htm"

def fetch_news_list(date_str: str):
    url = LIST_URL.format(date=date_str.replace("-", ""))
    resp = _SESSION.get(url, timeout=10)

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.select(".news-list a")

    out = []
    for a in links:
        href = urljoin(url, a.get("href"))
        out.append(href)

    return out


# ============================
#     钉钉机器人推送
# ============================
def sign_dingtalk(secret: str):
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    h = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(h))
    return ts, sign


def push_dingtalk(text: str):
    bases = (os.getenv("DINGTALK_BASES") or "").split(",")
    secrets = (os.getenv("DINGTALK_SECRETS") or "").split(",")

    for base, secret in zip(bases, secrets):
        if not base:
            continue
        ts, sign = sign_dingtalk(secret)
        url = f"{base}&timestamp={ts}&sign={sign}"

        body = {
            "msgtype": "markdown",
            "markdown": {"title": "每日商业资讯", "text": text},
        }
        try:
            requests.post(url, json=body, timeout=10)
        except:
            pass


# ============================
#            主流程
# ============================
def main():
    date = get_target_date()
    print(f"🗓 目标日期：{date}")

    news_urls = fetch_news_list(date)
    print(f"📌 共找到 {len(news_urls)} 条新闻。")

    items = []
    for url in news_urls:
        article = fetch_article(url)
        summary = get_ai_summary(article["content"], article["title"])
        items.append((summary, article["url"]))

    # 组装钉钉 Markdown
    md = f"### 📰 财富中文网 · 商业资讯（{date}）\n"
    for s, u in items:
        md += f"- **{s}**  \n  <{u}>\n"

    push_dingtalk(md)
    print("✅ 已推送至钉钉。")


if __name__ == "__main__":
    main()
