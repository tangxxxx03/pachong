name: HR News Crawler (Sanmao Daily)

on:
  schedule:
    # 每个工作日 北京时间 09:00（UTC+8 → UTC 01:00）
    - cron: "0 1 * * 1-5"
  workflow_dispatch: {}

jobs:
  run:
    runs-on: ubuntu-latest

    env:
      # ---- 你的钉钉机器人（必填）----
      DINGTALK_BASE: "https://oapi.dingtalk.com/robot/send?access_token=00c49f5d9aab4b8c86d60ef9bc0a25d46d9669b1b1d94645671062c4b845dced"
      DINGTALK_SECRET: "SEC2431e95f7bca3b419185a0fbd80530829c45c94977ba338022400433f064c6ad"
      # ---- 可选：支持多源 ----
      SRC_HRLOO_URLS: "https://www.hrloo.com/,https://www.hrloo.com/news/hr"
      # ---- 可选：固定抓取某一天 ----
      # HR_TARGET_DATE: "2025-01-15"
      # --------------------------------

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install beautifulsoup4 requests urllib3 backports.zoneinfo --quiet
          pip install -r requirements.txt --quiet || true

      - name: Run hr_news_crawler.py
        run: |
          echo "== 开始执行 hr_news_crawler.py =="
          python hr_news_crawler.py
