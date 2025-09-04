# -*- coding: utf-8 -*-
"""
HR 资讯自动抓取（工作日 8:30 推送到“HR 机器人”）
依赖：requests, beautifulsoup4, lxml
Secrets（HR 专用）:
- DINGTALK_WEBHOOKHR（必填：HR 群机器人 webhook）
- DINGTALK_SECRET_HR（必填：若开启“加签”，就是 SEC... 那串）
- DINGTALK_KEYWORD_HR（可选：若开启“关键字”）
"""
import os, re, time, csv, json, hmac, base64, hashlib, urllib.parse
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

# ========== 钉钉推送（HR 专用，带加签） ==========
def _sign_webhook(base_webhook: str, secret: str) -> str:
    """按钉钉规则生成签名并拼接到 webhook 上"""
    if not (secret and base_webhook):
        return base_webhook
    ts = str(round(time.time()*1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def send_to_dingtalk_markdown_hr(title: str, md_text: str) -> bool:
    """
    使用 HR 专用 Secrets 发送 Markdown 到钉钉。
    必填：DINGTALK_WEBHOOKHR, DINGTALK_SECRET_HR（加签）
    可选：DINGTALK_KEYWORD_HR（关键字）
    """
    base = (os.getenv("DINGTALK_WEBHOOKHR") or "").strip()
    secret = (os.getenv("DINGTALK_SECRET_HR") or "").strip()
    if not base:
        print("❌ 未设置 DINGTALK_WEBHOOKHR"); return False
    if not secret:
        print("❌ 未设置 DINGTALK_SECRET_HR（加签密钥）"); return False

    kw = (os.getenv("DINGTALK_KEYWORD_HR") or "").strip()
    webhook = _sign_webhook(base, secret)
    if kw and (kw not in title and kw not in md_text):
        title = f"{kw} | {title}"

    payload = {"msgtype":"markdown","markdown":{"title":title,"text":md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        print("HR DingTalk resp:", r.status_code, r.text[:300])
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
        return ok
    except Exception as e:
        print("❌ HR 钉钉异常：", e)
        return False

# ========== 爬虫主体（示例源，后续你可替换为真实源） ==========
class HRNewsCrawler:
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
        }
        self.results = []

    def get_recent_hr_news(self):
        """抓近两个月 HR 资讯；多源容错"""
        print("开始抓取 HR 资讯…")
        for fn in (self.crawl_beijing_hrss, self.crawl_mock_mohrss, self.crawl_mock_portals):
            try:
                fn()
                time.sleep(1.0)
            except Exception as e:
                print(f"[WARN] 来源异常：{fn.__name__} -> {e}")
        return self.results

    # —— 北京人社局（示例解析，尽量兼容不同列表结构） ——
    def crawl_beijing_hrss(self):
        print("抓取：北京人社局…")
        base = "https://rsj.beijing.gov.cn"
        paths = ["/xxgk/tzgg/", "/xxgk/gzdt/", "/xxgk/zcfg/"]
        selectors = [".list li", ".news-list li", ".content-list li", "ul li"]
        for p in paths:
            url = base + p
            try:
                r = self.session.get(url, headers=self.headers, timeout=15)
                r.encoding = "utf-8"
                if r.status_code != 200: 
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                items = []
                for sel in selectors:
                    items = soup.select(sel)
                    if items: break
                for li in items[:12]:
                    self._extract_li(li, base, "北京人社局")
            except Exception as e:
                print(f"[WARN] 北京人社局 {p} 解析失败：{e}")

    # —— 人社部 / 门户：先用示例数据占位，稳定云端流程；后续可替换为真实接口 ——
    def crawl_mock_mohrss(self):
        print("抓取：人社部（示例）…")
        mock = [
            {"title":"人社部发布最新就业促进措施",
             "url":"https://www.mohrss.gov.cn/example/1.html","source":"人社部",
             "date":(datetime.now()-timedelta(days=10)).strftime('%Y-%m-%d'),
             "content":"加大职业技能培训与创业扶持。"}
        ]
        self._take_if_recent(mock)

    def crawl_mock_portals(self):
        print("抓取：HR 门户（示例）…")
        mock = [
            {"title":"2025Q3人力资源市场供需报告","url":"https://portal.example/1",
             "source":"中国人力资源网","date":(datetime.now()-timedelta(days=20)).strftime('%Y-%m-%d'),
             "content":"IT、新能源需求旺盛。"},
            {"title":"灵活用工合规要点解读","url":"https://portal.example/2",
             "source":"前程无忧","date":(datetime.now()-timedelta(days=35)).strftime('%Y-%m-%d'),
             "content":"平台用工/外包用工注意事项。"}
        ]
        self._take_if_recent(mock)

    # —— 工具 —— 
    def _take_if_recent(self, items, days=60):
        for n in items:
            if self._is_recent(n.get("date",""), days):
                self.results.append(n)

    def _extract_li(self, li, base_url, source):
        a = li.find("a")
        if not a: return
        title = a.get_text(strip=True)
        href = a.get("href","").strip()
        if not href: return
        full = href if href.startswith("http") else (base_url + href if href.startswith("/") else "")
        if not full: return
        text = li.get_text(" ", strip=True)
        m = re.search(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", text)
        date_str = (m.group(0).replace('/', '-') if m else (datetime.now()-timedelta(days=30)).strftime('%Y-%m-%d'))
        if self._is_recent(date_str):
            self.results.append({
                "title": title, "url": full, "source": source,
                "date": date_str, "content": text[:120] + ("..." if len(text) > 120 else "")
            })

    def _is_recent(self, date_str, days=60):
        for fmt in ("%Y-%m-%d","%Y/%m/%d","%y-%m-%d"):
            try:
                d = datetime.strptime(date_str, fmt)
                return (datetime.now() - d).days <= days
            except: 
                continue
        return True  # 解析失败不过滤，保证不中断

    def save_results(self, fmt="csv"):
        if not self.results:
            print("暂无结果，不保存"); return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt.lower() == "csv":
            fn = f"hr_news_{ts}.csv"
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["title","url","source","date","content"])
                w.writeheader(); w.writerows(self.results)
            print("已保存：", fn); return fn
        else:
            fn = f"hr_news_{ts}.json"
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            print("已保存：", fn); return fn

    def display_results(self):
        if not self.results:
            print("没有抓到近两个月的 HR 资讯"); return
        print(f"\n共 {len(self.results)} 条：\n" + "-"*80)
        for i, it in enumerate(self.results[:20], 1):
            print(f"{i}. {it['title']} | {it['source']} | {it['date']}")
            print(f"   {it['url']}")
        print("-"*80)

# ========== 主流程 ==========
def main():
    print("HR 资讯自动抓取 |", datetime.now().isoformat(timespec="seconds"))
    crawler = HRNewsCrawler()
    crawler.get_recent_hr_news()
    crawler.display_results()

    saved = crawler.save_results("csv") if crawler.results else None

    # 组织推送（前 8 条）
    if crawler.results:
        topk = min(8, len(crawler.results))
        lines = [f"### HR资讯播报（{datetime.now().strftime('%Y-%m-%d')}）\n共 {len(crawler.results)} 条，Top {topk} 如下：\n"]
        for i, it in enumerate(crawler.results[:topk], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})\n> 来源：{it['source']} | 日期：{it['date']}")
        if saved:
            lines.append(f"\n> 已保存：{saved}（可在 Actions 里下载）")
        ok = send_to_dingtalk_markdown_hr("HR资讯播报", "\n".join(lines))
        print("HR 钉钉推送：", "成功" if ok else "失败")
    else:
        send_to_dingtalk_markdown_hr("HR资讯播报", "今日暂无近两个月内的人力资源资讯。")

    print("✅ HR 任务完成。")

if __name__ == "__main__":
    main()
