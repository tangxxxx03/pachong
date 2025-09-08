# -*- coding: utf-8 -*-
"""
HR News → 钉钉机器人（开启加签）
用法：
  python hr_news_push.py
"""

import time
import hmac
import base64
import hashlib
import urllib.parse
import requests
import os

# =========================
# 🚨 你提供的 HR 机器人配置（已写死，能直接用）
WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=9bb5d79464e0bf60f9c0f56ffd99744c4149fc43554982c0189ffe9c04162dce"
SECRET  = "SEC4d9521a7cf6f96fcf6ea9832116df97b13300441f4e513f487a6502d833def75"
# =========================

# ✅ 推荐安全做法（可选）：改用环境变量/Secrets（改好后把上面两行删掉）
WEBHOOK = os.getenv("DINGTALK_WEBHOOKHR", WEBHOOK).strip()
SECRET  = os.getenv("DINGTALK_SECRET_HR",  SECRET).strip()
KEYWORD = os.getenv("DINGTALK_KEYWORD_HR", "").strip()  # 若机器人启用“关键字”则填写

def _sign_webhook(base_webhook: str, secret: str) -> str:
    """按钉钉规则生成签名并拼到 webhook 上"""
    if not secret:
        return base_webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{base_webhook}&timestamp={ts}&sign={sign}"

def send_hr_markdown(title: str, md_text: str) -> bool:
    """
    发送 Markdown 到 HR 机器人（带加签）
    返回 True 表示发送成功
    """
    if not WEBHOOK:
        print("❌ 缺少 WEBHOOK"); return False
    if not SECRET:
        print("❌ 缺少 SECRET（你的机器人已开启加签就必须提供）"); return False

    webhook = _sign_webhook(WEBHOOK, SECRET)
    # 若启用了“关键字”，要求标题或正文包含该词
    if KEYWORD and (KEYWORD not in title and KEYWORD not in md_text):
        title = f"{KEYWORD} | {title}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": md_text
        }
    }
    try:
        r = requests.post(webhook, json=payload, timeout=20)
        print("HR DingTalk resp:", r.status_code, r.text[:300])
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("errcode") == 0)
        return ok
    except Exception as e:
        print("❌ 请求异常：", e)
        return False

if __name__ == "__main__":
    # ✅ 示例：发一条测试消息（你可替换为真实内容）
    md = """### HR资讯播报（测试）
- 条目 1：示例内容
- 条目 2：示例内容
> 汇总时间：自动发送测试
"""
    ok = send_hr_markdown("HR资讯播报（测试）", md)
    print("发送结果：", "成功 ✅" if ok else "失败 ❌")
