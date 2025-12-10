# -*- coding: utf-8 -*-
"""
三茅日报 + 财富中文网（商业/专栏）合并爬虫
------------------------------------------------
功能：
1. 三茅人力资源网：抓取指定日期的《三茅日报》标题列表；
2. 财富中文网·商业频道：抓取指定日期的新闻 / 专栏正文；
3. 调用硅基流动（OpenAI 兼容）生成一句话中文摘要（带“防幻觉”兜底）；
4. 合并成一条钉钉 Markdown 消息（编号连续、可点击跳转）；
5. 通过一个或多个钉钉机器人推送。

依赖（requirements.txt）：
- requests
- beautifulsoup4
"""

import os
import re
import time
import hmac
import ssl
import base64
import hashlib
import csv
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup, Tag

# ========== 通用工具 ==========
try:
    from zoneinfo import ZoneInfo
except Exception:  # py<3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore


def _tz():
    return ZoneInfo("Asia/Shanghai")


def now_tz():
    return datetime.now(_tz())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def zh_weekday(dt: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]


# ========== 钉钉工具（多机器人） ==========

def sign_dingtalk(secret: str, timestamp_ms: int) -> str:
    string_to_sign = f"{timestamp_ms}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return quote_plus(base64.b64encode(hmac_code))


def send_dingtalk_markdown(title: str, text: str):
    """
    将 Markdown 文本发送到一个或多个钉钉机器人。
    需要环境变量：
    - DINGTALK_BASES   : webhook 基础 URL，多个用英文逗号分隔
    - DINGTALK_SECRETS : 对应的 secret，多个用英文逗号分隔
    """
    bases_raw = os.getenv("DINGTALK_BASES", "").strip()
    secrets_raw = os.getenv("DINGTALK_SECRETS", "").strip()

    if not bases_raw or not secrets_raw:
        print("💡 未配置 DINGTALK_BASES / DINGTALK_SECRETS，跳过钉钉推送。")
        return

    bases = [b.strip() for b in bases_raw.split(",") if b.strip()]
    secrets = [s.strip() for s in secrets_raw.split(",") if s.strip()]

    if not bases or len(bases) != len(secrets):
        print("⚠️ DINGTALK_BASES 与 DINGTALK_SECRETS 数量不一致，跳过钉钉推送。")
        return

    for idx, (base_url, secret) in enumerate(zip(bases, secrets), start=1):
        try:
            ts = int(time.time() * 1000)
            sign = sign_dingtalk(secret, ts)
            full_url = f"{base_url}&timestamp={ts}&sign={sign}"

            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": text},
                "at": {"isAtAll": False},
            }

            print(f"\n📨 正在向第 {idx} 个钉钉机器人发送消息...")
            resp = requests.post(full_url, json=payload, timeout=10)
            print(f"  钉钉返回状态码：{resp.status_code}")
            try:
                print("  钉钉返回：", resp.text[:300])
            except Exception:
                pass

        except Exception as e:
            print(f"  ⚠️ 第 {idx} 个钉钉机器人发送失败：{e}")


# ========== requests Session（含老 TLS 兼容） ==========

class LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    s.mount("https://", LegacyTLSAdapter())
    return s


# ========== 一、三茅日报爬虫 ==========

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
    except Exception:
        return None


def looks_like_numbered(text: str) -> bool:
    return bool(
        re.match(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*\S+", text or "")
    )


CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def strip_leading_num(t: str) -> str:
    t = re.sub(r"^\s*[（(]?\s*\d{1,2}\s*[)）]?\s*[、.．]\s*", "", t)
    t = re.sub(r"^\s*[" + CIRCLED + r"]\s*", "", t)
    t = re.sub(r"^\s*[０-９]+\s*[、.．]\s*", "", t)
    return t.strip()


class HRLooCrawler:
    def __init__(self):
        self.session = make_session()
        self.results = []
        self.max_items = 1

        t = (os.getenv("HR_TARGET_DATE") or "").strip()
        if t:
            try:
                y, m, d = map(int, re.split(r"[-/\.]", t))
                self.target_date = date(y, m, d)
            except Exception:
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
        print(
            f"[HRLOO CFG] target_date={self.target_date} "
            f"{zh_weekday(now_tz())} sources={self.sources}"
        )

    # ---- 对外入口 ----
    def crawl(self):
        for base in self.sources:
            if self._crawl_source(base):
                break

    # ---- 抓首页，找到《三茅日报》 ----
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

        # 1）优先走“容器列表”通道
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
                    except Exception:
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
            print("[HRLOO MISS] 容器通道未命中：", base)

        # 2）兜底：遍历所有 /news/xxx.html
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

        print("[HRLOO MISS] 本源未命中目标日期：", base)
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
        print(f"[HRLOO HIT] {abs_url} -> {len(titles)} 条")
        return True

    # ---- 细节页解析 ----
    def _extract_pub_time(self, soup: BeautifulSoup):
        cand = []
        for t in soup.select("time[datetime]"):
            cand.append(t.get("datetime", ""))
        for m in soup.select(
            "meta[property='article:published_time'],"
            "meta[name='pubdate'],meta[name='publishdate']"
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
            except Exception:
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
                print("[HRLOO DetailFail]", url, r.status_code)
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
            print("[HRLOO DetailError]", url, e)
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


# ========== 二、财富中文网爬虫 + AI 摘要 ==========

BASE = "https://www.fortunechina.com"
LIST_URL_BASE = "https://www.fortunechina.com/shangye/"
MAX_PAGES = 1
MAX_RETRY = 3

OUTPUT_CSV = "fortunechina_articles_with_ai_title.csv"
OUTPUT_MD = "fortunechina_articles_with_ai_title.md"


def get_target_date() -> str:
    env_date = os.getenv("TARGET_DATE", "").strip()
    if env_date:
        return env_date
    tz_cn = timezone(timedelta(hours=8))
    yesterday_cn = (datetime.now(tz_cn) - timedelta(days=1)).strftime("%Y-%m-%d")
    return yesterday_cn


TARGET_DATE = get_target_date()

DEFAULT_HEADERS = {
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
AI_MODEL = os.getenv("AI_MODEL", "Qwen/Qwen2.5-7B-Instruct")


def _title_keywords(title: str):
    parts = re.split(r"[：:，,。；;、？?！!（）()【】\s]+", title or "")
    return [p for p in parts if len(p) >= 2]


def _summary_passes_check(summary: str, title: str, body: str) -> bool:
    """
    简单“一致性检查”：
    - 标题拆出关键词，至少有一个出现在摘要中，否则认为模型在胡编。
    """
    if not summary:
        return False
    kws = _title_keywords(title)
    if not kws:
        return True  # 没法校验就放行
    for k in kws:
        if k in summary:
            return True
    # 再宽松一点：正文前 200 字里，是否出现了摘要里的核心词？
    body_short = (body or "")[:200]
    for w in _title_keywords(summary):
        if w in body_short:
            return True
    return False


def get_ai_summary(content: str, title: str = "") -> str:
    """
    使用硅基流动生成一句话摘要，并做“防幻觉”兜底：
    - 要求围绕【正文 + 标题】概括；
    - 若摘要里完全不含标题关键词，则回退为原标题。
    """
    fallback_title = title or "（未命名）"

    if not content or len(content) < 30:
        return fallback_title

    if not AI_API_KEY:
        print("  ⚠️ 未配置 OPENAI_API_KEY，跳过 AI 摘要。")
        return fallback_title

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }

    prompt = (
        "你是一个严谨的中文新闻编辑。请根据下面给出的【标题】和【正文】"
        "写出一条不超过 25 个字的一句话摘要：\n"
        "1. 只基于提供的内容，不得捏造新的事实或事件；\n"
        "2. 摘要必须与标题主题高度一致，不能把别的新闻写进来；\n"
        "3. 保持客观、中性，不标题党；\n"
        "4. 尽量包含标题中的关键信息（如人名、机构名、国家等）。"
    )

    user_content = f"【标题】{fallback_title}\n\n【正文】\n{content[:2000]}"

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 120,
        "temperature": 0.3,
    }

    print(f"  🤖 正在调用 AI 生成摘要（模型={AI_MODEL}）...")

    try:
        resp = requests.post(AI_CHAT_URL, headers=headers, json=payload, timeout=40)
        if resp.status_code != 200:
            print(f"  ❌ AI 状态码：{resp.status_code}")
            try:
                print("  ❌ AI 返回内容：", resp.text[:300])
            except Exception:
                pass
            return fallback_title

        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
        summary = summary.splitlines()[0].strip()

        # —— 一致性检查：防止“小米新闻跑到马克龙专栏上” —— 
        if not _summary_passes_check(summary, fallback_title, content):
            print("  ⚠️ AI 摘要与标题不一致，已回退为原标题。")
            return fallback_title

        print(f"  ✨ AI 摘要：{summary}")
        return summary or fallback_title

    except Exception as e:
        print(f"  ⚠️ AI 调用失败：{e}")
        return fallback_title


def fetch_list(page: int = 1):
    if page == 1:
        current_list_url = LIST_URL_BASE
    else:
        current_list_url = f"{LIST_URL_BASE}?page={page}"

    print(f"\n--- 正在请求财富列表页: 第 {page} 页 ({current_list_url}) ---")

    try:
        r = requests.get(current_list_url, headers=DEFAULT_HEADERS, timeout=15)
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

        if pub_date != TARGET_DATE:
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

    print(f"  ✅ 第 {page} 页抓到目标日期({TARGET_DATE})文章数：{len(items)}")
    return items


def fetch_article_content(item: dict):
    url = item["url"]
    headers = DEFAULT_HEADERS.copy()
    headers["Referer"] = LIST_URL_BASE

    for attempt in range(MAX_RETRY):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.select_one("div.article-mod div.word-text-con")
            if not container:
                container = soup.select_one("div.article-content")
            if not container:
                # Plus 专栏有时候结构不同，再兜底找主内容区
                container = soup.find("article") or soup

            paras = [
                p.get_text(strip=True)
                for p in container.find_all("p")
                if p.get_text(strip=True)
            ]
            if not paras:
                # 再兜底：把 container 文本全抓了
                text_all = container.get_text("\n", strip=True)
                item["content"] = text_all
            else:
                item["content"] = "\n".join(paras)

            time.sleep(0.5)
            return

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRY - 1:
                print(
                    f"  ❌ 请求失败 ({r.status_code if 'r' in locals() else 'Error'}), 重试中...: {url}"
                )
                time.sleep(1)
            else:
                print(f"  ⛔️ 最终失败: {url} | 错误: {e}")
                item["content"] = f"[获取失败: {e}]"


def save_to_csv(data: list, filename: str):
    if not data:
        print("💡 没有数据可保存。")
        return

    fieldnames = ["title", "ai_summary", "date", "url", "content"]
    try:
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        print(f"\n🎉 成功保存到 CSV：{filename}，共 {len(data)} 条。")
    except Exception as e:
        print(f"\n❌ CSV 保存失败：{e}")


# ========== 三、合并 Markdown 输出 ==========

def _strip_trailing_punc(s: str) -> str:
    return re.sub(r"[；;。.\s]+$", "", s or "")


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
            merged_items.append(
                {
                    "title": title,
                    "url": detail_url or "#",
                }
            )

    # —— 财富 AI 摘要 ——
    for art in fc_items or []:
        raw_title = art.get("ai_summary") or art.get("title") or ""
        title = _strip_trailing_punc(raw_title)
        if not title:
            continue
        merged_items.append(
            {
                "title": title,
                "url": art.get("url", "#"),
            }
        )

    if not merged_items:
        return f"**日期：{today_str}（{weekday_str}）**  \n**标题：人资日报 | 每日要点**  \n\n> 今日未抓取到有效资讯。"

    lines = [
        f"**日期：{today_str}（{weekday_str}）**  ",
        f"**标题：人资日报 | 每日要点**  ",
        "",
    ]

    # —— 编号 + 标点：最后一条句号，其余分号 ——
    for idx, item in enumerate(merged_items, start=1):
        title = item["title"]
        url = item["url"]

        if idx == len(merged_items):
            # 最后一条：句号
            lines.append(f"{idx}. [{title}]({url})。")
        else:
            lines.append(f"{idx}. [{title}]({url})；")

    return "\n".join(lines)


# ========== 四、主流程 ==========

def main():
    print("=== 🚀 合并爬虫启动（HR 三茅 + 财富中文网） ===")

    # 1. 三茅日报
    hr_crawler = HRLooCrawler()
    hr_crawler.crawl()
    hr_results = hr_crawler.results

    # 2. 财富中文网列表
    all_articles = []
    print(
        f"\n=== 📅 财富中文网目标日期: {TARGET_DATE} "
        f"（列表入口: {LIST_URL_BASE}） ==="
    )

    for page in range(1, MAX_PAGES + 1):
        list_items = fetch_list(page)
        if not list_items:
            if page == 1:
                print(f"⚠️ 第 1 页未找到 {TARGET_DATE} 的文章，请确认网站上确实有该日期的内容。")
            break
        all_articles.extend(list_items)
        time.sleep(1)

    print(
        f"\n=== 📥 财富链接收集完成，共 {len(all_articles)} 篇。开始抓取正文 + 生成 AI 摘要... ==="
    )

    for idx, item in enumerate(all_articles, start=1):
        print(f"\n🔥 财富 ({idx}/{len(all_articles)}) 处理: {item['title']}")
        fetch_article_content(item)
        item["ai_summary"] = get_ai_summary(item["content"], item["title"])

    success_count = sum(
        1
        for item in all_articles
        if "获取失败" not in item["content"] and item["content"]
    )
    print(
        f"\n=== 财富统计: 成功 {success_count} 篇，失败 {len(all_articles) - success_count} 篇 ==="
    )
    save_to_csv(all_articles, OUTPUT_CSV)

    # 3. 生成单独的财富 Markdown（可选）
    fc_md_lines = []
    if all_articles:
        fc_md_lines.append(f"### 财富中文网·商业频道精选（{TARGET_DATE}）")
        fc_md_lines.append("")
        for i, art in enumerate(all_articles, start=1):
            t = art.get("ai_summary") or art.get("title") or "（无标题）"
            u = art.get("url", "")
            fc_md_lines.append(f"{i}. [{t}]({u})")
    fc_md = "\n".join(fc_md_lines) if fc_md_lines else ""
    if fc_md:
        try:
            with open(OUTPUT_MD, "w", encoding="utf-8") as f:
                f.write(fc_md)
            print(f"\n📄 已保存财富 Markdown 文件：{OUTPUT_MD}")
        except Exception as e:
            print(f"\n❌ 财富 Markdown 保存失败：{e}")

    # 4. 合并三茅 + 财富，生成总 Markdown，并推送钉钉
    md_merged = build_clean_markdown(hr_results, all_articles)
    print("\n===== 合并 Markdown 预览 =====\n")
    print(md_merged)

    print("\n>>> [步骤4] 推送到钉钉机器人")
    send_dingtalk_markdown("人资日报 | 每日要点", md_merged)


if __name__ == "__main__":
    main()
