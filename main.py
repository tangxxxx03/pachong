# -*- coding: utf-8 -*-
"""
京津冀（北京/河北/天津）- 招标公告 & 中标结果 抓取器（进入详情页补全字段）

站点（以静态为主）：
- 北京公共资源交易（ggzyfw.beijing.gov.cn）
- 北京站“京津冀协同发展”汇聚页（来源含“河北省/天津市公共资源交易服务平台”）

字段：
- 标题、地区、类别（公告/中标）、发布日期、截止/开标、金额（万元）、联系人、联系电话、摘要、来源、URL

输出：
- CSV/JSON：pachong_jjj_YYYYMMDD_HHMMSS.(csv|json)
- 钉钉 Markdown 推送（DINGTALK_WEBHOOK / 可选 DINGTALK_SECRET / 可选 DINGTALK_KEYWORD）

环境变量（可选）：
- TARGET_DATE：仅抓此日期（默认今天，系统时区，格式 YYYY-MM-DD）
- MAX_ITEMS：最多抓多少条（默认 30）
- DINGTALK_WEBHOOK：钉钉机器人 webhook（必填才能发送）
- DINGTALK_SECRET：钉钉加签密钥（可空，若机器人未开加签请留空）
- DINGTALK_KEYWORD：命中关键字才发（可空）
- DRY_RUN=1：只抓取不发送（调试用）
- CHUNK_SIZE：每条钉钉消息最大字符数（默认 3500；钉钉上限≈4000，预留冗余）
"""

import os, re, csv, json, time, base64, hmac, hashlib, urllib.parse
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple
import requests
from bs4 import BeautifulSoup

# ----------------- 基本配置 -----------------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
TIMEOUT = 20
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})
RETRY = 2

TARGET_DATE = os.getenv("TARGET_DATE") or date.today().strftime("%Y-%m-%d")
MAX_ITEMS = int(os.getenv("MAX_ITEMS") or "30")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE") or "3500")  # 钉钉 markdown 字数安全分片阈值

# 入口栏目
BEIJING_LISTS = [
    "https://ggzyfw.beijing.gov.cn/jyxxcggg/index.html",   # 北京 采购公告
    "https://ggzyfw.beijing.gov.cn/jyxxzbjggg/index.html", # 北京 中标/成交公告
]
JJJ_LISTS = [
    "https://ggzyfw.beijing.gov.cn/xtgghbcggg/index.html", # 河北 采购公告（汇聚）
    "https://ggzyfw.beijing.gov.cn/xtgghbcjjg/index.html", # 河北 成交结果（汇聚）
    "https://ggzyfw.beijing.gov.cn/xtggtjcggg/index.html", # 天津 采购公告（汇聚）
    "https://ggzyfw.beijing.gov.cn/xtggtjcjjg/index.html", # 天津 成交结果（汇聚）
]

# ----------------- 工具函数 -----------------
def http_get(url: str) -> Optional[requests.Response]:
    for i in range(RETRY + 1):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r
        except Exception as e:
            if i == RETRY:
                print(f"[HTTP] {url} -> {e}")
        time.sleep(0.6)
    return None

DATE_PAT = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")
PHONE_PAT = re.compile(r"(?:(?:\+?86[-\s]?)?(?:\d{3,4}[-\s]?)?\d{7,8})|(?:1[3-9]\d{9})")
MONEY_PAT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万元|万|元|人民币|RMB)?")

def norm_date(s: str) -> Optional[str]:
    m = DATE_PAT.search(s or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except Exception:
        return None

def first_text(soup: BeautifulSoup, selectors: List[str]) -> str:
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag:
            t = tag.get_text(" ", strip=True)
            if t:
                return t
    return ""

def extract_phone(text: str) -> Optional[str]:
    m = PHONE_PAT.search(text or "")
    return m.group(0) if m else None

def extract_money(text: str) -> Tuple[Optional[float], Optional[str]]:
    best = None
    for m in MONEY_PAT.finditer(text or ""):
        val = float(m.group(1))
        unit = (m.group(2) or "").strip()
        if unit in ("万元", "万"):
            wan = val
        else:
            wan = val / 10000.0
        if (best is None) or (wan > best[0]):
            best = (wan, m.group(0))
    return best if best else (None, None)

def clean(s: str) -> str:
    return (s or "").replace("\xa0", " ").strip()

def join_url(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    from urllib.parse import urljoin
    return urljoin(base, href)

# ----------------- 钉钉推送 -----------------
def _sign_webhook(base_webhook: str, secret: str) -> str:
    """如果 secret 为空表示未开加签，直接返回 base_webhook。"""
    if not secret:
        return base_webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def _chunk_text(md: str, limit: int) -> List[str]:
    """钉钉 markdown 限长，分片发送。尽量按段落分割。"""
    if len(md) <= limit:
        return [md]
    parts, cur = [], []
    cur_len = 0
    for line in md.splitlines():
        line_len = len(line) + 1
        if cur_len + line_len > limit and cur:
            parts.append("\n".join(cur))
            cur = [line]
            cur_len = line_len
        else:
            cur.append(line)
            cur_len += line_len
    if cur:
        parts.append("\n".join(cur))
    return parts

def send_dingtalk_md(title: str, md: str) -> bool:
    """发送钉钉 markdown。未配置 webhook 或 DRY_RUN=1 则不发。"""
    if os.getenv("DRY_RUN") == "1":
        print("[DRY_RUN] skip send")
        return True

    hook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    if not hook:
        print("No DINGTALK_WEBHOOK, skip send.")
        return False

    secret = os.getenv("DINGTALK_SECRET", "").strip()  # 未开加签可留空
    keyword = os.getenv("DINGTALK_KEYWORD", "").strip()

    # 如果设置了关键字，标题+正文都不包含则跳过
    if keyword and (keyword not in title and keyword not in md):
        print(f"[DingTalk] keyword '{keyword}' not matched, skip.")
        return True

    url = _sign_webhook(hook, secret)
    ok_all = True
    for idx, chunk in enumerate(_chunk_text(md, CHUNK_SIZE), 1):
        t = title if idx == 1 else f"{title}（{idx}）"
        try:
            r = requests.post(
                url,
                json={"msgtype": "markdown", "markdown": {"title": t, "text": chunk}},
                timeout=20,
            )
            print("DingTalk resp:", r.status_code, r.text[:300])
            ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
            ok_all = ok_all and ok
        except Exception as e:
            print("DingTalk error:", e)
            ok_all = False
    return ok_all

# ----------------- 详情页解析 -----------------
def parse_detail_generic(html: str) -> Dict[str, Optional[str]]:
    soup = BeautifulSoup(html, "lxml")
    whole = soup.get_text(" ", strip=True)

    title = first_text(soup, ["h1", "h2", "div.title h1", ".article-title", ".news-title", ".article h1"]) or ""

    # 摘要：选正文首段长度合适的
    summary = ""
    for sel in ["article p", "div.article p", ".TRS_Editor p", "#zoom p", ".content p", ".txt p", "p"]:
        ps = soup.select(sel)
        for p in ps:
            t = p.get_text(" ", strip=True)
            if 20 <= len(t) <= 180:
                summary = t
                break
        if summary:
            break

    # 联系人 / 电话
    contact = None
    for key in ["联系人", "联 系 人", "项目联系人"]:
        m = re.search(key + r"[:：]\s*([^\s，,。；;|]+)", whole)
        if m:
            contact = m.group(1)
            break
    phone = extract_phone(whole)

    # 金额（取最大值作为项目预算/成交额近似）
    amount_wan, _raw = extract_money(whole)

    # 截止/开标
    deadline = None
    for key in ["投标截止", "递交截止", "截止时间", "响应文件提交截止时间"]:
        m = re.search(key + r"[:：]?\s*([^\n。；;，,]+)", whole)
        if m:
            deadline = clean(m.group(1))
            break
    open_time = None
    for key in ["开标时间", "开标日期"]:
        m = re.search(key + r"[:：]?\s*([^\n。；;，,]+)", whole)
        if m:
            open_time = clean(m.group(1))
            break
    open_place = None
    for key in ["开标地点", "开标地址"]:
        m = re.search(key + r"[:：]?\s*([^\n。；;]+)", whole)
        if m:
            open_place = clean(m.group(1))
            break

    return {
        "title": clean(title),
        "summary": clean(summary),
        "contact": clean(contact or ""),
        "phone": clean(phone or ""),
        "amount_wan": f"{amount_wan:.4f}" if amount_wan else "",
        "deadline": clean(deadline or ""),
        "open_time": clean(open_time or ""),
        "open_place": clean(open_place or ""),
    }

# ----------------- 列表页解析 -----------------
def parse_list_beijing(list_url: str, category: str) -> List[Dict]:
    out = []
    r = http_get(list_url)
    if not r:
        return out
    soup = BeautifulSoup(r.text, "lxml")

    items = []
    for sel in ["ul li a", "div.list li a", ".news-list li a", "a"]:
        items = soup.select(sel)
        if items:
            break

    for a in items[:80]:
        title = clean(a.get_text(strip=True))
        href = a.get("href", "").strip()
        if not title or not href:
            continue
        link = join_url(list_url, href)

        # 日期：尽量找同级/父级文本
        line = a.parent.get_text(" ", strip=True) if a.parent else title
        d = norm_date(line) or norm_date(r.text)
        if not d or d != TARGET_DATE:
            continue

        rr = http_get(link)
        if not rr:
            continue
        det = parse_detail_generic(rr.text)

        region = "北京"
        source = "北京市政府采购监管系统" if ("zbjggg" in list_url or "cggg" in list_url) else "北京公共资源交易网"

        out.append({
            "title": det["title"] or title,
            "region": region,
            "category": "中标结果" if ("zbjggg" in list_url or "cjjg" in list_url) else "招标公告",
            "publish_date": d,
            "deadline": det["deadline"],
            "open_time": det["open_time"],
            "open_place": det["open_place"],
            "amount_wan": det["amount_wan"],
            "contact": det["contact"],
            "phone": det["phone"],
            "summary": det["summary"],
            "source": source,
            "url": link,
        })
        if len(out) >= MAX_ITEMS:
            break
    return out

def parse_list_hub(list_url: str, region_hint: str) -> List[Dict]:
    out = []
    r = http_get(list_url)
    if not r:
        return out
    soup = BeautifulSoup(r.text, "lxml")

    items = []
    for sel in ["ul li a", ".news-list li a", "a"]:
        items = soup.select(sel)
        if items:
            break

    for a in items[:120]:
        title = clean(a.get_text(strip=True))
        href = a.get("href", "").strip()
        if not title or not href:
            continue
        link = join_url(list_url, href)

        line = a.parent.get_text(" ", strip=True) if a.parent else title
        d = norm_date(line) or norm_date(r.text)
        if not d or d != TARGET_DATE:
            continue

        rr = http_get(link)
        if not rr:
            continue
        det = parse_detail_generic(rr.text)

        source = "河北省公共资源交易服务平台" if "hbc" in list_url else "天津市公共资源交易服务平台"
        if "信息来源" in line:
            m = re.search(r"信息来源[:：]\s*([^\s/|]+)", line)
            if m:
                source = clean(m.group(1))

        out.append({
            "title": det["title"] or title,
            "region": region_hint,
            "category": "中标结果" if ("cjjg" in list_url) else "招标公告",
            "publish_date": d,
            "deadline": det["deadline"],
            "open_time": det["open_time"],
            "open_place": det["open_place"],
            "amount_wan": det["amount_wan"],
            "contact": det["contact"],
            "phone": det["phone"],
            "summary": det["summary"],
            "source": source,
            "url": link,
        })
        if len(out) >= MAX_ITEMS:
            break
    return out

# ----------------- 汇总/输出/格式化 -----------------
def dedup(rows: List[Dict]) -> List[Dict]:
    seen, out = set(), []
    for r in rows:
        key = (r["title"], r["url"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

def to_csv_json(rows: List[Dict]) -> Tuple[str, str]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csvf = f"pachong_jjj_{ts}.csv"
    jsonf = f"pachong_jjj_{ts}.json"
    with open(csvf, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "title",
                "region",
                "category",
                "publish_date",
                "deadline",
                "open_time",
                "open_place",
                "amount_wan",
                "contact",
                "phone",
                "summary",
                "source",
                "url",
            ],
        )
        w.writeheader()
        w.writerows(rows)
    with open(jsonf, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("Saved:", csvf, jsonf)
    return csvf, jsonf

def _mk_block(title: str, arr: List[Dict]) -> str:
    if not arr:
        return ""
    lines = [f"**{title}｜京津冀** {TARGET_DATE} 共 {len(arr)} 条"]
    for i, it in enumerate(arr, 1):
        money = f"{it['amount_wan']} 万元" if it["amount_wan"] else "暂无"
        lines.append(f"{i}. [{it['title']}]({it['url']})")
        lines.append(f"> 地区：{it['region']} | 发布：{it['publish_date']} | 金额：{money}")
        if it["deadline"] or it["open_time"]:
            lines.append(f"> 截止/开标：{it['deadline'] or '暂无'} / {it['open_time'] or '暂无'}")
        if it["contact"] or it["phone"]:
            lines.append(f"> 联系：{it['contact'] or '暂无'} / {it['phone'] or '暂无'}")
        lines.append(f"> 来源：{it['source']}")
    return "\n".join(lines)

def to_markdown(rows: List[Dict]) -> str:
    if not rows:
        return f"**招采播报｜京津冀** {TARGET_DATE}\n\n今日未抓到目标日期的招采信息。"
    groups = {"招标公告": [], "中标结果": []}
    for r in rows:
        groups.get(r["category"], groups["招标公告"]).append(r)
    parts = [_mk_block("招标公告", groups["招标公告"]), _mk_block("中标结果", groups["中标结果"])]
    md = "\n\n".join([p for p in parts if p])
    md += "\n\n> 汇总时间：自动抓取发送"
    return md

# ----------------- 主逻辑 -----------------
def main():
    print(f"目标日期：{TARGET_DATE}（只抓这一天）")
    rows: List[Dict] = []

    # 北京
    for url in BEIJING_LISTS:
        cat = "中标结果" if "zbjggg" in url else "招标公告"
        rows += parse_list_beijing(url, cat)

    # 河北、天津（北京站“协同发展”栏目）
    rows += parse_list_hub(JJJ_LISTS[0], "河北")
    rows += parse_list_hub(JJJ_LISTS[1], "河北")
    rows += parse_list_hub(JJJ_LISTS[2], "天津")
    rows += parse_list_hub(JJJ_LISTS[3], "天津")

    rows = dedup(rows)
    rows.sort(key=lambda x: (x["category"], x["region"], x["amount_wan"] or 0), reverse=True)

    print(f"抓到 {len(rows)} 条。")
    to_csv_json(rows)

    md = to_markdown(rows)
    title = f"【京津冀招采】{TARGET_DATE}"
    send_dingtalk_md(title, md)

if __name__ == "__main__":
    main()
