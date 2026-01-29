import os
import time
import hmac
import hashlib
import base64
import requests

def send_markdown(title, text):
    webhook = os.getenv("DINGTALK_SHIYANQUNWEBHOOK")
    secret = os.getenv("DINGTALK_SHIYANQUNSECRET")

    if not webhook:
        print(text)
        return

    timestamp = str(round(time.time() * 1000))
    sign = ""

    if secret:
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256
        ).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")

    url = f"{webhook}&timestamp={timestamp}&sign={sign}" if secret else webhook

    data = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text
        }
    }

    requests.post(url, json=data)
