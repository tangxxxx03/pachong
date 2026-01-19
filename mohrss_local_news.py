# -*- coding: utf-8 -*-
"""
人社部 - 新闻中心 - 地方动态
按工作日规则抓取 + 钉钉实验群推送（增强版完整代码）

规则：
- 周一：抓上周五
- 周二~周五：抓前一天
- 周六/周日：不抓

钉钉（实验群）环境变量：
- SHIYANQUNWEBHOOK  钉钉机器人 webhook（含 access_token）
- SHIYANQUNSECRET   钉钉机器人加签 secret

可选：
- HR_TZ    默认 Asia/Shanghai
- LIST_URL 覆盖列表页地址
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"

# 你浏览器里看的“地方动态”目录
DEFAULT_LIST_URL = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/"

RE_DATE_DASH = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
RE_DATE_CN = re.compile(r"\b(20\d{2})年(\d{1,2})月(\d{1,2})日\b")


def _tz():
    return ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai"))


def now_tz():
    return datetime.now(_tz())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def zh_weekday(dt: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]


def compute_target_date(now: datetime) -> str | None:
    wd = now.weekday()
    if wd == 0:  # 周一 -> 上周五
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if 1 <= wd <= 4:  # 周二~周五 -> 昨天
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def normalize_date_text(text: str) -> str | None:
    """
    支持：
    - 2026-01-16
    - 2026年1月16日 / 2026年01月16日
    """
    if not text:
        return None
    s = norm(text)

    m1 = RE_DATE_DASH.search(s)
    if m1:
        return m1.group(1)

    m2 = RE_DATE_CN.search(s)
    if m2:
        y = m2.group(1)
        mo = int(m2.group(2))
        d = int(m2.group(3))
        return f"{y}-{mo:02d}-{d:02d}"

    return None


def fetch_html(url: str) -> str:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Referer": "https://www.mohrss.gov.cn/",
    })
    r = s.get(url, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r.text


def extract_iframe_src(html: str, page_url: str) -> str | None:
    """
    如果列表在 iframe 内页，这里把 iframe 的 src 抠出来
    """
    soup = BeautifulSoup(html, "html.parser")
    iframe = soup.find("iframe", src=True)
    if iframe and iframe.get("src"):
        return urljoin(page_url, iframe["src"].strip())
    return None


def parse_list_from_html(html: str, page_url: str) -> list[dict]:
    """
    鲁棒解析：不依赖固定 class
    - 找所有出现日期的节点
    - 往上找父容器，容器内找 a[href] 标题链接
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 找所有文本节点，试图提取日期
    for node in soup.find_all(string=True):
        dt = normalize_date_text(str(node))
        if not dt:
            continue

        container = node.parent
        for _ in range(10):
            if not container:
                break
            a = container.find("a", href=True)
            if a and norm(a.get_text()):
                href = a["href"].strip()
                if ".html" in href:
                    items.append({
                        "date": dt,
                        "title": norm(a.get_text()),
                        "url": urljoin(page_url, href)
                    })
                    break
            container = container.parent

    # 兜底：扫所有 a，在父容器里找日期
    if not items:
        for a in soup.find_all("a", href=True):
            title = norm(a.get_text())
            if not title:
                continue
            href = a["href"].strip()
            if ".html" not in href:
                continue

            parent = a
            found = None
            for _ in range(10):
                if not parent:
                    break
                found = normalize_date_text(parent.get_text(" "))
                if found:
                    break
                parent = parent.parent

            if found:
                items.append({
                    "date": found,
                    "title": title,
                    "url": urljoin(page_url, href)
                })

    # 去重
    seen = set()
    uniq = []
    for it in items:
        key = (it["date"], it["title"], it["url"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    uniq.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    return uniq


def parse_list(url: str) -> tuple[list[dict], dict]:
    """
    先抓 url 本身解析；
    如果解析不到条目：
      - 尝试抓 iframe src 的页面再解析
    """
    debug = {"used_url": url, "iframe_url": None}

    html = fetch_html(url)
    items = parse_list_from_html(html, url)

    if items:
        return items, debug

    iframe_url = extract_iframe_src(html, url)
    if iframe_url:
        debug["iframe_url"] = iframe_url
        html2 = fetch_html(iframe_url)
        items2 = parse_list_from_html(html2, iframe_url)
        if items2:
            debug["used_url"] = iframe_url
            return items2, debug

    return items, debug


def signed_dingtalk_url(webhook: str, secret: str) -> str:
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(h))
    joiner = "&" if "?" in webhook else "?"
    return f"{webhook}{joiner}timestamp={timestamp}&sign={sign}"


def send_to_shiyanqun(title: str, markdown: str):
    webhook = os.getenv("SHIYANQUNWEBHOOK", "").strip()
    secret = os.getenv("SHIYANQUNSECRET", "").strip()

    if not webhook or not secret:
        print("[WARN] 未配置 SHIYANQUNWEBHOOK / SHIYANQUNSECRET，跳过钉钉推送。")
        return {"skipped": True}

    url = signed_dingtalk_url(webhook, secret)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}

    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") not in (0, None):
        raise RuntimeError(f"钉钉发送失败：{data}")
    return data


def build_markdown(list_url: str, target_date: str, items: list[dict], hit: list[dict], now: datetime, debug: dict):
    title = f"人社部·地方动态（目标日：{target_date}）"

    head = [
        f"### 人社部·地方动态（目标日：**{target_date}**）",
        f"- 抓取时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{zh_weekday(now)}）",
        f"- 列表页：{list_url}",
        f"- 实际解析来源：{debug.get('used_url')}",
    ]
    if debug.get("iframe_url"):
        head.append(f"- 发现 iframe：{debug.get('iframe_url')}")
    head.append("")

    if hit:
        body = [f"{i}. [{it['title']}]({it['url']})  `({it['date']})`" for i, it in enumerate(hit, 1)]
        tail = ["", f"—— 共 **{len(hit)}** 条"]
        return title, "\n".join(head + body + tail)

    # 命中 0：把解析到的前几条“日期+标题”附上，方便你立刻定位
    preview = items[:8]
    if preview:
        pv_lines = [f"- `{it['date']}` {it['title']}" for it in preview]
        extra = ["本次未匹配到目标日期的内容。", "", "解析到的前几条是："] + pv_lines
    else:
        extra = ["本次未匹配到目标日期的内容。", "", "> 并且解析结果为 0 条（很可能是页面壳/iframe/环境返回差异导致）。"]

    return title, "\n".join(head + extra)


def main():
    list_url = os.getenv("LIST_URL", DEFAULT_LIST_URL).strip()
    now = now_tz()
    target = compute_target_date(now)

    if not target:
        print("周末，不执行。")
        return

    print(f"[INFO] 目标日期：{target}")

    items, debug = parse_list(list_url)
    hit = [x for x in items if x["date"] == target]

    print(f"[INFO] 解析 {len(items)} 条，命中 {len(hit)} 条。")

    out_path = f"mohrss_local_news_{target}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"target_date": target, "list_url": list_url, "debug": debug, "items": hit}, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 已写出：{out_path}")

    title, md = build_markdown(list_url, target, items, hit, now, debug)
    resp = send_to_shiyanqun(title, md)
    print(f"[INFO] 钉钉返回：{resp}")


if __name__ == "__main__":
    main()
