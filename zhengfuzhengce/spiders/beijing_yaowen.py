from bs4 import BeautifulSoup
from core.http import get_session
from core.timeutils import in_last_days

URL = "https://www.beijing.gov.cn/ynwdt/yaowen/index.html"

def crawl():
    session = get_session()
    resp = session.get(URL, timeout=15)
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")

    results = []

    for li in soup.select("div.listBox ul.list li"):
        a = li.find("a")
        span = li.find("span")

        if not a or not span:
            continue

        title = a.get_text(strip=True)
        url = a["href"]
        if not url.startswith("http"):
            url = "https://www.beijing.gov.cn" + url

        date = span.get_text(strip=True)

        if in_last_days(date, 7):
            results.append({
                "title": title,
                "url": url,
                "date": date
            })

    return results
