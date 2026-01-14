# -*- coding: utf-8 -*-
"""
外包/派遣：招标 & 中标采集（北京公共资源交易平台 + zsxtzb.cn 搜索）
—— 采集更完整（requests优先+selenium兜底+PDF附件回退）+ 输出更极简（钉钉卡片）
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
        m = re.search(pat, text,
