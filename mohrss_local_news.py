# -*- coding: utf-8 -*-
"""
人社部 - 新闻中心 - 地方动态
按工作日规则抓取 + 钉钉实验群推送（完整代码）

规则：
- 周一：抓上周五
- 周二~周五：抓前一天
- 周六/周日：不抓

钉钉（实验群）环境变量：
- SHIYANQUNWEBHOOK  钉钉机器人 webhook（含 access_token）
- SHIYANQUNSECRET   钉钉机器人加签 secret

可选：
- HR_TZ   默认 Asia/Shanghai
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

# ✅ 改动点：默认改成“新闻中心-地方动态”栏目目录（与你截图一致）
DEFAULT_LIST_URL = "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/dfdt/"

RE_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


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


def fetch_html(url: str) -> str:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    r = s.get(url, timeout=25)
    r.raise_for_status()
    return r.text


def parse_list(html: str, page_url: str) -> list[dict]:
    """
    鲁棒解析：不依赖固定 class
    思路：
    - 在页面里找所有出现 YYYY-MM-DD 的节点
    - 往上找父容器（最多 8 层），在容器内找 <a href> 当标题链接
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 1) 找到所有“含日期文本”的节点
    date_nodes = soup.find_all(string=lambda s: bool(s and RE_DATE.search(str(s))))
    for node in date_nodes:
        date_text = RE_DATE.search(str(node)).group(1)

        container = node.parent
        for _ in range(8):
            if not container:
                break
            a = container.find("a", href=True)
            if a and norm(a.get_text()):
                href = a["href"].strip()
                # 只要像文章页（t2024...html / .html），就收
                if ".html" in href:
                    items.append({
                        "date": date_text,
                        "title": norm(a.get_text()),
                        "url": urljoin(page_url, href)
                    })
                    break
            container = container.parent

    # 2) 兜底：如果上面仍然抓不到，直接扫所有 a，在父容器文本里找日期
    if not items:
        for a in soup.find_all("a", href=True):
            title = norm(a.get_text())
            if not title:
                continue
            href = a["href"].strip()
            if ".html" not in href:
                continue

            parent = a
            found_date = None
            for _ in range(8):
                if not parent:
                    break
                txt = norm(parent.get_text(" "))
                m = RE_DATE.search(txt)
                if m:
                    found_date = m.group(1)
                    break
                parent = parent.parent

            if found_date:
                items.append({
                    "date": found_date,
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


def build_markdown(list_url: str, target_date: str, items: list[dict], now: datetime):
    title = f"📰 人社部·地方动态（{target_date}）"
    head = [
        f"### 📰 人社部·地方动态（目标日：**{target_date}**）",
        f"- 抓取时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{zh_weekday(now)}）",
        f"- 列表页：{list_url}",
        ""
    ]
    if not items:
        return title, "\n".join(head + ["本次未匹配到目标日期的内容。"])

    body = [f"{i}. [{it['title']}]({it['url']})  `({it['date']})`" for i, it in enumerate(items, 1)]
    return title, "\n".join(head + body + ["", f"—— 共 **{len(items)}** 条"])


def main():
    list_url = os.getenv("LIST_URL", DEFAULT_LIST_URL).strip()
    now = now_tz()
    target = compute_target_date(now)

    if not target:
        print("周末，不执行。")
        return

    print(f"[INFO] 目标日期：{target}")
    html = fetch_html(list_url)
    items = parse_list(html, list_url)
    hit = [x for x in items if x["date"] == target]

    print(f"[INFO] 解析 {len(items)} 条，命中 {len(hit)} 条。")

    out_path = f"mohrss_local_news_{target}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"target_date": target, "list_url": list_url, "items": hit}, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 已写出：{out_path}")

    title, md = build_markdown(list_url, target, hit, now)
    resp = send_to_shiyanqun(title, md)
    print(f"[INFO] 钉钉返回：{resp}")


if __name__ == "__main__":
    main()
