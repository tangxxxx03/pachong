# -*- coding: utf-8 -*-
"""
外包/派遣：招标 & 中标采集（北京公共资源交易平台 + zsxtzb.cn 搜索）
"""

import os, re, time, math
from datetime import datetime, timedelta
import pandas as pd
import pdfplumber
from io import BytesIO
from urllib.parse import urlparse, urljoin

# ========== HTTP 会话（禁用环境代理 + 重试） ==========
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
for _k in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','all_proxy'):
    os.environ.pop(_k, None)

_SESSION = requests.Session()
_SESSION.trust_env = False
_retry = Retry(total=4, backoff_factor=1, status_forcelist=[429,500,502,503,504],
               allowed_methods=frozenset(["GET","POST"]))
_SESSION.mount("http://", HTTPAdapter(max_retries=_retry))
_SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
_SESSION.headers.update({"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"})

# ========== Selenium ==========
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ================== 配置（可被环境变量覆盖） ==================
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "").strip()
KEYWORDS = [k.strip() for k in os.getenv("KEYWORDS", "外包,派遣").split(",") if k.strip()]
CRAWL_BEIJING = os.getenv("CRAWL_BEIJING", "true").lower() in ("1","true","yes","y")
CRAWL_ZSXTZB  = os.getenv("CRAWL_ZSXTZB",  "true").lower() in ("1","true","yes","y")
DUE_FILTER_DAYS = int(os.getenv("DUE_FILTER_DAYS", "30"))
SKIP_EXPIRED = os.getenv("SKIP_EXPIRED", "true").lower() in ("1","true","yes","y")
HEADLESS = os.getenv("HEADLESS", "1").lower() in ("1","true","yes","y")
# ============================================================

def send_to_dingtalk_markdown(title: str, md_text: str, webhook: str = None):
    webhook = (webhook or DINGTALK_WEBHOOK).strip()
    if not webhook.startswith("http"):
        print("? Webhook 未配置或无效"); return
    headers = {"Content-Type": "application/json"}
    data = {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}
    try:
        resp = _SESSION.post(webhook, json=data, headers=headers, timeout=15)
        print("钉钉推送：", resp.status_code, resp.text[:180])
    except Exception as e:
        print("? 发送钉钉失败：", e)

def get_date_range():
    today = datetime.now()
    if today.weekday() == 0:
        start = today - timedelta(days=3); end = today - timedelta(days=1)
    else:
        start = today - timedelta(days=1); end = today - timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def classify(title: str) -> str:
    t = title or ""
    if any(k in t for k in ["中标","成交","结果","定标","候选人公示"]): return "中标公告"
    if any(k in t for k in ["更正","变更","澄清","补遗"]): return "更正公告"
    if any(k in t for k in ["终止","废标","流标"]): return "终止公告"
    if any(k in t for k in ["招标","采购","磋商","邀请","比选","谈判","竞争性"]): return "招标公告"
    return "其他"

def _safe_text(s: str) -> str: return (s or "").replace("\u3000"," ").replace("\xa0"," ")
def _pick(text, pat): g = re.search(pat, text, re.S|re.I); return g.group(1).strip() if g else "暂无"
def _normalize_amount(val: str):
    if not val or val == "暂无": return "暂无"
    s = str(val).replace(",","").replace("，",""); m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    return m.group(1) if m else val
def _date_in_text(s: str):
    if not s: return ""
    m = re.search(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})", s)
    return m.group(1).replace(".","-").replace("/", "-") if m else ""

def _normalize_date_string(s: str) -> str:
    if not s: return ""
    s = s.strip().replace("年","-").replace("月","-").replace("日"," ").replace("/", "-").replace("：",":").replace("．",".")
    s = re.sub(r"\s+", " ", s)
    m = re.search(r"(20\d{2})[-\.](\d{1,2})[-\.](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if not m: return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else None; mm = int(m.group(5)) if m.group(5) else None
    try:
        if hh is not None and mm is not None: return datetime(y,mo,d,hh,mm).strftime("%Y-%m-%d %H:%M")
        return datetime(y,mo,d).strftime("%Y-%m-%d")
    except Exception: return ""

def _to_datetime(s: str):
    if not s: return None
    for fmt in ("%Y-%m-%d %H:%M","%Y-%m-%d"):
        try: return datetime.strptime(s, fmt)
        except Exception: pass
    return None

def extract_deadline(detail_text: str) -> str:
    txt = _safe_text(detail_text)
    pats = [
        r"(?:投标(?:文件)?|递交(?:响应)?文件|响应文件提交|报价|报名)\s*截止(?:时间|日期)\s*[:：]?\s*([^\n\r，。;；]{6,40})",
        r"(?:截止(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})(?=.*?(?:投标|递交|响应|报价|报名))",
    ]
    for pat in pats:
        m = re.search(pat, txt, re.I)
        if m:
            norm = _normalize_date_string(m.group(1))
            if norm: return norm
    m2 = re.search(r"(?:开标(?:时间|日期))\s*[:：]?\s*([^\n\r，。;；]{6,40})", txt, re.I)
    if m2:
        norm = _normalize_date_string(m2.group(1))
        if norm: return norm
    return ""

def fetch_pdf_text(url: str, referer: str = None, timeout=20) -> str:
    try:
        headers = {"User-Agent":"Mozilla/5.0"}
        if referer: headers["Referer"] = referer
        r = _SESSION.get(url, headers=headers, timeout=timeout)
        ct = (r.headers.get("Content-Type") or "").lower()
        if "pdf" not in ct and not url.lower().endswith(".pdf"): return ""
        with pdfplumber.open(BytesIO(r.content)) as pdf:
            pages = []
            for p in pdf.pages:
                try: pages.append(p.extract_text() or "")
                except Exception: continue
        return "\n".join(pages)
    except Exception as e:
        print("PDF 读取失败：", e); return ""

def extract_detail_text_with_pdf_fallback(driver, page_html: str, page_url: str):
    xps = ["//*[@id='zoom']","//*[@id='vsb_content']","//*[@class='content']",
           "//*[@class='article']","//*[@id='info']","//*[@class='detail']",
           "//*[@id='xxnr']","//*[@class='cont']"]
    for xp in xps:
        try:
            t = driver.find_element(By.XPATH, xp).text
            if t and len(t.strip()) > 30: return t
        except Exception: pass
    try:
        links = re.findall(r'href=["\'](.*?)["\']', page_html, flags=re.I)
        pdfs = []
        for h in links:
            absu = urljoin(page_url, h.strip())
            if absu.lower().endswith(".pdf"): pdfs.append(absu)
        if not pdfs:
            for a in driver.find_elements(By.XPATH, "//a"):
                try:
                    txt = (a.text or "").strip(); href = a.get_attribute("href") or ""
                    if (("PDF" in txt.upper()) or ("附件" in txt) or ("下载" in txt)) and href:
                        absu = urljoin(page_url, href); pdfs.append(absu)
                except Exception: continue
        if pdfs:
            for pdf_url in pdfs[:3]:
                pdf_text = fetch_pdf_text(pdf_url, referer=page_url)
                if pdf_text and len(pdf_text.strip()) > 50: return pdf_text
    except Exception: pass
    try: return driver.find_element(By.TAG_NAME, "body").text
    except Exception: return ""

def parse_bidding_fields(detail_text: str):
    txt = _safe_text(detail_text)
    m_amt = re.search(r"(?:预算金额|最高限价|控制价|采购预算)\s*[:：]?\s*([0-9\.,，]+)\s*(万元|元)", txt)
    amount = (m_amt.group(1).replace(",","").replace("，","") + m_amt.group(2)) if m_amt else "暂无"
    m_lxr = re.search(r"(?:联系人|项目联系人|采购人联系人)\s*[:：]?\s*([^\s、，。;；]+)", txt)
    contact = m_lxr.group(1).strip() if m_lxr else "暂无"
    m_tel = re.search(r"(?:联系电话|联系方式|电话)\s*[:：]?\s*([0-9\-－—\s]{6,})", txt)
    phone = re.sub(r"\s+","", m_tel.group(1)) if m_tel else "暂无"
    plain = re.sub(r"\s+"," ", txt); brief = plain[:120] + ("..." if len(plain) > 120 else "")
    if not brief.strip(): brief = "暂无"
    industry = "暂无"
    deadline = extract_deadline(txt)
    return {"金额": amount, "联系人": contact, "联系电话": phone, "简要摘要": brief,
            "行业类型": industry, "投标截止": deadline or "暂无"}

def parse_award_from_tables(html: str):
    supplier = amount = score = content = "暂无"; unit = ""
    try: tables = pd.read_html(html, header=0)
    except Exception: tables = []
    def f2(v):
        if v in (None,"","暂无"): return None
        m = re.search(r"(-?\d+(?:\.\d+)?)", str(v).replace(",",""))
        return float(m.group(1)) if m else None
    rows = []
    for tb in tables:
        t = tb.fillna("").astype(str)
        cols = [str(c) for c in t.columns]
        if not any(k in "".join(cols) for k in ["供应商名称","供应商","单位名称"]): continue
        def find_col(keys):
            for k in keys:
                for c in cols:
                    if k in c: return c
            return None
        c_sup = find_col(["供应商名称","供应商","单位名称"])
        c_sco = find_col(["评审得分","综合得分","最终得分"])
        c_rnk = find_col(["名次","排序","排名"])
        c_pri = find_col(["评审报价","中标金额","成交金额","报价","投标报价"])
        for _, r in t.iterrows():
            name = r.get(c_sup,"").strip() if c_sup else ""
            if not name: continue
            row = {"supplier": name,
                   "score": f2(r.get(c_sco)) if c_sco else None,
                   "rank":  f2(r.get(c_rnk)) if c_rnk else None,
                   "price": f2(r.get(c_pri)) if c_pri else None}
            if c_pri and ("万元" in c_pri): unit = "万元"
            rows.append(row)
    chosen = None
    if rows:
        sc = [r for r in rows if r["score"] is not None]
        if sc: chosen = max(sc, key=lambda x: x["score"])
        else:
            rk = [r for r in rows if r["rank"] is not None]
            if rk: chosen = min(rk, key=lambda x: x["rank"])
            else:
                pr = [r for r in rows if r["price"] is not None]
                chosen = min(pr, key=lambda x: x["price"]) if pr else rows[0]
    if chosen:
        supplier = chosen["supplier"]
        amount   = _normalize_amount(chosen["price"]) if chosen["price"] is not None else "暂无"
        score    = str(chosen["score"]) if chosen["score"] is not None else "暂无"
        if amount != "暂无" and unit: amount = f"{amount}{unit}"
    return {"中标公司": supplier or "暂无",
            "中标金额": amount or "暂无",
            "评审得分": (score or "暂无").rstrip("分"),
            "中标内容": content or "暂无"}

def parse_award_from_text(detail_text: str):
    txt = _safe_text(detail_text)
    supplier = _pick(txt, r"(?:中标(?:供应商|人|单位)|成交(?:供应商|人|单位)|供应商名称)[：:]\s*([^\n\r，。；;]+)")
    amount   = _pick(txt, r"(?:中标(?:价|金额)|成交(?:价|金额)|评审报价)[：:]\s*([0-9\.,，]+(?:元|万元)?)")
    score    = _pick(txt, r"(?:评审(?:得分|分值)|综合得分|最终得分)[：:]\s*([0-9\.]+)")
    content  = _pick(txt, r"(?:采购内容|项目概况|采购需求|服务内容|中标内容)[：:]\s*([^\n\r]+)")
    amount   = _normalize_amount(amount)
    return {"中标公司": supplier if supplier else "暂无",
            "中标金额": amount or "暂无",
            "评审得分": (score or "暂无").rstrip("分"),
            "中标内容": content or "暂无"}

def choose_origin_notice_url(detail_html: str, current_url: str) -> str:
    if not detail_html: return "暂无"
    hrefs = re.findall(r'<a[^>]+href=["\'](.*?)["\']', detail_html, flags=re.I)
    if not hrefs: return "暂无"
    cur = urlparse(current_url or ""); cur_dom = f"{cur.scheme}://{cur.netloc}" if cur.scheme and cur.netloc else ""
    clean = []
    for h in hrefs:
        h = h.strip()
        if not h or h.startswith("#") or h.lower().startswith("javascript"): continue
        absu = urljoin(current_url or "", h)
        if absu == current_url: continue
        clean.append(absu)
    if not clean: return "暂无"
    kw_good = ["招标","采购","公告","公开","zb","zhaobiao","notice"]
    bad_words = ["首页","返回","上一页","下一页","更多","下载中心","栏目","频道"]
    good_exts = [".html",".shtml",".htm",".pdf"]
    def score(u: str) -> tuple:
        p = urlparse(u); s = 0; low = u.lower()
        if any(k in u for k in kw_good) or any(k in low for k in ["zbgg","zhaobiao","cgxx","notice"]): s += 5
        if any(low.endswith(ext) for ext in good_exts): s += 3
        if cur_dom and (f"{p.scheme}://{p.netloc}" == cur_dom): s += 2
        depth = len([seg for seg in p.path.split("/") if seg]); s += min(depth, 6)
        if re.search(r"(20\d{2}[-/_.]?\d{2}([-/_.]?\d{2})?)", low): s += 2
        if any(b in u for b in bad_words): s -= 4
        if any(w in low for w in ["index","list","channel","column"]): s -= 2
        return (s, -len(u))
    best = sorted(set(clean), key=score, reverse=True)[0]
    if not any(best.lower().endswith(ext) for ext in good_exts):
        if not any(k in best for k in kw_good) and not any(k in best.lower() for k in ["zbgg","zhaobiao","cgxx","notice"]):
            return "暂无"
    return best

def parse_award_fields(detail_html: str, detail_text: str, current_url: str = ""):
    data = parse_award_from_tables(detail_html)
    if data["中标公司"] == "暂无" and data["中标金额"] == "暂无":
        data = parse_award_from_text(detail_text)
    data["原招标网址"] = choose_origin_notice_url(detail_html, current_url) or "暂无"
    award_date = _pick(detail_text or "", r"(?:公告日期|公示时间|发布时间|成交日期|中标日期)[：:]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
    if award_date == "暂无":
        award_date = _pick(detail_text or "", r"([0-9]{4}-[0-9]{2}-[0-9]{2})")
    data["中标日期"] = award_date if award_date else "暂无"
    return data

# -------- Selenium（容器友好） --------
def _build_driver():
    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    if HEADLESS: opts.add_argument("--headless=new")
    try:
        driver = webdriver.Chrome(options=opts)  # Selenium Manager
    except Exception:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                                  options=opts)
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
            driver.get(url); time.sleep(3.0)
            # 时间过滤：一周
            try:
                driver.find_element(By.XPATH, "//span[contains(text(),'时间不限')]").click(); time.sleep(0.6)
                driver.find_element(By.ID, "week").click(); time.sleep(1.0)
            except Exception:
                pass
            for page in range(1, max_pages + 1):
                cards = driver.find_elements(By.CLASS_NAME, "cs_search_content_box")
                for c in cards:
                    try:
                        title_el = c.find_element(By.CLASS_NAME, "cs_search_title")
                        title = title_el.text.strip()
                        ann_type = classify(title)
                        if ann_type not in ("招标公告","中标公告"): continue

                        content = c.find_element(By.CLASS_NAME, "cs_search_content_p").text
                        source_line = c.find_element(By.CLASS_NAME, "cs_search_content_time").text
                        info_source, pub_time = "", ""
                        if "发布时间：" in source_line:
                            parts = source_line.split("发布时间：")
                            info_source = parts[0].replace("信息来源：","").strip()
                            pub_time = parts[1].strip()
                        pub_date = pub_time[:10] if pub_time else ""
                        if date_start and date_end and pub_date:
                            if pub_date < date_start or pub_date > date_end:
                                continue

                        try:
                            url_link = title_el.find_element(By.TAG_NAME, "a").get_attribute("href")
                        except Exception:
                            url_link = ""

                        if url_link and url_link in seen_links: continue
                        seen_links.add(url_link)

                        # 打开详情
                        detail_text, detail_html = "", ""
                        if url_link:
                            win = driver.current_window_handle
                            driver.execute_script('window.open(arguments[0])', url_link)
                            driver.switch_to.window(driver.window_handles[-1]); time.sleep(1.2)
                            detail_html = driver.page_source
                            detail_text = extract_detail_text_with_pdf_fallback(driver, detail_html, url_link) or content
                            driver.close(); driver.switch_to.window(win)

                        if ann_type == "招标公告":
                            fields = parse_bidding_fields(detail_text)
                            due_str = fields.get("投标截止","")
                            due_dt  = _to_datetime(due_str)
                            keep = True; now = datetime.now()
                            if SKIP_EXPIRED and due_dt and due_dt < now: keep = False
                            if keep and DUE_FILTER_DAYS > 0 and due_dt and due_dt > now + timedelta(days=DUE_FILTER_DAYS): keep = False
                            if keep:
                                all_bidding.append({
                                    "公告类型":"公开招标公告",
                                    "公告标题": title,
                                    "公告发布时间": pub_time or "暂无",
                                    "行业类型": fields["行业类型"],
                                    "金额": fields["金额"],
                                    "简要摘要": fields["简要摘要"],
                                    "联系人": fields["联系人"],
                                    "联系电话": fields["联系电话"],
                                    "投标截止": due_str or "暂无",
                                    "公告网址": url_link or "暂无",
                                    "信息来源": info_source or "暂无",
                                })
                        else:
                            fields = parse_award_fields(detail_html, detail_text, current_url=url_link)
                            all_award.append({
                                "中标日期": fields["中标日期"] if fields["中标日期"] != "暂无" else (pub_date or "暂无"),
                                "中标公司": fields["中标公司"],
                                "中标金额": fields["中标金额"],
                                "中标内容": fields["中标内容"],
                                "评审得分": fields["评审得分"],
                                "原招标网址": fields["原招标网址"],
                                "中标网址": url_link or "暂无",
                                "标题": title,
                                "发布时间": pub_time or "暂无",
                                "信息来源": info_source or "暂无"
                            })
                    except Exception as ex:
                        print("解析一条出错：", ex)
                # 翻页
                try:
                    next_btn = driver.find_element(By.LINK_TEXT, "下一页")
                    if "disable" in (next_btn.get_attribute("class") or "") or next_btn.get_attribute("aria-disabled") == 'true':
                        break
                    if page < max_pages:
                        driver.execute_script("arguments[0].click();", next_btn); time.sleep(1.0)
                except Exception:
                    break
    finally:
        driver.quit()
    return all_bidding, all_award

# ================== 站点二：zsxtzb.cn 聚合搜索 ==================
def _zs_search_url(keyword, page=1):
    base = f"https://www.zsxtzb.cn/search?keyword={keyword}"
    if page > 1: base += f"&page={page}"
    return base

def _zs_pick_list_items(driver):
    items = []
    lis = driver.find_elements(By.XPATH, "//div[contains(@class,'search') or contains(@class,'result') or contains(@class,'list')]//li[a]")
    for li in lis:
        try:
            a = li.find_element(By.TAG_NAME, "a")
            title = a.text.strip(); href = a.get_attribute("href")
            raw = li.text; dt = _date_in_text(raw)
            if title and href: items.append((title, href, dt))
        except Exception: pass
    if not items:
        blocks = driver.find_elements(By.XPATH, "//div[contains(@class,'search') or contains(@class,'result') or contains(@class,'list')]//h3[a]")
        for b in blocks:
            try:
                a = b.find_element(By.TAG_NAME, "a")
                title = a.text.strip(); href = a.get_attribute("href")
                raw = b.text; dt = _date_in_text(raw)
                if not dt:
                    try:
                        sib = b.find_element(By.XPATH, "./following-sibling::*[1]")
                        dt = _date_in_text(sib.text)
                    except Exception: pass
                if title and href: items.append((title, href, dt))
            except Exception: pass
    if not items:
        anchors = driver.find_elements(By.XPATH, "//div[contains(@class,'container') or contains(@class,'content') or contains(@id,'content')]//a")
        bad = ["首页","上一页","下一页","末页","更多","下载","返回"]
        for a in anchors:
            try:
                title = (a.text or "").strip(); href = a.get_attribute("href") or ""
                if not title or not href: continue
                if any(b in title for b in bad): continue
                parent_text = a.find_element(By.XPATH, "./ancestor::*[self::li or self::div][1]").text
                dt = _date_in_text(parent_text)
                items.append((title, href, dt))
            except Exception: pass
    return items

def _zs_next_page(driver, cur_page):
    for xp in ["//a[contains(.,'下一页') or contains(.,'下页')]",
               "//a[contains(@class,'next')]",
               f"//a[normalize-space(text())='{cur_page+1}']",
               f"//button[normalize-space(text())='{cur_page+1}']"]:
        try:
            el = driver.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].click();", el); time.sleep(1.0)
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
                driver.get(url); time.sleep(1.4)

                items = _zs_pick_list_items(driver)
                if not items: break

                for title, href, dt in items:
                    ann_type = classify(title)
                    if ann_type not in ("招标公告","中标公告"): continue
                    if href in seen: continue
                    seen.add(href)

                    pub_date = dt[:10] if dt else ""
                    if date_start and date_end and pub_date:
                        if pub_date < date_start or pub_date > date_end: continue

                    win = driver.current_window_handle
                    driver.execute_script('window.open(arguments[0])', href)
                    driver.switch_to.window(driver.window_handles[-1]); time.sleep(1.2)
                    detail_html = driver.page_source
                    detail_text = extract_detail_text_with_pdf_fallback(driver, detail_html, href)
                    driver.close(); driver.switch_to.window(win)

                    if ann_type == "招标公告":
                        fields = parse_bidding_fields(detail_text)
                        due_str = fields.get("投标截止","")
                        due_dt  = _to_datetime(due_str)
                        keep = True; now = datetime.now()
                        if SKIP_EXPIRED and due_dt and due_dt < now: keep = False
                        if keep and DUE_FILTER_DAYS > 0 and due_dt and due_dt > now + timedelta(days=DUE_FILTER_DAYS): keep = False
                        if keep:
                            all_bidding.append({
                                "公告类型":"公开招标公告",
                                "公告标题": title,
                                "公告发布时间": pub_date or "暂无",
                                "行业类型": fields["行业类型"],
                                "金额": fields["金额"],
                                "简要摘要": fields["简要摘要"],
                                "联系人": fields["联系人"],
                                "联系电话": fields["联系电话"],
                                "投标截止": due_str or "暂无",
                                "公告网址": href or "暂无",
                                "信息来源": "zsxtzb聚合搜索",
                            })
                    else:
                        fields = parse_award_fields(detail_html, detail_text, current_url=href)
                        all_award.append({
                            "中标日期": fields["中标日期"] if fields["中标日期"] != "暂无" else (pub_date or "暂无"),
                            "中标公司": fields["中标公司"],
                            "中标金额": fields["中标金额"],
                            "中标内容": fields["中标内容"],
                            "评审得分": fields["评审得分"],
                            "原招标网址": fields["原招标网址"],
                            "中标网址": href or "暂无",
                            "标题": title,
                            "发布时间": pub_date or "暂无",
                            "信息来源": "zsxtzb聚合搜索",
                        })
                if not _zs_next_page(driver, page): break
                page += 1
    finally:
        driver.quit()
    return all_bidding, all_award

# ================== Markdown & 推送 ==================
def md_escape(s: str) -> str:
    if not isinstance(s, str): s = str(s)
    return s.replace("|","\\|")

def format_bidding_markdown(items, date_start, date_end):
    lines = [f"### 【招标公告】{date_start} ~ {date_end} 共 {len(items)} 条"]
    for idx, it in enumerate(items, 1):
        url = it.get("公告网址",""); title = md_escape(it.get("公告标题",""))
        show = f"[{title}]({url})" if url.startswith("http") else title
        lines.append(f"\n**{idx}. {show}**")
        lines.append(f"- 公告发布时间：{md_escape(it.get('公告发布时间','暂无'))}")
        lines.append(f"- 投标截止：{md_escape(it.get('投标截止','暂无'))}")
        lines.append(f"- 金额：{md_escape(it.get('金额','暂无'))}")
        lines.append(f"- 联系人：{md_escape(it.get('联系人','暂无'))}")
        lines.append(f"- 联系电话：{md_escape(it.get('联系电话','暂无'))}")
        lines.append(f"- 简要摘要：{md_escape(it.get('简要摘要','暂无'))}")
        lines.append(f"- 信息来源：{md_escape(it.get('信息来源','暂无'))}")
    return "\n".join(lines)

def format_award_markdown(items, date_start, date_end):
    lines = [f"### 【中标结果】{date_start} ~ {date_end} 共 {len(items)} 条"]
    for idx, it in enumerate(items, 1):
        url = it.get("中标网址",""); title = md_escape(it.get("标题",""))
        show = f"[{title}]({url})" if url.startswith("http") else title
        lines.append(f"\n**{idx}. {show}**")
        lines.append(f"- 中标日期：{md_escape(it.get('中标日期','暂无'))}")
        lines.append(f"- 中标公司：{md_escape(it.get('中标公司','暂无'))}")
        lines.append(f"- 中标金额：{md_escape(it.get('中标金额','暂无'))}")
        lines.append(f"- 评审得分：{md_escape(it.get('评审得分','暂无'))}")
        lines.append(f"- 中标内容：{md_escape(it.get('中标内容','暂无'))}")
        yz = it.get("原招标网址","")
        lines.append(f"- 原招标网址：{('[点击跳转](' + yz + ')') if yz.startswith('http') else '暂无'}")
        lines.append(f"- 信息来源：{md_escape(it.get('信息来源','暂无'))}")
        lines.append(f"- 发布时间：{md_escape(it.get('发布时间','暂无'))}")
    return "\n".join(lines)

def split_and_send(title_prefix: str, full_text: str, webhook: str, chunk_size=4500):
    n = max(1, math.ceil(len(full_text) / chunk_size))
    for i in range(n):
        part = full_text[i*chunk_size:(i+1)*chunk_size]
        part_title = f"{title_prefix}（{i+1}/{n}）" if n > 1 else title_prefix
        send_to_dingtalk_markdown(part_title, part, webhook)

# ================== MAIN ==================
if __name__ == '__main__':
    date_start, date_end = get_date_range()
    print(f"采集日期：{date_start} ~ {date_end}")
    all_bidding, all_award = [], []

    if CRAWL_BEIJING:
        b1, a1 = crawl_beijing(KEYWORDS, max_pages=10, date_start=date_start, date_end=date_end)
        all_bidding.extend(b1); all_award.extend(a1)

    if CRAWL_ZSXTZB:
        b2, a2 = crawl_zsxtzb_search(KEYWORDS, max_pages=8, date_start=date_start, date_end=date_end)
        all_bidding.extend(b2); all_award.extend(a2)

    summary = (
        f"【播报】{date_start}~{date_end} 外包/派遣采集完成：招标 {len(all_bidding)} 条，中标 {len(all_award)} 条。\n"
        f"过滤策略：{'丢弃已过期' if SKIP_EXPIRED else '保留已过期'}；"
        f"{'仅保留未来 ' + str(DUE_FILTER_DAYS) + ' 天内' if DUE_FILTER_DAYS>0 else '不过滤未来天数'}。"
    )
    send_to_dingtalk_markdown("外包/派遣采集汇总", summary, DINGTALK_WEBHOOK)

    if all_bidding:
        md_bid = format_bidding_markdown(all_bidding, date_start, date_end)
        split_and_send("招标公告明细", md_bid, DINGTALK_WEBHOOK)
    if all_award:
        md_awd = format_award_markdown(all_award, date_start, date_end)
        split_and_send("中标结果明细", md_awd, DINGTALK_WEBHOOK)

    print("✔ 完成")
