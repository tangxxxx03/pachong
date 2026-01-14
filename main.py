# -*- coding: utf-8 -*-
"""
外包/派遣：招标 & 中标采集（北京公共资源交易平台 + zsxtzb.cn 搜索）
—— 采集更完整（requests优先+selenium兜底+PDF附件回退）+ 输出极简（只推明细，不推汇总）
"""

import os, re, time, math, hmac, base64, hashlib
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import urlparse, urljoin, quote_plus

import requests
import pandas as pd
import pdfplumber
from bs4 import BeautifulSoup

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ================== 固定配置（不读环境变量） ==================
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
DINGTALK_SECRET  = "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"  # 若开启“加签”，填入密钥；未开启则留空字符串

KEYWORDS        = ["外包", "派遣"]
CRAWL_BEIJING   = True
CRAWL_ZSXTZB    = True

# 只保留未来 N 天内截止的招标；<=0 表示不过滤
DUE_FILTER_DAYS = 30
# 丢弃已过期的招标（仅当能解析出截止时间）
SKIP_EXPIRED    = True

HEADLESS        = True

# 输出控制：摘要截断长度（极简）
BRIEF_MAX_LEN   = 80

# DingTalk 单条 markdown 安全长度（经验值）
DINGTALK_CHUNK  = 4200


# ========== HTTP 会话（禁用环境代理 + 重试） ==========
for _k in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','all_proxy','NO_PROXY'):
    os.environ.pop(_k, None)

_SESSION = requests.Session()
_SESSION.trust_env = False
_retry = Retry(
    total=4,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET", "POST"])
)
_SESSION.mount("http://", HTTPAdapter(max_retries=_retry))
_SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
})


# ================== DingTalk 加签与发送 ==================
def _build_signed_webhook(base_url: str, secret: str) -> str:
    base_url = (base_url or "").strip()
    if not base_url or not secret:
        return base_url
    ts = str(int(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(h))
    sep = "&" if ("?" in base_url) else "?"
    return f"{base_url}{sep}timestamp={ts}&sign={sign}"

def send_to_dingtalk_markdown(title: str, md_text: str):
    base_webhook = (DINGTALK_WEBHOOK or "").strip()
    if not base_webhook.startswith("http"):
        print("? Webhook 未配置或无效"); return
    final_url = _build_signed_webhook(base_webhook, (DINGTALK_SECRET or "").strip())
    headers = {"Content-Type": "application/json"}
    data = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    try:
        resp = _SESSION.post(final_url, json=data, headers=headers, timeout=15)
        print("钉钉推送：", resp.status_code, resp.text[:180])
    except Exception as e:
        print("? 发送钉钉失败：", e)


# ================== 日期范围：默认“昨日”，周一抓周五 ==================
def get_date_range():
    today = datetime.now()
    if today.weekday() == 0:
        start = today - timedelta(days=3)
        end   = today - timedelta(days=1)
    else:
        start = today - timedelta(days=1)
        end   = today - timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ================== 分类（保持你原逻辑） ==================
def classify(title: str) -> str:
    t = title or ""
    if any(k in t for k in ["中标", "成交", "结果", "定标", "候选人公示", "成交公告", "中标公告"]): return "中标公告"
    if any(k in t for k in ["更正", "变更", "澄清", "补遗"]): return "更正公告"
    if any(k in t for k in ["终止", "废标", "流标"]): return "终止公告"
    if any(k in t for k in ["招标", "采购", "磋商", "邀请", "比选", "谈判", "竞争性", "公开招标"]): return "招标公告"
    return "其他"


# ================== 文本工具 ==================
def _safe_text(s: str) -> str:
    return (s or "").replace("\u3000", " ").replace("\xa0", " ").strip()

def _date_in_text(s: str):
    if not s: return ""
    m = re.search(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})", s)
    return m.group(1).replace(".", "-").replace("/", "-") if m else ""

def _normalize_amount_text(s: str) -> str:
    if not s: return ""
    s = str(s).replace("，", ",").replace(",", "")
    s = re.sub(r"\s+", "", s)
    return s

def _normalize_date_string(s: str) -> str:
    if not s: return ""
    s = s.strip()
    s = s.replace("年", "-").replace("月", "-").replace("日", " ")
    s = s.replace("/", "-").replace("：", ":").replace("．", ".")
    s = re.sub(r"\s+", " ", s)

    m = re.search(r"(20\d{2})[-\.](\d{1,2})[-\.](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else None
    mm = int(m.group(5)) if m.group(5) else None
    try:
        if hh is not None and mm is not None:
            return datetime(y, mo, d, hh, mm).strftime("%Y-%m-%d %H:%M")
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except Exception:
        return ""

def _to_datetime(s: str):
    if not s: return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

def _pick_first(text: str, patterns):
    for pat in patterns:
        m = re.search(pat, text, re.S | re.I)
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return ""

def _clean_line(s: str) -> str:
    s = _safe_text(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _soup_text(soup: BeautifulSoup, selector: str) -> str:
    try:
        el = soup.select_one(selector)
        if not el:
            return ""
        return _clean_line(el.get_text("\n", strip=True))
    except Exception:
        return ""


# ================== 截止时间抽取（更稳） ==================
def extract_deadline(detail_text: str) -> str:
    txt = _safe_text(detail_text)
    pats = [
        r"(?:投标(?:文件)?|递交(?:响应)?文件|响应文件提交|报价|报名|获取招标文件)\s*截止(?:时间|日期)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
        r"(?:提交|递交)\s*截止(?:时间|日期)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
        r"(?:截止(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})(?=.*?(?:投标|递交|响应|报价|报名))",
        r"(?:截止至)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
    ]
    s = _pick_first(txt, pats)
    norm = _normalize_date_string(s)
    if norm:
        return norm

    s2 = _pick_first(txt, [r"(?:开标(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})"])
    norm2 = _normalize_date_string(s2)
    return norm2 or ""


# ================== 摘要抽取（极简） ==================
def extract_project_brief(detail_text: str, max_len: int = 80) -> str:
    txt = _safe_text(detail_text)

    for pat in [
        r"(?:项目概况|项目基本情况)\s*[:：]?\s*([\s\S]{0,260}?)\n",
        r"(?:采购需求|服务范围|服务内容|项目内容)\s*[:：]?\s*([\s\S]{0,260}?)\n",
    ]:
        t = _pick_first(txt, [pat])
        t = _clean_line(t)
        if len(t) >= 12:
            return (t[:max_len] + ("..." if len(t) > max_len else "")).strip()

    plain = _clean_line(txt)
    if not plain:
        return "暂无"
    return (plain[:max_len] + ("..." if len(plain) > max_len else "")).strip()


# ================== PDF 文本读取 ==================
def fetch_pdf_text(url: str, referer: str = None, timeout=20) -> str:
    try:
        headers = {"User-Agent":"Mozilla/5.0"}
        if referer:
            headers["Referer"] = referer
        r = _SESSION.get(url, headers=headers, timeout=timeout)
        ct = (r.headers.get("Content-Type") or "").lower()
        if "pdf" not in ct and not url.lower().endswith(".pdf"):
            return ""
        with pdfplumber.open(BytesIO(r.content)) as pdf:
            pages = []
            for p in pdf.pages:
                try:
                    pages.append(p.extract_text() or "")
                except Exception:
                    continue
        return "\n".join([x for x in pages if x.strip()])
    except Exception as e:
        print("PDF 读取失败：", e)
        return ""


# ================== 详情页：requests优先 + selenium兜底 + PDF附件回退 ==================
CONTENT_SELECTORS = [
    "#vsb_content", "#zoom", "#xxnr", "#info",
    "article", "main", ".content", ".article", ".detail", ".cont",
]

def _extract_main_text_from_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")

    for sel in CONTENT_SELECTORS:
        t = _soup_text(soup, sel)
        if t and len(t) >= 120:
            return t

    body = soup.body.get_text("\n", strip=True) if soup.body else soup.get_text("\n", strip=True)
    return _clean_line(body)

def _extract_pdf_links_from_html(html: str, base_url: str) -> list:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    pdfs = []
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        absu = urljoin(base_url, href)
        if absu.lower().endswith(".pdf"):
            pdfs.append(absu)
    out, seen = [], set()
    for u in pdfs:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out

def get_detail_text(url: str, driver=None) -> str:
    html = ""
    try:
        r = _SESSION.get(url, timeout=20)
        if r.status_code == 200 and (r.text or "").strip():
            html = r.text
    except Exception:
        html = ""

    text = _extract_main_text_from_html(html)
    if text and len(text) >= 120:
        return text

    if driver is not None:
        try:
            driver.get(url)
            WebDriverWait(driver, 12).until(lambda d: (d.page_source and len(d.page_source) > 2000))
            html2 = driver.page_source
            text2 = _extract_main_text_from_html(html2)
            if text2 and len(text2) >= 120:
                return text2
            html = html2 if html2 else html
        except Exception:
            pass

    for pdf_url in _extract_pdf_links_from_html(html, url)[:3]:
        pdf_text = fetch_pdf_text(pdf_url, referer=url)
        pdf_text = _safe_text(pdf_text)
        if pdf_text and len(pdf_text) >= 120:
            return pdf_text

    return text or _extract_main_text_from_html(html) or ""


# ================== 招标字段解析（更“抓得住”） ==================
def parse_bidding_fields(detail_text: str):
    txt = _safe_text(detail_text)

    amount = _pick_first(txt, [
        r"(?:预算金额|采购预算|项目预算)\s*[:：]?\s*([0-9\.,，]+\s*(?:万元|元))",
        r"(?:最高限价|控制价)\s*[:：]?\s*([0-9\.,，]+\s*(?:万元|元))",
    ])
    amount = _normalize_amount_text(amount) if amount else "暂无"

    purchaser = _pick_first(txt, [r"(?:采购人|采购单位|招标人)\s*[:：]?\s*([^\n\r，。;；]{2,80})"]) or "暂无"

    contact = "暂无"
    phone   = "暂无"
    m_cp = re.search(
        r"(?:项目联系人|联系人)[：:\s]*([^\s、，。;；]+)[\s\S]{0,160}?"
        r"(?:电\s*话|联系电话|联系方式)[：:\s]*([0-9\-－—\s]{6,})",
        txt, re.S
    )
    if m_cp:
        contact = m_cp.group(1).strip()
        phone = re.sub(r"\s+", "", m_cp.group(2)).replace("－", "-").replace("—", "-")
    else:
        c2 = _pick_first(txt, [r"(?:联系人|项目联系人|采购人联系人)\s*[:：]?\s*([^\s、，。;；]{2,20})"])
        p2 = _pick_first(txt, [r"(?:联系电话|联系方式|电\s*话)\s*[:：]?\s*([0-9\-－—\s]{6,})"])
        if c2: contact = c2
        if p2: phone = re.sub(r"\s+", "", p2).replace("－", "-").replace("—", "-")

    deadline = extract_deadline(txt) or "暂无"
    brief    = extract_project_brief(txt, max_len=BRIEF_MAX_LEN) or "暂无"

    return {
        "金额": amount,
        "采购人": purchaser,
        "联系人": contact,
        "联系电话": phone,
        "投标截止": deadline,
        "简要摘要": brief,
    }


# ================== 中标解析：表格优先 + 文本兜底（输出也极简） ==================
def _num_from_any(v):
    if v in (None, "", "暂无"): return None
    s = str(v).replace(",", "").replace("，", "")
    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None

def parse_award_from_tables(html: str):
    supplier = amount = score = "暂无"
    unit = ""

    try:
        tables = pd.read_html(html)
    except Exception:
        tables = []

    rows = []
    for tb in tables:
        t = tb.fillna("").astype(str)
        cols = [str(c) for c in t.columns]
        joined_cols = "".join(cols)
        if not any(k in joined_cols for k in ["供应商", "单位名称", "中标人", "成交人"]):
            continue

        def find_col(keys):
            for k in keys:
                for c in cols:
                    if k in c:
                        return c
            return None

        c_sup = find_col(["供应商名称", "供应商", "单位名称", "中标人", "成交人"])
        c_sco = find_col(["评审得分", "综合得分", "最终得分", "得分"])
        c_rnk = find_col(["名次", "排序", "排名"])
        c_pri = find_col(["评审报价", "中标金额", "成交金额", "报价", "投标报价", "金额"])

        for _, r in t.iterrows():
            name = (r.get(c_sup, "") if c_sup else "").strip()
            if not name:
                continue

            price_val = r.get(c_pri) if c_pri else None
            score_val = r.get(c_sco) if c_sco else None
            rank_val  = r.get(c_rnk) if c_rnk else None

            row = {
                "supplier": name,
                "score": _num_from_any(score_val),
                "rank":  _num_from_any(rank_val),
                "price": _num_from_any(price_val),
            }

            if c_pri and ("万元" in c_pri):
                unit = "万元"
            if isinstance(price_val, str) and ("万元" in price_val):
                unit = "万元"
            if isinstance(price_val, str) and (price_val.strip().endswith("元")):
                unit = "元"

            rows.append(row)

    chosen = None
    if rows:
        with_score = [r for r in rows if r["score"] is not None]
        if with_score:
            chosen = max(with_score, key=lambda x: x["score"])
        else:
            with_rank = [r for r in rows if r["rank"] is not None]
            if with_rank:
                chosen = min(with_rank, key=lambda x: x["rank"])
            else:
                with_price = [r for r in rows if r["price"] is not None]
                chosen = min(with_price, key=lambda x: x["price"]) if with_price else rows[0]

    if chosen:
        supplier = chosen["supplier"] or "暂无"
        if chosen["price"] is not None:
            amount = str(chosen["price"])
            if unit:
                amount = f"{amount}{unit}"
        score = str(chosen["score"]) if chosen["score"] is not None else "暂无"

    return {"中标公司": supplier, "中标金额": amount, "评审得分": (score or "暂无").rstrip("分")}

def parse_award_from_text(detail_text: str):
    txt = _safe_text(detail_text)
    supplier = _pick_first(txt, [
        r"(?:中标(?:供应商|人|单位)|成交(?:供应商|人|单位)|供应商名称)\s*[：:]\s*([^\n\r，。；;]{2,80})",
        r"(?:成交单位)\s*[：:]\s*([^\n\r，。；;]{2,80})",
    ]) or "暂无"

    amount = _pick_first(txt, [
        r"(?:中标(?:价|金额)|成交(?:价|金额)|评审报价|成交价)\s*[：:]\s*([0-9\.,，]+\s*(?:万元|元)?)",
        r"(?:合同金额)\s*[：:]\s*([0-9\.,，]+\s*(?:万元|元)?)",
    ])
    amount = _normalize_amount_text(amount) if amount else "暂无"

    score = _pick_first(txt, [r"(?:评审(?:得分|分值)|综合得分|最终得分|得分)\s*[：:]\s*([0-9\.]+)"])
    score = (score or "暂无").rstrip("分")

    return {"中标公司": supplier, "中标金额": amount, "评审得分": score}

def parse_award_fields(detail_html: str, detail_text: str):
    data = parse_award_from_tables(detail_html)
    if data.get("中标公司") == "暂无" and data.get("中标金额") == "暂无":
        data = parse_award_from_text(detail_text)

    txt = _safe_text(detail_text or "")
    award_date = _pick_first(txt, [
        r"(?:公告日期|公示时间|发布时间|成交日期|中标日期)\s*[：:]\s*([0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2})",
    ]) or _date_in_text(txt)
    award_date = _normalize_date_string(award_date) or award_date or "暂无"
    data["中标日期"] = award_date
    return data


# -------- Selenium（容器友好） --------
def _build_driver():
    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    if HEADLESS:
        opts.add_argument("--headless=new")
    try:
        driver = webdriver.Chrome(options=opts)  # Selenium Manager
    except Exception:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.implicitly_wait(6)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    return driver


# ================== 站点一：北京公共资源交易平台 ==================
def crawl_beijing(keywords, max_pages=10, date_start=None, date_end=None):
    driver = _build_driver()
    all_bidding, all_award = [], []
    seen_url = set()

    try:
        for kw in keywords:
            url = f"https://ggzyfw.beijing.gov.cn/elasticsearch/index.jsp?qt={kw}"
            driver.get(url)

            try:
                WebDriverWait(driver, 12).until(EC.presence_of_all_elements_located((By.CLASS_NAME, "cs_search_content_box")))
            except Exception:
                pass

            try:
                driver.find_element(By.XPATH, "//span[contains(text(),'时间不限')]").click()
                time.sleep(0.4)
                driver.find_element(By.ID, "week").click()
                time.sleep(0.8)
            except Exception:
                pass

            for page in range(1, max_pages + 1):
                try:
                    WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CLASS_NAME, "cs_search_content_box")))
                except Exception:
                    break

                cards = driver.find_elements(By.CLASS_NAME, "cs_search_content_box")
                if not cards:
                    break

                for c in cards:
                    try:
                        title_el = c.find_element(By.CLASS_NAME, "cs_search_title")
                        title = title_el.text.strip()
                        ann_type = classify(title)
                        if ann_type not in ("招标公告", "中标公告"):
                            continue

                        info_source, pub_time = "暂无", "暂无"
                        try:
                            source_line = c.find_element(By.CLASS_NAME, "cs_search_content_time").text
                            if "发布时间：" in source_line:
                                parts = source_line.split("发布时间：")
                                info_source = parts[0].replace("信息来源：", "").strip() or "暂无"
                                pub_time = parts[1].strip() or "暂无"
                        except Exception:
                            pass

                        pub_date = pub_time[:10] if pub_time and pub_time != "暂无" else ""
                        if date_start and date_end and pub_date:
                            if pub_date < date_start or pub_date > date_end:
                                continue

                        url_link = ""
                        try:
                            url_link = title_el.find_element(By.TAG_NAME, "a").get_attribute("href")
                        except Exception:
                            url_link = ""

                        if not url_link:
                            continue
                        if url_link in seen_url:
                            continue
                        seen_url.add(url_link)

                        detail_text = get_detail_text(url_link, driver=driver)

                        detail_html = ""
                        try:
                            r = _SESSION.get(url_link, timeout=20)
                            if r.status_code == 200:
                                detail_html = r.text or ""
                        except Exception:
                            pass
                        if not detail_html:
                            try:
                                driver.get(url_link)
                                WebDriverWait(driver, 10).until(lambda d: d.page_source and len(d.page_source) > 2000)
                                detail_html = driver.page_source
                            except Exception:
                                detail_html = ""

                        if ann_type == "招标公告":
                            fields = parse_bidding_fields(detail_text)
                            due_str = fields.get("投标截止", "暂无")
                            due_dt  = _to_datetime(due_str if due_str != "暂无" else "")

                            keep = True
                            now = datetime.now()
                            if SKIP_EXPIRED and due_dt and due_dt < now:
                                keep = False
                            if keep and DUE_FILTER_DAYS > 0 and due_dt and due_dt > now + timedelta(days=DUE_FILTER_DAYS):
                                keep = False

                            if keep:
                                all_bidding.append({
                                    "站点": "北京公共资源",
                                    "关键词": kw,
                                    "公告标题": title,
                                    "公告发布时间": pub_time,
                                    "信息来源": info_source,
                                    "投标截止": due_str,
                                    "金额": fields["金额"],
                                    "采购人": fields["采购人"],
                                    "联系人": fields["联系人"],
                                    "联系电话": fields["联系电话"],
                                    "简要摘要": fields["简要摘要"],
                                    "公告网址": url_link,
                                })
                        else:
                            fields = parse_award_fields(detail_html, detail_text)
                            all_award.append({
                                "站点": "北京公共资源",
                                "关键词": kw,
                                "标题": title,
                                "发布时间": pub_time,
                                "信息来源": info_source,
                                "中标日期": fields.get("中标日期", pub_date or "暂无"),
                                "中标公司": fields.get("中标公司", "暂无"),
                                "中标金额": fields.get("中标金额", "暂无"),
                                "评审得分": fields.get("评审得分", "暂无"),
                                "中标网址": url_link,
                            })

                    except Exception as ex:
                        print("解析一条出错：", ex)

                try:
                    next_btn = driver.find_element(By.LINK_TEXT, "下一页")
                    cls = (next_btn.get_attribute("class") or "")
                    if "disable" in cls or next_btn.get_attribute("aria-disabled") == 'true':
                        break
                    if page < max_pages:
                        driver.execute_script("arguments[0].click();", next_btn)
                        time.sleep(0.9)
                except Exception:
                    break

    finally:
        driver.quit()

    return all_bidding, all_award


# ================== 站点二：zsxtzb.cn 聚合搜索 ==================
def _zs_search_url(keyword, page=1):
    base = f"https://www.zsxtzb.cn/search?keyword={keyword}"
    if page > 1:
        base += f"&page={page}"
    return base

def _zs_pick_list_items_from_html(html: str, base_url: str):
    soup = BeautifulSoup(html or "", "lxml")
    items = []

    for a in soup.find_all("a"):
        title = _clean_line(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        if not title or not href:
            continue
        if len(title) < 6:
            continue
        if any(x in title for x in ["首页", "上一页", "下一页", "末页", "更多", "下载", "返回"]):
            continue
        absu = urljoin(base_url, href)
        if not (absu.startswith("http") and ("/" in urlparse(absu).path)):
            continue

        parent_text = ""
        try:
            parent = a.find_parent(["li", "div", "section"])
            parent_text = parent.get_text(" ", strip=True) if parent else ""
        except Exception:
            parent_text = ""
        dt = _date_in_text(parent_text)
        items.append((title, absu, dt))

    uniq, seen = [], set()
    for t, h, d in items:
        if h in seen:
            continue
        seen.add(h)
        uniq.append((t, h, d))
    return uniq

def crawl_zsxtzb_search(keywords, max_pages=8, date_start=None, date_end=None):
    driver = _build_driver()
    all_bidding, all_award = [], []
    seen_url = set()

    try:
        for kw in keywords:
            for page in range(1, max_pages + 1):
                url = _zs_search_url(kw, page)
                print(f"[zsxtzb] {kw} 第{page}页 -> {url}")

                html = ""
                try:
                    r = _SESSION.get(url, timeout=20)
                    if r.status_code == 200:
                        html = r.text or ""
                except Exception:
                    html = ""

                if not html.strip():
                    try:
                        driver.get(url)
                        WebDriverWait(driver, 10).until(lambda d: d.page_source and len(d.page_source) > 2000)
                        html = driver.page_source
                    except Exception:
                        html = ""

                items = _zs_pick_list_items_from_html(html, url)
                if not items:
                    break

                for title, href, dt in items:
                    ann_type = classify(title)
                    if ann_type not in ("招标公告", "中标公告"):
                        continue

                    pub_date = dt[:10] if dt else ""
                    if date_start and date_end and pub_date:
                        if pub_date < date_start or pub_date > date_end:
                            continue

                    if href in seen_url:
                        continue
                    seen_url.add(href)

                    detail_text = get_detail_text(href, driver=driver)

                    detail_html = ""
                    try:
                        rr = _SESSION.get(href, timeout=20)
                        if rr.status_code == 200:
                            detail_html = rr.text or ""
                    except Exception:
                        pass
                    if not detail_html:
                        try:
                            driver.get(href)
                            WebDriverWait(driver, 10).until(lambda d: d.page_source and len(d.page_source) > 2000)
                            detail_html = driver.page_source
                        except Exception:
                            detail_html = ""

                    if ann_type == "招标公告":
                        fields = parse_bidding_fields(detail_text)
                        due_str = fields.get("投标截止", "暂无")
                        due_dt  = _to_datetime(due_str if due_str != "暂无" else "")

                        keep = True
                        now = datetime.now()
                        if SKIP_EXPIRED and due_dt and due_dt < now:
                            keep = False
                        if keep and DUE_FILTER_DAYS > 0 and due_dt and due_dt > now + timedelta(days=DUE_FILTER_DAYS):
                            keep = False

                        if keep:
                            all_bidding.append({
                                "站点": "zsxtzb聚合",
                                "关键词": kw,
                                "公告标题": title,
                                "公告发布时间": pub_date or "暂无",
                                "信息来源": "zsxtzb聚合搜索",
                                "投标截止": due_str,
                                "金额": fields["金额"],
                                "采购人": fields["采购人"],
                                "联系人": fields["联系人"],
                                "联系电话": fields["联系电话"],
                                "简要摘要": fields["简要摘要"],
                                "公告网址": href,
                            })
                    else:
                        fields = parse_award_fields(detail_html, detail_text)
                        all_award.append({
                            "站点": "zsxtzb聚合",
                            "关键词": kw,
                            "标题": title,
                            "发布时间": pub_date or "暂无",
                            "信息来源": "zsxtzb聚合搜索",
                            "中标日期": fields.get("中标日期", pub_date or "暂无"),
                            "中标公司": fields.get("中标公司", "暂无"),
                            "中标金额": fields.get("中标金额", "暂无"),
                            "评审得分": fields.get("评审得分", "暂无"),
                            "中标网址": href,
                        })

    finally:
        driver.quit()

    return all_bidding, all_award


# ================== Markdown 输出（极简卡片式） ==================
def md_escape(s: str) -> str:
    if not isinstance(s, str):
        s = str(s)
    return s.replace("|", "\\|")

def _mk_link(text: str, url: str):
    t = md_escape(text or "")
    u = (url or "").strip()
    return f"[{t}]({u})" if u.startswith("http") else t

def _sort_key_time(s: str):
    if not s or s == "暂无":
        return datetime(1970, 1, 1)
    ns = _normalize_date_string(s)
    dt = _to_datetime(ns)
    return dt or datetime(1970, 1, 1)

def _merge_by_url(items, url_field, kw_field="关键词"):
    mp = {}
    for it in items:
        u = (it.get(url_field) or "").strip()
        if not u:
            continue
        if u not in mp:
            mp[u] = it
            mp[u][kw_field] = str(it.get(kw_field, "") or "").strip()
        else:
            old = mp[u]
            kws = set([x.strip() for x in (old.get(kw_field, "") or "").split("，") if x.strip()])
            kws2 = set([x.strip() for x in (it.get(kw_field, "") or "").split("，") if x.strip()])
            merged = [x for x in (list(kws | kws2)) if x]
            old[kw_field] = "，".join(sorted(merged))

            t_old = _sort_key_time(old.get("公告发布时间") or old.get("发布时间"))
            t_new = _sort_key_time(it.get("公告发布时间") or it.get("发布时间"))
            if t_new > t_old:
                for k, v in it.items():
                    if k not in (kw_field,):
                        old[k] = v
    return list(mp.values())

def format_bidding_markdown(items, date_start, date_end):
    items = _merge_by_url(items, "公告网址", "关键词")
    items = sorted(items, key=lambda x: _sort_key_time(x.get("公告发布时间")), reverse=True)

    by_site = {}
    for it in items:
        by_site[it.get("站点","未知")] = by_site.get(it.get("站点","未知"), 0) + 1

    head = f"### 🧾【招标公告】{date_start} ~ {date_end}  共 {len(items)} 条"
    stat = "｜".join([f"{k}:{v}" for k, v in by_site.items()]) if by_site else "暂无"
    lines = [head, f"> 站点统计：{stat}", ""]

    for idx, it in enumerate(items, 1):
        show = _mk_link(it.get("公告标题",""), it.get("公告网址",""))
        due  = it.get("投标截止","暂无")
        amt  = it.get("金额","暂无")
        pur  = it.get("采购人","暂无")
        ctc  = it.get("联系人","暂无")
        tel  = it.get("联系电话","暂无")
        pub  = it.get("公告发布时间","暂无")
        src  = it.get("信息来源","暂无")
        site = it.get("站点","暂无")
        kw   = it.get("关键词","")
        brief = it.get("简要摘要","暂无")

        lines.append(f"**{idx}. {show}**")
        lines.append(f"- ⏱️ 截止：{md_escape(due)}")
        lines.append(f"- 💰 预算/限价：{md_escape(amt)}")
        lines.append(f"- 🧩 采购人：{md_escape(pur)}")
        lines.append(f"- 👤 联系：{md_escape(ctc)}（{md_escape(tel)}）")
        lines.append(f"- 🗂️ 来源：{md_escape(site)}｜{md_escape(src)}｜发布：{md_escape(pub)}｜关键词：{md_escape(kw)}")
        lines.append(f"- 📝 摘要：{md_escape(brief)}")
        lines.append("")

    return "\n".join(lines).strip()

def format_award_markdown(items, date_start, date_end):
    items = _merge_by_url(items, "中标网址", "关键词")
    items = sorted(items, key=lambda x: _sort_key_time(x.get("发布时间")), reverse=True)

    by_site = {}
    for it in items:
        by_site[it.get("站点","未知")] = by_site.get(it.get("站点","未知"), 0) + 1

    head = f"### ✅【中标/成交结果】{date_start} ~ {date_end}  共 {len(items)} 条"
    stat = "｜".join([f"{k}:{v}" for k, v in by_site.items()]) if by_site else "暂无"
    lines = [head, f"> 站点统计：{stat}", ""]

    for idx, it in enumerate(items, 1):
        show = _mk_link(it.get("标题",""), it.get("中标网址",""))
        awd_date = it.get("中标日期","暂无")
        sup      = it.get("中标公司","暂无")
        amt      = it.get("中标金额","暂无")
        score    = it.get("评审得分","暂无")
        pub      = it.get("发布时间","暂无")
        src      = it.get("信息来源","暂无")
        site     = it.get("站点","暂无")
        kw       = it.get("关键词","")

        lines.append(f"**{idx}. {show}**")
        lines.append(f"- 📅 中标日期：{md_escape(awd_date)}")
        lines.append(f"- 🏷️ 中标单位：{md_escape(sup)}")
        lines.append(f"- 💰 中标金额：{md_escape(amt)}")
        if score and score != "暂无":
            lines.append(f"- 🧮 评审得分：{md_escape(score)}")
        lines.append(f"- 🗂️ 来源：{md_escape(site)}｜{md_escape(src)}｜发布：{md_escape(pub)}｜关键词：{md_escape(kw)}")
        lines.append("")

    return "\n".join(lines).strip()

def split_and_send(title_prefix: str, full_text: str, chunk_size=DINGTALK_CHUNK):
    full_text = full_text or ""
    if not full_text.strip():
        return
    n = max(1, math.ceil(len(full_text) / chunk_size))
    for i in range(n):
        part = full_text[i*chunk_size:(i+1)*chunk_size]
        part_title = f"{title_prefix}（{i+1}/{n}）" if n > 1 else title_prefix
        send_to_dingtalk_markdown(part_title, part)


# ================== MAIN ==================
if __name__ == '__main__':
    date_start, date_end = get_date_range()
    print(f"采集日期：{date_start} ~ {date_end}")

    all_bidding, all_award = [], []

    if CRAWL_BEIJING:
        b1, a1 = crawl_beijing(KEYWORDS, max_pages=10, date_start=date_start, date_end=date_end)
        all_bidding.extend(b1)
        all_award.extend(a1)

    if CRAWL_ZSXTZB:
        b2, a2 = crawl_zsxtzb_search(KEYWORDS, max_pages=8, date_start=date_start, date_end=date_end)
        all_bidding.extend(b2)
        all_award.extend(a2)

    # ✅ 不推汇总！只推明细（有内容才推）
    all_bidding_u = _merge_by_url(all_bidding, "公告网址", "关键词")
    all_award_u   = _merge_by_url(all_award, "中标网址", "关键词")

    if all_bidding_u:
        md_bid = format_bidding_markdown(all_bidding_u, date_start, date_end)
        split_and_send("招标公告", md_bid)

    if all_award_u:
        md_awd = format_award_markdown(all_award_u, date_start, date_end)
        split_and_send("中标/成交结果", md_awd)

    print("✔ 完成")

