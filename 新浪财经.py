# -*- coding: utf-8 -*-
"""
新浪财经 - 上市公司研究院
抓取【前一天】新闻标题 + 链接，并通过【钉钉机器人】自动推送到群里（Markdown）

页面：https://finance.sina.com.cn/roll/c/221431.shtml

使用方式（GitHub Actions 推荐）：
- 在仓库 Secrets 里配置：
  - DINGTALK_TOKEN  = 机器人 access_token（webhook 里那个）
  - DINGTALK_SECRET = 机器人加签密钥（安全设置里“加签”）

本脚本会：
1) 抓取昨天标题+链接
2) 生成 Markdown
3) 推送到钉钉群
4) 同时写入本地文件 sina_yesterday.md（便于留档）
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


# ================= 配置 =================
START_URL = "https://finance.sina.com.cn/roll/c/221431.shtml"
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "0.8"))

OUT_FILE = os.getenv("OUT_FILE", "sina_yesterday.md")

TZ = ZoneInfo("Asia/Shanghai")
DATE_RE = re.compile(r"\((\d{2})月(\d{2})日\s*(\d{2}):(\d{2})\)")


# ================= 时间/解析 =================
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
    # 跨年兜底：1月抓到12月 -> 认为是去年
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


# ================= 钉钉推送（加签） =================
def dingtalk_signed_url(access_token: str, secret: str) -> str:
    """
    钉钉机器人“加签”URL生成
    """
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}&sign={sign}"


def dingtalk_send_markdown(title: str, markdown_text: str) -> dict:
    token = (os.getenv("DINGTALK_TOKEN") or "").strip()
    secret = (os.getenv("DINGTALK_SECRET") or "").strip()

    if not token or not secret:
        raise RuntimeError("缺少 DINGTALK_TOKEN 或 DINGTALK_SECRET（请在 GitHub Secrets 配置）")

    url = dingtalk_signed_url(token, secret)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text
        }
    }

    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    # 钉钉成功一般是 {"errcode":0,"errmsg":"ok"}
    if str(data.get("errcode")) != "0":
        raise RuntimeError(f"钉钉发送失败：{data}")
    return data


# ================= Markdown 生成 =================
def build_markdown(yesterday_date, results):
    """
    results: [(dt, title, link), ...]
    """
    header = f"### 📰 新浪财经 · 昨日更新（{yesterday_date}）\n"
    lines = [header]

    if not results:
        lines.append("（昨日无更新或页面结构变化）")
    else:
        for dt, title, link in results:
            # 钉钉 markdown 支持标准链接：[text](url)
            lines.append(f"- [{title}]({link})  `{dt.strftime('%H:%M')}`")

    lines.append(f"\n> 生成时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）")
    return "\n".join(lines)


# ================= 主流程 =================
def main():
    yesterday = (now_cn() - timedelta(days=1)).date()
    results = []

    url = START_URL
    hit_yesterday = False

    for page in range(1, MAX_PAGES + 1):
        html = get_html(url)
        soup = BeautifulSoup(html, "html.parser")

        # 稳态锚点：div.listBlk 下的 li
        container = soup.select_one("div.listBlk")
        if not container:
            print("❌ 未找到 listBlk 容器，页面结构可能变化")
            break

        lis = container.find_all("li")
        if not lis:
            print("❌ listBlk 下未找到 li，页面结构可能变化")
            break

        for li in lis:
            a = li.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            link = urljoin(START_URL, a["href"])
            text = li.get_text(" ", strip=True)

            dt = parse_datetime(text)
            if not dt:
                continue

            if dt.date() == yesterday:
                results.append((dt, title, link))
                hit_yesterday = True

        # 早停：已经抓到昨天，并且本页全是更早日期 -> 停止
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

    # 按时间倒序
    results.sort(key=lambda x: x[0], reverse=True)

    md = build_markdown(yesterday, results)

    # 写文件留档
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(md + "\n")

    print(f"✅ 抓取完成，共 {len(results)} 条，已写入 {OUT_FILE}")

    # 推送到钉钉
    title = f"新浪财经昨日更新 {yesterday}"
    resp = dingtalk_send_markdown(title=title, markdown_text=md)
    print(f"✅ 钉钉推送成功：{resp}")


if __name__ == "__main__":
    main()
