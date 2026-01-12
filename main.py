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


# ================== 固定配置（不读环境变量） ==================
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=6e945607bb71c2fd9bb3399c6424fa7dece4b9798d2a8ff74b0b71ab47c9d182"
DINGTALK_SECRET  = ""  # 若开启“加签”，填入密钥；未开启则留空字符串

KEYWORDS        = ["外包", "派遣"]
CRAWL_BEIJING   = True
CRAWL_ZSXTZB    = True

# 只保留未来 N 天内截止的招标；<=0 表示不过滤
DUE_FILTER_DAYS = 30
# 丢弃已过期的招标（仅当能解析出截止时间）
SKIP_EXPIRED    = True

HEADLESS        = True

# 输出控制：摘要截断长度、单条卡片最多显示几行“扩展字段”
BRIEF_MAX_LEN   = 120
EXTRA_MAX_LINES = 3

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


# ================== 分类（保持你原逻辑，略增强） ==================
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
    """把金额里常见的空格/逗号去掉，保留原单位"""
    if not s: return ""
    s = str(s).replace("，", ",").replace(",", "")
    s = re.sub(r"\s+", "", s)
    return s

def _normalize_date_string(s: str) -> str:
    """把 '2026年1月9日 09:30' / '2026-01-09 9:30' 规整成 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' """
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
    """多套正则：返回第一个命中的 group(1)"""
    for pat in patterns:
        m = re.search(pat, text, re.S | re.I)
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return ""


# ================== 截止时间抽取（增强：更多触发词） ==================
def extract_deadline(detail_text: str) -> str:
    txt = _safe_text(detail_text)

    pats = [
        # 投标/响应/递交 截止
        r"(?:投标(?:文件)?|递交(?:响应)?文件|响应文件提交|报价|报名|获取招标文件)\s*截止(?:时间|日期)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
        r"(?:截止(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})(?=.*?(?:投标|递交|响应|报价|报名))",
        # “提交截止”“截止至”
        r"(?:提交|递交)\s*截止(?:时间|日期)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
        r"(?:截止至)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
    ]
    s = _pick_first(txt, pats)
    norm = _normalize_date_string(s)
    if norm:
        return norm

    # 兜底：开标时间
    s2 = _pick_first(txt, [r"(?:开标(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})"])
    norm2 = _normalize_date_string(s2)
    return norm2 or ""


# ================== 摘要抽取（增强：多段名兜底） ==================
def extract_project_brief(detail_text: str, max_len: int = 120) -> str:
    txt = _safe_text(detail_text)
    blocks = []

    # 1) 项目概况段
    m = re.search(r"项目概况\s*([\s\S]{0,900}?)(?=\n\s*[一二三四五六七八九十]、|\n\s*一、|$)", txt)
    if m:
        blocks.append(m.group(1))

    # 2) 项目基本情况
    m2 = re.search(r"(?:项目基本情况|一、项目基本情况)\s*([\s\S]{0,900}?)(?=\n\s*[二三四五六七八九十]、|\n\s*二、|$)", txt)
    if m2:
        blocks.append(m2.group(1))

    # 3) 采购需求/服务范围
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
    # 先尝试更常见内容容器
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

    # 再找附件 pdf
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

    # 兜底：整页 body
    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return ""


# ================== 招标字段解析（增强：预算/采购人/代理/地址/联系人/电话/截止/摘要） ==================
def parse_bidding_fields(detail_text: str):
    txt = _safe_text(detail_text)

    # 预算/最高限价/控制价
    amount = _pick_first(txt, [
        r"(?:预算金额|采购预算)\s*[:：]?\s*([0-9\.,，]+\s*(?:万元|元))",
        r"(?:最高限价|控制价)\s*[:：]?\s*([0-9\.,，]+\s*(?:万元|元))",
        r"(?:项目预算)\s*[:：]?\s*([0-9\.,，]+\s*(?:万元|元))",
    ])
    amount = _normalize_amount_text(amount) if amount else "暂无"

    # 采购人
    purchaser = _pick_first(txt, [
        r"(?:采购人|采购单位|招标人)\s*[:：]?\s*([^\n\r，。;；]{2,60})",
    ])
    purchaser = purchaser or "暂无"

    # 代理机构
    agent = _pick_first(txt, [
        r"(?:采购代理机构|代理机构|招标代理)\s*[:：]?\s*([^\n\r，。;；]{2,60})",
    ])
    agent = agent or "暂无"

    # 地址（采购人地址/项目地点）
    address = _pick_first(txt, [
        r"(?:地址|项目地点|服务地点|实施地点)\s*[:：]?\s*([^\n\r。；;]{5,80})",
    ])
    address = address or "暂无"

    # 联系人+电话（优先“项目联系人”块）
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

    # 截止
    deadline = extract_deadline(txt) or "暂无"

    # 摘要
    brief = extract_project_brief(txt, max_len=BRIEF_MAX_LEN) or "暂无"

    # 扩展字段（少量关键字命中时才加）
    extra = []
    # 获取文件方式/平台提示（经常对你们很有用）
    m_get = re.search(r"(潜在投标人.*?获取招标文件.*?)(?=。\s|\n)", txt)
    if m_get:
        extra.append(re.sub(r"\s+", " ", m_get.group(1)).strip())

    # 服务期限/合同履行期限
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


# ================== 中标解析：表格优先 + 文本兜底（增强） ==================
def _num_from_any(v):
    if v in (None, "", "暂无"): return None
    s = str(v).replace(",", "").replace("，", "")
    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None

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

            # 单位从列名或单元格猜
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

    return {
        "中标公司": supplier,
        "中标金额": amount,
        "评审得分": (score or "暂无").rstrip("分"),
        "中标内容": content or "暂无",
    }

def parse_award_from_text(detail_text: str):
    txt = _safe_text(detail_text)

    supplier = _pick_first(txt, [
        r"(?:中标(?:供应商|人|单位)|成交(?:供应商|人|单位)|供应商名称)\s*[：:]\s*([^\n\r，。；;]{2,80})",
        r"(?:成交单位)\s*[：:]\s*([^\n\r，。；;]{2,80})",
    ])

    amount = _pick_first(txt, [
        r"(?:中标(?:价|金额)|成交(?:价|金额)|评审报价|成交价)\s*[：:]\s*([0-9\.,，]+\s*(?:万元|元)?)",
        r"(?:合同金额)\s*[：:]\s*([0-9\.,，]+\s*(?:万元|元)?)",
    ])
    amount = _normalize_amount_text(amount) if amount else "暂无"

    score = _pick_first(txt, [
        r"(?:评审(?:得分|分值)|综合得分|最终得分|得分)\s*[：:]\s*([0-9\.]+)",
    ])
    score = (score or "暂无").rstrip("分")

    content = _pick_first(txt, [
        r"(?:采购内容|采购需求|项目概况|服务内容|中标内容)\s*[：:]\s*([^\n\r]{6,120})",
    ])
    content = content or "暂无"

    return {
        "中标公司": supplier or "暂无",
        "中标金额": amount,
        "评审得分": score,
        "中标内容": content,
    }


# ================== 原招标网址（更稳：剔除“列表/频道/返回”等） ==================
def choose_origin_notice_url(detail_html: str, current_url: str) -> str:
    if not detail_html:
        return "暂无"

    hrefs = re.findall(r'<a[^>]+href=["\'](.*?)["\']', detail_html, flags=re.I)
    if not hrefs:
        return "暂无"

    cur = urlparse(current_url or "")
    cur_dom = f"{cur.scheme}://{cur.netloc}" if cur.scheme and cur.netloc else ""

    clean = []
    for h in hrefs:
        h = (h or "").strip()
        if not h or h.startswith("#") or h.lower().startswith("javascript"):
            continue
        absu = urljoin(current_url or "", h)
        if absu == current_url:
            continue
        clean.append(absu)

    if not clean:
        return "暂无"

    kw_good = ["招标", "采购", "公告", "公开", "zb", "zhaobiao", "notice"]
    bad_words = ["首页", "返回", "上一页", "下一页", "更多", "下载中心", "栏目", "频道", "列表", "index", "list", "channel", "column"]
    good_exts = [".html", ".shtml", ".htm", ".pdf"]

    def score(u: str) -> tuple:
        p = urlparse(u)
        low = u.lower()
        s = 0
        if any(k in u for k in kw_good) or any(k in low for k in ["zbgg", "zhaobiao", "cgxx", "notice"]):
            s += 6
        if any(low.endswith(ext) for ext in good_exts):
            s += 3
        if cur_dom and (f"{p.scheme}://{p.netloc}" == cur_dom):
            s += 2
        depth = len([seg for seg in p.path.split("/") if seg])
        s += min(depth, 6)
        if re.search(r"(20\d{2}[-/_.]?\d{2}([-/_.]?\d{2})?)", low):
            s += 2
        if any(b in u for b in bad_words):
            s -= 6
        return (s, -len(u))

    best = sorted(set(clean), key=score, reverse=True)[0]

    # 最后再做一次“像不像公告页”的兜底
    if not any(best.lower().endswith(ext) for ext in good_exts):
        if not any(k in best for k in kw_good) and not any(k in best.lower() for k in ["zbgg", "zhaobiao", "cgxx", "notice"]):
            return "暂无"

    return best


def parse_award_fields(detail_html: str, detail_text: str, current_url: str = ""):
    # 1) 表格优先
    data = parse_award_from_tables(detail_html)

    # 2) 表格失败再用文本
    if data.get("中标公司") == "暂无" and data.get("中标金额") == "暂无":
        data = parse_award_from_text(detail_text)

    # 3) 原招标网址
    data["原招标网址"] = choose_origin_notice_url(detail_html, current_url) or "暂无"

    # 4) 中标日期：优先字段，再兜底页面内日期
    txt = _safe_text(detail_text or "")
    award_date = _pick_first(txt, [
        r"(?:公告日期|公示时间|发布时间|成交日期|中标日期)\s*[：:]\s*([0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2})",
    ])
    if not award_date:
        award_date = _date_in_text(txt)

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
    driver.implicitly_wait(5)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(45)
    return driver


# ================== 站点一：北京公共资源交易平台 ==================
def crawl_beijing(keywords, max_pages=10, date_start=None, date_end=None):
    driver = _build_driver()
    all_bidding, all_award, seen_links = [], [], set()

    try:
        for kw in keywords:
            url = f"https://ggzyfw.beijing.gov.cn/elasticsearch/index.jsp?qt={kw}"
            driver.get(url)
            time.sleep(3.0)

            # 时间过滤：一周（尽量点，点不到就算）
            try:
                driver.find_element(By.XPATH, "//span[contains(text(),'时间不限')]").click()
                time.sleep(0.6)
                driver.find_element(By.ID, "week").click()
                time.sleep(1.0)
            except Exception:
                pass

            for page in range(1, max_pages + 1):
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

                        # 列表摘要
                        snippet = ""
                        try:
                            snippet = c.find_element(By.CLASS_NAME, "cs_search_content_p").text
                        except Exception:
                            pass

                        # 信息来源 + 发布时间（列表行）
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

                        # 日期过滤（按发布日期）
                        if date_start and date_end and pub_date:
                            if pub_date < date_start or pub_date > date_end:
                                continue

                        # 详情链接
                        url_link = ""
                        try:
                            url_link = title_el.find_element(By.TAG_NAME, "a").get_attribute("href")
                        except Exception:
                            url_link = ""

                        if url_link and url_link in seen_links:
                            continue
                        if url_link:
                            seen_links.add(url_link)

                        detail_text, detail_html = "", ""
                        if url_link:
                            win = driver.current_window_handle
                            driver.execute_script('window.open(arguments[0])', url_link)
                            driver.switch_to.window(driver.window_handles[-1])
                            time.sleep(1.2)

                            detail_html = driver.page_source
                            detail_text = extract_detail_text_with_pdf_fallback(driver, detail_html, url_link) or snippet

                            # 详情页“发布来源”兜底覆盖
                            if detail_text:
                                m_src = re.search(r"发布来源[：:\s]*([^\n\r]+)", detail_text)
                                if m_src:
                                    info_source = (m_src.group(1).strip() or info_source)

                            driver.close()
                            driver.switch_to.window(win)

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
                                    "代理机构": fields["代理机构"],
                                    "联系人": fields["联系人"],
                                    "联系电话": fields["联系电话"],
                                    "地址": fields["地址"],
                                    "简要摘要": fields["简要摘要"],
                                    "扩展信息": fields.get("扩展信息", []),
                                    "公告网址": url_link or "暂无",
                                })
                        else:
                            fields = parse_award_fields(detail_html, detail_text, current_url=url_link)
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
                                "中标内容": fields.get("中标内容", "暂无"),
                                "原招标网址": fields.get("原招标网址", "暂无"),
                                "中标网址": url_link or "暂无",
                            })

                    except Exception as ex:
                        print("解析一条出错：", ex)

                # 翻页
                try:
                    next_btn = driver.find_element(By.LINK_TEXT, "下一页")
                    if "disable" in (next_btn.get_attribute("class") or "") or next_btn.get_attribute("aria-disabled") == 'true':
                        break
                    if page < max_pages:
                        driver.execute_script("arguments[0].click();", next_btn)
                        time.sleep(1.0)
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

def _zs_pick_list_items(driver):
    items = []

    # 常见 li 列表
    lis = driver.find_elements(By.XPATH, "//div[contains(@class,'search') or contains(@class,'result') or contains(@class,'list')]//li[a]")
    for li in lis:
        try:
            a = li.find_element(By.TAG_NAME, "a")
            title = a.text.strip()
            href = a.get_attribute("href")
            raw = li.text
            dt = _date_in_text(raw)
            if title and href:
                items.append((title, href, dt))
        except Exception:
            pass

    # h3 列表
    if not items:
        blocks = driver.find_elements(By.XPATH, "//div[contains(@class,'search') or contains(@class,'result') or contains(@class,'list')]//h3[a]")
        for b in blocks:
            try:
                a = b.find_element(By.TAG_NAME, "a")
                title = a.text.strip()
                href = a.get_attribute("href")
                raw = b.text
                dt = _date_in_text(raw)
                if not dt:
                    try:
                        sib = b.find_element(By.XPATH, "./following-sibling::*[1]")
                        dt = _date_in_text(sib.text)
                    except Exception:
                        pass
                if title and href:
                    items.append((title, href, dt))
            except Exception:
                pass

    # 兜底：抓所有链接
    if not items:
        anchors = driver.find_elements(By.XPATH, "//a")
        bad = ["首页", "上一页", "下一页", "末页", "更多", "下载", "返回"]
        for a in anchors:
            try:
                title = (a.text or "").strip()
                href = a.get_attribute("href") or ""
                if not title or not href:
                    continue
                if any(b in title for b in bad):
                    continue
                parent_text = a.find_element(By.XPATH, "./ancestor::*[self::li or self::div][1]").text
                dt = _date_in_text(parent_text)
                items.append((title, href, dt))
            except Exception:
                pass

    # 去重（同 href）
    uniq = []
    seen = set()
    for t, h, d in items:
        if h in seen:
            continue
        seen.add(h)
        uniq.append((t, h, d))
    return uniq

def _zs_next_page(driver, cur_page):
    for xp in [
        "//a[contains(.,'下一页') or contains(.,'下页')]",
        "//a[contains(@class,'next')]",
        f"//a[normalize-space(text())='{cur_page+1}']",
        f"//button[normalize-space(text())='{cur_page+1}']"
    ]:
        try:
            el = driver.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].click();", el)
            time.sleep(1.0)
            return True
        except Exception:
            pass
    return False

def crawl_zsxtzb_search(keywords, max_pages=8, date_start=None, date_end=None):
    driver = _build_driver()
    all_bidding, all_award, seen = [], [], set()

    try:
        for kw in keywords:
            page = 1
            while page <= max_pages:
                url = _zs_search_url(kw, page)
                print(f"[zsxtzb] {kw} 第{page}页 -> {url}")
                driver.get(url)
                time.sleep(1.4)

                items = _zs_pick_list_items(driver)
                if not items:
                    break

                for title, href, dt in items:
                    ann_type = classify(title)
                    if ann_type not in ("招标公告", "中标公告"):
                        continue
                    if href in seen:
                        continue
                    seen.add(href)

                    pub_date = dt[:10] if dt else ""
                    if date_start and date_end and pub_date:
                        if pub_date < date_start or pub_date > date_end:
                            continue

                    win = driver.current_window_handle
                    driver.execute_script('window.open(arguments[0])', href)
                    driver.switch_to.window(driver.window_handles[-1])
                    time.sleep(1.2)

                    detail_html = driver.page_source
                    detail_text = extract_detail_text_with_pdf_fallback(driver, detail_html, href)

                    driver.close()
                    driver.switch_to.window(win)

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
                                "代理机构": fields["代理机构"],
                                "联系人": fields["联系人"],
                                "联系电话": fields["联系电话"],
                                "地址": fields["地址"],
                                "简要摘要": fields["简要摘要"],
                                "扩展信息": fields.get("扩展信息", []),
                                "公告网址": href or "暂无",
                            })
                    else:
                        fields = parse_award_fields(detail_html, detail_text, current_url=href)
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
                            "中标内容": fields.get("中标内容", "暂无"),
                            "原招标网址": fields.get("原招标网址", "暂无"),
                            "中标网址": href or "暂无",
                        })

                if not _zs_next_page(driver, page):
                    break
                page += 1

    finally:
        driver.quit()

    return all_bidding, all_award


# ================== Markdown 输出（清爽卡片式） ==================
def md_escape(s: str) -> str:
    if not isinstance(s, str):
        s = str(s)
    return s.replace("|", "\\|")

def _mk_link(text: str, url: str):
    t = md_escape(text or "")
    u = (url or "").strip()
    return f"[{t}]({u})" if u.startswith("http") else t

def _sort_key_time(s: str):
    """用于排序：优先按 'YYYY-MM-DD HH:MM' 再按 'YYYY-MM-DD' """
    if not s or s == "暂无":
        return datetime(1970, 1, 1)
    ns = _normalize_date_string(s)
    dt = _to_datetime(ns)
    return dt or datetime(1970, 1, 1)

def _dedup_items(items, key_fields):
    """
    去重：按 (站点, 标题, 链接) 或用户指定字段组合
    """
    seen = set()
    out = []
    for it in items:
        key = tuple((it.get(k) or "").strip() for k in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def format_bidding_markdown(items, date_start, date_end):
    items = _dedup_items(items, ["站点", "公告标题", "公告网址"])
    items = sorted(items, key=lambda x: _sort_key_time(x.get("公告发布时间")), reverse=True)

    # 顶部统计
    by_site = {}
    for it in items:
        by_site[it.get("站点","未知")] = by_site.get(it.get("站点","未知"), 0) + 1

    head = f"### 🧾【招标公告】{date_start} ~ {date_end}  共 {len(items)} 条"
    stat = "｜".join([f"{k}:{v}" for k, v in by_site.items()]) if by_site else "暂无"
    lines = [head, f"> 站点统计：{stat}", ""]

    for idx, it in enumerate(items, 1):
        title = it.get("公告标题","")
        url   = it.get("公告网址","")
        show  = _mk_link(title, url)

        pub   = it.get("公告发布时间","暂无")
        due   = it.get("投标截止","暂无")
        amt   = it.get("金额","暂无")
        pur   = it.get("采购人","暂无")
        agt   = it.get("代理机构","暂无")
        ctc   = it.get("联系人","暂无")
        tel   = it.get("联系电话","暂无")
        src   = it.get("信息来源","暂无")
        site  = it.get("站点","暂无")
        kw    = it.get("关键词","")

        brief = it.get("简要摘要","暂无")
        extras = it.get("扩展信息", []) or []

        lines.append(f"**{idx}. {show}**")
        lines.append(f"- ⏱️ 截止：{md_escape(due)}")
        lines.append(f"- 💰 预算/限价：{md_escape(amt)}")
        lines.append(f"- 🧩 采购人：{md_escape(pur)}")
        if agt and agt != "暂无":
            lines.append(f"- 🏢 代理：{md_escape(agt)}")
        lines.append(f"- 👤 联系：{md_escape(ctc)}（{md_escape(tel)}）")
        lines.append(f"- 🗂️ 来源：{md_escape(site)}｜{md_escape(src)}｜发布：{md_escape(pub)}｜关键词：{md_escape(kw)}")
        lines.append(f"- 📝 摘要：{md_escape(brief)}")

        for ex in extras[:EXTRA_MAX_LINES]:
            ex = re.sub(r"\s+", " ", ex).strip()
            if ex:
                lines.append(f"- 🔎 {md_escape(ex)}")
        lines.append("")  # 空行分隔

    return "\n".join(lines).strip()

def format_award_markdown(items, date_start, date_end):
    items = _dedup_items(items, ["站点", "标题", "中标网址"])
    items = sorted(items, key=lambda x: _sort_key_time(x.get("发布时间")), reverse=True)

    by_site = {}
    for it in items:
        by_site[it.get("站点","未知")] = by_site.get(it.get("站点","未知"), 0) + 1

    head = f"### ✅【中标/成交结果】{date_start} ~ {date_end}  共 {len(items)} 条"
    stat = "｜".join([f"{k}:{v}" for k, v in by_site.items()]) if by_site else "暂无"
    lines = [head, f"> 站点统计：{stat}", ""]

    for idx, it in enumerate(items, 1):
        title = it.get("标题","")
        url   = it.get("中标网址","")
        show  = _mk_link(title, url)

        awd_date = it.get("中标日期","暂无")
        sup      = it.get("中标公司","暂无")
        amt      = it.get("中标金额","暂无")
        score    = it.get("评审得分","暂无")
        content  = it.get("中标内容","暂无")

        src   = it.get("信息来源","暂无")
        site  = it.get("站点","暂无")
        pub   = it.get("发布时间","暂无")
        kw    = it.get("关键词","")

        yz = (it.get("原招标网址","") or "").strip()
        yz_line = f"[点击跳转]({yz})" if yz.startswith("http") else "暂无"

        lines.append(f"**{idx}. {show}**")
        lines.append(f"- 📅 中标日期：{md_escape(awd_date)}")
        lines.append(f"- 🏷️ 中标单位：{md_escape(sup)}")
        lines.append(f"- 💰 中标金额：{md_escape(amt)}")
        lines.append(f"- 🧮 评审得分：{md_escape(score)}")
        if content and content != "暂无":
            lines.append(f"- 📌 中标内容：{md_escape(content)}")
        lines.append(f"- 🔗 原招标网址：{yz_line}")
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
    print("Webhook 状态：", "已配置（加签）" if (DINGTALK_WEBHOOK and DINGTALK_SECRET) else ("已配置" if DINGTALK_WEBHOOK else "未配置"))
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

    # 汇总（更短更清楚）
    # 招标只统计“被保留的”（已做过期/未来天数过滤）
    sum_text = (
        f"### 📣 外包/派遣采集完成\n"
        f"- 日期：{date_start} ~ {date_end}\n"
        f"- 招标：{len(all_bidding)} 条\n"
        f"- 中标/成交：{len(all_award)} 条\n"
        f"- 过滤：{'丢弃已过期' if SKIP_EXPIRED else '保留已过期'}；"
        f"{('仅保留未来 ' + str(DUE_FILTER_DAYS) + ' 天内截止') if DUE_FILTER_DAYS>0 else '不过滤未来天数'}\n"
    )
    send_to_dingtalk_markdown("外包/派遣采集汇总", sum_text)

    # 明细（清爽卡片）
    if all_bidding:
        md_bid = format_bidding_markdown(all_bidding, date_start, date_end)
        split_and_send("招标公告明细", md_bid)

    if all_award:
        md_awd = format_award_markdown(all_award, date_start, date_end)
        split_and_send("中标结果明细", md_awd)

    print("✔ 完成")
