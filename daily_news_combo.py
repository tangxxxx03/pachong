# -*- coding: utf-8 -*-
"""
三茅人资日报 + 财富中文网·商业频道
—— 合并版爬虫 + SiliconFlow AI 摘要 + 钉钉 Markdown 一次推送

功能概览：
1）抓取三茅人力资源网的「三茅日报」要点列表；
2）抓取财富中文网·商业频道指定日期新闻（默认北京时间昨天）；
3）对财富新闻正文调用 SiliconFlow（OpenAI 兼容接口）生成一句话中文摘要；
4）三茅 + 财富 结果合并成一条 Markdown 消息；
5）通过钉钉机器人（支持多机器人或单机器人）一次性发送。

环境变量约定（按需配置）：
- HR_TARGET_DATE          ：三茅日报目标日期（YYYY-MM-DD，不填则默认今天）
- SRC_HRLOO_URLS          ：三茅抓取入口，多个用逗号分隔（默认：官网 + 新闻页）

- TARGET_DATE             ：财富中文网目标日期（YYYY-MM-DD，不填则默认“北京时间昨天”）
- OPENAI_API_KEY          ：SiliconFlow / OpenAI 兼容 Key（形如 sk-xxx）
- AI_API_BASE             ：SiliconFlow Base URL（默认 https://api.siliconflow.cn/v1）
- AI_MODEL                ：模型名（默认 Qwen/Qwen2.5-14B-Instruct）

- DINGTALK_BASES          ：钉钉 webhook 基础 URL，多个用逗号分隔（含 access_token）
- DINGTALK_SECRETS        ：对应的 secret，多个用逗号分隔
    —— 或者使用单机器人老配置：
- DINGTALK_BASE / DINGTALK_BASEA
- DINGTALK_SECRET / DINGTALK_SECRETA
"""

import os
import re
import time
import csv
import hmac
import ssl
import base64
import hashlib
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup, Tag
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ===================== 通用工具 =====================

try:
    from zoneinfo import ZoneInfo
except:
    from backports.zoneinfo import ZoneInfo


def _tz():
    return ZoneInfo("Asia/Shanghai")


def now_tz():
    return datetime.now(_tz())


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def zh_weekday(dt):
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]


def _sign_webhook(base, secret):
    """
    钉钉签名，兼容“base 不带参数 / 已带 ?access_token=”两种情况。
    """
    if not base:
        return ""
    if not secret:
        return base
    ts = str(round(time.time() * 1000))
    s = f"{ts}\n{secret}".encode("utf-8")
    sign = urllib.parse.quote_plus(
        base64.b64encode(hmac.new(secret.encode("utf-8"), s, hashlib.sha256).digest())
    )
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}timestamp={ts}&sign={sign}"


class LegacyTLSAdapter(HTTPAdapter):
    """
    为一些老站点兼容 TLS
    """

    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def make_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    r = Retry(total=3, backoff_factor=0.6, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", LegacyTLSAdapter(max_retries=r))
    return s


# ===================== 一、三茅 · HRLoo 三茅日报爬虫 =====================

CN_TITLE_DATE = re.compile(
    r"[（(]\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[)）]"
)


def date_from_bracket_title(text: str):
    m = CN_TITLE_DATE.search(text or "")
    if not m:
        return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return date(y, mo, d)
    except:
        return None


def looks_like_numbered(text: str) -> bool:
    return bool(
        re.match(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*\S+", text or "")
    )


# —— 统一去掉自带编号（“1、…/1. …/(1) …/① …/１． …”）
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def strip_leading_num(t: str) -> str:
    t = re.sub(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*", "", t)
    t = re.sub(r"^\s*[" + CIRCLED + r"]\s*", "", t)
    t = re.sub(r"^\s*[０-９]+\s*[、.．]\s*", "", t)
    return t.strip()


class HRLooCrawler:
    """
    三茅人力资源网 · 三茅日报 抓取
    """

    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1

        t = (os.getenv("HR_TARGET_DATE") or "").strip()
        if t:
            try:
                y, m, d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y, m, d)
            except:
                print("⚠️ HR_TARGET_DATE 解析失败，使用今日。")
                self.target_date = now_tz().date()
        else:
            self.target_date = now_tz().date()

        self.daily_title_pat = re.compile(r"三茅日[报報]")
        self.sources = [
            u.strip()
            for u in os.getenv(
                "SRC_HRLOO_URLS",
                "https://www.hrloo.com/,https://www.hrloo.com/news/hr",
            ).split(",")
            if u.strip()
        ]
        print(f"[CFG] HR target_date={self.target_date} {zh_weekday(now_tz())}  sources={self.sources}")

    def crawl(self):
        """
        尝试从所有 sources 中找到“符合 target_date 的三茅日报”
        """
        for base in self.sources:
            if self._crawl_source(base):
                break

    def _crawl_source(self, base):
        try:
            r = self.session.get(base, timeout=20)
        except Exception as e:
            print("首页请求异常：", base, e)
            return False
        if r.status_code != 200:
            print("首页请求失败：", base, r.status_code)
            return False

        soup = BeautifulSoup(r.text, "html.parser")

        # 1）新容器结构
        items = soup.select("div.dwxfd-list-items div.dwxfd-list-content-left")
        if items:
            for div in items:
                dts = (div.get("dwdata-time") or "").strip()
                if dts:
                    try:
                        pub_d = datetime.strptime(
                            dts.split()[0], "%Y-%m-%d"
                        ).date()
                        if pub_d != self.target_date:
                            continue
                    except:
                        pass
                a = div.find("a", href=True)
                if not a:
                    continue
                title_text = norm(a.get_text())
                if not self.daily_title_pat.search(title_text):
                    continue
                t2 = date_from_bracket_title(title_text)
                if t2 and t2 != self.target_date:
                    continue
                abs_url = urljoin(base, a["href"])
                if self._try_detail(abs_url):
                    return True
            print("[MISS] HR 容器通道未命中：", base)

        # 2）fallback：从 /news/xxx.html 里筛
        links = []
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href", "")
            if not re.search(r"/news/\d+\.html$", href):
                continue
            text = norm(a.get_text())
            if not self.daily_title_pat.search(text):
                continue
            t2 = date_from_bracket_title(text)
            if t2 and t2 != self.target_date:
                continue
            links.append(urljoin(base, href))

        seen = set()
        for url in links:
            if url in seen:
                continue
            seen.add(url)
            if self._try_detail(url):
                return True

        print("[MISS] HR 本源未命中目标日期：", base)
        return False

    def _try_detail(self, abs_url):
        pub_dt, titles, page_title = self._fetch_detail_clean(abs_url)
        if not page_title or not self.daily_title_pat.search(page_title):
            return False

        t3 = date_from_bracket_title(page_title)
        if t3 and t3 != self.target_date:
            return False
        if pub_dt and pub_dt.date() != self.target_date and not t3:
            return False
        if not titles:
            return False

        self.results.append(
            {
                "title": page_title,
                "url": abs_url,
                "date": (
                    pub_dt.strftime("%Y-%m-%d %H:%M")
                    if pub_dt
                    else f"{self.target_date} 09:00"
                ),
                "titles": titles,
            }
        )
        print(f"[HR HIT] {abs_url} -> {len(titles)} 条")
        return True

    def _extract_pub_time(self, soup: BeautifulSoup):
        cand = []
        for t in soup.select("time[datetime]"):
            cand.append(t.get("datetime", ""))
        for m in soup.select(
            "meta[property='article:published_time'],meta[name='pubdate'],meta[name='publishdate']"
        ):
            cand.append(m.get("content", ""))
        for sel in [
            ".time",
            ".date",
            ".pubtime",
            ".publish-time",
            ".post-time",
            ".info",
            "meta[itemprop='datePublished']",
        ]:
            for x in soup.select(sel):
                if isinstance(x, Tag):
                    cand.append(x.get_text(" ", strip=True))

        pat = re.compile(
            r"(20\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:\D+(\d{1,2}):(\d{1,2}))?"
        )

        def parse_one(s):
            m = pat.search(s or "")
            if not m:
                return None
            try:
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
                hh = int(m[4]) if m[4] else 9
                mm = int(m[5]) if m[5] else 0
                return datetime(y, mo, d, hh, mm, tzinfo=_tz())
            except:
                return None

        dts = [dt for dt in map(parse_one, cand) if dt]
        if dts:
            now = now_tz()
            past = [dt for dt in dts if dt <= now]
            return min(past or dts, key=lambda dt: abs((now - dt).total_seconds()))
        return None

    def _fetch_detail_clean(self, url):
        try:
            r = self.session.get(url, timeout=(6, 20))
            if r.status_code != 200:
                print("[HR DetailFail]", url, r.status_code)
                return None, [], ""
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            title_tag = soup.find(["h1", "h2"])
            page_title = norm(title_tag.get_text()) if title_tag else ""
            pub_dt = self._extract_pub_time(soup)
            container = soup.select_one(
                ".content-con.hr-rich-text.fn-wenda-detail-infomation.fn-hr-rich-text.custom-style-w"
            ) or soup
            for sel in [
                ".other-wrap",
                ".txt",
                "a.prev.fn-dataStatistics-btn",
                "a.next.fn-dataStatistics-btn",
                ".footer",
                ".bottom",
            ]:
                for bad in container.select(sel):
                    bad.decompose()
            titles = self._extract_strong_titles(container)
            if not titles:
                titles = self._extract_numbered_titles(container)
            return pub_dt, titles, page_title
        except Exception as e:
            print("[HR DetailError]", url, e)
            return None, [], ""

    def _extract_strong_titles(self, root: Tag):
        keep = []
        for st in root.select("strong"):
            text = norm(st.get_text())
            if not text:
                continue
            if len(text) < 4:
                continue
            text = re.split(
                r"[（(]?(阅读|阅读量|浏览|来源)[:：]\s*\d+.*$", text
            )[0].strip()
            if not text:
                continue
            text = strip_leading_num(text)
            if text:
                keep.append(text)
        seen, out = set(), []
        for t in keep:
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    def _extract_numbered_titles(self, root: Tag):
        out = []
        for p in root.find_all(["p", "h2", "h3", "div", "span", "li"]):
            text = norm(p.get_text())
            if looks_like_numbered(text):
                text = strip_leading_num(text)
                text = re.split(r"[（(]", text)[0].strip()
                if text and len(text) >= 4:
                    out.append(text)
        seen, final = set(), []
        for t in out:
            if t in seen:
                continue
            seen.add(t)
            final.append(t)
        return final


# ===================== 二、财富中文网 · 商业频道爬虫 + AI 摘要 =====================

FC_BASE = "https://www.fortunechina.com"
FC_LIST_URL_BASE = "https://www.fortunechina.com/shangye/"
FC_MAX_PAGES = 1
FC_MAX_RETRY = 3

FC_OUTPUT_CSV = "fortunechina_articles_with_ai_title.csv"
FC_OUTPUT_MD = "fortunechina_articles_with_ai_title.md"


def get_target_date() -> str:
    env_date = os.getenv("TARGET_DATE", "").strip()
    if env_date:
        return env_date
    tz_cn = timezone(timedelta(hours=8))
    yesterday_cn = (datetime.now(tz_cn) - timedelta(days=1)).strftime("%Y-%m-%d")
    return yesterday_cn


FC_TARGET_DATE = get_target_date()

FC_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}

AI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.siliconflow.cn/v1").rstrip("/")
AI_CHAT_URL = f"{AI_API_BASE}/chat/completions"
# —— 默认升级为 14B，更稳 —— 
AI_MODEL = os.getenv("AI_MODEL", "Qwen/Qwen2.5-14B-Instruct")


def _need_fallback(summary: str, title: str, content: str) -> bool:
    """
    简单的安全检查：
    1）摘要太短 / 太长；
    2）标题里有数字，但摘要里一个都没保留；
    3）摘要出现高风险词，但原文 + 标题中都不存在。
    满足任一条件时，建议退回原标题。
    """
    if not summary:
        return True

    s = summary.strip()
    if len(s) < 6 or len(s) > 40:
        return True

    title = title or ""
    content = content or ""

    # 数字保护：标题里有数字，摘要里必须至少保留一个
    nums_title = re.findall(r"\d+", title)
    if nums_title:
        if not any(n in s for n in nums_title):
            return True

    risky_words = ["竞争对手", "对手", "首次", "史上", "重磅", "爆款"]
    snippet = (content[:500] or "") + title
    for w in risky_words:
        if w in s and w not in snippet:
            return True

    return False


def get_ai_summary(content: str, fallback_title: str = "") -> str:
    """
    使用 SiliconFlow 生成一句话摘要。
    增强版：
    - 强约束 prompt：禁止脑补、禁止虚构关系；
    - 事后安全检测：不合格就退回原标题。
    """
    if not content or len(content) < 30:
        return fallback_title or "内容过短，无需摘要"

    if not AI_API_KEY:
        print("  ⚠️ 未配置 OPENAI_API_KEY，跳过 AI 摘要。")
        return fallback_title or "（未配置 AI 摘要）"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }

    # —— 零脑补 Prompt —— 
    system_prompt = (
        "你是一个严谨的中文新闻编辑，请根据给定的新闻正文，生成【一句话】中文摘要。\n"
        "必须严格遵守：\n"
        "1. 摘要必须完全基于原文事实，不允许添加原文中没有的信息；\n"
        "2. 不得推断公司之间的关系（如竞争对手、盟友等），除非原文明确说明；\n"
        "3. 不得推断“首次、史上、里程碑、重磅、爆款”等评价性结论；\n"
        "4. 不要加入主观评价，不使用夸张或营销化措辞；\n"
        "5. 尽量保留关键数字、时间、主体名称；\n"
        "6. 长度控制在 25 个汉字以内，保持客观、中性、简洁。"
    )

    user_content = (
        "请在不脑补、不新增信息的前提下，为下面这篇新闻写一句话摘要：\n\n"
        f"{content[:2000]}"
    )

    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "max_tokens": 120,
        "temperature": 0.2,
    }

    print(f"  🤖 正在调用 AI（{AI_CHAT_URL}，模型={AI_MODEL}）生成摘要...")

    try:
        resp = requests.post(AI_CHAT_URL, headers=headers, json=payload, timeout=30)

        if resp.status_code != 200:
            print(f"  ❌ AI 状态码：{resp.status_code}")
            try:
                print("  ❌ AI 返回内容：", resp.text)
            except Exception:
                pass
            resp.raise_for_status()

        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
        summary = summary.splitlines()[0].strip()
        print(f"  ✨ 原始 AI 摘要：{summary}")

        # —— 安全检测，不合格就用原标题兜底 —— 
        if _need_fallback(summary, fallback_title or "", content):
            print("  ⚠️ 摘要通过安全检查失败，改用原标题兜底。")
            return fallback_title or summary or "（AI 摘要不可靠，已回退）"

        print(f"  ✅ 通过安全检查的摘要：{summary}")
        return summary or (fallback_title or "（AI 摘要为空）")

    except Exception as e:
        print(f"  ⚠️ AI 调用失败：{e}")
        return fallback_title or f"[AI 调用失败: {e}]"


def fc_fetch_list(page: int = 1):
    if page == 1:
        current_list_url = FC_LIST_URL_BASE
    else:
        current_list_url = f"{FC_LIST_URL_BASE}?page={page}"

    print(f"\n--- 财富：正在请求列表页: 第 {page} 页 ({current_list_url}) ---")

    try:
        r = requests.get(current_list_url, headers=FC_DEFAULT_HEADERS, timeout=15)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"⚠️ 列表页请求失败: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []

    for li in soup.select("ul.news-list li.news-item"):
        h2 = li.find("h2")
        a = li.find("a", href=True)
        date_div = li.find("div", class_="date")

        if not (h2 and a and date_div):
            continue

        href = a["href"].strip()
        pub_date = date_div.get_text(strip=True) if date_div else ""

        if pub_date != FC_TARGET_DATE:
            continue

        if not re.search(r"content_\d+\.htm", href):
            continue

        url_full = urljoin(current_list_url, href)

        items.append(
            {
                "title": h2.get_text(strip=True),
                "url": url_full,
                "date": pub_date,
                "content": "",
                "ai_summary": "",
            }
        )

    print(
        f"  ✅ 第 {page} 页抓到目标日期({FC_TARGET_DATE})文章数：{len(items)}"
    )
    return items


def fc_fetch_article_content(item: dict):
    url = item["url"]
    headers = FC_DEFAULT_HEADERS.copy()
    headers["Referer"] = FC_LIST_URL_BASE

    for attempt in range(FC_MAX_RETRY):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.select_one("div.article-mod div.word-text-con")
            if not container:
                container = soup.select_one("div.article-content")

            if not container:
                item["content"] = "[正文容器未找到]"
                print(f"  ⚠️ 警告：URL {url} 访问成功但未找到正文容器")
                return

            paras = [
                p.get_text(strip=True)
                for p in container.find_all("p")
                if p.get_text(strip=True)
            ]
            item["content"] = "\n".join(paras)
            time.sleep(0.5)
            return

        except requests.exceptions.RequestException as e:
            if attempt < FC_MAX_RETRY - 1:
                print(
                    f"  ❌ 请求失败 ({r.status_code if 'r' in locals() else 'Error'}), 重试中...: {url}"
                )
                time.sleep(1)
            else:
                print(f"  ⛔️ 最终失败: {url} | 错误: {e}")
                item["content"] = f"[获取失败: {e}]"


def fc_save_to_csv(data: list, filename: str):
    if not data:
        print("💡 财富：没有数据可保存 CSV。")
        return

    fieldnames = ["title", "ai_summary", "date", "url", "content"]
    try:
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        print(f"\n🎉 财富：成功保存到 CSV：{filename}，共 {len(data)} 条。")
    except Exception as e:
        print(f"\n❌ 财富：CSV 保存失败：{e}")


def fc_build_markdown(items: list) -> str:
    if not items:
        return (
            f"### 财富中文网·商业频道精选（{FC_TARGET_DATE}）\n\n"
            f"今日未抓到符合条件的新闻。"
        )

    lines = [
        f"### 财富中文网·商业频道精选（{FC_TARGET_DATE}）",
        "",
    ]

    for idx, item in enumerate(items, start=1):
        title = item.get("ai_summary") or item.get("title") or "（无标题）"
        url = item.get("url", "")
        lines.append(f"{idx}. [{title}]({url})")

    return "\n".join(lines)


def fc_save_markdown(content: str, filename: str):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n📄 财富：已保存 Markdown 文件：{filename}")
    except Exception as e:
        print(f"\n❌ 财富：Markdown 保存失败：{e}")


def run_fortune_crawler():
    all_articles = []
    print(f"\n=== 🚀 财富爬虫启动 (目标日期: {FC_TARGET_DATE}) ===")
    print(
        f"=== 🛠️ 财富路径策略: 基于列表页 URL ({FC_LIST_URL_BASE}) 进行相对路径拼接 ==="
    )

    for page in range(1, FC_MAX_PAGES + 1):
        list_items = fc_fetch_list(page)
        if not list_items:
            if page == 1:
                print(
                    f"⚠️ 第 1 页未找到 {FC_TARGET_DATE} 的文章，请确认网站上确实有该日期的内容。"
                )
            break
        all_articles.extend(list_items)
        time.sleep(1)

    print(
        f"\n=== 📥 财富链接收集完成，共 {len(all_articles)} 篇。开始抓取正文 + 生成 AI 摘要... ==="
    )

    count = 0
    for item in all_articles:
        count += 1
        print(f"\n🔥 财富 ({count}/{len(all_articles)}) 处理: {item['title']}")
        fc_fetch_article_content(item)
        item["ai_summary"] = get_ai_summary(item["content"], item["title"])

    success_count = sum(
        1
        for item in all_articles
        if "获取失败" not in item["content"] and item["content"]
    )
    print(
        f"\n=== 财富统计: 成功 {success_count} 篇，失败 {len(all_articles) - success_count} 篇 ==="
    )
    fc_save_to_csv(all_articles, FC_OUTPUT_CSV)
    fc_md_content = fc_build_markdown(all_articles)
    fc_save_markdown(fc_md_content, FC_OUTPUT_MD)

    return all_articles


# ===================== 三、统一钉钉推送工具 =====================

def send_dingtalk_markdown(title: str, text: str):
    bases_raw = os.getenv("DINGTALK_BASES", "").strip()
    secrets_raw = os.getenv("DINGTALK_SECRETS", "").strip()

    if bases_raw and secrets_raw:
        bases = [b.strip() for b in bases_raw.split(",") if b.strip()]
        secrets = [s.strip() for s in secrets_raw.split(",") if s.strip()]

        if not bases or len(bases) != len(secrets):
            print("⚠️ DINGTALK_BASES 与 DINGTALK_SECRETS 数量不一致，跳过多机器人推送。")
        else:
            for idx, (base_url, secret) in enumerate(zip(bases, secrets), start=1):
                try:
                    full_url = _sign_webhook(base_url, secret)
                    payload = {
                        "msgtype": "markdown",
                        "markdown": {
                            "title": title,
                            "text": text,
                        },
                        "at": {
                            "isAtAll": False,
                        },
                    }
                    print(f"\n📨 正在向第 {idx} 个钉钉机器人发送消息...")
                    resp = requests.post(full_url, json=payload, timeout=10)
                    print(f"  钉钉返回状态码：{resp.status_code}")
                    try:
                        print("  钉钉返回：", resp.text)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"  ⚠️ 第 {idx} 个钉钉机器人发送失败：{e}")

    base_single = os.getenv("DINGTALK_BASE") or os.getenv("DINGTALK_BASEA")
    secret_single = os.getenv("DINGTALK_SECRET") or os.getenv("DINGTALK_SECRETA")

    if not base_single:
        if not (bases_raw and secrets_raw):
            print("💡 未配置任何钉钉 webhook（DINGTALK_BASE(S)），跳过推送。")
        return

    try:
        full_url = _sign_webhook(base_single, secret_single)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
            "at": {
                "isAtAll": False,
            },
        }
        print("\n📨 正在向单一钉钉机器人发送消息...")
        resp = requests.post(full_url, json=payload, timeout=10)
        print(f"  钉钉返回状态码：{resp.status_code}")
        try:
            print("  钉钉返回：", resp.text)
        except Exception:
            pass
    except Exception as e:
        print(f"  ⚠️ 单机器人钉钉发送失败：{e}")


# ===================== 四、合并 Markdown：统一编号 + 标题可点击 =====================

def _strip_trailing_punc(title: str) -> str:
    """
    去掉标题末尾多余的句号/分号/感叹号/逗号等，然后再统一加分号或句号。
    """
    if not title:
        return ""
    return re.sub(r"[；;。.!！?？、，,]+$", "", title.strip())


def build_clean_markdown(hr_items: list, fc_items: list) -> str:
    now_cn = now_tz()
    today_str = now_cn.strftime("%Y-%m-%d")
    weekday_str = zh_weekday(now_cn)

    merged_items = []

    # —— 三茅 titles ——
    if hr_items and hr_items[0].get("titles"):
        it = hr_items[0]
        detail_url = it.get("url", "")
        for t in it["titles"]:
            title = _strip_trailing_punc(t)
            if not title:
                continue
            merged_items.append({
                "title": title,
                "url": detail_url or "#"
            })

    # —— 财富 AI 摘要 ——
    for art in fc_items or []:
        raw_title = (art.get("ai_summary") or art.get("title") or "")
        title = _strip_trailing_punc(raw_title)
        if not title:
            continue
        merged_items.append({
            "title": title,
            "url": art.get("url", "#")
        })

    if not merged_items:
        return (
            f"**日期：{today_str}（{weekday_str}）**  \n"
            f"**标题：人资日报｜每日要点**\n"
            "今日未抓取到有效资讯。"
        )

    # 题头：日期/标题整行加粗；日期行尾两个空格强制换行
    lines = [
        f"**日期：{today_str}（{weekday_str}）**  ",
        "**标题：人资日报｜每日要点**",
        ""
    ]

    # 内容：最后一条句号，其余分号
    for idx, item in enumerate(merged_items, start=1):
        title = item["title"]
        url = item["url"]
        if idx == len(merged_items):
            lines.append(f"{idx}. [{title}]({url})。")
        else:
            lines.append(f"{idx}. [{title}]({url})；")

    return "\n".join(lines)


# ===================== 五、主入口 =====================

def main():
    print("=== 执行合并爬虫：三茅 + 财富中文网 ===")

    print("\n>>> [步骤1] 抓取三茅人力资源网 · 三茅日报")
    hr_crawler = HRLooCrawler()
    hr_crawler.crawl()
    hr_results = hr_crawler.results

    print("\n>>> [步骤2] 抓取财富中文网 · 商业频道 + AI 摘要")
    fc_articles = run_fortune_crawler()

    print("\n>>> [步骤3] 生成合并 Markdown 消息（统一编号 + 标题可点击）")
    combined_md = build_clean_markdown(hr_results, fc_articles)
    print("\n===== 合并 Markdown 预览 =====\n")
    print(combined_md)

    print("\n>>> [步骤4] 推送到钉钉机器人")
    md_title = f"人资 & 商业资讯日报（{now_tz().strftime('%Y-%m-%d')}）"
    send_dingtalk_markdown(md_title, combined_md)


if __name__ == "__main__":
    main()
