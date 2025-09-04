# -*- coding: utf-8 -*-
"""
HR 资讯自动抓取（工作日 8:30 推送）
来源：国务院、人社部、北京市人社局
过滤：仅近3天 + 关键词（可通过环境变量 KEYWORDS_HR 自定义，逗号分隔）
推送：钉钉自定义机器人（开启加签）
依赖：requests, beautifulsoup4, lxml

需要的 Secrets：
- DINGTALK_WEBHOOKHR（必填：HR 群机器人 webhook）
- DINGTALK_SECRET_HR（必填：HR 机器人加签密钥 SEC...）
- DINGTALK_KEYWORD_HR（可选：若启用“关键词”安全策略，就填你的关键字）
"""

import os, re, time, hmac, base64, hashlib, urllib.parse, json, csv
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup

# ========== 配置 ==========
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}
TIMEOUT = 20
RECENT_DAYS = 3

DEFAULT_KEYWORDS = [
    "人社","人力资源","就业","社保","养老","医保","工伤","工资","薪酬",
    "用工","劳动","人才","培训","技能","稳岗","就业服务","招聘","招聘会",
]
KEYWORDS = [k.strip() for k in (os.getenv("KEYWORDS_HR") or "").split(",") if k.strip()] or DEFAULT_KEYWORDS

# ========== 钉钉（加签） ==========
def _sign_webhook(base_webhook: str, secret: str) -> str:
    ts = str(round(time.time()*1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def send_to_dingtalk_markdown_hr(title: str, md_text: str) -> bool:
    base = (os.getenv("DINGTALK_WEBHOOKHR") or "").strip()
    secret = (os.getenv("DINGTALK_SECRET_HR") or "").strip()
    if not base or not secret:
        print("❌ 缺少 DINGTALK_WEBHOOKHR 或 DINGTALK_SECRET_HR"); return False
    kw = (os.getenv("DINGTALK_KEYWORD_HR") or "").strip()

    webhook = _sign_webhook(base, secret)
    if kw and (kw not in title and kw not in md_text):
        title = f"{kw} | {title}"

    payload = {"msgtype":"markdown","markdown":{"title":title,"text":md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=TIMEOUT)
        print("HR DingTalk resp:", r.status_code, r.text[:300])
        return (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
    except Exception as e:
        print("❌ 钉钉异常：", e)
        return False

# ========== 工具 ==========
DATE_PAT = re.compile(r"(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})")

def parse_date(text: str) -> Optional[str]:
    m = DATE_PAT.search(text or "")
    if not m: return None
    y, mo, d = map(int, m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except:
        return None

def is_recent(datestr: str, days:int=RECENT_DAYS) -> bool:
    try:
        d = datetime.strptime(datestr, "%Y-%m-%d")
        return (datetime.now() - d).days <= days
    except:
        return False

def match_keywords(title: str, content: str = "") -> bool:
    text = f"{title} {content}".lower()
    return any(k.lower() in text for k in KEYWORDS)

def http_get(url: str) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            r.encoding = r.apparent_encoding or "utf-8"
            return r
    except Exception as e:
        print(f"[HTTP] {url} -> {e}")
    return None

def extract_first_paragraph(url: str) -> str:
    r = http_get(url)
    if not r: return ""
    soup = BeautifulSoup(r.text, "lxml")
    for sel in ["article p","div.article p","div.TRS_Editor p","div#zoom p","div.article-con p","div.txt p","div.content p","p"]:
        ps = soup.select(sel)
        if ps:
            for p in ps:
                t = p.get_text(" ", strip=True)
                if len(t) >= 24:
                    return t[:120] + ("…" if len(t)>120 else "")
    return ""

# ========== 抓取：国务院 / 人社部 / 北京市人社局 ==========
def crawl_gov_cn() -> List[Dict]:
    results = []
    pages = [
        "https://www.gov.cn/yaowen/index.htm",
        "https://www.gov.cn/zhengce/zuixin.htm",
        "https://www.gov.cn/zhengce/index.htm",
    ]
    for url in pages:
        r = http_get(url)
        if not r: continue
        soup = BeautifulSoup(r.text, "lxml")
        items = []
        for sel in ["ul li a","div.list li a","div.biglist li a","div.news-list li a","a"]:
            items = soup.select(sel)
            if items: break
        for a in items[:25]:
            title = a.get_text(strip=True)
            href  = a.get("href","").strip()
            if not title or not href: continue
            link = href if href.startswith("http") else urllib.parse.urljoin(url, href)
            li_text = a.parent.get_text(" ", strip=True) if a.parent else title
            d = parse_date(li_text) or parse_date(r.text)
            if not d or not is_recent(d): continue
            summary = extract_first_paragraph(link)
            if not match_keywords(title, summary): continue
            results.append({"title": title, "url": link, "date": d, "source": "国务院", "summary": summary})
    return results

def crawl_mohrss() -> List[Dict]:
    results = []
    pages = [
        "https://www.mohrss.gov.cn/SYrlzyhshbzb/rsxw/",
        "https://www.mohrss.gov.cn/SYrlzyhshbzb/jiuye/",
        "https://www.mohrss.gov.cn/SYrlzyhshbzb/zcwj/",
    ]
    for url in pages:
        r = http_get(url)
        if not r: continue
        soup = BeautifulSoup(r.text, "lxml")
        items = []
        for sel in ["ul li a","div.list li a","div.news-list li a","a"]:
            items = soup.select(sel)
            if items: break
        for a in items[:25]:
            title = a.get_text(strip=True)
            href  = a.get("href","").strip()
            if not title or not href: continue
            link = href if href.startswith("http") else urllib.parse.urljoin(url, href)
            li_text = a.parent.get_text(" ", strip=True) if a.parent else title
            d = parse_date(li_text) or parse_date(r.text)
            if not d or not is_recent(d): continue
            summary = extract_first_paragraph(link)
            if not match_keywords(title, summary): continue
            results.append({"title": title, "url": link, "date": d, "source": "人社部", "summary": summary})
    return results

def crawl_bj_hrss() -> List[Dict]:
    results = []
    base = "https://rsj.beijing.gov.cn"
    pages = ["/xxgk/tzgg/","/xxgk/gzdt/"]
    for p in pages:
        url = base + p
        r = http_get(url)
        if not r: continue
        soup = BeautifulSoup(r.text, "lxml")
        items = []
        for sel in [".list li a",".news-list li a","ul li a","a"]:
            items = soup.select(sel)
            if items: break
        for a in items[:25]:
            title = a.get_text(strip=True)
            href  = a.get("href","").strip()
            if not title or not href: continue
            link = href if href.startswith("http") else urllib.parse.urljoin(url, href)
            li_text = a.parent.get_text(" ", strip=True) if a.parent else title
            d = parse_date(li_text) or parse_date(r.text)
            if not d or not is_recent(d): continue
            summary = extract_first_paragraph(link)
            if not match_keywords(title, summary): continue
            results.append({"title": title, "url": link, "date": d, "source": "北京人社局", "summary": summary})
    return results

# ========== 汇总 & 推送 ==========
def dedup(items: List[Dict]) -> List[Dict]:
    seen, out = set(), []
    for it in items:
        key = (it["title"], it["url"])
        if key in seen: continue
        seen.add(key); out.append(it)
    return out

def save_results(items: List[Dict]):
    if not items: return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # CSV
    with open(f"hr_news_{ts}.csv","w",newline="",encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["title","url","source","date","summary"])
        w.writeheader(); w.writerows(items)
    # JSON
    with open(f"hr_news_{ts}.json","w",encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

def format_markdown_daily(items: List[Dict]) -> str:
    now = datetime.now()
    head = [
        "### 🧩 人力资源资讯每日汇总",
        f"**汇总时间：{now.strftime('%Y年%m月%d日 %H:%M')}**",
        f"**今日资讯：{len(items)} 条人力资源相关资讯**",
        "",
        "🗞️ **资讯详情**",
    ]
    body = []
    for idx, it in enumerate(items, 1):
        body.append(f"{idx}. [{it['title']}]({it['url']})")
        if it.get("summary"): body.append(f"> {it['summary']}")
        body.append(f"> 📅 {it['date']}　|　🏛️ {it['source']}\n")
    tail = ["💡 早安！今日人力资源资讯已为您整理完毕"]
    return "\n".join(head + [""] + body + tail)

def main():
    print("HR 每日抓取开始：", datetime.now().isoformat(timespec="seconds"))
    all_items: List[Dict] = []
    try: all_items += crawl_gov_cn()
    except Exception as e: print("gov.cn 抓取异常：", e)
    try: all_items += crawl_mohrss()
    except Exception as e: print("mohrss 抓取异常：", e)
    try: all_items += crawl_bj_hrss()
    except Exception as e: print("北京人社局 抓取异常：", e)

    all_items = dedup(all_items)
    all_items.sort(key=lambda x: x["date"], reverse=True)

    if not all_items:
        send_to_dingtalk_markdown_hr("人力资源资讯每日汇总", "今天未抓到符合条件的人社/就业类资讯。")
        print("无结果，已空播报。"); return

    TOP = min(8, len(all_items))  # 想只发 2 条就改成 2
    chosen = all_items[:TOP]
    save_results(chosen)
    md = format_markdown_daily(chosen)
    ok = send_to_dingtalk_markdown_hr("人力资源资讯每日汇总", md)
    print("推送结果：", "成功" if ok else "失败")

if __name__ == "__main__":
    main()
