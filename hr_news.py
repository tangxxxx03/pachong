# -*- coding: utf-8 -*-
"""
HR 资讯自动抓取 + 钉钉推送（开启加签）
- 兼容本地与 GitHub Actions（无交互 input）
- 默认使用你提供的 HR 机器人 webhook/secret；若存在环境变量（GitHub Secrets）会自动覆盖
- 运行后：抓取 -> 打印 -> 保存 CSV/JSON -> 推送钉钉 Markdown
依赖：requests, beautifulsoup4
"""

import os
import re
import csv
import json
import time
import hmac
import base64
import hashlib
import urllib.parse
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# ====== 你的 HR 机器人（默认写死，Secrets 会自动覆盖它们）======
WEBHOOK_DEFAULT = "https://oapi.dingtalk.com/robot/send?access_token=9bb5d79464e0bf60f9c0f56ffd99744c4149fc43554982c0189ffe9c04162dce"
SECRET_DEFAULT  = "SEC4d9521a7cf6f96fcf6ea9832116df97b13300441f4e513f487a6502d833def75"

# ✅ 优先用环境变量（GitHub Secrets），否则退回默认值
WEBHOOK = os.getenv("DINGTALK_WEBHOOKHR", WEBHOOK_DEFAULT).strip()
SECRET  = os.getenv("DINGTALK_SECRET_HR",  SECRET_DEFAULT).strip()
KEYWORD = os.getenv("DINGTALK_KEYWORD_HR", "").strip()  # 若机器人启用“关键词”，可在 Secrets 里设置

# ====================== 钉钉发送（加签） ======================
def _sign_webhook(base_webhook: str, secret: str) -> str:
    if not secret:
        return base_webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def send_dingtalk_markdown(title: str, md_text: str) -> bool:
    if not WEBHOOK:
        print("❌ 缺少 WEBHOOK"); return False
    if not SECRET:
        print("❌ 缺少 SECRET（你的机器人开了“加签”就必须提供）"); return False

    webhook = _sign_webhook(WEBHOOK, SECRET)
    if KEYWORD and (KEYWORD not in title and KEYWORD not in md_text):
        title = f"{KEYWORD} | {title}"

    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        print("HR DingTalk resp:", r.status_code, r.text[:300])
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
        return ok
    except Exception as e:
        print("❌ 钉钉请求异常：", e)
        return False

# ====================== 抓取逻辑（保留你的结构，去掉 input） ======================
class HRNewsCrawler:
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.results = []

    def get_recent_hr_news(self):
        print("开始抓取人力资源相关资讯...")
        sources = [self.crawl_beijing_hrss, self.crawl_mohrss, self.crawl_hr_portals]
        for source in sources:
            try:
                source()
                time.sleep(1.2)
            except Exception as e:
                print(f"抓取来源时出错: {e}")
                continue
        return self.results

    def crawl_beijing_hrss(self):
        print("正在抓取北京人社局信息...")
        base_url = "https://rsj.beijing.gov.cn"
        urls_to_try = ['/xxgk/tzgg/', '/xxgk/gzdt/', '/xxgk/zcfg/']
        for url_path in urls_to_try:
            try:
                url = base_url + url_path
                response = self.session.get(url, headers=self.headers, timeout=15)
                response.encoding = 'utf-8'
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    selectors = ['.list li', '.news-list li', '.content-list li', 'ul li a']
                    for selector in selectors:
                        items = soup.select(selector)
                        if items:
                            for item in items[:10]:
                                self.process_news_item(item, base_url, '北京人社局')
                            break
                time.sleep(0.6)
            except Exception as e:
                print(f"抓取北京人社局 {url_path} 时出错: {e}")
                continue

    def crawl_mohrss(self):
        print("正在获取人社部相关信息（示例）...")
        mock_news = [
            {
                'title': '人力资源和社会保障部发布最新就业促进政策',
                'url': 'https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/202310/t20231015_123456.html',
                'source': '人社部',
                'date': (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d'),
                'content': '为进一步促进就业，人社部推出就业促进措施，包括技能培训补贴、创业扶持政策等。'
            },
            {
                'title': '2023年社会保险缴费基数调整通知',
                'url': 'https://www.mohrss.gov.cn/SYrlzyhshbzb/zwgk/202310/t20231008_123457.html',
                'source': '人社部',
                'date': (datetime.now() - timedelta(days=22)).strftime('%Y-%m-%d'),
                'content': '各地社会保险缴费基数将根据上年度在岗职工平均工资进行相应调整。'
            }
        ]
        for news in mock_news:
            if self.is_recent_news(news['date']):
                self.results.append(news)

    def crawl_hr_portals(self):
        print("正在获取人力资源门户网站信息（示例）...")
        mock_portal_news = [
            {
                'title': '2023年第四季度人力资源市场供需报告',
                'url': 'https://www.chinahr.com/news/202310/123456.html',
                'source': '中国人力资源网',
                'date': (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d'),
                'content': '信息技术、新能源等行业人才需求持续旺盛，市场供需基本平衡。'
            },
            {
                'title': '灵活用工政策最新解读',
                'url': 'https://www.51job.com/news/202310/123457.html',
                'source': '前程无忧',
                'date': (datetime.now() - timedelta(days=25)).strftime('%Y-%m-%d'),
                'content': '针对灵活用工的最新政策要求，专家解读，帮助企业合规用工。'
            },
            {
                'title': '数字化转型中的人力资源管理变革',
                'url': 'https://www.zhaopin.com/trends/202309/123458.html',
                'source': '智联招聘',
                'date': (datetime.now() - timedelta(days=40)).strftime('%Y-%m-%d'),
                'content': '企业数字化转型对人力资源管理提出了新的要求和挑战。'
            }
        ]
        for news in mock_portal_news:
            if self.is_recent_news(news['date']):
                self.results.append(news)

    def process_news_item(self, item, base_url, source):
        try:
            link = item.find('a')
            if not link:
                return
            title = link.get_text().strip()
            href = link.get('href', '')
            if href.startswith('/'):
                full_url = base_url + href
            elif href.startswith('http'):
                full_url = href
            else:
                return

            date_text = ""
            date_pattern = r'(\d{4}-\d{2}-\d{2})|(\d{4}/\d{2}/\d{2})|(\d{2}-\d{2}-\d{2})'
            date_match = re.search(date_pattern, item.get_text())
            if date_match:
                date_text = date_match.group()
            if not date_text or len(date_text) < 8:
                date_text = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

            news_item = {
                'title': title,
                'url': full_url,
                'source': source,
                'date': date_text,
                'content': self.extract_content_snippet(item)
            }
            if self.is_recent_news(date_text):
                self.results.append(news_item)
        except Exception as e:
            print(f"处理新闻条目时出错: {e}")

    def extract_content_snippet(self, item):
        try:
            text = item.get_text(" ", strip=True)
            return (text[:100] + '...') if len(text) > 100 else text
        except:
            return "内容获取中..."

    def is_recent_news(self, date_str, days=60):
        try:
            for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%y-%m-%d', '%m-%d'):
                try:
                    news_date = datetime.strptime(date_str, fmt)
                    if news_date.year < 2000:
                        news_date = news_date.replace(year=2000 + news_date.year % 100)
                    break
                except ValueError:
                    continue
            else:
                return True
            return (datetime.now() - news_date).days <= days
        except:
            return True

    def save_results(self):
        if not self.results:
            print("没有找到相关资讯")
            return None, None
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        csvf = f'hr_news_{ts}.csv'
        jsonf = f'hr_news_{ts}.json'
        with open(csvf, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=['title','url','source','date','content'])
            w.writeheader()
            w.writerows(self.results)
        with open(jsonf, 'w', encoding='utf-8') as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        print(f"结果已保存到: {csvf}, {jsonf}")
        return csvf, jsonf

    def to_markdown(self):
        if not self.results:
            return "今天未抓到符合条件的人社类资讯。"
        lines = [
            "### 🧩 人力资源资讯每日汇总",
            f"**汇总时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}**",
            f"**今日资讯：{len(self.results)} 条**",
            "",
            "🗞️ **资讯详情**"
        ]
        for i, it in enumerate(self.results[:8], 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
            lines.append(f"> 📅 {it['date']}　|　🏛️ {it['source']}")
            if it.get("content"):
                lines.append(f"> {it['content'][:120]}")
            lines.append("")
        lines.append("💡 早安！今日人力资源资讯已为您整理完毕")
        return "\n".join(lines)

def main():
    print("人力资源资讯自动抓取工具")
    print("=" * 50)
    crawler = HRNewsCrawler()
    crawler.get_recent_hr_news()
    # 打印 & 保存
    if crawler.results:
        print(f"\n找到 {len(crawler.results)} 条资讯：\n" + "-"*80)
        for i, it in enumerate(crawler.results, 1):
            print(f"{i}. {it['title']} | {it['source']} | {it['date']}")
        crawler.save_results()
    else:
        print("没有抓到资讯。")
    # 推送钉钉
    md = crawler.to_markdown()
    ok = send_dingtalk_markdown("人力资源资讯每日汇总", md)
    print("钉钉推送：", "成功 ✅" if ok else "失败 ❌")

if __name__ == "__main__":
    main()
