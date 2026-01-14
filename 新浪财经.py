# -*- coding: utf-8 -*-
"""
新浪财经 - 上市公司研究院
抓取【前一天】新闻标题 + 链接，并通过【钉钉机器人】自动推送到群里（Markdown）

页面：https://finance.sina.com.cn/roll/c/221431.shtml

修复点：
- 强去重：按 link 去重 + (title, time) 兜底
- 选“真实正文链接”：优先 .shtml 或 /doc- 这类详情页
- 防止同页重复 a 导致刷屏
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
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


START_URL = "https://finance.sina.com.cn/roll/c/221431.shtml"
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "0.8"))
OUT_FILE = os.getenv("OUT_FILE", "sina_yesterday.md")

TZ = ZoneInfo("Asia/Shanghai")
DATE_RE = re.compile(r"\((\d{2})月(\d{2})日\s*(\d{2}):(\d{2})\)")


def now_cn():
    return datetime.now(TZ)


def get_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def parse_datetime(text: str):
    m = DATE_RE.search(text)
    if not m:
        return None
    month, day, hh, mm = map(int, m.groups())
    now = now_cn()
    year = now.year
    if now.month == 1 and month == 12:
        year -= 1
    try:
        return datetime(year, month, day, hh, mm, tzinfo=TZ)
    except Exception:
        return None


def find_next_page(soup: BeautifulSoup):
    a = soup.find("a", string=lambda s: s and "下一页" in s)
    if a and a.get("href"):
        return urljoin(START_URL, a["href"])
    return None


# ===== 钉钉 =====
def extract_access_token(token_or_webhook: str) -> str:
    s = (token_or_webhook or "").strip()
    if not s:
        return ""
    if "access_token=" in s:
        try:
            if s.startswith("http"):
                u = urllib.parse.urlparse(s)
                q = urllib.parse.parse_qs(u.query)
                return (q.get("access_token") or [""])[0].strip()
            part = s.split("access_token=", 1)[1]
            return part.split("&", 1)[0].strip()
        except Exception:
            return ""
    return s


def dingtalk_signed_url(access_token: str, secret: str) -> str:
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}&sign={sign}"


def dingtalk_send_markdown(title: str, markdown_text: str) -> dict:
    raw = (os.getenv("DINGTALK_TOKEN") or "").strip()
    secret = (os.getenv("DINGTALK_SECRET") or "").strip()
    access_token = extract_access_token(raw)

    if not access_token:
        raise RuntimeError("缺少 DINGTALK_TOKEN（可填整条 webhook 或 access_token）")
    if not secret:
        raise RuntimeError("缺少 DINGTALK_SECRET（请确认机器人已开启“加签”并填入 secret）")
    if len(access_token) < 10:
        raise RuntimeError(f"DINGTALK_TOKEN 解析后太短，疑似配置错误（len={len(access_token)}）")

    url = dingtalk_signed_url(access_token, secret)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown_text}}

    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if str(data.get("errcode")) != "0":
        raise RuntimeError(f"钉钉发送失败：{data}")
    return data


# ===== 关键：从一个 li 里选“真实正文链接” =====
def pick_best_link(li: BeautifulSoup):
    """
    li 里可能有多个 <a>，我们挑最像正文页的：
    1) 优先 href 包含 '.shtml' 或 '/doc-' 或 '/article/'
    2) 再退回第一个 href
    """
    links = []
    for a in li.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href:
            continue
        abs_url = urljoin(START_URL, href)
        links.append((abs_url, text))

    if not links:
        return None, None

    def score(u: str):
        s = 0
        if ".shtml" in u:
            s += 10
        if "/doc-" in u:
            s += 8
        if "/article/" in u:
            s += 6
        if "finance.sina.com.cn" in u:
            s += 2
        return s

    links.sort(key=lambda x: score(x[0]), reverse=True)
    return links[0][0], links[0][1]


def build_markdown(yesterday_date, results):
    lines = [f"### 📰 新浪财经 · 昨日更新（{yesterday_date}）\n"]
    if not results:
        lines.append("（昨日无更新或页面结构变化）")
    else:
        for dt, title, link in results:
            lines.append(f"- [{title}]({link})  `{dt.strftime('%H:%M')}`")
    lines.append(f"\n> 生成时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）")
    return "\n".join(lines)


def main():
    yesterday = (now_cn() - timedelta(days=1)).date()

    # 强去重容器
    seen_link = set()
    seen_title_time = set()

    results = []
    url = START_URL
    hit_yesterday = False

    for _ in range(1, MAX_PAGES + 1):
        html = get_html(url)
        soup = BeautifulSoup(html, "html.parser")

        container = soup.select_one("div.listBlk")
        if not container:
            print("❌ 未找到 listBlk 容器，页面结构可能变化")
            break

        lis = container.find_all("li")
        if not lis:
            print("❌ listBlk 下未找到 li")
            break

        page_links = []

        for li in lis:
            text_all = li.get_text(" ", strip=True)
            dt = parse_datetime(text_all)
            if not dt:
                continue

            # 只要昨天
            if dt.date() != yesterday:
                continue

            link, _anchor_text = pick_best_link(li)
            if not link:
                continue

            # 标题：优先用 li 里第一个 a 的文字；如果为空，用 anchor_text
            a0 = li.find("a")
            title = (a0.get_text(strip=True) if a0 else "") or (_anchor_text or "")
            title = title.strip()
            if not title:
                continue

            # 去重 key
            k_link = link
            k_tt = (title, dt.strftime("%Y-%m-%d %H:%M"))

            # 强去重：先按 link
            if k_link in seen_link:
                continue
            # 兜底去重：同标题同时间
            if k_tt in seen_title_time:
                continue

            seen_link.add(k_link)
            seen_title_time.add(k_tt)

            results.append((dt, title, link))
            page_links.append(link)
            hit_yesterday = True

        # 如果这一页抓到的 link 全一样，直接提示（避免刷屏）
        if page_links and len(set(page_links)) == 1 and len(page_links) >= 2:
            print("⚠️ 警告：本页抓到的链接全部相同，已通过去重过滤；建议后续继续观察页面结构。")

        # 早停：已经命中昨天，并且本页所有时间都 < yesterday -> 停
        if hit_yesterday:
            dts = [parse_datetime(li.get_text(" ", strip=True)) for li in lis]
            dts = [d for d in dts if d]
            if dts and all(d.date() < yesterday for d in dts):
                break

        next_url = find_next_page(soup)
        if not next_url:
            break
        url = next_url
        time.sleep(SLEEP_SEC)

    # 倒序排列
    results.sort(key=lambda x: x[0], reverse=True)

    md = build_markdown(yesterday, results)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(md + "\n")

    print(f"✅ 抓取完成（去重后），共 {len(results)} 条，已写入 {OUT_FILE}")

    title = f"新浪财经昨日更新 {yesterday}"
    resp = dingtalk_send_markdown(title=title, markdown_text=md)
    print(f"✅ 钉钉推送成功：{resp}")


if __name__ == "__main__":
    main()
