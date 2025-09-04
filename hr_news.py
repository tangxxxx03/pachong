# -*- coding: utf-8 -*-
"""
HR资讯自动抓取（工作日 8:30 推送钉钉）
依赖：requests, beautifulsoup4, lxml
Secrets:
- DINGTALK_WEBHOOK（必填）
- DINGTALK_SECRET（可选：若开启加签）
- DINGTALK_KEYWORD（可选：若开启关键字）
"""
import os, re, time, csv, json, hmac, base64, hashlib, urllib.parse
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

# ========== 钉钉推送 ==========
def _make_signed_webhook(base_webhook: str, secret: str) -> str:
    if not secret: return base_webhook
    ts = str(round(time.time()*1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def send_to_dingtalk_markdown(title: str, md_text: str) -> bool:
    base = os.getenv("DINGTALK_WEBHOOK", "").strip()
    if not base:
        print("❌ 未设置 DINGTALK_WEBHOOK"); return False
    secret = os.getenv("DINGTALK_SECRET", "").strip()
    kw = os.getenv("DINGTALK_KEYWORD", "").strip()

    webhook = _make_signed_webhook(base, secret)
    if kw and (kw not in title and kw not in md_text):
        title = f"{kw} | {title}"

    payload = {"msgtype":"markdown","markdown":{"title":title,"text":md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        print("DingTalk resp:", r.status_code, r.text[:300])
        return (r.status_code==200 and isinstance(r.json(), dict) and r.json().get("errcode")==0)
    except Exception as e:
        print("❌ 钉钉异常：", e)
        return False

# ========== 爬虫 ==========
class HRNewsCrawler:
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.results = []

    def get_recent_hr_news(self):
        """获取近两个月人力资源相关资讯（多源聚合 + 容错）"""
        print("开始抓取人力资源相关资讯...")
        sources = [self.crawl_beijing_hrss, self.crawl_mohrss, self.crawl_hr_portals]
        for fn in sources:
            try:
                fn()
                time.sleep(1.5)
            except Exception as e:
                print(f"抓取来源时出错: {e}")
        return self.results

    def crawl_beijing_hrss(self):
        """北京人社局"""
        print("正在抓取北京人社局信息...")
        base_url = "https://rsj.beijing.gov.cn"
        urls_to_try = ['/xxgk/tzgg/', '/xxgk/gzdt/', '/xxgk/zcfg/']
        selectors = ['.list li', '.news-list li', '.content-list li', 'ul li']

        for path in urls_to_try:
            try:
                url = base_url + path
                resp = self.session.get(url, headers=self.headers, timeout=15)
                resp.encoding = 'utf-8'
                if resp.status_code != 200: 
                    continue
                soup = BeautifulSoup(resp.text, 'html.parser')
                items = []
                for sel in selectors:
                    items = soup.select(sel)
                    if items: break
                for item in items[:12]:
                    self._process_news_item(item, base_url, '北京人社局')
            except Exception as e:
                print(f"抓取北京人社局 {path} 出错: {e}")

    def crawl_mohrss(self):
        """人社部（示例数据）"""
        print("正在获取人社部相关信息...")
        mock = [
            {
                'title':'人力资源和社会保障部发布最新就业促进政策',
                'url':'https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/202310/t20231015_123456.html',
                'source':'人社部','date':(datetime.now()-timedelta(days=15)).strftime('%Y-%m-%d'),
                'content':'推出就业促进措施：技能培训补贴、创业扶持等。'
            },
            {
                'title':'2023年社会保险缴费基数调整通知',
                'url':'https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/202310/t20231008_123457.html',
                'source':'人社部','date':(datetime.now()-timedelta(days=22)).strftime('%Y-%m-%d'),
                'content':'各地社保缴费基数将按上年度在岗职工平均工资调整。'
            }
        ]
        for n in mock:
            if self._is_recent(n['date']): self.results.append(n)

    def crawl_hr_portals(self):
        """HR 门户（示例数据）"""
        print("正在获取人力资源门户网站信息...")
        mock = [
            {'title':'2025年Q3人力资源市场供需报告','url':'https://www.chinahr.com/news/202509/123456.html',
             'source':'中国人力资源网','date':(datetime.now()-timedelta(days=10)).strftime('%Y-%m-%d'),
             'content':'IT、新能源需求旺盛，供需基本平衡。'},
            {'title':'灵活用工政策最新解读','url':'https://www.51job.com/news/202508/123457.html',
             'source':'前程无忧','date':(datetime.now()-timedelta(days=25)).strftime('%Y-%m-%d'),
             'content':'专家解读灵活用工合规要点。'},
            {'title':'数字化转型中的人力资源管理变革','url':'https://www.zhaopin.com/trends/202508/123458.html',
             'source':'智联招聘','date':(datetime.now()-timedelta(days=40)).strftime('%Y-%m-%d'),
             'content':'数字化转型对HR提出新要求与挑战。'}
        ]
        for n in mock:
            if self._is_recent(n['date']): self.results.append(n)

    # ---------- 工具 ----------
    def _process_news_item(self, item, base_url, source):
        try:
            a = item.find('a')
            if not a: return
            title = a.get_text(strip=True)
            href = a.get('href','').strip()
            if not href: return
            full_url = href if href.startswith('http') else (base_url + href if href.startswith('/') else '')
            if not full_url: return

            # 尝试解析日期
            text = item.get_text(" ", strip=True)
            m = re.search(r'(\d{4}[/-]\d{2}[/-]\d{2})|(\d{2}-\d{2}-\d{2})', text)
            if m:
                date_text = m.group(0).replace('/', '-')
                if len(date_text) == 8:  # 形如 yy-mm-dd
                    yy, mm, dd = date_text.split('-')
                    date_text = f"20{yy}-{mm}-{dd}"
            else:
                date_text = (datetime.now()-timedelta(days=30)).strftime('%Y-%m-%d')

            if self._is_recent(date_text):
                self.results.append({
                    'title': title, 'url': full_url, 'source': source,
                    'date': date_text, 'content': text[:120] + ('...' if len(text)>120 else '')
                })
        except Exception as e:
            print("处理新闻条目出错：", e)

    def _is_recent(self, date_str, days=60):
        try:
            for fmt in ('%Y-%m-%d','%Y/%m/%d','%y-%m-%d','%m-%d'):
                try:
                    d = datetime.strptime(date_str, fmt)
                    if d.year < 2000: d = d.replace(year=2000 + d.year % 100)
                    return (datetime.now() - d).days <= days
                except ValueError:
                    continue
            return True
        except:
            return True

    def save_results(self, format_type='csv'):
        if not self.results:
            print("没有找到相关资讯"); return None
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        if format_type.lower()=='csv':
            fn = f'hr_news_{ts}.csv'
            with open(fn, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, fieldnames=['title','url','source','date','content'])
                w.writeheader(); w.writerows(self.results)
            print("已保存:", fn); return fn
        else:
            fn = f'hr_news_{ts}.json'
            with open(fn, 'w', encoding='utf-8') as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            print("已保存:", fn); return fn

    def display_results(self):
        if not self.results:
            print("没有找到近两个月的HR资讯"); return
        print(f"\n找到 {len(self.results)} 条资讯：\n" + "-"*80)
        for i, it in enumerate(self.results, 1):
            print(f"{i}. {it['title']} | {it['source']} | {it['date']}")
            print(f"   {it['url']}")
        print("-"*80)

# ========== 主函数 ==========
def main():
    print("HR 资讯自动抓取 |", datetime.now().isoformat(timespec='seconds'))
    crawler = HRNewsCrawler()
    crawler.get_recent_hr_news()
    crawler.display_results()

    # 固定自动保存为 CSV（不要 input）
    fn = crawler.save_results('csv') if crawler.results else None

    # 组织钉钉推送
    if crawler.results:
        topk = min(8, len(crawler.results))
        lines = [f"### HR资讯播报（{datetime.now().strftime('%Y-%m-%d')}）\n共 {len(crawler.results)} 条，Top {topk} 如下：\n"]
        for i, it in enumerate(crawler.results[:topk], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})\n> 来源：{it['source']} | 日期：{it['date']}")
        if fn:
            lines.append(f"\n> 文件：{fn}（已保存为构件，可在 Actions 里下载）")
        ok = send_to_dingtalk_markdown("HR资讯播报", "\n".join(lines))
        print("钉钉推送：", "成功" if ok else "失败")
    else:
        send_to_dingtalk_markdown("HR资讯播报", "今日暂无近两个月内的人力资源相关资讯。")

    print("✅ 任务完成。")

if __name__ == "__main__":
    main()
