import re
import time
from urllib.parse import urljoin
from datetime import datetime

# ... (其他导入和 BASE/session 设置保持不变) ...

def fetch_list(page=1):
    """
    抓取指定页码的文章列表，并修复可能不完整的 URL。
    """
    url = f"{BASE}/shangye/" if page == 1 else f"{BASE}/shangye/?page={page}"
    print(f"\n--- 正在请求列表页: 第 {page} 页 ---")

    # ... (请求和 soup 部分保持不变) ...

    try:
        r = session.get(url, timeout=15)
        r.raise_for_status() 
    except requests.exceptions.RequestException as e:
        print(f"⚠️ 列表页请求失败 ({url}): {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    
    # 获取今天的日期，用于修复可能不完整的 URL 路径
    today_path = datetime.now().strftime("/%Y-%m/%d")

    for li in soup.select("ul.news-list li.news-item"):
        h2 = li.find("h2")
        a = li.find("a", href=True)
        date_div = li.find("div", class_="date")
        
        if not (h2 and a):
            continue
            
        href = a["href"].strip()
        
        # 1. 检查 href 是否包含完整的日期路径：/YYYY-MM/DD/content_ID.htm
        if re.match(r"/\d{4}-\d{2}/\d{2}/content_\d+\.htm", href):
            # 路径完整，直接使用
            url_to_fetch = urljoin(BASE, href)
        
        # 2. 检查 href 是否是缺失日期路径的：/content_ID.htm
        elif re.match(r"/content_\d+\.htm", href):
            # 路径缺失，尝试用当天日期路径修复
            print(f"  ⚠️ 链接缺少日期路径，尝试修复: {href}")
            
            # 从 date div 获取发布日期（如果存在），以便更精确地修复路径
            pub_date_str = date_div.get_text(strip=True) if date_div else ""
            
            if re.match(r"\d{4}-\d{2}-\d{2}", pub_date_str):
                # 假设发布日期格式为 YYYY-MM-DD
                # 构造正确的路径 /YYYY-MM/DD
                correct_path_prefix = pub_date_str.replace('-', '/')
                url_to_fetch = urljoin(BASE, f"/{correct_path_prefix}{href}")
            else:
                # 如果无法从日期 div 获取，则退回到使用今天的日期路径
                url_to_fetch = urljoin(BASE, f"{today_path}{href}")

        else:
            # 不符合任何文章格式，跳过
            continue

        title = h2.get_text(strip=True)
        pub_date = date_div.get_text(strip=True) if date_div else ""

        items.append({
            "title": title,
            "url": url_to_fetch, # 使用修复后的 URL
            "date": pub_date,
            "content": "",
        })

    print(f"  ✅ 第 {page} 页抓到文章数：{len(items)}")
    return items

# ... (main, save_to_csv, fetch_article_content 保持不变) ...
