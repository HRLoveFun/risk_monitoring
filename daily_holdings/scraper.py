import time

import requests

from daily_holdings.config import HTTP_RETRIES, HTTP_TIMEOUT, logger


def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            logger.info("抓取页面(第 %d 次): %s", attempt, url)
            resp = requests.get(url, headers=headers, timeout=(10, HTTP_TIMEOUT))
            resp.raise_for_status()
            html = resp.text
            if len(html) < 10000 or "holdingsList" not in html:
                raise ValueError(f"页面内容异常(长度 {len(html)},未含 holdingsList),疑似错误页")
            logger.info("页面抓取成功,大小 %d 字节", len(html))
            return html
        except Exception as e:
            last_err = e
            logger.warning("抓取失败: %s", e)
            if attempt < HTTP_RETRIES:
                time.sleep(2**attempt)
    raise RuntimeError(f"页面抓取最终失败: {last_err}")
