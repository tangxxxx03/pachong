# -*- coding: utf-8 -*-
"""
财富中文网 · 商业频道 爬虫 + 钉钉推送
https://www.fortunechina.com/shangye/

功能：
1. 抓取商业频道列表页（可多页）
2. 提取：标题 / 链接 / 日期
3. （可选）抓取每篇文章正文内容
4. 把抓到的文章整理成 Markdown，推送到一个或多个钉钉群

环境变量（建议通过 GitHub Secrets 配置）：
  DINGTALK_BASES   = "url1,url2"          # 多个群的 webhook，用逗号隔开
  DINGTALK_SECRETS = "sec1,sec2"         # 对应每个群的 secret（数量可以是 1 个或 N 个）
  —— 或者只配单个：
  DINGTALK_BASE    = "单个群 webhook"
  DINGTALK_SECRET  = "单个群 secret"
"""

import os
import re
import time
import hmac
import base64
import hashlib
import urllib.parse

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://www.fortunechina.com"

# --- 创建带 UA 的 session，稍微友好一点 ---
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
})


# ================== 钉钉推送相关 ==================

def _sign_webhook(base: str, secret: str) -> str:
    """
    给单个 webhook 加签，返回完整请求 URL
    """
    if not base:
        return ""
    if not secret:
        return base

    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    sign = urllib.parse.quote_plus(
        base64.b64encode(
            hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
        )
    )
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"


def send_dingtalk_markdown(title: str, md: str) -> bool:
    """
    同一条 markdown 消息推送到多个钉钉群。

    环境变量：
      DINGTALK_BASES   = "url1,url2"
      DINGTALK_SECRETS = "sec1,sec2"

      或单个：
      DINGTALK_BASE
      DINGTALK_SECRET
    """
    bases_str = os.getenv("DINGTALK_BASES") or os.getenv("DINGTALK_BASE") or ""
    secrets_str = os.getenv("DINGTALK_SECRETS") or os.getenv("DINGTALK_SECRET") or ""

    bases = [b.strip() for b in bases_str.split(",") if b.strip()]
    secrets = [s.strip() for s in secrets_str.split(",") if s.strip()]

    if not bases:
        print("🔕 未配置 DINGTALK_BASES/DINGTALK_BASE，跳过推送。")
        return False

    # 只配置了一个 secret，但有多个 webhook：复用这一个
    if len(secrets) == 1 and len(bases) > 1:
        secrets = secrets * len(bases)

    # 长度不一致时，用空字符串补齐（表示不加签）
    if secrets and len(secrets) != len(bases):
        print("⚠️ DINGTALK_BASES 与 DINGTALK_SECRETS 数量不一致，缺失的将不加签。")
        while len(secrets) < len(bases):
            secrets.append("")

    ok_any = False
    for i, base in enumerate(bases):
        secret = secrets[i] if i < len(secrets) else ""
        full_url = _sign_webhook(base, secret)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": md
            }
        }

        try:
            resp = requests.post(full_url, json=payload, timeout=20)
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            ok = (resp.status_code == 200 and data.get("errcode") == 0)
            print(f"[DingTalk #{i+1}] push={ok} code={resp.status_code}")
            if not ok:
                print("  resp:", resp.text[:300])
            ok_any = ok_any or ok
        except Exception as e:
            print(f"[DingTalk #{i+1}] error:", e)

    return ok_any


# ================== 爬虫核心逻辑 ==================

def fetch_list(page: int = 1):
    """
    抓取商业频道某一页的文章列表（标题、链接、日期）

    返回：list[dict]，每个元素：
    {
        "title": 标题,
        "url": 详情链接,
        "date": 日期字符串（可能为空）
    }
    """
    if page == 1:
        url = f"{BASE}/shangye/"
    else:
        # “更多文章”后的分页 URL 规律
        url = f"{BASE}/shangye/node_12143_{page}.htm"

    print(f"\n=== 抓取列表页：第 {page} 页 ===")
    print("URL:", url)

    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # 列表页中，每篇文章一般在 <h2><a href="...">标题</a></h2>
    for h2 in soup.find_all("h2"):
        a = h2.find("a", href=True)
        if not a:
            continue

        href = a["href"]
        # 只保留真正的商业频道文章链接
        if "/shangye/c/" not in href:
            continue

        title = a.get_text(strip=True)
        full_url = urljoin(BASE, href)

        # 尝试在所在块中抓日期（YYYY-MM-DD）
        block_text = " ".join(h2.parent.get_text(" ", strip=True).split())
        m = re.search(r"\d{4}-\d{2}-\d{2}", block_text)
        pub_date = m.group(0) if m else ""

        items.append({
            "title": title,
            "url": full_url,
            "date": pub_date,
        })

    print(f"本页抓到 {len(items)} 篇文章")
    return items


def fetch_detail(url: str) -> dict:
    """
    抓一篇文章详情：标题 + 日期 + 正文（纯文本）

    返回：
    {
        "title": 标题,
        "date": 日期（可能为空）,
        "content": 正文纯文本（多段用换行拼接）
    }
    """
    print("  -> 抓取详情页：", url)
    r = session.get(url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # 标题：一般在 <h1> 或 <h2>
    h1 = soup.find(["h1", "h2"])
    title = h1.get_text(strip=True) if h1 else ""

    # 主内容块：简单用 class 名匹配 content/article 之类
    main = soup.find("div", class_=re.compile("content|article", re.I)) or soup

    paras = [p.get_text(strip=True) for p in main.find_all("p")]
    content = "\n".join(p for p in paras if p)

    # 页面中糊一遍找日期
    all_text = soup.get_text(" ", strip=True)
    m = re.search(r"\d{4}-\d{2}-\d2", all_text)
    pub_date = m.group(0) if m else ""

    return {
        "title": title,
        "date": pub_date,
        "content": content,
    }


def crawl_pages(max_page: int = 1, with_detail: bool = False):
    """
    一次性抓多页商业频道文章列表，必要时顺便抓正文

    :param max_page: 抓取的列表页数量（从第 1 页开始）
    :param with_detail: 是否同时抓正文
    :return: list[dict]
             每个元素：
             {
                 "title": ...,
                 "url": ...,
                 "date": ...,
                 "content": ... (如果 with_detail=True 才有)
             }
    """
    all_items = []

    for page in range(1, max_page + 1):
        items = fetch_list(page)
        for it in items:
            if with_detail:
                # 抓正文内容
                detail = fetch_detail(it["url"])
                it["date"] = it["date"] or detail["date"]
                it["content"] = detail["content"]
                # 防止频率太高，可以适当 sleep 一下
                time.sleep(1)

            all_items.append(it)

    return all_items


# ================== Markdown 构造 ==================

def build_markdown(articles, max_items: int = 10) -> str:
    """
    把文章列表转成适合钉钉的 Markdown 文本
    """
    out = []
    out.append("**财富中文网 · 商业频道 · 每日精选**  ")
    out.append("")
    if not articles:
        out.append("> 今日未抓到商业频道文章。")
        return "\n".join(out)

    for idx, art in enumerate(articles[:max_items], 1):
        title = art.get("title", "")
        date_str = art.get("date", "")
        line = f"{idx}. **{title}**"
        if date_str:
            line += f"（{date_str}）"
        out.append(line + "  ")
        out.append(f"> {art.get('url', '')}  ")
        out.append("")

    return "\n".join(out)


# ================== 主入口 ==================

if __name__ == "__main__":
    print("执行 fortune_cn_crawler.py（商业频道列表抓取 + 钉钉推送）")

    # 抓取 1 页列表；想多一点可以改成 max_page=2/3...
    articles = crawl_pages(max_page=1, with_detail=False)

    print("\n=== 控制台预览（前 5 条） ===")
    for art in articles[:5]:
        print(f"{art['date']} | {art['title']}")
        print(f"  {art['url']}")

    md = build_markdown(articles, max_items=10)

    print("\n===== Markdown Preview =====\n")
    print(md)

    # 推送到钉钉（需要提前配置环境变量）
    send_dingtalk_markdown("财富商业 · 每日精选", md)
