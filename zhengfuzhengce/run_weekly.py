from spiders.beijing_yaowen import crawl as crawl_beijing
from core.render import render_markdown
from core.dingtalk import send_markdown

def main():
    blocks = []

    beijing_items = crawl_beijing()
    blocks.append(
        render_markdown("北京政府｜要闻动态（近7天）", beijing_items)
    )

    final_text = "\n\n---\n\n".join(blocks)

    send_markdown("政府政策周报", final_text)

if __name__ == "__main__":
    main()
