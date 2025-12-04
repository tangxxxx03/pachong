# -*- coding: utf-8 -*-
"""
cf_ai_demo.py
通过 Cloudflare AI Gateway 调用 OpenAI，并做一个“总结文章”的小示例。

运行前，需要在环境变量里配置：
  CF_GATEWAY_URL  = 完整的 Cloudflare Gateway chat/completions URL
  CF_AIG_TOKEN    = 你在 "Authenticated AI Gateway" 里创建的 token
  OPENAI_API_KEY  = 你的 OpenAI API key（sk-开头）

在 GitHub Actions 里，把上面三个值从 secrets 传进来即可。
"""

import os
import json
import textwrap
import requests


def _get_env(name: str) -> str:
    """读环境变量，没有的话给出比较友好的报错。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"环境变量 {name} 未设置，请在 GitHub Secrets 或本地环境中配置。")
    return value


# ========= 1. 读取配置 =========

CF_GATEWAY_URL = _get_env("CF_GATEWAY_URL")
CF_AIG_TOKEN = _get_env("CF_AIG_TOKEN")
OPENAI_API_KEY = _get_env("OPENAI_API_KEY")


# ========= 2. 核心调用函数 =========

def chat_with_ai(messages, model: str = "gpt-4.1-mini", timeout: int = 60) -> str:
    """
    通过 Cloudflare AI Gateway 调用 OpenAI 的 chat/completions 接口。

    :param messages: 传统 OpenAI messages 列表
    :param model:    例如 "gpt-4.1-mini"、"gpt-4.1" 等
    :return:         assistant 的回复文本
    """
    headers = {
        # Cloudflare AI Gateway 的认证头
        "cf-aig-authorization": f"Bearer {CF_AIG_TOKEN}",
        # 真实 OpenAI key，Cloudflare 会帮你转发
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": messages,
    }

    print("请求 AI Gateway：", CF_GATEWAY_URL)
    resp = requests.post(
        CF_GATEWAY_URL,
        headers=headers,
        json=payload,
        timeout=timeout,
    )

    # 如果不是 2xx，直接抛异常，方便在 Actions 里看到日志
    try:
        resp.raise_for_status()
    except Exception as e:
        print("❌ AI 请求失败，状态码：", resp.status_code)
        print("响应体：", resp.text[:1000])
        raise

    data = resp.json()
    # 兼容标准 OpenAI 格式：choices[0].message.content
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        # 打印完整 JSON 方便排查
        print("⚠️ 无法解析返回结果：", json.dumps(data, ensure_ascii=False, indent=2))
        raise

    return content


# ========= 3. 一个“总结文章”的示例 =========

def summarize_article(title: str, url: str, content: str) -> str:
    """
    用 AI 把一篇文章总结成几条要点；你之后可以把爬虫抓到的正文塞进来。
    """
    system_prompt = (
        "你是一个中文财经编辑，擅长把长篇报道总结成简洁的要点。\n"
        "要求：\n"
        "1）用简体中文回答；\n"
        "2）输出 3-5 条条目，每条前面加 - 作为 Markdown 列表；\n"
        "3）尽量保留重要数字、机构名称和结论。"
    )

    user_content = textwrap.dedent(f"""
    标题：{title}
    链接：{url}

    正文内容如下（可能比较长）：
    {content}

    请根据上面的内容，总结 3-5 条最关键的要点，输出为 Markdown 列表。
    """)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    return chat_with_ai(messages)


# ========= 4. main：本地/Actions 测试入口 =========

def main():
    # 这里我用一段假文章内容做示例，你后面可以替换成爬虫抓到的真正正文
    fake_title = "某公司发布新一代 AI 芯片，性能提升 3 倍"
    fake_url = "https://www.example.com/article/123"
    fake_content = (
        "某科技公司今天发布了新一代 AI 芯片，相比上一代在算力上提升了 3 倍，"
        "功耗降低 40%。这款芯片主要面向大模型推理场景，预计将在 2025 年大规模商用。"
        "公司管理层表示，新产品将帮助客户显著降低算力成本。"
    )

    print("开始调用 AI，总结示例文章...\n")
    summary = summarize_article(fake_title, fake_url, fake_content)

    print("\n===== AI 总结结果 =====\n")
    print(summary)


if __name__ == "__main__":
    main()
