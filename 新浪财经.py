# -*- coding: utf-8 -*-

import os
import re
import time
import ssl
import hmac
import base64
import hashlib
import urllib.parse
import requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from datetime import datetime, timedelta, date
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

def now_cn():
    return datetime.now(TZ)

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())

# ================= 钉钉 =================
def extract_access_token(s):
    if not s:
        return ""
    if "access_token=" in s:
        u = urllib.parse.urlparse(s)
        q = urllib.parse.parse_qs(u.query)
        return (q.get("access_token") or [""])[0]
    return s

def dingtalk_url(token, secret):
    ts = str(int(time.time() * 1000))
    sign_str = f"{ts}\n{secret}"
    sign = urllib.parse.quote_plus(
        base64.b64encode(
            hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()
        )
    )
    return f"https://oapi.dingtalk.com/robot/send?access_token={token}&timestamp={ts}&sign={sign}"

def send_dingtalk(title, md):
    token = extract_access_token(os.getenv("DINGTALK_TOKEN"))
    secret = os.getenv("DINGTALK_SECRET")
    url = dingtalk_url(token, secret)
    r = requests.post(url, json={
        "msgtype": "markdown",
        "markdown": {"title": title, "text": md}
    }, timeout=20)
    r.raise_for_status()

# ================= 人力资讯 =================
def crawl_hr():
    return [
        "携程深夜误发全员离职通知",
        "前程无忧：2025年离职率降至14.8%",
        "花旗：本周裁员约1000人",
        "Meta：计划裁员虚拟现实部门10%",
        "贝莱德：裁员数百人"
    ]

# ================= 企业新闻（示意，保留你现有逻辑即可） =================
def crawl_sina():
    return [
        ("臻驿科技港股IPO：认定无控股股东是否合规避税？", "https://finance.sina.com.cn")
    ]

# ================= 主体 =================
def main():
    today = now_cn().strftime("%m-%d")
    title = f"📌 {today} 每日早报"

    hr_items = crawl_hr()
    sina_items = crawl_sina()

    md = []
    md.append(f"## {title}\n")

    md.append("## 👥 人力资讯")
    for i, t in enumerate(hr_items, 1):
        md.append(f"{i}. {t}")

    md.append("\n---\n")
    md.append("## 🏢 企业新闻")
    for t, link in sina_items:
        md.append(f"- [{t}]({link})")

    final_md = "\n".join(md)

    send_dingtalk(title, final_md)

if __name__ == "__main__":
    main()
