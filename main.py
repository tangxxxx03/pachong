# -*- coding: utf-8 -*-
"""
外包/派遣：招标 & 中标采集（北京公共资源交易平台 + zsxtzb.cn 搜索）
—— 清爽输出 + 字段增强版（完整代码）
"""

import os, re, time, math, hmac, base64, hashlib
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import urlparse, urljoin, quote_plus

import pandas as pd
import pdfplumber
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


# ================== 固定配置（优先读环境变量） ==================
DINGTALK_WEBHOOK =  "https://oapi.dingtalk.com/robot/send?access_token=0d9943129de109072430567e03689e8c7d9012ec160e023cfa94cf6cdc703e49"
DINGTALK_SECRET  =  "SEC820601d706f1894100cbfc500114a1c0977a62cfe72f9ea2b5ac2909781753d0"  # 未开启加签就会是空字符串

KEYWORDS        = ["外包", "派遣"]
CRAWL_BEIJING   = True
CRAWL_ZSXTZB    = True

DUE_FILTER_DAYS = 30  # 只保留未来30天内的公告
SKIP_EXPIRED    = False  # 不丢弃已过期的招标

HEADLESS        = True

BRIEF_MAX_LEN   = 120
EXTRA_MAX_LINES = 3
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


# ================== 分类 ==================
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


def extract_deadline(detail_text: str) -> str:
    txt = _safe_text(detail_text)

    pats = [
        r"(?:投标(?:文件)?|递交(?:响应)?文件|响应文件提交|报价|报名|获取招标文件)\s*截止(?:时间|日期)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
        r"(?:截止(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})(?=.*?(?:投标|递交|响应|报价|报名))",
        r"(?:提交|递交)\s*截止(?:时间|日期)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
        r"(?:截止至)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
    ]
    s = _pick_first(txt, pats)
    norm = _normalize_date_string(s)
    if norm:
        return norm

    s2 = _pick_first(txt, [r"(?:开标(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})"])
    norm2 = _normalize_date_string(s2)
    return norm2 or ""


def extract_project_brief(detail_text: str, max_len: int = 120) -> str:
    txt = _safe_text(detail_text)
    blocks = []

    m = re.search(r"项目概况\s*([\s\S]{0,900}?)(?=\n\s*[一二三四五六七八九十]、|\n\s*一、|$)", txt)
    if m:
        blocks.append(m.group(1))

    m2 = re.search(r"(?:项目基本情况|一、项目基本情况)\s*([\s\S]{0,900}?)(?=\n\s*[二三四五六七八九十]、|\n\s*二、|$)", txt)
    if m2:
        blocks.append(m2.group(1))

    m3 = re.search(r"(?:采购需求|服务范围|项目内容|服务内容)\s*[:：]?\s*([\s\S]{0,300}?)\n", txt)
    if m3:
        blocks.append(m3.group(1))

    block = ""
    for b in blocks:
        b = re.sub(r"\s+", " ", (b or "")).strip()
        b = re.sub(r"^[：:、\-，。.\s]*", "", b).strip()
        if len(b) >= 20:
            block = b
            break

    if not block:
        plain = re.sub(r"\s+", " ", txt)
        block = plain[:max_len]

    block = block[:max_len] + ("..." if len(block) > max_len else "")
    return block.strip()


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
        return "\n".join(pages)
    except Exception as e:
        print("PDF 读取失败：", e)
        return ""


# ================== 详情文本抽取（增强：更多容器 + 附件 PDF 兜底） ==================
def extract_detail_text_with_pdf_fallback(driver, page_html: str, page_url: str):
    xps = [
        "//*[@id='vsb_content']",
        "//*[@id='zoom']",
        "//*[@class='content']",
        "//*[@class='article']",
        "//*[@class='detail']",
        "//*[@class='cont']",
        "//*[@id='xxnr']",
        "//*[@id='info']",
        "//article",
        "//main",
    ]
    for xp in xps:
        try:
            t = driver.find_element(By.XPATH, xp).text
            if t and len(t.strip()) > 80:
                return t
        except Exception:
            pass

    try:
        links = re.findall(r'href=["\'](.*?)["\']', page_html, flags=re.I)
        pdfs = []
        for h in links:
            absu = urljoin(page_url, (h or "").strip())
            if absu.lower().endswith(".pdf"):
                pdfs.append(absu)

        if not pdfs:
            for a in driver.find_elements(By.XPATH, "//a"):
                try:
                    txt = (a.text or "").strip()
                    href = a.get_attribute("href") or ""
                    if href and (("PDF" in (txt.upper())) or ("附件" in txt) or ("下载" in txt)):
                        absu = urljoin(page_url, href)
                        if absu.lower().endswith(".pdf"):
                            pdfs.append(absu)
                except Exception:
                    continue

        for pdf_url in pdfs[:3]:
            pdf_text = fetch_pdf_text(pdf_url, referer=page_url)
            if pdf_text and len(pdf_text.strip()) > 80:
                return pdf_text
    except Exception:
        pass

    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return ""


# ================== 招标字段解析（增强：预算/采购人/代理/地址/联系人/电话/截止/摘要） ==================
def parse_bidding_fields(detail_text: str):
    txt = _safe_text(detail_text)

    amount = _pick_first(txt, [
        r"(?:预算金额|采购预算)\s*[:：]?\s*([0-9\.,，]+\s*(?:万元|元))",
        r"(?:最高限价|控制价)\s*[:：]?\s*([0-9\.,，]+\s*(?:万元|元))",
    ])
    amount = _normalize_amount_text(amount) if amount else "暂无"

    purchaser = _pick_first(txt, [
        r"(?:采购人|采购单位|招标人)\s*[:：]?\s*([^\n\r，。;；]{2,60})",
    ])
    purchaser = purchaser or "暂无"

    agent = _pick_first(txt, [
        r"(?:采购代理机构|代理机构|招标代理)\s*[:：]?\s*([^\n\r，。;；]{2,60})",
    ])
    agent = agent or "暂无"

    address = _pick_first(txt, [
        r"(?:地址|项目地点|服务地点|实施地点)\s*[:：]?\s*([^\n\r。；;]{5,80})",
    ])
    address = address or "暂无"

    contact = "暂无"
    phone   = "暂无"
    m_cp = re.search(
        r"项目联系人[：:\s]*([^\s、，。;；]+)[\s\S]{0,120}?"
        r"(?:电\s*话|联系电话|联系方式)[：:\s]*([0-9\-－—\s]{6,})",
        txt, re.S
    )
    if m_cp:
        contact = m_cp.group(1).strip()
        phone = re.sub(r"\s+", "", m_cp.group(2)).replace("－", "-").replace("—", "-")
    else:
        c2 = _pick_first(txt, [
            r"(?:联系人|项目联系人|采购人联系人)\s*[:：]?\s*([^\s、，。;；]{2,20})"
        ])
        p2 = _pick_first(txt, [
            r"(?:联系电话|联系方式|电\s*话)\s*[:：]?\s*([0-9\-－—\s]{6,})"
        ])
        if c2: contact = c2
        if p2: phone = re.sub(r"\s+", "", p2).replace("－", "-").replace("—", "-")

    deadline = extract_deadline(txt) or "暂无"

    brief = extract_project_brief(txt, max_len=BRIEF_MAX_LEN) or "暂无"

    extra = []
    m_get = re.search(r"(潜在投标人.*?获取招标文件.*?)(?=。\s|\n)", txt)
    if m_get:
        extra.append(re.sub(r"\s+", " ", m_get.group(1)).strip())

    m_term = re.search(r"(?:服务期限|合同履行期限|履约期限)\s*[:：]?\s*([^\n\r。；;]{3,60})", txt)
    if m_term:
        extra.append(f"期限：{m_term.group(1).strip()}")

    return {
        "金额": amount,
        "采购人": purchaser,
        "代理机构": agent,
        "地址": address,
        "联系人": contact,
        "联系电话": phone,
        "简要摘要": brief,
        "投标截止": deadline,
        "扩展信息": extra[:EXTRA_MAX_LINES],
    }


# ================== 中标解析 ==================
def parse_award_from_tables(html: str):
    supplier = amount = score = content = "暂无"
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
        c
