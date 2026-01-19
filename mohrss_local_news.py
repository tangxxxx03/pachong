# -*- coding: utf-8 -*-
"""
人社部-新闻中心-地方动态：按日期抓取并用钉钉机器人推送（完整代码）

规则：
- 周一：抓上周五
- 周二~周五：抓前一天
- 周六/周日：不抓（可自行改）

钉钉（可选）：
- 自定义机器人 + 加签
- 环境变量（建议 GitHub Secrets）：
  - DINGTALK_BASE   例：https://oapi.dingtalk.com/robot/send?access_token=xxxxx
  - DINGTALK_SECRET 机器人加签 secret

其他可选环境变量：
  - HR_TZ           默认 Asia/Shanghai
  - LIST_URL        覆盖列表页地址
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
DEFAULT_LIST_URL = "https://www.mohrss.gov.cn/SYrlzyhshbzb/rdzt/gzdt/"


def _tz():
    return ZoneInfo(os.getenv("HR_TZ", "Asia/Shanghai"))


def now_tz():
    return datetime.now(_tz())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def zh_weekday(dt: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]


def compute_target_date(now: datetime) -> str | None:
    """
    - 周一：抓上周五（-3天）
    - 周二~周五：抓昨天（-1天）
    - 周六/周日：None（不抓）
    """
    wd = now.weekday()
    if wd == 0:
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if 1 <= wd <= 4:
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
    解析列表页：title + url + date(YYYY-MM-DD)
    你的截图里日期是 span.organMenuTxtLink，标题是 a 标签
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 方案1：按日期 span 定位
    date_spans = soup.select("span.organMenuTxtLink")
    for sp in date_spans:
        date_text = norm(sp.get_text())
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            continue

        container = sp
        for _ in range(6):
            if container is None:
                break
            a = container.find("a", href=True)
            if a and norm(a.get_text()):
                title = norm(a.get_text())
                href = a["href"].strip()
                full_url = urljoin(page_url, href)
                items.append({"date": date_text, "title": title, "url": full_url})
                break
            container = container.parent

    # 兜底：抓所有 a 并在父容器找日期
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
            for _ in range(6):
                if parent is None:
                    break
                txt = norm(parent.get_text(" "))
                m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", txt)
                if m:
                    found_date = m.group(1)
                    break
                parent = parent.parent

            if found_date:
                items.append({"date": found_date, "title": title, "url": urljoin(page_url, href)})

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


def dingtalk_signed_url(base_url: str, secret: str) -> str:
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(h))
    joiner = "&" if "?" in base_url else "?"
    return f"{base_url}{joiner}timestamp={timestamp}&sign={sign}"


def dingtalk_send_markdown(title: str, markdown: str):
    base = os.getenv("DINGTALK_BASE", "").strip()
    secret = os.getenv("DINGTALK_SECRET", "").strip()

    # ✅ 改动点：没配置钉钉就跳过，不让 workflow 失败
    if not base or not secret:
        print("[WARN] 未配置 DINGTALK_BASE / DINGTALK_SECRET，跳过钉钉推送。")
        return {"skipped": True}

    url = dingtalk_signed_url(base, secret)

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown
        }
    }

    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"钉钉发送失败：{data}")
    return data


def build_markdown(list_url: str, target_date: str, items: list[dict], now: datetime) -> tuple[str, str]:
    title = f"📰 人社部·地方动态（{target_date}）"

    head = [
        f"### 📰 人社部·地方动态（目标日：**{target_date}**）",
        f"- 抓取时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{zh_weekday(now)}）",
        f"- 列表页：{list_url}",
        ""
    ]

    if not items:
        body = [
            "本次未匹配到目标日期的条目。",
            "",
            "> 可能原因：当天未发布 / 页面延迟更新 / 列表页结构变动。",
        ]
        return title, "\n".join(head + body)

    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['title']}]({it['url']})  `({it['date']})`")

    tail = ["", f"—— 共 **{len(items)}** 条"]
    return title, "\n".join(head + lines + tail)


def main():
    list_url = os.getenv("LIST_URL", DEFAULT_LIST_URL).strip()
    now = now_tz()
    target = compute_target_date(now)

    if not target:
        print("今天是周末（或未安排抓取日），按规则不抓取，也不推送。")
        return

    print(f"[INFO] 目标日期：{target}")

    html = fetch_html(list_url)
    items = parse_list(html, list_url)
    hit = [x for x in items if x.get("date") == target]

    print(f"[INFO] 解析 {len(items)} 条，命中 {len(hit)} 条。")

    out = {
        "source": "mohrss_local_news",
        "list_url": list_url,
        "target_date": target,
        "count": len(hit),
        "items": hit,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path = f"mohrss_local_news_{target}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 已写出：{out_path}")

    title, md = build_markdown(list_url, target, hit, now)
    resp = dingtalk_send_markdown(title, md)
    print(f"[INFO] 钉钉返回：{resp}")


if __name__ == "__main__":
    main()
