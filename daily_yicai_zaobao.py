name: Daily YiCai ZaoBao (RSS Only)

on:
  schedule:
    # 北京时间 09:05 = UTC 01:05
    - cron: "5 1 * * *"
  workflow_dispatch:

# 关键：允许 workflow 用 GITHUB_TOKEN push 回仓库
permissions:
  contents: write

jobs:
  run:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4 feedparser

      - name: Run YiCai ZaoBao RSS script
        env:
          # 钉钉（必填）
          DINGTALK_WEBHOOK: ${{ secrets.DINGTALK_WEBHOOK }}
          DINGT
