# -*- coding: utf-8 -*-
"""
外包/派遣 · 京津冀融合采集（北京公共资源 + zsxtzb）
pip install -U selenium webdriver-manager requests beautifulsoup4 lxml pdfplumber
"""
import os, re, time, math, random, urllib.parse
from io import BytesIO
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set

import requests
from bs4 import BeautifulSoup

# ==================== 基本配置 ====================
# 可用环境变量 DINGTALK_WEBHOOK 覆盖；默认用你给的 webhook
DINGTALK_WEBHOOK = os.getenv(
    "DINGTALK_WEBHOOK",
    "https://oapi.dingtalk.com/robot/send?access_token=6e945607bb71c2fd9bb3399c6424fa7dece4b9798d2a8ff74b0b71ab47c9d182"
).strip()

# 关键词会各自检索
KEYWORDS = ["外包","派遣","劳务外包","服务外包","人力外包","劳务派遣","人力派遣"]

CRAWL_BEIJING = True          # 北京公共资源
CRAWL_ZSXTZB  = True          # zsxtzb.cn

MAX_PAGES_BJ = 10
MAX_PAGES_ZS = 6
REQUEST_DELAY = (1.0, 2.0)

# 北京“宽松模式”回补近 N 天（严格=0）
BJ_LOOSE_DAYS = 2

# 是否强制用网站自带“时间范围”筛选（下拉→一周内）
USE_SITE_TIME_FILTER = True
BJ_TIME_FILTER_TEXT  = "一周内"

# 地区白名单 & 识别
REGION_ALLOWED = {"北京","天津","河北"}
REGION_MAP = {
    "北京": ["北京","北京市","海淀","朝阳","丰台","石景山","门头沟","房山","通州","顺义","昌平","大兴","怀柔","平谷","密云","延庆","经开区","亦庄"],
    "天津": ["天津","天津市","和平","河东","河西","南开","河北","红桥","滨海新区","东丽","西青","津南","北辰","武清","宝坻","宁河","静海","蓟州","蓟县"],
    "河北": ["河北","河北省","石家庄","唐山","秦皇岛","邯郸","邢台","保定","张家口","承德","沧州","廊坊","衡水"],
}
NON_JJJ_PROVINCES = [
    "上海","江苏","浙江","安徽","山东","广东","福建","湖北","湖南","河南","四川","重庆","云南","贵州","广西","西藏",
    "陕西","甘肃","宁夏","青海","新疆","内蒙古","辽宁","吉林","黑龙江","山西","江西","海南","香港","澳门","台湾"
]

# zsxtzb 城市参数（依次尝试）
ZS_CITY_IDS_TRY = {"北京":1,"天津":2,"河北":3}

# ==================== 自动日期（北京站） ====================
# 周一：抓上周五/六/日（可选是否加今天）；周二~周五：抓昨天
DATE_POLICY         = "auto"      # auto | last_n | today
LAST_N_DAYS         = 1
MON_INCLUDE_TODAY   = True

def get_target_dates(policy=DATE_POLICY, last_n=LAST_N_DAYS, include_mon_today=MON_INCLUDE_TODAY):
    today = datetime.now()
    wd = today.weekday()  # 0=Mon
    if policy == "auto":
        if wd == 0:  # 周一
            days = [3,2,1] + ([0] if include_mon_today else [])
        else:        # 其他工作日
            days = [1]
    elif policy == "last_n":
        days = list(range(1, max(1, int(last_n))+1))
    else:  # today
        days = [0]
    return {(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in days}

def pretty_range(dates: Set[str]) -> str:
    ds = sorted(dates)
    return ds[0] if len(ds)==1 else f"{ds[0]} ~ {ds[-1]}"

# ==================== 通用 ====================
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
})
def sleep(): time.sleep(random.uniform(*REQUEST_DELAY))

def std_time(s: str) -> str:
    if not s: return ""
    s = (s.replace("年","-").replace("月","-").replace("日","")
           .replace("/","-").replace(".","-").replace("：",":"))
    s = re.sub(r"-+","-", s).strip()
    parts = s.split(); ymd = parts[0].split("-")
    if len(ymd) >= 3:
        d = f"{ymd[0]}-{ymd[1].zfill(2)}-{ymd[2].zfill(2)}"
        return f"{d} {parts[1]}" if len(parts)>=2 else d
    return s

def md_escape(s: str) -> str:
    if not isinstance(s, str): s = str(s)
    return s.replace("|","\\|")

# ==================== 金额/电话/地区抽取 ====================
AMOUNT_KEYS = ["预算金额","采购预算","采购金额","预算资金","项目资金","资金","最高限价","招标控制价","控制价","拦标价","合同估算价","限价","金额","中标金额","成交金额","预算"]
NUM_UNIT_RE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*(万元|万?元|元|人民币|RMB)", re.I)
NUM_ONLY_RE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)")
AMOUNT_STOP_WORDS = ["电话","联系电话","传真","编号","项目编号","公告编号","统一社会信用代码","邮编","地址","开户行","账号","QQ","微信","税号"]
PHONE_RE   = re.compile(r"\b(?:1\d{10}|(?:\d{3,4}-)?\d{7,8})\b")
DATE_NUM_RE= re.compile(r"20\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")

def normalize_amount(text: str) -> str:
    if not text: return ""
    m = re.search(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*(万元|万?元|元|人民币|RMB)?", text, re.I)
    if not m: return ""
    num_s, unit = m.group(1), (m.group(2) or "").lower()
    num = float(num_s.replace(",", ""))
    if "万" in unit:
        s = f"{num:.4f}".rstrip("0").rstrip("."); return f"{s}万元"
    if "元" in unit or "rmb" in unit or "人民币" in unit or unit=="":
        if num >= 10000:
            s = f"{num/10000:.4f}".rstrip("0").rstrip("."); return f"{s}万元"
        return f"{int(num)}元" if num.is_integer() else f"{num}元"
    return f"{num}{unit}"

def extract_phone(text: str) -> str:
    for m in PHONE_RE.finditer(text or ""):
        s = m.group(0)
        if DATE_NUM_RE.match(s):  # 排除 yyyyMMdd
            continue
        return s
    return ""

def detect_region_from_text(text: str) -> str:
    if not text: return ""
    for region, names in REGION_MAP.items():
        for n in names:
            if n in text: return region
    return ""

def contains_non_jjj(text: str) -> bool:
    if not text: return False
    return any(p in text for p in NON_JJJ_PROVINCES)

AREA_FIELD_RE = re.compile(r"(项目所在地|项目所在地区|项目地点|项目位置|采购项目所在地|项目区域|项目实施地点|服务地点|服务区域|履约地点|交付地点)[：:\s]*([^\n\r，。；;]{2,40})")

def decide_region(title: str, full_text: str) -> str:
    t = (title or "") + " " + (full_text or "")
    m = AREA_FIELD_RE.search(full_text or "")
    if m:
        area_txt = m.group(2)
        if contains_non_jjj(area_txt): return ""
        reg = detect_region_from_text(area_txt)
        if reg in REGION_ALLOWED: return reg
    if contains_non_jjj(t): return ""
    reg = detect_region_from_text(t)
    return reg if reg in REGION_ALLOWED else ""

def region_from_source(src: str) -> str:
    s = src or ""
    if "北京市" in s: return "北京"
    if "天津市" in s: return "天津"
    if "河北省" in s: return "河北"
    return ""

# ==================== 钉钉 ====================
def send_to_dingtalk_markdown(title: str, md_text: str) -> bool:
    if not DINGTALK_WEBHOOK.startswith("http"):
        print("❌ Webhook 无效"); return False
    try:
        r = requests.post(DINGTALK_WEBHOOK, json={"msgtype":"markdown","markdown":{"title":title,"text":md_text}}, timeout=20)
        ok = (r.status_code==200 and r.json().get("errcode")==0)
        print("钉钉发送成功" if ok else f"钉钉失败：{r.status_code} {r.text[:180]}")
        return ok
    except Exception as e:
        print("❌ 钉钉异常：", e); return False

def split_and_send(title_prefix: str, full_text: str, chunk_size=4500):
    n = max(1, math.ceil(len(full_text)/chunk_size))
    for i in range(n):
        part = full_text[i*chunk_size:(i+1)*chunk_size]
        title = f"{title_prefix}（{i+1}/{n}）" if n>1 else title_prefix
        send_to_dingtalk_markdown(title, part)

# ==================== Selenium 与“时间范围：一周内” ====================
def build_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    # 调试期建议有头；稳定后可无头： opts.add_argument("--headless=new")
    try:
        drv = webdriver.Chrome(options=opts)  # Selenium Manager
    except Exception:
        from webdriver_manager.chrome import ChromeDriverManager
        drv = webdriver.Chrome(ChromeDriverManager().install(), options=opts)
    drv.implicitly_wait(6)
    return drv

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

def _get_first_card_text(driver):
    try:
        el = driver.find_elements(By.CSS_SELECTOR, ".cs_search_content_box")
        return el[0].text[:80] if el else ""
    except Exception:
        return ""

def beijing_time_filter_is(driver, option_text="一周内"):
    # 读出“时间范围”后第一个可见元素文本
    for xp in [
        "//*[contains(text(),'时间范围')]/following::span[1]",
        "//*[contains(@class,'time') and .//span[contains(.,'时间')]]//span[1]"
    ]:
        try:
            el = driver.find_element(By.XPATH, xp)
            txt = (el.text or "").strip()
            if option_text in txt:
                return True
        except Exception:
            continue
    # 兜底：页面存在“⼀周内”已选中按钮
    try:
        els = driver.find_elements(By.XPATH, "//*[contains(text(),'一周内')]")
        for e in els:
            if e.is_displayed():
                return True
    except Exception:
        pass
    return False

def set_beijing_time_filter(driver, option_text="一周内"):
    """点击下拉把北京站“时间范围”设为『一周内』；成功返回 True。"""
    def open_dropdown(drv):
        for xp in [
            "//span[normalize-space()='时间不限']",
            "//*[contains(@class,'time') and .//span[contains(.,'时间')]]//span[contains(.,'时间不限')]",
            "//*[contains(text(),'时间范围')]/following::span[contains(.,'时间')][1]"
        ]:
            try:
                el = drv.find_element(By.XPATH, xp)
                drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                ActionChains(drv).move_to_element(el).click(el).perform()
                return True
            except Exception:
                continue
        return False

    def click_week(drv):
        for xp in [
            "//*[@id='week']","//li[@id='week']","//a[@id='week']","//*[contains(@id,'week')]",
            "//li[.//text()[contains(.,'一周内')]]","//*[contains(text(),'一周内')]"
        ]:
            try:
                btn = drv.find_element(By.XPATH, xp)
                drv.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                ActionChains(drv).move_to_element(btn).click(btn).perform()
                return True
            except Exception:
                continue
        return False

    before = _get_first_card_text(driver)
    ok_open = open_dropdown(driver)
    ok_pick = click_week(driver) if ok_open else False
    if not ok_pick:
        open_dropdown(driver)
        ok_pick = click_week(driver)
    if not ok_pick:
        return False

    # 等待列表刷新，否则手动触发
    try:
        WebDriverWait(driver, 6).until(lambda d: _get_first_card_text(d) != before)
    except Exception:
        clicked = False
        for xp in ["//button[contains(.,'搜索')]", "//button[contains(.,'查询')]", "//*[@role='button' and (contains(.,'搜索') or contains(.,'查询'))]"]:
            try:
                btn = driver.find_element(By.XPATH, xp)
                driver.execute_script("arguments[0].click();", btn)
                clicked = True; break
            except Exception:
                continue
        if not clicked:
            try:
                called = driver.execute_script("try{ if (typeof search==='function'){ search(); return true;} }catch(e){ return false; }")
                clicked = bool(called)
            except Exception:
                pass
        if not clicked:
            for css in ["#qt", "input[name='qt']", "input[type='text']"]:
                try:
                    box = driver.find_element(By.CSS_SELECTOR, css)
                    box.send_keys(Keys.ENTER); clicked = True; break
                except Exception:
                    continue
        if not clicked:
            try:
                driver.execute_script("""
                    var n = document.querySelector("span:contains('时间范围')");
                    var f = n ? n.closest('form') : null; if(f){ f.submit(); }
                """)
            except Exception:
                pass
        try:
            WebDriverWait(driver, 6).until(lambda d: _get_first_card_text(d) != before)
        except Exception:
            pass
    return beijing_time_filter_is(driver, option_text="一周内")

# ==================== 北京公共资源：解析 ====================
def classify(title: str) -> str:
    t = (title or "").strip()
    if any(k in t for k in ["中标","成交","结果","定标","授标","候选人公示"]): return "中标公告"
    if any(k in t for k in ["更正","变更","澄清","补遗"]):                 return "更正公告"
    if any(k in t for k in ["终止","废标","流标"]):                         return "终止公告"
    if any(k in t for k in ["招标","公开招标","采购","磋商","邀请","比选","谈判","竞争性"]): return "招标公告"
    return "其他"

def _safe_text(s: str): return (s or "").replace("\u3000"," ").replace("\xa0"," ")

def extract_deadline(txt: str) -> str:
    for pat in [
        r"(?:投标|递交|响应|报价|报名)[\s\S]{0,10}截止(?:时间|日期)?[:：]?\s*([^\n\r，。;；]{6,40})",
        r"开标(?:时间|日期)[:：]?\s*([^\n\r，。;；]{6,40})",
    ]:
        mm = re.search(pat, txt, re.I)
        if mm: return std_time(mm.group(1))
    return ""

def parse_bidding_fields(detail_text: str):
    txt = _safe_text(detail_text)
    amount = "暂无"
    m = re.search(r"(?:预算金额|最高限价|控制价|采购预算)\s*[:：]?\s*([0-9\.,，]+)\s*(万?\s*元|万元|元|人民币|RMB|￥|¥)", txt, re.I)
    if m:
        num = m.group(1).replace(",", "").replace("，","")
        unit = "万元" if "万" in m.group(2) else "元"
        amount = f"{num}{unit}"
    else:
        m2 = re.search(r"(?:预算金额|最高限价|控制价|采购预算)\s*[:：]?\s*([0-9][0-9\.,，]{3,})", txt, re.I)
        if m2:
            num = m2.group(1).replace(",", "").replace("，",""); amount = f"{num}元"
        else:
            m3 = re.search(r"[￥¥]\s*([0-9][0-9\.,，]*)", txt)
            if m3: amount = f"{m3.group(1).replace(',', '').replace('，','')}元"
    m_lxr = re.search(r"(?:项目联系人|采购人联系人|联系人)\s*[:：]?\s*([^\s、，。;；:：\n\r]+)", txt)
    contact = m_lxr.group(1).strip() if m_lxr else "暂无"
    m_tel = re.search(r"(?:联系电话|联系方式|电话)\s*[:：]?\s*([0-9\-－—\s]{6,})", txt)
    phone = re.sub(r"\s+","", m_tel.group(1)) if m_tel else extract_phone(txt) or "暂无"
    brief = re.sub(r"\s+"," ", txt)[:120] or "暂无"
    deadline = extract_deadline(txt) or "暂无"
    return {"金额":amount,"联系人":contact,"联系电话":phone,"简要摘要":brief,"投标截止":deadline}

def parse_award_from_text(detail_text: str):
    txt = _safe_text(detail_text)
    def pick(pat,dflt="暂无"):
        g = re.search(pat, txt, re.S); return g.group(1).strip() if g else dflt
    supplier = pick(r"(?:中标(?:供应商|人|单位)|成交(?:供应商|人|单位)|供应商名称)[：:]\s*([^\n\r，。；;]+)")
    amount   = pick(r"(?:中标(?:价|金额)|成交(?:价|金额)|评审报价)[：:]\s*([0-9\.,，]+(?:元|万元)?)")
    if amount!="暂无": amount = normalize_amount(amount)
    score    = pick(r"(?:评审(?:得分|分值)|综合得分|最终得分)[：:]\s*([0-9\.]+)")
    content  = pick(r"(?:采购内容|项目概况|采购需求|服务内容|中标内容)[：:]\s*([^\n\r]+)")
    award_date = pick(r"(?:公告日期|公示时间|发布时间|成交日期|中标日期)[：:]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
    if award_date=="暂无": award_date = pick(r"([0-9]{4}-[0-9]{2}-[0-9]{2})")
    return {"中标公司":supplier or "暂无","中标金额":amount or "暂无","评审得分":(score or "暂无").rstrip("分"),
            "中标内容":content or "暂无","中标日期":award_date or "暂无"}

def crawl_beijing(keywords, date_start, date_end, max_pages=10, loose_days=BJ_LOOSE_DAYS):
    def run_pass(loosen: bool):
        results_bid, results_award, seen = [], [], set()
        driver = build_driver()
        try:
            for kw in keywords:
                url = f"https://ggzyfw.beijing.gov.cn/elasticsearch/index.jsp?qt={kw}"
                print(f"[北京|{kw}] 打开搜索页: {url}")
                driver.get(url); time.sleep(2.5)

                if USE_SITE_TIME_FILTER:
                    ok = set_beijing_time_filter(driver, BJ_TIME_FILTER_TEXT)
                    print(f"[北京|{kw}] 时间筛选设置为 {BJ_TIME_FILTER_TEXT} -> {'OK' if ok else 'FAIL'}")

                for page in range(1, max_pages+1):
                    if USE_SITE_TIME_FILTER and not beijing_time_filter_is(driver, BJ_TIME_FILTER_TEXT):
                        ok_keep = set_beijing_time_filter(driver, BJ_TIME_FILTER_TEXT)
                        print(f"[北京|{kw}] 页面{page} 重置时间筛选 -> {'OK' if ok_keep else 'FAIL'}")

                    try:
                        WebDriverWait(driver, 8).until(
                            EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".cs_search_content_box"))
                        )
                    except Exception:
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);"); time.sleep(1.0)

                    cards = driver.find_elements(By.CSS_SELECTOR, ".cs_search_content_box")
                    print(f"[北京|{kw}] 第{page}页，抓到卡片 {len(cards)} 条")
                    if not cards: break

                    for c in cards:
                        try:
                            title_el = c.find_element(By.CLASS_NAME, "cs_search_title")
                            title = title_el.text.strip()
                            ann_type = classify(title)
                            if ann_type not in ("招标公告","中标公告"): continue

                            source_line, info_source, pub_time = "", "", ""
                            try:
                                source_line = c.find_element(By.CLASS_NAME, "cs_search_content_time").text
                            except Exception:
                                pass
                            if "发布时间：" in source_line:
                                parts = source_line.split("发布时间：")
                                info_source = parts[0].replace("信息来源：","").strip()
                                pub_time = parts[1].strip()
                            pub_day = (pub_time[:10] if pub_time else "")

                            if pub_day:
                                if not loosen:
                                    if not (date_start <= pub_day <= date_end):
                                        continue
                                else:
                                    dt_end = datetime.strptime(date_end, "%Y-%m-%d")
                                    dt_loose = dt_end - timedelta(days=max(0, loose_days))
                                    if not (dt_loose.strftime("%Y-%m-%d") <= pub_day <= date_end):
                                        continue

                            try:
                                link = title_el.find_element(By.TAG_NAME, "a").get_attribute("href")
                            except Exception:
                                link = ""
                            if link and link in seen: continue
                            seen.add(link)

                            # 抓详情文本（含 PDF 尝试）
                            detail_text = ""
                            if link:
                                win = driver.current_window_handle
                                driver.execute_script('window.open(arguments[0])', link)
                                driver.switch_to.window(driver.window_handles[-1]); time.sleep(1.2)
                                try:
                                    for sel in ["content","xxnr","main","con","article","detail","center","container"]:
                                        try:
                                            elem = driver.find_element(By.CLASS_NAME, sel); detail_text = elem.text; break
                                        except:
                                            try:
                                                elem = driver.find_element(By.ID, sel); detail_text = elem.text; break
                                            except: pass
                                except: pass
                                # PDF 抽文本
                                try:
                                    pdf_links = [a.get_attribute("href") for a in driver.find_elements(By.TAG_NAME, "a")
                                                 if a.get_attribute("href") and a.get_attribute("href").lower().endswith(".pdf")]
                                    try:
                                        import pdfplumber
                                        import requests as rq
                                        for p in pdf_links[:2]:
                                            try:
                                                resp = rq.get(p, timeout=25)
                                                if resp.status_code==200:
                                                    with pdfplumber.open(BytesIO(resp.content)) as pdf:
                                                        txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
                                                    if len(txt.strip()) > 60:
                                                        detail_text = txt; break
                                            except: pass
                                    except Exception:
                                        pass
                                except: pass
                                driver.close(); driver.switch_to.window(win)

                            region = region_from_source(info_source) or "北京"

                            if ann_type == "招标公告":
                                f = parse_bidding_fields(detail_text)
                                results_bid.append({
                                    "type":"招标公告","地区":region,"公告标题":title,"公告发布时间":pub_time or "暂无",
                                    "金额":f["金额"],"简要摘要":f["简要摘要"],"联系人":f["联系人"],"联系电话":f["联系电话"],
                                    "公告网址":link or "暂无","信息来源":info_source or "北京公共资源","投标截止": f["投标截止"]
                                })
                            else:
                                f = parse_award_from_text(detail_text)
                                results_award.append({
                                    "type":"中标公告","地区":region,"标题":title,"发布时间":pub_time or "暂无",
                                    "中标公司":f["中标公司"],"中标金额":f["中标金额"],"中标内容":f["中标内容"],
                                    "评审得分":f["评审得分"],"中标日期":f["中标日期"],
                                    "中标网址":link or "暂无","信息来源":info_source or "北京公共资源"
                                })
                        except Exception as ex:
                            print("北京站解析一条出错：", ex)

                    # 翻页
                    try:
                        next_btn = driver.find_element(By.LINK_TEXT, "下一页")
                        cls = next_btn.get_attribute("class") or ""
                        if "disable" in cls or next_btn.get_attribute("aria-disabled") == 'true': break
                        if page < max_pages:
                            driver.execute_script("arguments[0].click();", next_btn); time.sleep(1.0)
                    except Exception:
                        break
        finally:
            try: driver.quit()
            except: pass
        return results_bid, results_award

    bid1, awd1 = run_pass(loosen=False)
    if (not bid1 and not awd1) and loose_days > 0:
        print(f"[北京] 严格日期 0 条，启动宽松模式：最近 {loose_days} 天")
        bid2, awd2 = run_pass(loosen=True)
        return bid2, awd2
    return bid1, awd1

# ==================== zsxtzb ====================
ZS_BASE = "https://www.zsxtzb.cn"
DETAIL_URL_RE = re.compile(r"/class(\d+)/\d+\.html")
DATE_IN_LIST_RE = re.compile(r"(20\d{2}[年\-/\.]\d{1,2}[月\-/\.]\d{1,2}日?)")
PUBLISH_RE = re.compile(r"(发布|时间|日期)[：:\s]*((?:20|19)\d{2}[年\-/\.]\d{1,2}[月\-/\.]\d{1,2}日?)")

def get_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        r = SESSION.get(url, timeout=25)
        if not r.encoding or r.encoding.lower() in ("iso-8859-1","ascii"):
            r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"[ERR] {url} -> {e}"); return None

def zs_search_url(keyword: str, page: int, city: Optional[int]=None) -> str:
    q = urllib.parse.quote(keyword)
    if city is None:
        return f"{ZS_BASE}/search/?keyword={q}&datetime=1&page={page}"
    return f"{ZS_BASE}/search/?keyword={q}&city={city}&datetime=1&page={page}"

def zs_parse_list(url: str) -> List[Dict]:
    soup = get_soup(url); rows=[]
    if not soup: return rows
    for a in soup.find_all("a", href=True):
        m = DETAIL_URL_RE.search(a["href"])
        if not m: 
            continue
        # 放宽栏目：1=货物, 2=工程, 4=服务, 5=电力, 6=石油化工
        if m.group(1) not in {"1","2","4","5","6"}:
            continue
        link = a["href"] if a["href"].startswith("http") else urllib.parse.urljoin(ZS_BASE, a["href"])
        title = a.get_text(strip=True) or (a.parent.get_text(" ", strip=True)[:120] if a.parent else "")
        if not any(k in title for k in KEYWORDS):
            continue
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        md = DATE_IN_LIST_RE.search(parent_text)
        list_date = std_time(md.group(1)) if md else ""
        rows.append({"title": title, "url": link, "list_date": list_date})
    uniq={}
    for r in rows: uniq.setdefault(r["url"], r)
    return list(uniq.values())

def extract_amount_from_tables(soup: BeautifulSoup) -> Optional[str]:
    for tbl in soup.find_all("table"):
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["th","td"])
            if not cells: continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            for i, t in enumerate(texts):
                if any(k in t for k in AMOUNT_KEYS):
                    cand = texts[i+1] if i+1 < len(texts) else re.sub("|".join(map(re.escape, AMOUNT_KEYS)), "", t)
                    m = NUM_UNIT_RE.search(cand)
                    if m: return normalize_amount(m.group(0))
                    m2 = NUM_ONLY_RE.search(cand)
                    if m2:
                        val = float(m2.group(1).replace(",", ""))
                        if val >= 1000: return normalize_amount(m2.group(1) + "元")
    return None

def extract_amount_from_text(text: str) -> Optional[str]:
    flat = re.sub(r"\s+"," ", text or "")
    for key in AMOUNT_KEYS:
        pat = re.compile(rf"{re.escape(key)}[^0-9]{{0,10}}{NUM_UNIT_RE.pattern}", re.I)
        m = pat.search(flat)
        if m: return normalize_amount(m.group(0))
    for key in AMOUNT_KEYS:
        pat2 = re.compile(rf"{re.escape(key)}[^0-9]{{0,10}}{NUM_ONLY_RE.pattern}\b(?!\s*(?:年|月|日|号|人|项|台|次|页))", re.I)
        m2 = pat2.search(flat)
        if m2:
            val = float(m2.group(1).replace(",", ""))
            if val >= 1000: return normalize_amount(m2.group(1) + "元")
    for m3 in re.finditer(NUM_UNIT_RE, flat):
        left = flat[max(0, m3.start()-12):m3.start()]
        if not any(w in left for w in AMOUNT_STOP_WORDS):
            return normalize_amount(m3.group(0))
    return None

def zs_parse_detail(url: str) -> Dict:
    soup = get_soup(url)
    if not soup: return {}
    h1 = soup.find("h1"); title = h1.get_text(strip=True) if h1 else ""
    main = soup.find("article") or soup.find(attrs={"class": re.compile(r"content|article|neirong|detail", re.I)})
    text = (main.get_text("\n", strip=True) if main else soup.get_text("\n", strip=True))
    flat = re.sub(r"\s+"," ", text or "")

    pub = std_time(PUBLISH_RE.search(text or "").group(2)) if PUBLISH_RE.search(text or "") else ""
    # 截止
    deadline = ""
    for pat in [r"(投标|递交|提交)[^。\n]{0,12}截止[：:\s]*([0-9]{4}[年\-/\.][0-9]{1,2}[月\-/\.][0-9]{1,2}日?(?:\s*[0-9]{1,2}[:：][0-9]{1,2})?)",
                r"(报名|获取招标文件)[^。\n]{0,12}截止[：:\s]*([0-9]{4}[年\-/\.][0-9]{1,2}[月\-/\.][0-9]{1,2}日?(?:\s*[0-9]{1,2}[:：][0-9]{1,2})?)"]:
        mm = re.search(pat, text or "")
        if mm: deadline = std_time(mm.group(2)); break

    amount  = extract_amount_from_tables(main or soup) or extract_amount_from_text(text) or ""
    contact = (re.search(r"联系人[：:\s]*([^\s，。,；;:/\n]{2,15})", text or "").group(1)
               if re.search(r"联系人[：:\s]*([^\s，。,；;:/\n]{2,15})", text or "") else "")
    phone   = extract_phone(text or "")

    region = decide_region(title, text or "")
    summary = flat[:120]
    return {"title": title, "publish_date": pub, "deadline": deadline, "amount": amount,
            "contact": contact, "phone": phone, "summary": summary, "region": region}

def crawl_zsxtzb_by_dates(target_dates: Set[str]) -> List[Dict]:
    out, seen = [], set()

    def handle_one_search(kw: str, city: Optional[int]):
        for page in range(1, MAX_PAGES_ZS + 1):
            url = zs_search_url(kw, page, city)
            print(f"[zsxtzb|{kw}|city={city}] 第{page}页 -> {url}")
            rows = zs_parse_list(url)
            if not rows: break
            for it in rows:
                if it["url"] in seen: continue
                seen.add(it["url"])
                sleep()
                d = zs_parse_detail(it["url"])

                pub = d.get("publish_date") or it.get("list_date","")
                pub_day = pub.split()[0] if pub else ""
                # 只要“当日”
                if not pub_day or pub_day not in target_dates:
                    continue

                region = d.get("region")
                if region not in REGION_ALLOWED:
                    continue

                out.append({
                    "type":"招标公告","地区": region,
                    "公告标题": d.get("title") or it["title"],
                    "公告发布时间": pub,
                    "投标截止": d.get("deadline",""),
                    "金额": d.get("amount",""),
                    "联系人": d.get("contact",""),
                    "联系电话": d.get("phone",""),
                    "简要摘要": d.get("summary",""),
                    "公告网址": it["url"],
                    "信息来源": "zsxtzb.cn"
                })

    for kw in KEYWORDS:
        for _city in ZS_CITY_IDS_TRY.values():
            handle_one_search(kw, _city)
    for kw in KEYWORDS:
        handle_one_search(kw, None)
    return out

# ==================== Markdown 组装 ====================
def fmt_bid_md(items, date_range_str):
    lines = [f"### 【招标公告｜京津冀】{date_range_str} 共 {len(items)} 条"]
    for idx, it in enumerate(items, 1):
        title = md_escape(it.get("公告标题","")); url = it.get("公告网址","")
        show  = f"[{title}]({url})" if url else title
        lines.append(f"\n**{idx}. {show}**")
        if it.get("地区"): lines.append(f"- 地区：{md_escape(it.get('地区'))}")
        lines.append(f"- 公告发布时间：{md_escape(it.get('公告发布时间','暂无'))}")
        if it.get("投标截止"): lines.append(f"- 投标截止：{md_escape(it.get('投标截止'))}")
        lines.append(f"- 金额：{md_escape(it.get('金额','暂无'))}")
        lines.append(f"- 联系人：{md_escape(it.get('联系人','暂无'))}")
        lines.append(f"- 联系电话：{md_escape(it.get('联系电话','暂无'))}")
        lines.append(f"- 简要摘要：{md_escape(it.get('简要摘要','暂无'))}")
        lines.append(f"- 信息来源：{md_escape(it.get('信息来源',''))}")
    return "\n".join(lines)

def fmt_award_md(items, date_range_str):
    lines = [f"### 【中标结果｜京津冀】{date_range_str} 共 {len(items)} 条"]
    for idx, it in enumerate(items, 1):
        title = md_escape(it.get("标题","")); url = it.get("中标网址","")
        show  = f"[{title}]({url})" if url else title
        lines.append(f"\n**{idx}. {show}**")
        if it.get("地区"): lines.append(f"- 地区：{md_escape(it.get('地区'))}")
        lines.append(f"- 中标日期：{md_escape(it.get('中标日期','暂无'))}")
        lines.append(f"- 中标公司：{md_escape(it.get('中标公司','暂无'))}")
        lines.append(f"- 中标金额：{md_escape(it.get('中标金额','暂无'))}")
        if it.get("中标内容"): lines.append(f"- 中标内容：{md_escape(it.get('中标内容'))}")
        if it.get("评审得分"): lines.append(f"- 评审得分：{md_escape(it.get('评审得分'))}")
        lines.append(f"- 信息来源：{md_escape(it.get('信息来源',''))}")
        lines.append(f"- 发布时间：{md_escape(it.get('发布时间','暂无'))}")
    return "\n".join(lines)

def parse_dt(s: str):
    for fmt in ("%Y-%m-%d %H:%M","%Y-%m-%d"):
        try: return datetime.strptime(s, fmt)
        except: pass
    return None

# ==================== 主流程 ====================
if __name__ == "__main__":
    START = os.getenv("START"); END = os.getenv("END")
    if START and END:
        target_dates = set()
        d = datetime.strptime(START, "%Y-%m-%d"); e = datetime.strptime(END, "%Y-%m-%d")
        while d <= e:
            target_dates.add(d.strftime("%Y-%m-%d")); d += timedelta(days=1)
        print("本次抓取日期(ENV)：", sorted(target_dates))
    else:
        target_dates = get_target_dates()
        print("本次抓取日期（北京站）：", sorted(target_dates))

    date_start, date_end = sorted(target_dates)[0], sorted(target_dates)[-1]
    date_range_str = pretty_range(target_dates)

    all_bids: List[Dict] = []
    all_awards: List[Dict] = []

    # —— 北京站：保持原策略（自动/一周内）——
    if CRAWL_BEIJING:
        bj_bids, bj_awards = crawl_beijing(KEYWORDS, date_start, date_end, max_pages=MAX_PAGES_BJ, loose_days=BJ_LOOSE_DAYS)
        all_bids.extend(bj_bids); all_awards.extend(bj_awards)

    # —— zsxtzb：仅抓“当日” ——（你要求的改动）
    if CRAWL_ZSXTZB:
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_dates_zs = { today_str }
        print("本次抓取日期（zsxtzb，当日）：", sorted(target_dates_zs))
        zs_bids = crawl_zsxtzb_by_dates(target_dates_zs)
        all_bids.extend(zs_bids)

    # 单次执行内去重（按链接）
    def _lk(x): return x.get("公告网址") or x.get("中标网址") or ""
    uniq_b, seen = [], set()
    for r in all_bids:
        u = _lk(r)
        if u and u not in seen:
            uniq_b.append(r); seen.add(u)
    uniq_a, seen2 = [], set()
    for r in all_awards:
        u = _lk(r)
        if u and u not in seen2:
            uniq_a.append(r); seen2.add(u)

    # —— 排序：对“None”兜底为 datetime.min，避免 TypeError —— 
    def sort_key_bid(x):
        pub = x.get("公告发布时间") or ""
        return (parse_dt(pub) or datetime.min, x.get("公告标题",""))
    uniq_b.sort(key=sort_key_bid, reverse=True)

    def sort_key_aw(x):
        dt = parse_dt(x.get("发布时间") or x.get("中标日期") or "")
        return (dt or datetime.min, x.get("标题",""))
    uniq_a.sort(key=sort_key_aw, reverse=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary = f"【播报｜京津冀融合】{date_range_str} 外包/派遣采集完成：招标 {len(uniq_b)} 条，中标 {len(uniq_a)} 条。（推送于 {now}）"
    send_to_dingtalk_markdown("外包/派遣采集汇总（京津冀融合）", summary)

    if uniq_b:
        split_and_send("招标公告明细（京津冀融合）", fmt_bid_md(uniq_b, date_range_str))
    if uniq_a:
        split_and_send("中标结果明细（京津冀融合）", fmt_award_md(uniq_a, date_range_str))

    print("✅ 全部完成")
