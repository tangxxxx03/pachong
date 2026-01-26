# -*- coding: utf-8 -*-
"""
北京市人民政府 - 要闻动态（近7天，按周抓取）
目标页：https://www.beijing.gov.cn/ynwdt/yaowen/index.html

功能：
1) 抓取列表页标题、日期、详情链接
2) 只保留最近 7 天（含今天）
3) 钉钉 Markdown 推送（每条后面都有 👉 [详情](url) 蓝字可点）
4) 失败重试 + 兼容相对链接 + 去重

环境变量（建议放 GitHub Actions secrets / vars）：
- DINGTALK_WEBHOOK  : 钉钉机器人 webhook（完整URL）
- DINGTALK_SECRET   : 钉钉加签 secret（如果你机器人开启了加签就必须填）
可选：
- HR_TZ             : 默认 Asia/Shanghai
- MAX_ITEMS         : 默认 50
- OUT               : 输出到本地 markdown 文件（比如 OUT=weekly_beijing_yaowen.md），不填则不写文件
"""

import os
import re
import hmac
import time
import json
import base64
import hashlib
import random
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


# -------------------------
# 基础配置
# -------------------------
BASE_URL = "https://www.beijing.gov.cn"
LIST_URL = "https://www.beijing.gov.cn/ynwdt/yaowen/index.html"

TZ_NAME = os.getenv("HR_TZ", "Asia/Shanghai")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "50"))

DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "").strip()
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "").strip()

OUT = os.getenv("OUT", "").strip()


# -------------------------
# 工具函数
# -------------------------
def tz():
    return ZoneInfo(TZ_NAME)


def now_tz():
    return datetime.now(tz())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def parse_yyyy_mm_dd(s: str):
    s = norm(s)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except Exception:
        return None


def build_session():
    sess = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    return sess


def ding_sign(secret: str, timestamp_ms: str) -> str:
    """
    钉钉加签：sign = base64( HMAC_SHA256(timestamp+'\n'+secret, secret) )
    """
    string_to_sign = f"{timestamp_ms}\n{secret}".encode("utf-8")
    h = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def dingtalk_send_markdown(title: str, md: str):
    if not DINGTALK_WEBHOOK:
        print("[WARN] 未配置 DINGTALK_WEBHOOK，跳过推送。以下为输出内容：\n")
        print(md)
        return

    url = DINGTALK_WEBHOOK
    params = {}

    if DINGTALK_SECRET:
        ts = str(int(time.time() * 1000))
        params["timestamp"] = ts
        params["sign"] = ding_sign(DINGTALK_SECRET, ts)

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": md,
        },
    }

    sess = build_session()
    resp = sess.post(url, params=params, json=payload, timeout=20)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    if resp.status_code != 200 or (isinstance(data, dict) and data.get("errcode", 0) != 0):
        raise RuntimeError(f"钉钉推送失败：HTTP {resp.status_code} / {data}")
    print("[OK] 钉钉推送成功。")


# -------------------------
# 抓取逻辑
# -------------------------
def fetch_yaowen_last_7_days():
    sess = build_session()

    # 轻微随机延迟，礼貌一点
    time.sleep(0.4 + random.random() * 0.6)

    r = sess.get(LIST_URL, timeout=20)
    r.encoding = "utf-8"
    if r.status_code != 200:
        raise RuntimeError(f"列表页访问失败：HTTP {r.status_code}")

    soup = BeautifulSoup(r.text, "html.parser")

    items = []

    # 常见结构：div.listBox ul.list li -> a + span(日期)
    # 你截图里就是这种：a 后面跟 span 2026-01-26
    for a in soup.select("div.listBox ul.list li a[href]"):
        title = norm(a.get_text())
        href = norm(a.get("href", ""))
        if not title or not href:
            continue

        url = href if href.startswith("http") else urljoin(BASE_URL, href)

        # 日期一般在 a 后面的 span；不行就从 li 里找
        li = a.find_parent("li")
        date_text = ""
        if li:
            # 找到 li 内第一个 span
            sp = li.find("span")
            if sp:
                date_text = norm(sp.get_text())

        d = parse_yyyy_mm_dd(date_text)
        if d is None:
            # 兜底：有些站点会把日期写在文本里
            d = parse_yyyy_mm_dd(li.get_text(" ", strip=True) if li else "")

        items.append({"title": title, "url": url, "date": d, "date_text": date_text})

    # 去重（按 url）
    dedup = []
    seen = set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        dedup.append(it)

    # 过滤最近7天（含今天）
    today = now_tz().date()
    start = today - timedelta(days=6)

    filtered = []
    for it in dedup:
        if it["date"] is None:
            continue
        if start <= it["date"] <= today:
            filtered.append(it)

    # 日期倒序（最新在前）
    filtered.sort(key=lambda x: x["date"], reverse=True)

    return filtered[:MAX_ITEMS], start, today


def render_markdown(items, start, today):
    title = f"北京市政府要闻（近7天：{start} ~ {today}）"

    if not items:
        md = f"### {title}\n\n近7天没有抓到新条目（或页面结构变动）。"
        return title, md

    lines = [f"### {title}", ""]
    for i, it in enumerate(items, 1):
        # 标题不做整段链接，减少“花眼”
        # 每条后面给一个 详情 蓝字可点
        d = it["date"].strftime("%Y-%m-%d") if it["date"] else it["date_text"]
        lines.append(f"{i}. {it['title']}（{d}） 👉 [详情]({it['url']})")
    md = "\n".join(lines)
    return title, md


def main():
    items, start, today = fetch_yaowen_last_7_days()
    title, md = render_markdown(items, start, today)

    if OUT:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[OK] 已写入：{OUT}")

    dingtalk_send_markdown(title, md)


if __name__ == "__main__":
    main()
